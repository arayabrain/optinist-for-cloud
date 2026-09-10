import json
import os
import shutil
import tempfile
from glob import glob
from pathlib import PurePath
from typing import Dict, List
from urllib.parse import urlparse

import requests
import tifffile
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from requests.models import Response
from sqlmodel import Session
from tqdm import tqdm

from studio.app.common.core.auth.auth_dependencies import (
    get_current_user,
    get_user_remote_bucket_name,
)
from studio.app.common.core.cloud.cloud_utils import get_effective_quota_bytes
from studio.app.common.core.cloud.storage_tracking import (
    get_current_user_storage_usage,
    get_user_storage_usage,
)
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.storage.remote_storage_controller import (
    RemoteStorageController,
    RemoteStorageSimpleReader,
    RemoteStorageSimpleWriter,
)
from studio.app.common.core.utils.file_reader import JsonReader
from studio.app.common.core.utils.filepath_creater import (
    create_directory,
    join_filepath,
)
from studio.app.common.core.workspace.workspace_data_capacity_services import (
    WorkspaceDataCapacityService,
)
from studio.app.common.core.workspace.workspace_dependencies import (
    is_workspace_available,
    is_workspace_owner,
)
from studio.app.common.db.database import get_db
from studio.app.common.schemas.files import (
    DownloadFileRequest,
    DownloadStatus,
    FilePath,
    SyncStatus,
    TreeNode,
    TreeNodeWithSync,
)
from studio.app.common.schemas.users import User
from studio.app.const import ACCEPT_FILE_EXT, FILETYPE, MetadataCacheFile
from studio.app.dir_path import DIRPATH

router = APIRouter(prefix="/files", tags=["files"])

logger = AppLogger.get_logger()


class DirTreeGetter:
    @classmethod
    def get_tree(
        cls, workspace_id, file_types: List[str], dirname: str = None
    ) -> List[TreeNode]:
        nodes: List[TreeNode] = []

        if dirname is None:
            absolute_dirpath = join_filepath([DIRPATH.INPUT_DIR, workspace_id])
        else:
            absolute_dirpath = join_filepath([DIRPATH.INPUT_DIR, workspace_id, dirname])

        if not os.path.exists(absolute_dirpath):
            return nodes

        sorted_listdir = sorted(
            os.listdir(absolute_dirpath),
            key=lambda x: (not os.path.isdir(join_filepath([absolute_dirpath, x])), x),
        )

        IMAGE_SHAPE_DICT = (
            get_image_shape_dict(workspace_id)
            if file_types == ACCEPT_FILE_EXT.TIFF_EXT.value
            else {}
        )

        for node_name in sorted_listdir:
            if dirname is None:
                relative_path = node_name
            else:
                relative_path = join_filepath([dirname, node_name])

            search_dirpath = join_filepath([absolute_dirpath, node_name])

            if os.path.isfile(search_dirpath) and node_name.endswith(tuple(file_types)):
                shape = IMAGE_SHAPE_DICT.get(relative_path, {}).get("shape")
                if shape is None and file_types == ACCEPT_FILE_EXT.TIFF_EXT.value:
                    shape = update_image_shape(workspace_id, relative_path)
                nodes.append(
                    TreeNode(
                        path=relative_path,
                        name=node_name,
                        isdir=False,
                        nodes=[],
                        shape=shape,
                    )
                )
            elif (
                os.path.isdir(search_dirpath)
                and len(cls.accept_files(search_dirpath, file_types)) > 0
            ):
                nodes.append(
                    TreeNode(
                        path=node_name,
                        name=node_name,
                        isdir=True,
                        nodes=cls.get_tree(workspace_id, file_types, relative_path),
                    )
                )

        return nodes

    @classmethod
    def accept_files(cls, path: str, file_types: List[str]):
        files_list = []
        for file_type in file_types:
            files_list.extend(
                glob(join_filepath([path, "**", f"*{file_type}"]), recursive=True)
            )

        return files_list


def get_image_shape_dict(workspace_id: str):
    dirpath = join_filepath([DIRPATH.INPUT_DIR, workspace_id])
    try:
        tiff_format_dict = JsonReader.read(
            join_filepath([dirpath, MetadataCacheFile.IMAGE_SHAPE])
        )
        return tiff_format_dict
    except FileNotFoundError:
        return {}


