from functools import reduce
from typing import List

import numpy as np
from fastapi import APIRouter, Depends
from pymatreader import read_mat

from studio.app.common.core.auth.auth_dependencies import get_user_remote_bucket_name
from studio.app.common.core.storage.remote_storage_controller import (
    RemoteStorageController,
    RemoteStorageSimpleReader,
)
from studio.app.common.core.utils.filepath_creater import join_filepath
from studio.app.common.core.utils.path_guard import (
    secure_component,
    secure_input_relpath,
)
from studio.app.common.routers.files import get_mat_structure_dict
from studio.app.const import MetadataCacheFile
from studio.app.dir_path import DIRPATH
from studio.app.optinist.schemas.mat import MatNode

router = APIRouter()


class MatGetter:
    @classmethod
    def data(cls, filepath, dataPath: str = None):
        data = read_mat(filepath)
        data = {
            key: value
            for key, value in data.items()
            if not key.startswith("__") and not key.startswith("#")
        }
        keys = dataPath.split("/") if dataPath is not None else []
        return reduce(lambda d, key: d[key], keys, data)

    @classmethod
    def get(cls, filepath, workspace_id) -> List[MatNode]:
        filepath = join_filepath([DIRPATH.INPUT_DIR, workspace_id, filepath])
        data = cls.data(filepath, dataPath=None)
        return [cls.dict_to_matnode(value, key, key) for key, value in data.items()]

    @classmethod
    def dict_to_matnode(cls, data, name, current_path="") -> MatNode:
        if isinstance(data, dict):
            return MatNode(
                isDir=True,
                name=name,
                path=current_path,
                nodes=[
                    cls.dict_to_matnode(
                        v, k, f"{current_path}/{k}" if current_path else k
                    )
                    for k, v in data.items()
                ],
            )
        elif isinstance(data, np.ndarray):
            return MatNode(
                isDir=False,
                name=name,
                path=current_path,
                shape=data.shape,
                nbytes=data.nbytes,
                dataType="array",
            )
        else:
            return MatNode(
                isDir=False,
                name=name,
                path=current_path,
                dataType=type(data).__name__,
            )


def _dict_to_mat_node(d: dict) -> MatNode:
    """Convert a dict from cached JSON back to MatNode."""
    return MatNode(
        isDir=d["isDir"],
        name=d["name"],
        path=d["path"],
        nodes=[_dict_to_mat_node(n) for n in d.get("nodes", [])] or None,
        shape=tuple(d["shape"]) if d.get("shape") else None,
        nbytes=d.get("nbytes"),
        dataType=d.get("dataType"),
    )


@router.get(
    "/mat/{file_path:path}",
    response_model=List[MatNode],
    tags=["outputs"],
)
async def get_matfiles(
    file_path: str,
    workspace_id: str,
    remote_bucket_name: str = Depends(get_user_remote_bucket_name),
):
    """Get MATLAB file structure.

    First checks for cached structure in .mat_structure.json (downloaded from S3
    if remote storage is available). Falls back to extracting from the file directly.
    """
    workspace_id = secure_component(workspace_id)
    file_path = secure_input_relpath(workspace_id, file_path)

    # Try to download cached structure from S3 first
    if RemoteStorageController.is_available():
        try:
            async with RemoteStorageSimpleReader(
                remote_bucket_name
            ) as remote_storage_controller:
                await remote_storage_controller.download_input_data(
                    workspace_id, MetadataCacheFile.MAT_STRUCTURE
                )
        except Exception:
            pass  # Ignore errors - will fall back to file extraction

    # Check for cached structure
    structure_dict = get_mat_structure_dict(workspace_id)
    if file_path in structure_dict:
        cached = structure_dict[file_path]
        return [_dict_to_mat_node(node) for node in cached]

    # Fall back to extracting from file directly
    return MatGetter.get(file_path, workspace_id)
