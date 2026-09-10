from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from studio.app.common.core.auth.auth_dependencies import (
    get_current_user,
    get_user_remote_bucket_name,
)
from studio.app.common.core.cloud.cloud_utils import get_effective_quota_bytes
from studio.app.common.core.cloud.storage_tracking import (
    get_current_user_storage_usage,
    get_user_storage_usage,
)
from studio.app.common.core.experiment.experiment import ExptConfig
from studio.app.common.core.experiment.experiment_reader import ExptConfigReader
from studio.app.common.core.experiment.experiment_record_services import (
    ExperimentRecordService,
)
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.storage.remote_storage_controller import (
    RemoteStorageController,
    RemoteStorageLockError,
    RemoteSyncStatusFileUtil,
)
from studio.app.common.core.utils.datetime_utils import (
    TIMEZONE_KEY,
    get_datetime_for_timezone,
)
from studio.app.common.core.workflow.workflow import DataFilterParam, NodeItem, RunItem
from studio.app.common.core.workflow.workflow_filter import WorkflowNodeDataFilter
from studio.app.common.core.workflow.workflow_result import (
    NodeResult,
    WorkflowMonitor,
    WorkflowResult,
)
from studio.app.common.core.workflow.workflow_runner import WorkflowRunner
from studio.app.common.core.workspace.workspace_data_capacity_services import (
    WorkspaceDataCapacityService,
)
from studio.app.common.core.workspace.workspace_dependencies import (
    is_workspace_available,
    is_workspace_owner,
)
from studio.app.common.schemas.users import User
from studio.app.common.schemas.workflow import CompleteStatus, PollRunResultResponse
from studio.app.const import DATE_FORMAT

router = APIRouter(prefix="/run", tags=["run"])

logger = AppLogger.get_logger()

# Max wait for the executor's async record write before the poll completes
# anyway, so a dead post-process can't strand the run in a spinner. Sized to
# cover thumbnail generation on large experiments.
RECORD_WRITE_GRACE_SEC = 300


def _finished_within_grace(expt_config: ExptConfig) -> bool:
    finished_at = getattr(expt_config, "finished_at", None)
    if not finished_at:
        return False
    try:
        finished = datetime.strptime(finished_at, DATE_FORMAT)
    except (ValueError, TypeError):
        return False
    tz = getattr(expt_config, TIMEZONE_KEY, None)
    now = get_datetime_for_timezone(tz).replace(tzinfo=None)
    return (now - finished).total_seconds() < RECORD_WRITE_GRACE_SEC


async def _check_storage_quota(user_id: int) -> None:
    """Raise 403 if user has exceeded their storage quota."""
    current_usage = await get_current_user_storage_usage(user_id, force_live=False)
    storage_info = get_user_storage_usage(user_id)
    quota_limit = get_effective_quota_bytes(user_id, storage_info=storage_info)

    if quota_limit > 0:
        usage_percent = (current_usage / quota_limit) * 100

        if usage_percent >= 100:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Cannot run job: Storage quota exceeded "
                f"({usage_percent:.1f}% used). "
                f"Please free up space before running jobs.",
            )


@router.post(
    "/{workspace_id}",
    response_model=str,
    dependencies=[Depends(is_workspace_owner)],
)
async def run(
    workspace_id: str,
    runItem: RunItem,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    remote_bucket_name: str = Depends(get_user_remote_bucket_name),
):
    try:
        await _check_storage_quota(current_user.id)

        unique_id = WorkflowRunner.create_workflow_unique_id()
        runner = WorkflowRunner(
            remote_bucket_name, workspace_id, unique_id, runItem, current_user.id
        )

        # Download any remote-only input files before workflow runs
        # This ensures migrated users' input data is available locally
        if RemoteStorageController.is_available():
            await runner.ensure_input_data_local()

        runner.run_workflow(background_tasks)

        # Refresh storage cache in background to keep it up-to-date
        background_tasks.add_task(
            get_current_user_storage_usage, current_user.id, force_live=True
        )

        logger.info("run snakemake")

        return unique_id

    except KeyError as e:
        logger.error(e, exc_info=True)
        # Pass through the specific error message for KeyErrors
        raise HTTPException(
            # Changed to 422 since it's a client configuration issue
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e).strip('"'),  # Remove quotes from the KeyError message
        )

    except RemoteStorageLockError as e:
        logger.error(e)
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=str(e))

    except Exception as e:
        logger.error(e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to run workflow.",
        )