def update_image_shape(workspace_id: str, relative_file_path: str):
    dirpath = join_filepath([DIRPATH.INPUT_DIR, workspace_id])
    filepath = join_filepath([dirpath, relative_file_path])

    try:
        img = tifffile.imread(filepath)
        shape = img.shape
    except:  # noqa
        shape = []

    # Save to .image_shape.json with atomic write
    tiff_format_file = join_filepath([dirpath, MetadataCacheFile.IMAGE_SHAPE])
    _atomic_json_update(tiff_format_file, relative_file_path, {"shape": shape})

    return shape


def _structure_node_to_dict(node) -> dict:
    """Convert HDF5Node or MatNode to a JSON-serializable dict.

    Both node types share the same structure (isDir, name, path, nodes, shape,
    nbytes, dataType), so this function handles both.
    """
    result = {
        "isDir": node.isDir,
        "name": node.name,
        "path": node.path,
    }
    if node.nodes:
        result["nodes"] = [_structure_node_to_dict(n) for n in node.nodes]
    else:
        result["nodes"] = []
    if node.shape is not None:
        result["shape"] = list(node.shape) if hasattr(node.shape, "__iter__") else []
    if node.nbytes is not None:
        result["nbytes"] = node.nbytes
    if node.dataType is not None:
        result["dataType"] = node.dataType
    return result


def _atomic_json_update(filepath: str, key: str, value) -> None:
    """Atomically update a JSON file with a new key-value pair.

    Uses a temporary file and os.replace() to ensure atomic writes,
    preventing race conditions when multiple processes update the same file.
    """
    # Read existing data
    try:
        existing_data = JsonReader.read(filepath)
    except FileNotFoundError:
        existing_data = {}

    existing_data[key] = value

    # Write to a temporary file in the same directory, then atomically replace
    dir_path = os.path.dirname(filepath)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=dir_path, suffix=".tmp", delete=False
    ) as tmp_file:
        json.dump(existing_data, tmp_file, indent=2)
        tmp_path = tmp_file.name

    # Atomic replace (POSIX guarantees atomicity for same-filesystem rename)
    os.replace(tmp_path, filepath)


def update_hdf5_structure(workspace_id: str, relative_file_path: str) -> List[dict]:
    """
    Extract and cache HDF5 structure for a file.

    This caches the structure tree to .hdf5_structure.json so that
    remote-only files can show their structure without downloading
    the full file.
    """
    from studio.app.optinist.routers.hdf5 import HDF5Getter

    dirpath = join_filepath([DIRPATH.INPUT_DIR, workspace_id])
    filepath = join_filepath([dirpath, relative_file_path])

    try:
        structure = HDF5Getter.get(filepath)
        structure_dict = [_structure_node_to_dict(node) for node in structure]
    except Exception as e:
        logger.warning(
            f"Failed to extract HDF5 structure for {relative_file_path}: {e}"
        )
        structure_dict = []

    # Save to .hdf5_structure.json with atomic write
    structure_file = join_filepath([dirpath, MetadataCacheFile.HDF5_STRUCTURE])
    _atomic_json_update(structure_file, relative_file_path, structure_dict)

    return structure_dict


def update_mat_structure(workspace_id: str, relative_file_path: str) -> List[dict]:
    """
    Extract and cache MATLAB structure for a file.

    This caches the structure tree to .mat_structure.json so that
    remote-only files can show their structure without downloading
    the full file.
    """
    from studio.app.optinist.routers.mat import MatGetter

    dirpath = join_filepath([DIRPATH.INPUT_DIR, workspace_id])

    try:
        structure = MatGetter.get(relative_file_path, workspace_id)
        structure_dict = [_structure_node_to_dict(node) for node in structure]
    except Exception as e:
        logger.warning(
            f"Failed to extract MATLAB structure for {relative_file_path}: {e}"
        )
        structure_dict = []

    # Save to .mat_structure.json with atomic write
    structure_file = join_filepath([dirpath, MetadataCacheFile.MAT_STRUCTURE])
    _atomic_json_update(structure_file, relative_file_path, structure_dict)

    return structure_dict


