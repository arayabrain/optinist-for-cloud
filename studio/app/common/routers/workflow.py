import os
import shutil
from dataclasses import asdict

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse

from studio.app.common.core.auth.auth_dependencies import get_user_remote_bucket_name
from studio.app.common.core.experiment.experiment_reader import ExptConfigReader
from studio.app.common.core.experiment.experiment_services import ExperimentService
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.storage.remote_storage_controller import (
    RemoteStorageController,
    RemoteStorageLockError,
    RemoteStorageReader,
    RemoteStorageWriter,
    RemoteSyncStatusFileUtil,
)
from studio.app.common.core.utils.filepath_creater import (
    create_directory,
    join_filepath,
)
from studio.app.common.core.workflow.workflow_reader import WorkflowConfigReader
from studio.app.common.core.workspace.workspace_dependencies import (
    is_workspace_available,
)
from studio.app.common.schemas.workflow import WorkflowWithResults
from studio.app.dir_path import DIRPATH

router = APIRouter(prefix="/workflow", tags=["workflow"])

logger = AppLogger.get_logger()


@router.get(
    "/fetch/{workspace_id}",
    response_model=WorkflowWithResults,
    dependencies=[Depends(is_workspace_available)],
)
async def fetch_last_experiment(
    workspace_id: str,
    remote_bucket_name: str = Depends(get_user_remote_bucket_name),
):
    try:
        last_expt_config = ExperimentService.get_last_experiment(workspace_id)
        if last_expt_config:
            unique_id = last_expt_config.unique_id

            # sync unsynced remote storage data.
            is_remote_synced = False
            if RemoteStorageController.is_available():
                is_remote_synced = await force_sync_unsynced_experiment(
                    remote_bucket_name,
                    workspace_id,
                    unique_id,
                    last_expt_config.success,
                )

            # fetch workflow
            workflow_config = WorkflowConfigReader.read(workspace_id, unique_id)
            return WorkflowWithResults(
                **asdict(last_expt_config),
                **asdict(workflow_config),
                is_remote_synced=is_remote_synced,
            )
        else:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    except HTTPException as e:
        logger.error(e)
        raise e
    except RemoteStorageLockError as e:
        logger.error(e)
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=str(e))
    except Exception as e:
        logger.error(e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="can not reproduce record.",
        )


@router.get(
    "/reproduce/{workspace_id}/{unique_id}",
    response_model=WorkflowWithResults,
    dependencies=[Depends(is_workspace_available)],
)
async def reproduce_experiment(
    workspace_id: str,
    unique_id: str,
    remote_bucket_name: str = Depends(get_user_remote_bucket_name),
):
    try:
        experiment_config_path = ExptConfigReader.get_config_yaml_path(
            workspace_id, unique_id
        )
        workflow_config_path = WorkflowConfigReader.get_config_yaml_path(
            workspace_id, unique_id
        )
        if os.path.exists(experiment_config_path) and os.path.exists(
            workflow_config_path
        ):
            experiment_config = ExptConfigReader.read(workspace_id, unique_id)
            workflow_config = WorkflowConfigReader.read(workspace_id, unique_id)

            # sync unsynced remote storage data.
            is_remote_synced = False
            if RemoteStorageController.is_available():
                is_remote_synced = await force_sync_unsynced_experiment(
                    remote_bucket_name,
                    workspace_id,
                    unique_id,
                    experiment_config.success,
                )

            return WorkflowWithResults(
                **asdict(experiment_config),
                **asdict(workflow_config),
                is_remote_synced=is_remote_synced,
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="file not found"
            )

    except HTTPException as e:
        logger.error(e)
        raise e
    except RemoteStorageLockError as e:
        logger.error(e)
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=str(e))
    except Exception as e:
        logger.error(e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="can not reproduce record.",
        )


@router.get(
    "/download/{workspace_id}/{unique_id}",
    dependencies=[Depends(is_workspace_available)],
)
async def download_workspace_config(workspace_id: str, unique_id: str):
    config_filepath = WorkflowConfigReader.get_config_yaml_path(workspace_id, unique_id)
    if os.path.exists(config_filepath):
        return FileResponse(config_filepath, filename=DIRPATH.WORKFLOW_YML)
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="file not found"
        )


@router.post("/import")
async def import_workflow_config(file: UploadFile = File(...)):
    try:
        contents = WorkflowConfigReader.read_from_bytes(await file.read())
        return contents
    except Exception as e:
        logger.error(e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Parsing yaml failed: {str(e)}",
        )


@router.get(
    "/sample_data/{workspace_id}/{category}",
    dependencies=[Depends(is_workspace_available)],
)
async def import_sample_data(
    workspace_id: str,
    category: str,
    remote_bucket_name: str = Depends(get_user_remote_bucket_name),
):
    sample_data_dir_name = "sample_data"
    folders = ["input", "output"]

    for folder in folders:
        import_data_dir = join_filepath(
            [DIRPATH.ROOT_DIR, sample_data_dir_name, category, folder]
        )
        if not os.path.exists(import_data_dir):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="import data not found"
            )

        user_dir = join_filepath([DIRPATH.DATA_DIR, folder, workspace_id])

        create_directory(user_dir)
        shutil.copytree(import_data_dir, user_dir, dirs_exist_ok=True)

    # Operate remote storage data.
    if RemoteStorageController.is_available():
        from glob import glob

        # Get list of sample data names.
        sample_data_output_dir = join_filepath(
            [DIRPATH.ROOT_DIR, sample_data_dir_name, category, "output"]
        )
        sample_data_subdirs = sorted(glob(f"{sample_data_output_dir}/*"))

        # Process sample data individually.
        for sample_data_subdir in sample_data_subdirs:
            unique_id = os.path.basename(sample_data_subdir)

            # Note: Force transfer of sample_data to remote storage
            #   to enable reproduction of sample data.
            async with RemoteStorageWriter(
                remote_bucket_name, workspace_id, unique_id
            ) as remote_storage_controller:
                await remote_storage_controller.upload_experiment(
                    workspace_id, unique_id
                )

    return True


async def force_sync_unsynced_experiment(
    remote_bucket_name: str, workspace_id: str, unique_id: str, workflow_status: str
) -> bool:
    """
    Utility function: If experiment is unsynchronized, perform synchronization
    """

    if not RemoteStorageController.is_available():
        return False

    # If in running, return without remote sync
    is_running = workflow_status == "running"
    if is_running:
        return True

    # If not, perform synchronization
    # check remote synced status.
    is_remote_unsynced = RemoteSyncStatusFileUtil.check_sync_status_unsynced(
        workspace_id, unique_id
    )

    if is_remote_unsynced:
        async with RemoteStorageReader(
            remote_bucket_name, workspace_id, unique_id
        ) as remote_storage_controller:
            result = await remote_storage_controller.download_experiment(
                workspace_id, unique_id
            )

            if not result:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="sync remote experiment failed",
                )

    return True
