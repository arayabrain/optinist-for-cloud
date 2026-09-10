import os

from fastapi import APIRouter, Depends, HTTPException, status

from studio.app.common.core.auth.auth_dependencies import get_user_remote_bucket_name
from studio.app.common.core.experiment.experiment import ExptOutputPathIds
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.storage.remote_storage_controller import (
    RemoteStorageController,
    RemoteStorageLockError,
    RemoteStorageReader,
    RemoteSyncStatusFileUtil,
)
from studio.app.common.core.utils.filepath_creater import resolve_absolute_output_path
from studio.app.common.core.utils.path_guard import secure_output_relpath
from studio.app.common.core.workspace.workspace_dependencies import is_workspace_owner
from studio.app.optinist.core.edit_ROI import EditROI, EditRoiUtils
from studio.app.optinist.schemas.roi import RoiList, RoiPos, RoiStatus

router = APIRouter(prefix="/api/visualizations", tags=["visualizations"])

logger = AppLogger.get_logger()


async def ensure_experiment_synced_for_edit(
    filepath: str, remote_bucket_name: str
) -> None:
    """
    Ensure experiment files are synced locally before Edit ROI operations.
    Downloads from S3 if needed (lazy loading).
    """
    if not RemoteStorageController.is_available():
        return

    # Extract workspace_id and unique_id from filepath
    node_dirpath = os.path.dirname(filepath)
    path_ids = ExptOutputPathIds(node_dirpath)
    workspace_id = path_ids.workspace_id
    unique_id = path_ids.unique_id

    # Check if sync is needed
    is_unsynced = RemoteSyncStatusFileUtil.check_sync_status_unsynced(
        workspace_id, unique_id
    )

    if is_unsynced:
        logger.info(
            f"Edit ROI: Lazy loading experiment {workspace_id}/{unique_id} from S3"
        )

        try:
            async with RemoteStorageReader(
                remote_bucket_name, workspace_id, unique_id
            ) as remote_storage_controller:
                result = await remote_storage_controller.download_experiment(
                    workspace_id, unique_id
                )
                if not result:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Failed to sync experiment data for Edit ROI",
                    )
        except RemoteStorageLockError as e:
            logger.warning(e)
            raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=str(e))


@router.post(
    "/image/{filepath:path}/status",
    response_model=RoiStatus,
    dependencies=[Depends(is_workspace_owner)],
)
async def status_roi(
    filepath: str,
    remote_bucket_name: str = Depends(get_user_remote_bucket_name),
):
    filepath = resolve_absolute_output_path(secure_output_relpath(filepath))
    # Ensure experiment is synced before Edit ROI operations
    await ensure_experiment_synced_for_edit(filepath, remote_bucket_name)
    return EditROI(file_path=filepath).get_status()


@router.post(
    "/image/{filepath:path}/add_roi",
    response_model=bool,
    dependencies=[Depends(is_workspace_owner)],
)
async def add_roi(filepath: str, pos: RoiPos):
    filepath = resolve_absolute_output_path(secure_output_relpath(filepath))
    EditROI(file_path=filepath).add(pos)
    return True


@router.post(
    "/image/{filepath:path}/merge_roi",
    response_model=bool,
    dependencies=[Depends(is_workspace_owner)],
)
async def merge_roi(filepath: str, roi_list: RoiList):
    filepath = resolve_absolute_output_path(secure_output_relpath(filepath))
    EditROI(file_path=filepath).merge(roi_list.ids)
    return True


@router.post(
    "/image/{filepath:path}/delete_roi",
    response_model=bool,
    dependencies=[Depends(is_workspace_owner)],
)
async def delete_roi(filepath: str, roi_list: RoiList):
    filepath = resolve_absolute_output_path(secure_output_relpath(filepath))
    EditROI(file_path=filepath).delete(roi_list.ids)
    return True


@router.post(
    "/image/{filepath:path}/commit_edit",
    response_model=bool,
    dependencies=[Depends(is_workspace_owner)],
)
async def commit_edit(
    filepath: str,
    remote_bucket_name: str = Depends(get_user_remote_bucket_name),
):
    filepath = resolve_absolute_output_path(secure_output_relpath(filepath))
    try:
        EditRoiUtils.execute(filepath, remote_bucket_name)

    except RemoteStorageLockError as e:
        logger.error(e)
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=str(e))
    except Exception as e:
        logger.error(e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to commit Edit ROI",
        )

    return True


@router.post(
    "/image/{filepath:path}/cancel_edit",
    response_model=bool,
    dependencies=[Depends(is_workspace_owner)],
)
async def cancel_edit(filepath: str):
    filepath = resolve_absolute_output_path(secure_output_relpath(filepath))
    EditROI(file_path=filepath).cancel()
    return True