def get_hdf5_structure_dict(workspace_id: str) -> dict:
    """Get cached HDF5 structure dictionary."""
    dirpath = join_filepath([DIRPATH.INPUT_DIR, workspace_id])
    try:
        return JsonReader.read(
            join_filepath([dirpath, MetadataCacheFile.HDF5_STRUCTURE])
        )
    except FileNotFoundError:
        return {}


def get_mat_structure_dict(workspace_id: str) -> dict:
    """Get cached MATLAB structure dictionary."""
    dirpath = join_filepath([DIRPATH.INPUT_DIR, workspace_id])
    try:
        return JsonReader.read(
            join_filepath([dirpath, MetadataCacheFile.MAT_STRUCTURE])
        )
    except FileNotFoundError:
        return {}


async def _update_and_upload_metadata(
    workspace_id: str,
    filename: str,
    remote_bucket_name: str,
    update_metadata: bool = True,
):
    """Update metadata cache and upload to S3 if needed.

    Called as a background task after syncing input files. This helps
    backfill metadata (image shape, HDF5/MATLAB structure) for files
    that were uploaded before structure caching was implemented.
    """
    try:
        # Determine file type and update appropriate metadata
        if filename.endswith(tuple(ACCEPT_FILE_EXT.TIFF_EXT.value)):
            if update_metadata:
                update_image_shape(workspace_id, filename)
            metadata_file = MetadataCacheFile.IMAGE_SHAPE
        elif filename.endswith(tuple(ACCEPT_FILE_EXT.HDF5_EXT.value)):
            if update_metadata:
                update_hdf5_structure(workspace_id, filename)
            metadata_file = MetadataCacheFile.HDF5_STRUCTURE
        elif filename.endswith(tuple(ACCEPT_FILE_EXT.MATLAB_EXT.value)):
            if update_metadata:
                update_mat_structure(workspace_id, filename)
            metadata_file = MetadataCacheFile.MAT_STRUCTURE
        else:
            # No metadata to update for this file type
            return

        # Upload updated metadata to S3
        async with RemoteStorageSimpleWriter(
            remote_bucket_name
        ) as remote_storage_controller:
            await remote_storage_controller.upload_input_data(
                workspace_id, metadata_file
            )
        logger.debug(
            f"Updated and uploaded metadata {metadata_file} "
            f"for {workspace_id}/{filename}"
        )
    except Exception as e:
        # Don't fail the sync if metadata update fails - just log warning
        logger.warning(
            f"Failed to update/upload metadata for {workspace_id}/{filename}: {e}"
        )


def _get_file_extensions_for_type(file_type: str) -> List[str]:
    """Get file extensions for a given file type."""
    match file_type:
        case FILETYPE.IMAGE:
            return ACCEPT_FILE_EXT.TIFF_EXT.value
        case FILETYPE.CSV:
            return ACCEPT_FILE_EXT.CSV_EXT.value
        case FILETYPE.HDF5:
            return ACCEPT_FILE_EXT.HDF5_EXT.value
        case FILETYPE.MICROSCOPE:
            return ACCEPT_FILE_EXT.MICROSCOPE_EXT.value
        case FILETYPE.MATLAB:
            return ACCEPT_FILE_EXT.MATLAB_EXT.value
        case _:
            return []


def _extract_all_paths(nodes: List[TreeNode], prefix: str = "") -> set:
    """Extract all file paths from a tree of nodes."""
    paths = set()
    for node in nodes:
        path = join_filepath([prefix, node.path]) if prefix else node.path
        if node.isdir:
            paths.update(_extract_all_paths(node.nodes, path))
        else:
            paths.add(node.path)
    return paths


def _matches_file_type(filename: str, file_type: str) -> bool:
    """Check if filename matches the given file type."""
    if not file_type:
        return True
    extensions = _get_file_extensions_for_type(file_type)
    return filename.endswith(tuple(extensions)) if extensions else False


