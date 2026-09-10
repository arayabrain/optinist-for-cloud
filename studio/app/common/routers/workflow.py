import asyncio
import os
import shutil
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse

from studio.app.common.core.auth.auth_dependencies import (
    get_current_user,
    get_user_remote_bucket_name,
)
from studio.app.common.core.experiment.experiment_reader import ExptConfigReader
from studio.app.common.core.experiment.experiment_services import ExperimentService
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.storage.remote_storage_controller import (
    RemoteStorageBucketNotFoundError,
    RemoteStorageController,
    RemoteStorageLockError,
    RemoteStorageReader,
    RemoteStorageSimpleReader,
    RemoteStorageSimpleWriter,
    RemoteSyncStatusFileUtil,
    upload_experiment_wrapper,
)
from studio.app.common.core.utils.filepath_creater import (
    create_directory,
    join_filepath,
)
from studio.app.common.core.workflow.workflow_reader import WorkflowConfigReader
from studio.app.common.core.workspace.workspace_dependencies import (
    is_workspace_available,
)
from studio.app.common.routers.files import (
    update_hdf5_structure,
    update_image_shape,
    update_mat_structure,
)
from studio.app.common.schemas.users import User
from studio.app.common.schemas.workflow import WorkflowWithResults
from studio.app.const import ACCEPT_FILE_EXT, MetadataCacheFile
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
    current_user: User = Depends(get_current_user),
):
    try:
        last_expt_config = ExperimentService.get_last_experiment(workspace_id)

        # If no local experiments found, try syncing from S3 (multi-instance scenario)
        if not last_expt_config and RemoteStorageController.is_available():
            logger.info(
                f"No local experiments in workspace {workspace_id}, syncing from S3"
            )
            async with RemoteStorageSimpleReader(
                remote_bucket_name
            ) as remote_storage_controller:
                await remote_storage_controller.download_all_experiments_metas(
                    [workspace_id]
                )
            # Retry finding last experiment after sync
            last_expt_config = ExperimentService.get_last_experiment(workspace_id)

        if last_expt_config:
            unique_id = last_expt_config.unique_id

            # Ensure experiment yaml exists locally before accessing.
            # Downloads from S3 if not present (handles multi-instance scenarios).
            await ExptConfigReader.ensure_synced_async(
                workspace_id, unique_id, remote_bucket_name
            )

            # Check remote sync status without triggering full download.
            # Full data sync is deferred to reproduce_experiment when user
            # explicitly clicks Reproduce.
            is_remote_synced = RemoteSyncStatusFileUtil.check_sync_status_success(
                workspace_id, unique_id
            )

            # fetch workflow
            workflow_config = WorkflowConfigReader.read(workspace_id, unique_id)
            return WorkflowWithResults(
                **asdict(last_expt_config),
                **asdict(workflow_config),
                is_remote_synced=is_remote_synced,
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No experiment found in workspace",
            )

    except HTTPException as e:
        # A workspace with no previous run is an expected state, not an error.
        if e.status_code == status.HTTP_404_NOT_FOUND:
            logger.debug(f"No previous experiment in workspace {workspace_id}")
        else:
            logger.error(e, exc_info=True)
        raise
    except RemoteStorageLockError as e:
        logger.error(e)
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=str(e))
    except RemoteStorageBucketNotFoundError as e:
        logger.error(f"User bucket not found for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Storage bucket not found. "
            "Please sign out and sign in again to recover.",
        )
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
        # Ensure experiment yaml exists locally before accessing.
        # Downloads from S3 if not present (handles multi-instance scenarios).
        await ExptConfigReader.ensure_synced_async(
            workspace_id, unique_id, remote_bucket_name
        )

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

            return WorkflowWithResults(
                **asdict(experiment_config),
                **asdict(workflow_config),
                is_remote_synced=True,
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="file not found"
            )

    except HTTPException as e:
        logger.error(e, exc_info=True)
        raise e
    except RemoteStorageLockError as e:
        logger.error(e)
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=str(e))
    except RemoteStorageBucketNotFoundError as e:
        # Bucket is gone, so experiment data is lost - can't recover
        logger.error(f"User bucket not found, experiment data unavailable: {e}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Experiment data not available. Storage may have been deleted.",
        )
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
    logger.info(
        f"Starting sample data import: workspace: {workspace_id}, category: {category}"
    )

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
        # ------------------------------------------------------------
        # Upload the input sample data to remote storage process.
        # ------------------------------------------------------------
        async with RemoteStorageSimpleWriter(
            remote_bucket_name,
        ) as remote_storage_controller:
            sample_data_dir = Path(
                join_filepath(
                    [DIRPATH.ROOT_DIR, sample_data_dir_name, category, "input"]
                )
            )

            sample_data_subdir = sorted(
                [
                    p
                    for p in sample_data_dir.iterdir()
                    if p.is_file() and p.name.startswith("sample_")
                ]
            )

            logger.info(f"Found {len(sample_data_subdir)} sample input subdirectories.")

            if not sample_data_subdir:
                logger.warning("No valid sample input subdirectories found for upload.")

            tasks = [
                remote_storage_controller.upload_input_data(workspace_id, filename.name)
                for filename in sample_data_subdir
            ]
            await asyncio.gather(*tasks)

            # Generate metadata files for various file types and upload to S3

            # TIFF files: .image_shape.json
            tiff_files = [
                p
                for p in sample_data_subdir
                if p.name.endswith(tuple(ACCEPT_FILE_EXT.TIFF_EXT.value))
            ]
            if tiff_files:
                for tiff_file in tiff_files:
                    update_image_shape(workspace_id, tiff_file.name)
                await remote_storage_controller.upload_input_data(
                    workspace_id, MetadataCacheFile.IMAGE_SHAPE
                )

            # HDF5 files: .hdf5_structure.json
            hdf5_files = [
                p
                for p in sample_data_subdir
                if p.name.endswith(tuple(ACCEPT_FILE_EXT.HDF5_EXT.value))
            ]
            if hdf5_files:
                for hdf5_file in hdf5_files:
                    update_hdf5_structure(workspace_id, hdf5_file.name)
                await remote_storage_controller.upload_input_data(
                    workspace_id, MetadataCacheFile.HDF5_STRUCTURE
                )

            # MATLAB files: .mat_structure.json
            mat_files = [
                p
                for p in sample_data_subdir
                if p.name.endswith(tuple(ACCEPT_FILE_EXT.MATLAB_EXT.value))
            ]
            if mat_files:
                for mat_file in mat_files:
                    update_mat_structure(workspace_id, mat_file.name)
                await remote_storage_controller.upload_input_data(
                    workspace_id, MetadataCacheFile.MAT_STRUCTURE
                )

            # ------------------------------------------------------------
            # Upload the output sample data to remote storage process.
            # ------------------------------------------------------------

            sample_data_output_dir = Path(
                join_filepath(
                    [DIRPATH.ROOT_DIR, sample_data_dir_name, category, "output"]
                )
            )

            sample_data_output_subdirs = sorted(
                [p for p in sample_data_output_dir.iterdir() if p.is_dir()]
            )

            if not sample_data_output_subdirs:
                logger.warning("No valid sample output directories found for upload.")

            tasks = [
                upload_experiment_wrapper(remote_bucket_name, workspace_id, p.name)
                for p in sample_data_output_subdirs
            ]
            await asyncio.gather(*tasks)

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
        try:
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
        except RemoteStorageLockError as e:
            logger.warning(e)
            raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=str(e))

    return True
