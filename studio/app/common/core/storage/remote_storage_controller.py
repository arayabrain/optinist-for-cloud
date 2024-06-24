import os
from abc import ABCMeta, abstractmethod
from enum import Enum


class RemoteStorageType(Enum):
    NO_USE = "0"
    MOCK = "1"
    S3 = "2"


class BaseRemoteStorageController(metaclass=ABCMeta):
    @staticmethod
    def use_remote_storage():
        """
        Determine if remote storage is used
        """
        remote_storage_type = os.environ.get(
            "REMOTE_STORAGE_TYPE", RemoteStorageType.NO_USE.value
        )
        use_remote_storage = remote_storage_type in [
            RemoteStorageType.MOCK.value,
            RemoteStorageType.S3.value,
        ]
        return use_remote_storage

    @abstractmethod
    def download_experiment_metas(self, workspace_id: str, unique_id: str):
        """
        download experiment metadata from remote storage.
        """

    @abstractmethod
    def download_experiment(self, workspace_id: str, unique_id: str):
        """
        download experiment data from remote storage.
        """

    @abstractmethod
    def upload_experiment(self, workspace_id: str, unique_id: str):
        """
        download experiment data to remote storage.
        """

    @abstractmethod
    def remove_experiment(self, workspace_id: str, unique_id: str):
        """
        remove experiment data to remote storage.
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

    def download_experiment_metas(self, workspace_id: str, unique_id: str):
        self.__controller.download_experiment_metas(workspace_id, unique_id)

    def download_experiment(self, workspace_id: str, unique_id: str):
        self.__controller.download_experiment(workspace_id, unique_id)

    def upload_experiment(self, workspace_id: str, unique_id: str):
        self.__controller.upload_experiment(workspace_id, unique_id)

    def remove_experiment(self, workspace_id: str, unique_id: str):
        self.__controller.remove_experiment(workspace_id, unique_id)