def _convert_to_sync_nodes(
    nodes: List[TreeNode],
    local_paths: set,
    remote_files: dict,
) -> List[TreeNodeWithSync]:
    """Convert TreeNodes to TreeNodeWithSync with sync_status."""
    result = []
    for node in nodes:
        if node.isdir:
            # Recursively convert children
            child_nodes = _convert_to_sync_nodes(node.nodes, local_paths, remote_files)
            result.append(
                TreeNodeWithSync(
                    path=node.path,
                    name=node.name,
                    isdir=True,
                    nodes=child_nodes,
                    shape=node.shape,
                    sync_status=SyncStatus.SYNCED,
                    size=None,
                )
            )
        else:
            # Determine sync status for files
            is_local = node.path in local_paths
            is_remote = node.path in remote_files
            if is_local and is_remote:
                sync_status = SyncStatus.SYNCED
            elif is_local:
                sync_status = SyncStatus.LOCAL
            else:
                sync_status = SyncStatus.REMOTE

            size = remote_files.get(node.path, {}).get("size")
            result.append(
                TreeNodeWithSync(
                    path=node.path,
                    name=node.name,
                    isdir=False,
                    nodes=[],
                    shape=node.shape,
                    sync_status=sync_status,
                    size=size,
                )
            )
    return result


def _build_tree_from_remote_files(
    remote_files: dict,
    local_paths: set,
    file_type: str,
    image_shape_dict: dict,
) -> List[TreeNodeWithSync]:
    """Build tree nodes for remote-only files."""
    # Group files by directory
    dir_files = {}
    for filename, file_info in remote_files.items():
        if filename in local_paths:
            continue  # Already handled by local tree
        if not _matches_file_type(filename, file_type):
            continue

        parts = filename.split("/")
        if len(parts) == 1:
            # Root level file
            dir_files.setdefault("", []).append((filename, file_info))
        else:
            # File in subdirectory
            dir_name = "/".join(parts[:-1])
            dir_files.setdefault(dir_name, []).append((filename, file_info))

    # Build nodes for root level remote-only files
    result = []
    for filename, file_info in dir_files.get("", []):
        shape = image_shape_dict.get(filename, {}).get("shape")
        result.append(
            TreeNodeWithSync(
                path=filename,
                name=filename,
                isdir=False,
                nodes=[],
                shape=shape,
                sync_status=SyncStatus.REMOTE,
                size=file_info.get("size"),
            )
        )

    return result


@router.get(
    "/{workspace_id}",
    response_model=List[TreeNode],
    dependencies=[Depends(is_workspace_available)],
)
async def get_files(workspace_id: str, file_type: str = None):
    if file_type == FILETYPE.IMAGE:
        return DirTreeGetter.get_tree(workspace_id, ACCEPT_FILE_EXT.TIFF_EXT.value)
    elif file_type == FILETYPE.CSV:
        return DirTreeGetter.get_tree(workspace_id, ACCEPT_FILE_EXT.CSV_EXT.value)
    elif file_type == FILETYPE.HDF5:
        return DirTreeGetter.get_tree(workspace_id, ACCEPT_FILE_EXT.HDF5_EXT.value)
    elif file_type == FILETYPE.MICROSCOPE:
        return DirTreeGetter.get_tree(
            workspace_id, ACCEPT_FILE_EXT.MICROSCOPE_EXT.value
        )
    elif file_type == FILETYPE.MATLAB:
        return DirTreeGetter.get_tree(workspace_id, ACCEPT_FILE_EXT.MATLAB_EXT.value)
    else:
        return []


