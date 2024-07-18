import datetime
import json
import os
from abc import ABCMeta, abstractmethod
from enum import Enum

from studio.app.common.core.utils.filepath_creater import join_filepath
from studio.app.dir_path import DIRPATH


class RemoteStorageType(Enum):
    NO_USE = "0"
    MOCK = "1"
    S3 = "2"

    @staticmethod
    def get_activated_type():
        return os.environ.get("REMOTE_STORAGE_TYPE", RemoteStorageType.NO_USE.value)


class RemoteSyncStatus(Enum):
    OK = "OK"
    NG = "NG"


class RemoteSyncAction(Enum):
    UPLOAD = "upload"
    DOWNLOAD = "download"


class RemoteSyncStatusFileUtil:
    REMOTE_SYNC_STATUS_FILE = "remote_sync_stat.json"

    @classmethod
    def make_sync_status_file_path(cls, workspace_id: str, unique_id: str) -> None:
        """
        make remote storage sync status file path.
        """
        experiment_local_path = join_filepath(
            [DIRPATH.OUTPUT_DIR, workspace_id, unique_id]
        )
        remote_sync_status_file_path = os.path.join(
            experiment_local_path, cls.REMOTE_SYNC_STATUS_FILE
        )
        return remote_sync_status_file_path

    @classmethod
    def check_sync_status_file(cls, workspace_id: str, unique_id: str) -> None:
        """
        create remote storage sync status file.
        """
        remote_sync_status_file_path = cls.make_sync_status_file_path(
            workspace_id, unique_id
        )

        remote_sync_status = None
        if os.path.isfile(remote_sync_status_file_path):
            with open(remote_sync_status_file_path) as f:
                sync_status_data = json.load(f)
                remote_sync_status = (
                    sync_status_data.get("status") == RemoteSyncStatus.OK.value
                )

        return remote_sync_status

    @classmethod
    def create_sync_status_file(
        cls, workspace_id: str, unique_id: str, remote_sync_action: RemoteSyncAction
    ) -> None:
        """
        create remote storage sync status file.
        """
        remote_sync_status_file_path = cls.make_sync_status_file_path(
            workspace_id, unique_id
        )

        with open(remote_sync_status_file_path, "w") as f:
            sync_status_data = {
                "remote_storage_type": RemoteStorageType.get_activated_type(),
                "action": remote_sync_action.value,
                "status": RemoteSyncStatus.OK.value,
                "timestamp": datetime.datetime.now(),
            }
            json.dump(sync_status_data, f, default=str, indent=2)

    @classmethod
    def delete_sync_status_file(cls, workspace_id: str, unique_id: str) -> None:
        """
        delete remote storage sync status file.
        """
        remote_sync_status_file_path = cls.make_sync_status_file_path(
            workspace_id, unique_id
        )

        if os.path.isfile(remote_sync_status_file_path):
            os.remove(remote_sync_status_file_path)


class BaseRemoteStorageController(metaclass=ABCMeta):
    @staticmethod
    def use_remote_storage():
        """
        Determine if remote storage is used
        """
        remote_storage_type = RemoteStorageType.get_activated_type()
        use_remote_storage = remote_storage_type in [
            RemoteStorageType.MOCK.value,
            RemoteStorageType.S3.value,
        ]
        return use_remote_storage

    @abstractmethod
    def make_experiment_local_path(self, workspace_id: str, unique_id: str) -> str:
        """
        make experiment data directory local path.
        """

    @abstractmethod
    def make_experiment_remote_path(self, workspace_id: str, unique_id: str) -> str:
        """
        make experiment data directory remote path.
        """

    @abstractmethod
    def download_all_experiments_metas(self) -> bool:
        """
        download all experiment metadata from remote storage.
        """

    @abstractmethod
    def download_experiment(self, workspace_id: str, unique_id: str) -> bool:
        """
        download experiment data from remote storage.
        """

    @abstractmethod
    def upload_experiment(
        self, workspace_id: str, unique_id: str, target_files: list = None
    ) -> bool:
        """
        download experiment data to remote storage.

        Args:
            target_files list:
                Specify files to be uploaded (By default, all files are targeted)
        """

    @abstractmethod
    def delete_experiment(self, workspace_id: str, unique_id: str) -> bool:
        """
        delete experiment data to remote storage.
        """


class RemoteStorageController(BaseRemoteStorageController):
    def __init__(self):
        remote_storage_type = os.environ.get("REMOTE_STORAGE_TYPE")

        if remote_storage_type == RemoteStorageType.MOCK.value:
            from studio.app.common.core.storage.mock_storage_controller import (
                MockStorageController,
            )

            self.__controller = MockStorageController()
        elif remote_storage_type == RemoteStorageType.S3.value:
            from studio.app.common.core.storage.s3_storage_controller import (
                S3StorageController,
            )

            self.__controller = S3StorageController()
        else:
            assert False, f"Invalid remote_storage_type: {remote_storage_type}"

    def make_experiment_local_path(self, workspace_id: str, unique_id: str) -> str:
        return self.__controller.make_experiment_local_path(workspace_id, unique_id)

    def make_experiment_remote_path(self, workspace_id: str, unique_id: str) -> str:
        return self.__controller.make_experiment_remote_path(workspace_id, unique_id)

    def download_all_experiments_metas(self) -> bool:
        return self.__controller.download_all_experiments_metas()

    def download_experiment(self, workspace_id: str, unique_id: str) -> bool:
        return self.__controller.download_experiment(workspace_id, unique_id)

    def upload_experiment(
        self, workspace_id: str, unique_id: str, target_files: list = None
    ) -> bool:
        return self.__controller.upload_experiment(
            workspace_id, unique_id, target_files
        )

    def delete_experiment(self, workspace_id: str, unique_id: str) -> bool:
        return self.__controller.delete_experiment(workspace_id, unique_id)
