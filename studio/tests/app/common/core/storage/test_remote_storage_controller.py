"""
Note of this file:
- This file is the test code for RemoteStorageController.
- RemoteStorageController's mode (Mock, S3, ...)
  is set by the value of the environment variable (.env).
"""

import os
import shutil

from studio.app.common.core.storage.remote_storage_controller import (  # noqa: E402
    RemoteStorageController,
    RemoteSyncAction,
    RemoteSyncStatusFileUtil,
)
from studio.app.dir_path import DIRPATH

workspace_id = "default"
unique_id = "0123"


def test_initialize():
    if not RemoteStorageController.use_remote_storage():
        print("RemoteStorageController is available, skip this test.")
        return

    test_data_src_path = f"{DIRPATH.DATA_DIR}/output_test/{workspace_id}/{unique_id}"
    test_data_output_path = f"{DIRPATH.DATA_DIR}/output/{workspace_id}/{unique_id}"

    # cleaning local storage
    if os.path.exists(test_data_output_path):
        shutil.rmtree(test_data_output_path)

    # copy test data
    shutil.copytree(
        test_data_src_path,
        test_data_output_path,
        dirs_exist_ok=True,
    )


def test_RemoteSyncStatusFileUtil():
    if not RemoteStorageController.use_remote_storage():
        print("RemoteStorageController is available, skip this test.")
        return

    # test create_sync_status_file()
    RemoteSyncStatusFileUtil.create_sync_status_file(
        workspace_id, unique_id, RemoteSyncAction.UPLOAD
    )
    is_remote_sync_status_ok = RemoteSyncStatusFileUtil.check_sync_status_file(
        workspace_id, unique_id
    )
    assert is_remote_sync_status_ok, "create_sync_status_file failed.."

    # test delete_sync_status_file()
    RemoteSyncStatusFileUtil.delete_sync_status_file(workspace_id, unique_id)


def test_RemoteStorageController_upload():
    if not RemoteStorageController.use_remote_storage():
        print("RemoteStorageController is available, skip this test.")
        return

    remote_storage_controller = RemoteStorageController()

    # upload specific files to remote
    target_files = [DIRPATH.EXPERIMENT_YML, DIRPATH.WORKFLOW_YML]
    remote_storage_controller.upload_experiment(workspace_id, unique_id, target_files)

    # delete remote files
    remote_storage_controller.delete_experiment(workspace_id, unique_id)

    # upload all files to remote
    remote_storage_controller.upload_experiment(workspace_id, unique_id)


def test_RemoteStorageController_download():
    if not RemoteStorageController.use_remote_storage():
        print("RemoteStorageController is available, skip this test.")
        return

    remote_storage_controller = RemoteStorageController()

    test_data_output_path = f"{DIRPATH.DATA_DIR}/output/{workspace_id}/{unique_id}"
    test_data_output_experiment_yaml = (
        f"{test_data_output_path}/{DIRPATH.EXPERIMENT_YML}"
    )

    # cleaning local storage
    if os.path.exists(test_data_output_path):
        shutil.rmtree(test_data_output_path)

    # download remote metadata files
    remote_storage_controller.download_all_experiments_metas()
    assert os.path.isfile(
        test_data_output_experiment_yaml
    ), "download_all_experiments_metas failed.."

    # re cleaning local storage
    if os.path.exists(test_data_output_path):
        shutil.rmtree(test_data_output_path)

    # download remote files
    remote_storage_controller.download_experiment(workspace_id, unique_id)
    assert os.path.isfile(
        test_data_output_experiment_yaml
    ), "download_all_experiments_metas failed.."