@router.get(
    "/{workspace_id}/merged",
    response_model=List[TreeNodeWithSync],
    dependencies=[Depends(is_workspace_available)],
)
async def get_files_merged(
    workspace_id: str,
    file_type: str = None,
    remote_bucket_name: str = Depends(get_user_remote_bucket_name),
):
    """Get merged file tree from local filesystem and S3."""
    # 1. Download .image_shape.json from S3 if available (for shape data)
    if RemoteStorageController.is_available():
        try:
            async with RemoteStorageSimpleReader(
                remote_bucket_name
            ) as remote_storage_controller:
                await remote_storage_controller.download_input_data(
                    workspace_id, MetadataCacheFile.IMAGE_SHAPE
                )
        except Exception as e:
            logger.debug(f"Could not download .image_shape.json: {e}")

    # 2. Get local files
    local_nodes = await get_files(workspace_id, file_type)
    local_paths = _extract_all_paths(local_nodes)

    # 3. Get S3 files if available
    remote_files = {}
    if RemoteStorageController.is_available():
        try:
            async with RemoteStorageSimpleReader(
                remote_bucket_name
            ) as remote_storage_controller:
                s3_objects = await remote_storage_controller.list_input_data_objects(
                    workspace_id
                )
                for obj in s3_objects:
                    filename = obj["filename"]
                    # Skip hidden files like .image_shape.json
                    if not filename.startswith("."):
                        if _matches_file_type(filename, file_type):
                            remote_files[filename] = obj
        except Exception as e:
            logger.warning(f"Could not list S3 objects: {e}")

    # 4. Get image shape dict for shape data
    image_shape_dict = (
        get_image_shape_dict(workspace_id) if file_type == FILETYPE.IMAGE else {}
    )

    # 5. Convert local nodes to sync nodes with status
    result = _convert_to_sync_nodes(local_nodes, local_paths, remote_files)

    # 6. Add remote-only files
    remote_only_nodes = _build_tree_from_remote_files(
        remote_files, local_paths, file_type, image_shape_dict
    )
    result.extend(remote_only_nodes)

    return result


@router.post(
    "/{workspace_id}/sync/{filename:path}",
    response_model=FilePath,
    dependencies=[Depends(is_workspace_available)],
)
async def sync_input_file(
    workspace_id: str,
    filename: str,
    background_tasks: BackgroundTasks,
    remote_bucket_name: str = Depends(get_user_remote_bucket_name),
):
    """Download a specific input file from S3 to local storage.

    Used by config dialogs (CSV Settings, HDF5/MATLAB Structure) that need
    to read file contents for a remote-only file.

    Also generates and uploads metadata (image shape, HDF5/MATLAB structure)
    if not already present in S3, helping to backfill metadata for files
    uploaded before structure caching was implemented.
    """
    if not RemoteStorageController.is_available():
        raise HTTPException(status_code=503, detail="Remote storage not available")

    try:
        async with RemoteStorageSimpleReader(
            remote_bucket_name
        ) as remote_storage_controller:
            success = await remote_storage_controller.download_input_data(
                workspace_id, filename
            )
            if not success:
                raise HTTPException(status_code=404, detail="File not found in S3")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to sync input file {filename}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to sync file: {str(e)}")

    # Generate and upload metadata in background for files that need it
    # This helps backfill metadata for files uploaded before caching was added
    background_tasks.add_task(
        _update_and_upload_metadata, workspace_id, filename, remote_bucket_name
    )

    return {"file_path": filename}


@router.post(
    "/{workspace_id}/shape/{filepath}",
    response_model=bool,
    dependencies=[Depends(is_workspace_owner)],
)
async def set_shape(workspace_id: str, filepath: str):
    try:
        update_image_shape(workspace_id, filepath)
    except Exception as e:
        raise HTTPException(status=422, detail=str(e))
    return True


