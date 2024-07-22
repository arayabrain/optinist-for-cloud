import os
from glob import glob
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from studio.app.common.core.experiment.experiment import ExptConfig, ExptExtConfig
from studio.app.common.core.experiment.experiment_reader import ExptConfigReader
from studio.app.common.core.experiment.experiment_writer import ExptDataWriter
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.storage.remote_storage_controller import (
    RemoteStorageController,
    RemoteSyncStatusFileUtil,
)
from studio.app.common.core.utils.filepath_creater import join_filepath
from studio.app.common.core.workspace.workspace_dependencies import (
    is_workspace_available,
    is_workspace_owner,
)
from studio.app.common.schemas.experiment import DeleteItem, RenameItem
from studio.app.dir_path import DIRPATH

router = APIRouter(prefix="/experiments", tags=["experiments"])

logger = AppLogger.get_logger()


@router.get(
    "/{workspace_id}",
    response_model=Dict[str, ExptExtConfig],
    dependencies=[Depends(is_workspace_available)],
)
async def get_experiments(workspace_id: str):
    exp_config = {}
    config_paths = glob(
        join_filepath([DIRPATH.OUTPUT_DIR, workspace_id, "*", DIRPATH.EXPERIMENT_YML])
    )

    use_remote_storage = RemoteStorageController.use_remote_storage()

    for path in config_paths:
        try:
            config = ExptConfigReader.read(path)

            # NOTE: Include procs in the function and respond
            #   (for display on the frontend Record screen)
            if config.procs:
                config.function.update(config.procs)

            # Operate remote storage.
            if use_remote_storage:
                # check remote synced status.
                is_remote_synced = RemoteSyncStatusFileUtil.check_sync_status_file(
                    workspace_id, config.unique_id
                )

                # extend config to ExptExtConfig
                config = ExptExtConfig(**config.__dict__)
                config.is_remote_synced = is_remote_synced

            exp_config[config.unique_id] = config
        except Exception as e:
            logger.error(e, exc_info=True)
            pass

    return exp_config


@router.patch(
    "/{workspace_id}/{unique_id}/rename",
    response_model=ExptConfig,
    dependencies=[Depends(is_workspace_owner)],
)
async def rename_experiment(workspace_id: str, unique_id: str, item: RenameItem):
    config = ExptDataWriter(
        workspace_id,
        unique_id,
    ).rename(item.new_name)
    config.nodeDict = []
    config.edgeDict = []

    return config


@router.delete(
    "/{workspace_id}/{unique_id}",
    response_model=bool,
    dependencies=[Depends(is_workspace_owner)],
)
async def delete_experiment(workspace_id: str, unique_id: str):
    try:
        ExptDataWriter(
            workspace_id,
            unique_id,
        ).delete_data()
        return True
    except Exception as e:
        logger.error(e, exc_info=True)
        return False


@router.post(
    "/delete/{workspace_id}",
    response_model=bool,
    dependencies=[Depends(is_workspace_owner)],
)
async def delete_experiment_list(workspace_id: str, deleteItem: DeleteItem):
    try:
        for unique_id in deleteItem.uidList:
            ExptDataWriter(
                workspace_id,
                unique_id,
            ).delete_data()
        return True
    except Exception as e:
        logger.error(e, exc_info=True)
        return False


@router.get(
    "/download/config/{workspace_id}/{unique_id}",
    dependencies=[Depends(is_workspace_available)],
)
async def download_config_experiment(workspace_id: str, unique_id: str):
    config_filepath = join_filepath(
        [DIRPATH.OUTPUT_DIR, workspace_id, unique_id, DIRPATH.SNAKEMAKE_CONFIG_YML]
    )
    if os.path.exists(config_filepath):
        return FileResponse(config_filepath)
    else:
        raise HTTPException(status_code=404, detail="file not found")


@router.get(
    "/sync_remote/{workspace_id}/{unique_id}",
    response_model=bool,
    dependencies=[Depends(is_workspace_available)],
)
async def sync_remote_experiment(workspace_id: str, unique_id: str):
    remote_storage_controller = RemoteStorageController()
    result = remote_storage_controller.download_experiment(workspace_id, unique_id)

    if result:
        return True
    else:
        raise HTTPException(status_code=404, detail="sync remote experiment failed")
