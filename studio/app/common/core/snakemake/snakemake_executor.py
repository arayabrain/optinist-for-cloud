import asyncio
import os
import time
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Dict, List

from snakemake.api import (
    DAGSettings,
    DeploymentMethod,
    DeploymentSettings,
    OutputSettings,
    ResourceSettings,
    SnakemakeApi,
    StorageSettings,
)

from studio.app.common.core.cloud.storage_tracking import (
    update_user_storage_after_workflow,
)
from studio.app.common.core.experiment.experiment_record_services import (
    ExperimentRecordService,
)
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.logger_context_helpers import (
    get_client_id_for_subprocess,
    with_client_id_context,
)
from studio.app.common.core.snakemake.smk import ForceRun, SmkParam
from studio.app.common.core.snakemake.smk_status_logger import SmkStatusLogger
from studio.app.common.core.storage.remote_storage_controller import (
    RemoteStorageController,
    RemoteStorageLockError,
    RemoteSyncAction,
    RemoteSyncLockFileUtil,
    RemoteSyncStatusFileUtil,
)
from studio.app.common.core.utils.filepath_creater import get_pickle_file, join_filepath
from studio.app.common.core.workflow.workflow import Edge, Node
from studio.app.common.core.workflow.workflow_result import WorkflowResult
from studio.app.common.core.workspace.workspace_data_capacity_services import (
    WorkspaceDataCapacityService,
)
from studio.app.dir_path import DIRPATH

logger = AppLogger.get_logger()


def snakemake_execute(
    workspace_id: str, unique_id: str, params: SmkParam, user_id: int = None
):
    """
    Main entry point for Snakemake execution.

    Args:
        workspace_id: Workspace ID
        unique_id: Unique ID for the workflow
        params: Snakemake parameters
        user_id: User ID (for tracking free tier workflow counts)
    """
    client_id = get_client_id_for_subprocess()

    try:
        logger.info("Starting local execution mode")
        with ProcessPoolExecutor(max_workers=1) as executor:
            logger.info("start snakemake running process.")

            future = executor.submit(
                _snakemake_execute_process,
                workspace_id,
                unique_id,
                params,
                client_id=client_id,
            )
            future_result = future.result()

        # Update user storage after workflow completion
        asyncio.run(update_user_storage_after_workflow(workspace_id))

        return future_result

    finally:
        # Decrement workflow count in finally block to ensure it ALWAYS runs
        # This prevents workflow count leaks when exceptions occur during execution
        if user_id is not None:
            try:
                from studio.app.common.core.workflow.workflow_tracking import (
                    decrement_workflow_count,
                )

                decrement_workflow_count(user_id)
                logger.info(f"Decremented workflow count for user {user_id}")
            except Exception as e:
                logger.error(f"Failed to decrement workflow count: {e}", exc_info=True)