@router.post(
    "/{workspace_id}/upload/{filename}",
    response_model=FilePath,
    dependencies=[Depends(is_workspace_owner)],
)
async def create_file(
    workspace_id: str,
    filename: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    remote_bucket_name: str = Depends(get_user_remote_bucket_name),
):
    try:
        # Check storage quota before allowing file upload
        # (use cached data to avoid timeout)
        current_usage = await get_current_user_storage_usage(
            current_user.id, force_live=False
        )
        storage_info = get_user_storage_usage(current_user.id)
        quota_limit = get_effective_quota_bytes(
            current_user.id, storage_info=storage_info
        )

        if quota_limit > 0:
            storage_usage_percent = (current_usage / quota_limit) * 100

            # Block file upload if over quota (100%)
            if storage_usage_percent >= 100:
                logger.warning(
                    f"File upload blocked for user {current_user.id}: "
                    f"storage quota exceeded ({storage_usage_percent:.1f}% used)"
                )
                raise HTTPException(
                    status_code=403,
                    detail=f"Cannot upload file: Storage quota exceeded "
                    f"({storage_usage_percent:.1f}% used). "
                    f"Please free up space before uploading files.",
                )

        create_directory(join_filepath([DIRPATH.INPUT_DIR, workspace_id]))

        filepath = join_filepath([DIRPATH.INPUT_DIR, workspace_id, filename])

        with open(filepath, "wb") as f:
            shutil.copyfileobj(file.file, f)

        # Update metadata caches based on file type
        if filename.endswith(tuple(ACCEPT_FILE_EXT.TIFF_EXT.value)):
            update_image_shape(workspace_id, filename)
        elif filename.endswith(tuple(ACCEPT_FILE_EXT.HDF5_EXT.value)):
            update_hdf5_structure(workspace_id, filename)
        elif filename.endswith(tuple(ACCEPT_FILE_EXT.MATLAB_EXT.value)):
            update_mat_structure(workspace_id, filename)

        if WorkspaceDataCapacityService.is_available():
            background_tasks.add_task(
                WorkspaceDataCapacityService.update_workspace_data_usage,
                db,
                workspace_id,
            )

        # Operate remote storage data.
        if RemoteStorageController.is_available():
            # upload input data to remote storage
            async with RemoteStorageSimpleWriter(
                remote_bucket_name
            ) as remote_storage_controller:
                await remote_storage_controller.upload_input_data(
                    workspace_id, filename
                )
                # Upload metadata files so they're available for remote-only files
                if filename.endswith(tuple(ACCEPT_FILE_EXT.TIFF_EXT.value)):
                    await remote_storage_controller.upload_input_data(
                        workspace_id, MetadataCacheFile.IMAGE_SHAPE
                    )
                elif filename.endswith(tuple(ACCEPT_FILE_EXT.HDF5_EXT.value)):
                    await remote_storage_controller.upload_input_data(
                        workspace_id, MetadataCacheFile.HDF5_STRUCTURE
                    )
                elif filename.endswith(tuple(ACCEPT_FILE_EXT.MATLAB_EXT.value)):
                    await remote_storage_controller.upload_input_data(
                        workspace_id, MetadataCacheFile.MAT_STRUCTURE
                    )

        # Refresh storage cache in background to keep it up-to-date
        background_tasks.add_task(
            get_current_user_storage_usage, current_user.id, force_live=True
        )

        return {"file_path": filename}

    except HTTPException:
        # Re-raise HTTPExceptions (like 403 storage quota error)
        raise
    except Exception as e:
        logger.error(
            f"Error uploading file {filename} to workspace {workspace_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to upload file: {str(e)}",
        )


DOWNLOAD_STATUS: Dict[str, DownloadStatus] = {}


def _remove_from_metadata_cache(workspace_id: str, filename: str) -> None:
    """Remove a file entry from metadata cache files."""
    dirpath = join_filepath([DIRPATH.INPUT_DIR, workspace_id])

    # Determine which metadata file to update based on file extension
    if filename.endswith(tuple(ACCEPT_FILE_EXT.TIFF_EXT.value)):
        metadata_file = MetadataCacheFile.IMAGE_SHAPE
    elif filename.endswith(tuple(ACCEPT_FILE_EXT.HDF5_EXT.value)):
        metadata_file = MetadataCacheFile.HDF5_STRUCTURE
    elif filename.endswith(tuple(ACCEPT_FILE_EXT.MATLAB_EXT.value)):
        metadata_file = MetadataCacheFile.MAT_STRUCTURE
    else:
        return  # No metadata to clean up for this file type

    metadata_path = join_filepath([dirpath, metadata_file])
    if not os.path.exists(metadata_path):
        return

    try:
        existing_data = JsonReader.read(metadata_path)
        if filename in existing_data:
            del existing_data[filename]
            # Write back atomically
            with tempfile.NamedTemporaryFile(
                mode="w", dir=dirpath, suffix=".tmp", delete=False
            ) as tmp_file:
                json.dump(existing_data, tmp_file, indent=2)
                tmp_path = tmp_file.name
            os.replace(tmp_path, metadata_path)
    except Exception as e:
        logger.warning(f"Failed to remove {filename} from metadata cache: {e}")


