from typing import List

import h5py
import numpy as np
from fastapi import APIRouter, Depends

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
from studio.app.common.routers.files import get_hdf5_structure_dict
from studio.app.const import MetadataCacheFile
from studio.app.dir_path import DIRPATH
from studio.app.optinist.schemas.hdf5 import HDF5Node

router = APIRouter()


class HDF5Getter:
    @classmethod
    def get(cls, filepath) -> List[HDF5Node]:
        cls.hdf5_list: List[HDF5Node] = []
        with h5py.File(filepath, "r") as f:
            f.visititems(cls.get_ds_dictionaries)

        return cls.hdf5_list

    @classmethod
    def get_ds_dictionaries(cls, path: str, node: h5py.Dataset):
        if isinstance(node, h5py.Dataset):
            if len(node.shape) != 0:
                cls.recursive_dir_tree(cls.hdf5_list, path.split("/"), node, "")

    @classmethod
    def recursive_dir_tree(
        cls,
        node_list: List[HDF5Node],
        path_list: List[str],
        node: h5py.Dataset,
        parent_path: str,
    ):
        name = path_list[0]
        if name.startswith("#"):
            return

        path = name if parent_path == "" else f"{parent_path}/{name}"

        is_exists = False
        # 既にkeyがある
        for i, value in enumerate(node_list):
            if value.name == name:
                is_exists = True
                if len(path_list) > 1:
                    cls.recursive_dir_tree(
                        node_list[i].nodes, path_list[1:], node, path
                    )

        if not is_exists:
            if len(path_list) > 1:
                node_list.append(
                    HDF5Node(
                        isDir=True,
                        name=name,
                        path=path,
                        nodes=[],
                    )
                )
                cls.recursive_dir_tree(node_list[-1].nodes, path_list[1:], node, path)
            else:
                node_list.append(
                    HDF5Node(
                        isDir=False,
                        name=name,
                        path=path,
                        shape=node.shape,
                        nbytes=node.nbytes,
                        dataType=(
                            "array"
                            if isinstance(node[:], np.ndarray)
                            else type(node[:]).__name__
                        ),
                    )
                )


def _dict_to_hdf5_node(d: dict) -> HDF5Node:
    """Convert a dict from cached JSON back to HDF5Node."""
    return HDF5Node(
        isDir=d["isDir"],
        name=d["name"],
        path=d["path"],
        nodes=[_dict_to_hdf5_node(n) for n in d.get("nodes", [])] or None,
        shape=tuple(d["shape"]) if d.get("shape") else None,
        nbytes=d.get("nbytes"),
        dataType=d.get("dataType"),
    )


@router.get("/hdf5/{file_path:path}", response_model=List[HDF5Node], tags=["outputs"])
async def get_files(
    file_path: str,
    workspace_id: str,
    remote_bucket_name: str = Depends(get_user_remote_bucket_name),
):
    """Get HDF5 file structure.

    First checks for cached structure in .hdf5_structure.json (downloaded from S3
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
                    workspace_id, MetadataCacheFile.HDF5_STRUCTURE
                )
        except Exception:
            pass  # Ignore errors - will fall back to file extraction

    # Check for cached structure
    structure_dict = get_hdf5_structure_dict(workspace_id)
    if file_path in structure_dict:
        cached = structure_dict[file_path]
        return [_dict_to_hdf5_node(node) for node in cached]

    # Fall back to extracting from file directly
    full_path = join_filepath([DIRPATH.INPUT_DIR, workspace_id, file_path])
    return HDF5Getter.get(full_path)