@with_client_id_context  # Automatically set client_id for logging
def _snakemake_execute_process(
    workspace_id: str,
    unique_id: str,
    params: SmkParam,
    client_id: str = None,
) -> bool:
    # ------------------------------------------------------------
    # Snakemake execution process
    # ------------------------------------------------------------

    smk_logger = SmkStatusLogger(workspace_id, unique_id)
    smk_workdir = join_filepath(
        [
            DIRPATH.OUTPUT_DIR,
            workspace_id,
            unique_id,
        ]
    )

    # Use context manager for proper cleanup
    cores = getattr(params, "cores", 1)

    deployment_methods = []
    if getattr(params, "use_conda", True):
        deployment_methods.append(DeploymentMethod.CONDA)

    # Use context manager for proper cleanup
    with SnakemakeApi(
        OutputSettings(
            verbose=True,  # Print debugging output
            show_failed_logs=True,  # Automatically display logs of failed jobs
            debug_dag=True,  # Print candidate and selected jobs with wildcards
            printshellcmds=True,  # Show shell commands
        ),
    ) as snakemake_api:
        workflow_api = snakemake_api.workflow(
            snakefile=Path(DIRPATH.SNAKEMAKE_FILEPATH),
            workdir=Path(smk_workdir),
            storage_settings=StorageSettings(),
            resource_settings=ResourceSettings(cores=cores),
            deployment_settings=DeploymentSettings(
                deployment_method=deployment_methods,
                conda_frontend="conda",
                conda_prefix=DIRPATH.SNAKEMAKE_CONDA_ENV_DIR,
            ),
        )

        logger.debug("Workflow API created successfully")
        logger.debug("Creating DAG...")

        forceall = getattr(params, "forceall", False)

        dag_api = workflow_api.dag(
            dag_settings=DAGSettings(
                forceall=forceall,
            )
        )

        logger.info("DAG created successfully")
        logger.info("Starting workflow execution...")

        snakemake_result = False

        try:
            dag_api.execute_workflow()

            snakemake_result = True
            logger.info("snakemake_execute succeeded.")
        except Exception as e:
            snakemake_result = False
            logger.error(f"snakemake_execute failed: {e}")

            # Logging errors via SmkStatusLogger to notify
            #   the monitoring process (WorkflowMonitor) of the error occurrence
            smk_logger.logger.error(e)

    if snakemake_result:
        logger.info("snakemake_execute succeeded.")
    else:
        logger.error("snakemake_execute failed..")

    smk_logger.clean_up()

    # ------------------------------------------------------------
    # Snakemake execution post process
    # ------------------------------------------------------------

    # If workflow failed, delete the lock file BEFORE post-process
    # so that WorkflowResult.observe_overall() can access remote storage
    if not snakemake_result and RemoteStorageController.is_available():
        RemoteSyncLockFileUtil.delete_sync_lock_file(workspace_id, unique_id)

    # Wait for post_process upload to release the lock
    if snakemake_result and RemoteStorageController.is_available():
        RemoteSyncLockFileUtil.wait_for_lock_release(workspace_id, unique_id)

    try:
        # Finalize workflow results. Reaching the upload lock proves the local
        # ExptConfig is finalized, so a lock conflict (idempotent upload handled
        # by the concurrent observe path) still finalizes the DB. Finalization is
        # skipped only when observe never succeeded and never reached the lock.
        (
            observe_success,
            observe_lock_conflict,
            upload_confirmed,
        ) = _observe_overall_with_lock_retry(workspace_id, unique_id)
        should_finalize = observe_success or observe_lock_conflict

        # Update experiment database record
        if should_finalize and ExperimentRecordService.is_available():
            ExperimentRecordService.regist_record_on_workflow_completed(
                workspace_id, unique_id
            )

        # Data usage calculation
        if should_finalize:
            WorkspaceDataCapacityService.update_experiment_data_usage(
                workspace_id, unique_id
            )

        if observe_lock_conflict and not observe_success and upload_confirmed:
            logger.warning(
                "observe_overall() upload stayed locked; finalized DB "
                "registration and data usage regardless "
                "(upload completed by the concurrent observe path, verified "
                "via remote sync status). [workspace: %s] [unique_id: %s]",
                workspace_id,
                unique_id,
            )
        elif observe_lock_conflict and not observe_success:
            # Local ExptConfig is finalized (DB is correct), but the redundant
            # remote upload is unconfirmed; the periodic re-sync reconciles it.
            logger.warning(
                "observe_overall() upload stayed locked and the concurrent "
                "observe path has not reported sync success; finalized DB "
                "registration and data usage from the finalized local "
                "ExptConfig, but the remote upload is unconfirmed and will be "
                "reconciled by the periodic re-sync. "
                "[workspace: %s] [unique_id: %s]",
                workspace_id,
                unique_id,
            )
        elif not should_finalize:
            logger.warning(
                "Skipped experiment record registration and data usage update "
                "due to observe_overall() failure. [workspace: %s] [unique_id: %s]",
                workspace_id,
                unique_id,
            )
    except Exception as e:
        logger.error(f"snakemake_execute post process failed: {e}", exc_info=True)

    # result error handling
    if not snakemake_result:
        # Operate remote storage.
        if RemoteStorageController.is_available():
            remote_bucket_name = RemoteSyncStatusFileUtil.get_remote_bucket_name(
                workspace_id, unique_id
            )

            # force update sync status file
            RemoteSyncStatusFileUtil.create_sync_status_file_for_error(
                remote_bucket_name,
                workspace_id,
                unique_id,
                RemoteSyncAction.UPLOAD,
            )

    return snakemake_result