@router.post(
    "/{workspace_id}/{uid}",
    response_model=str,
    dependencies=[Depends(is_workspace_owner)],
)
async def run_id(
    workspace_id: str,
    uid: str,
    runItem: RunItem,
    background_tasks: BackgroundTasks,
    remote_bucket_name: str = Depends(get_user_remote_bucket_name),
    current_user: User = Depends(get_current_user),
):
    try:
        await _check_storage_quota(current_user.id)

        runner = WorkflowRunner(
            remote_bucket_name, workspace_id, uid, runItem, current_user.id
        )

        # Download any remote-only input files before workflow runs
        if RemoteStorageController.is_available():
            await runner.ensure_input_data_local()

        runner.run_workflow(background_tasks)

        # Refresh storage cache in background
        background_tasks.add_task(
            get_current_user_storage_usage,
            current_user.id,
            force_live=True,
        )

        logger.info("run snakemake")
        logger.info("forcerun list: %s", runItem.forceRunList)

        return uid

    except RemoteStorageLockError as e:
        logger.error(e)
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=str(e))
    except Exception as e:
        # Check if this is a KeyError with a specific workflow yaml error message
        if isinstance(e, KeyError) and "Workflow yaml error" in str(e):
            logger.error(f"YAML validation error: {e}", exc_info=True)
            # Return 422 for YAML validation errors
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Workflow yaml error, see FAQ",
            )
        else:
            # Keep original error handling for other errors
            logger.error(e, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to run workflow.",
            )


@router.post(
    "/result/{workspace_id}/{uid}",
    response_model=PollRunResultResponse,
    dependencies=[Depends(is_workspace_available)],
)
async def run_result(
    workspace_id: str,
    uid: str,
    nodeDict: NodeItem,
    background_tasks: BackgroundTasks,
    remote_bucket_name: str = Depends(get_user_remote_bucket_name),
):
    try:
        # Ensure experiment yaml exists locally before accessing results.
        # Downloads from S3 if not present (handles multi-instance scenarios).
        await ExptConfigReader.ensure_synced_async(
            workspace_id, uid, remote_bucket_name
        )

        node_results = await WorkflowResult(workspace_id, uid).observe(
            nodeDict.pendingNodeIdList
        )
        if node_results:
            background_tasks.add_task(
                WorkspaceDataCapacityService.update_experiment_data_usage,
                workspace_id,
                uid,
            )

        # Check post-run completion status (e.g. remote storage upload)
        complete_status = None
        if RemoteStorageController.is_available():
            sync_status = RemoteSyncStatusFileUtil.check_sync_status_file(
                workspace_id, uid
            )
            if sync_status:
                # Create CompleteStatus (converted from RemoteSyncStatus)
                complete_status = CompleteStatus(sync_status.value)

        # A poll can see all nodes finished before the executor's async record
        # write lands; hold "processing" until it exists so the dataview isn't
        # opened against a missing record, but only within the grace window.
        try:
            if ExperimentRecordService.is_available() and complete_status not in (
                CompleteStatus.PROCESSING,
                CompleteStatus.ERROR,
            ):
                expt_config = ExptConfigReader.read(workspace_id, uid)
                if NodeResult.is_all_nodes_already_finished(
                    expt_config
                ) and not ExperimentRecordService.record_exists(workspace_id, uid):
                    if _finished_within_grace(expt_config):
                        complete_status = CompleteStatus.PROCESSING
                    else:
                        logger.warning(
                            "experiment_records row still absent for finished run "
                            "[%s/%s]; completing poll without it",
                            workspace_id,
                            uid,
                        )
        except Exception as e:
            logger.warning(
                "Poll-time record existence check failed [%s/%s]: %s",
                workspace_id,
                uid,
                e,
            )

        return PollRunResultResponse(
            nodeResults=node_results,
            completeStatus=(complete_status.value if complete_status else None),
        )

    except RemoteStorageLockError as e:
        logger.error(e)
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=str(e))

    except Exception as e:
        logger.error(e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to result workflow.",
        )


@router.post(
    "/cancel/{workspace_id}/{uid}",
    response_model=bool,
    dependencies=[Depends(is_workspace_owner)],
)
async def cancel_run(workspace_id: str, uid: str):
    try:
        return WorkflowMonitor(workspace_id, uid).cancel_run()
    except HTTPException as e:
        logger.error(e)
        raise e
    except Exception as e:
        logger.error(e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to cencel workflow.",
        )


@router.post("/filter/{workspace_id}/{uid}/{node_id}", response_model=bool)
async def apply_filter(
    workspace_id: str,
    uid: str,
    node_id: str,
    background_tasks: BackgroundTasks,
    params: Optional[DataFilterParam] = None,
):
    try:
        WorkflowNodeDataFilter(
            workspace_id=workspace_id, unique_id=uid, node_id=node_id
        ).filter_node_data(params)

        background_tasks.add_task(
            WorkspaceDataCapacityService.update_experiment_data_usage, workspace_id, uid
        )

        return True
    except Exception as e:
        logger.error(e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to filter data.",
        )