@router.delete(
    "/{workspace_id}/delete/{filename:path}",
    response_model=bool,
    dependencies=[Depends(is_workspace_owner)],
)
async def delete_file(
    workspace_id: str,
    filename: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    remote_bucket_name: str = Depends(get_user_remote_bucket_name),
):
    filepath = join_filepath([DIRPATH.INPUT_DIR, workspace_id, filename])
    local_exists = os.path.exists(filepath)
    remote_exists = False

    # Check if file exists on S3
    if RemoteStorageController.is_available():
        try:
            async with RemoteStorageSimpleReader(
                remote_bucket_name
            ) as remote_storage_controller:
                s3_objects = await remote_storage_controller.list_input_data_objects(
                    workspace_id
                )
                remote_exists = any(obj["filename"] == filename for obj in s3_objects)
        except Exception as e:
            logger.warning(f"Could not check S3 for file existence: {e}")

    if not local_exists and not remote_exists:
        raise HTTPException(status_code=404, detail="File not found.")

    try:
        # Remove from remote storage if available
        if RemoteStorageController.is_available() and remote_exists:
            async with RemoteStorageSimpleWriter(
                remote_bucket_name
            ) as remote_storage_controller:
                await remote_storage_controller.delete_input_data(
                    workspace_id, filename
                )

        # Remove local file if it exists
        if local_exists:
            os.remove(filepath)

        # Clean up metadata cache entries
        _remove_from_metadata_cache(workspace_id, filename)

        # Upload updated metadata to S3 so it stays in sync
        await _update_and_upload_metadata(
            workspace_id, filename, remote_bucket_name, update_metadata=False
        )

        if WorkspaceDataCapacityService.is_available():
            background_tasks.add_task(
                WorkspaceDataCapacityService.update_workspace_data_usage,
                db,
                workspace_id,
            )

        return True
    except Exception as e:
        logger.error(e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{workspace_id}/download/status",
    response_model=DownloadStatus,
    dependencies=[Depends(is_workspace_available)],
)
async def get_download_status(workspace_id: str, file_name: str):
    filepath = join_filepath([DIRPATH.INPUT_DIR, workspace_id, file_name])
    try:
        return DOWNLOAD_STATUS[filepath]
    except:  # noqa
        raise HTTPException(status_code=404)


@router.post(
    "/{workspace_id}/download",
    dependencies=[Depends(is_workspace_owner)],
)
async def download_file(
    workspace_id: str,
    file: DownloadFileRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    path = PurePath(urlparse(file.url).path)
    if path.suffix not in ACCEPT_FILE_EXT.ALL_EXT.value:
        raise HTTPException(status_code=400, detail="Invalid url")

    create_directory(join_filepath([DIRPATH.INPUT_DIR, workspace_id]))

    try:
        res = requests.get(file.url, stream=True)
        res.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
    background_tasks.add_task(download, db, res, path.name, workspace_id)
    # Only update image shape for TIFF files
    if path.name.endswith(tuple(ACCEPT_FILE_EXT.TIFF_EXT.value)):
        background_tasks.add_task(update_image_shape, workspace_id, path.name)
    return {"file_name": path.name}


def download(
    db: Session, res: Response, file_name: str, workspace_id: str, chunk_size=1024
):
    total = int(res.headers.get("content-length", 0))
    filepath = join_filepath([DIRPATH.INPUT_DIR, workspace_id, file_name])
    current = 0

    try:
        with (
            open(filepath, "wb") as file,
            tqdm(
                desc=filepath,
                total=total,
                unit="iB",
                unit_scale=True,
                unit_divisor=1024,
            ) as bar,
        ):
            for data in res.iter_content(chunk_size=chunk_size):
                size = file.write(data)
                current += size
                DOWNLOAD_STATUS[filepath] = DownloadStatus(total=total, current=current)
                bar.update(size)
    except Exception as e:
        DOWNLOAD_STATUS[filepath] = DownloadStatus(error=str(e))

    if WorkspaceDataCapacityService.is_available():
        WorkspaceDataCapacityService.update_workspace_data_usage(db, workspace_id)