def _observe_overall_with_lock_retry(workspace_id: str, unique_id: str) -> tuple:
    """Run observe_overall() at finalization, tolerating upload-lock conflicts.

    observe_overall() finalizes the local ExptConfig (node statuses) first, then
    uploads the experiment to remote storage under the per-experiment lock. A
    concurrent /run/result observe (main API process) may hold that lock, in
    which case the upload raises RemoteStorageLockError even though the local
    ExptConfig is already finalized. The upload is idempotent and is completed
    by whichever path wins the lock, so the conflict is retried a bounded number
    of times to let this path land its own upload once the lock frees.

    A lock conflict only proves the other path holds the lock, not that its
    upload succeeded, so the remote sync-status file (written SUCCESS by the
    winning writer) is checked before reporting the upload as confirmed.

    Returns:
        (observe_success, observe_lock_conflict, upload_confirmed):
            observe_success is True when observe_overall() completed here.
            observe_lock_conflict is True when at least one attempt hit the
            upload lock; the caller finalizes the DB even if it never succeeded,
            because the local ExptConfig is already finalized.
            upload_confirmed is True when the remote upload is known complete —
            either this path uploaded, or a lock conflict was accompanied by a
            SUCCESS remote sync-status written by the path that won the lock.
    """
    observe_success = False
    observe_lock_conflict = False
    upload_confirmed = False
    retry_max = RemoteSyncLockFileUtil.LOCK_CONFLICT_RETRY_MAX

    for attempt in range(1, retry_max + 1):
        try:
            asyncio.run(WorkflowResult(workspace_id, unique_id).observe_overall())
            observe_success = True
            upload_confirmed = True
            break
        except RemoteStorageLockError as e:
            observe_lock_conflict = True
            # A SUCCESS sync status proves the winner's upload landed; stop
            # retrying our own.
            if RemoteSyncStatusFileUtil.check_sync_status_success(
                workspace_id, unique_id
            ):
                upload_confirmed = True
                logger.info(
                    "observe_overall() upload lock held by the concurrent "
                    "observe path, which reported sync success (attempt %d/%d). "
                    "[workspace: %s] [unique_id: %s]",
                    attempt,
                    retry_max,
                    workspace_id,
                    unique_id,
                )
                break
            logger.warning(
                "observe_overall() upload lock conflict (attempt %d/%d): %s",
                attempt,
                retry_max,
                e,
            )
            if attempt < retry_max:
                time.sleep(RemoteSyncLockFileUtil.LOCK_CONFLICT_RETRY_BACKOFF_SECONDS)
        except Exception as e:
            logger.error(
                f"snakemake_execute post process (WorkflowResult) failed: {e}",
                exc_info=True,
            )
            break

    # The winner may have completed between our last attempt and now.
    if observe_lock_conflict and not upload_confirmed:
        upload_confirmed = RemoteSyncStatusFileUtil.check_sync_status_success(
            workspace_id, unique_id
        )

    return observe_success, observe_lock_conflict, upload_confirmed


def delete_dependencies(
    workspace_id: str,
    unique_id: str,
    smk_params: SmkParam,
    nodeDict: Dict[str, Node],
    edgeDict: Dict[str, Edge],
):
    queue = deque()

    for param in smk_params.forcerun:
        queue.append(param.nodeId)

    while True:
        # terminate condition
        if len(queue) == 0:
            break

        # delete pickle
        node_id = queue.pop()
        algo_name = nodeDict[node_id].data.label

        pickle_filepath = join_filepath(
            [
                DIRPATH.OUTPUT_DIR,
                get_pickle_file(
                    workspace_id=workspace_id,
                    unique_id=unique_id,
                    node_id=node_id,
                    algo_name=algo_name,
                ),
            ]
        )

        if os.path.exists(pickle_filepath):
            os.remove(pickle_filepath)

        # 全てのedgeを見て、node_idがsourceならtargetをqueueに追加する
        for edge in edgeDict.values():
            if node_id == edge.source:
                queue.append(edge.target)


def delete_procs_dependencies(
    workspace_id: str,
    unique_id: str,
    forceRunList: List[ForceRun],
):
    """
    Delete procs (ExptConfig.procs) dependencies
    """

    for proc in forceRunList:
        # delete pickle
        pickle_filepath = join_filepath(
            [
                DIRPATH.OUTPUT_DIR,
                get_pickle_file(
                    workspace_id=workspace_id,
                    unique_id=unique_id,
                    node_id=proc.nodeId,
                    algo_name=proc.name,
                ),
            ]
        )

        if os.path.exists(pickle_filepath):
            os.remove(pickle_filepath)
