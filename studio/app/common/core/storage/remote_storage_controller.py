import os
import shutil
from abc import ABCMeta, abstractmethod
from enum import Enum

from studio.app.common.core.logger import AppLogger
from studio.app.common.core.utils.filepath_creater import (
    create_directory,
    join_filepath,
)
from studio.app.dir_path import DIRPATH

logger = AppLogger.get_logger()


class RemoteStorageType(Enum):
    OFFILINE = "1"
    S3 = "2"


# TODO: DIRPATH.OUTPUT_DIR & RemoteStorageController 操作 class (検討中)
# class DataStorageController(metaclass=ABCMeta):
#     def read_experiment(self, workspace_id: str, unique_id: str):
#         pass
#
#     def update_experiment(self, workspace_id: str, unique_id: str):
#         pass
#
#     def remove_experiment(self, workspace_id: str, unique_id: str):
#         pass


class BaseRemoteStorageController(metaclass=ABCMeta):
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

        if remote_storage_type == RemoteStorageType.OFFILINE.value:
            self.__controller = OfflineStorageController()
        elif remote_storage_type == RemoteStorageType.S3.value:
            # TODO: Implementation is required
            # self.__controller = OfflineStorageController()
            pass
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


class OfflineStorageController(BaseRemoteStorageController):
    """
    Offlice Storage Controller (uses of development)
    """

    OFFLINE_DATA_DIR = (
        os.environ.get("OFFLINE_STORAGE_DIR")
        if "OFFLINE_STORAGE_DIR" in os.environ
        else "/tmp/studio/offline"
    )
    OFFLINE_INPUT_DIR = f"{OFFLINE_DATA_DIR}/input"
    OFFLINE_OUTPUT_DIR = f"{OFFLINE_DATA_DIR}/output"

    def __init__(self):
        # initialization: create directories
        create_directory(__class__.OFFLINE_INPUT_DIR)
        create_directory(__class__.OFFLINE_OUTPUT_DIR)

    def download_experiment_metas(self, workspace_id: str, unique_id: str):
        # TODO: Implementation is required
        pass

    def download_experiment(self, workspace_id: str, unique_id: str):
        # TODO: Implementation is required
        pass

    def upload_experiment(self, workspace_id: str, unique_id: str):
        # make paths
        experiment_source_path = join_filepath(
            [DIRPATH.OUTPUT_DIR, workspace_id, unique_id]
        )
        experiment_remote_path = join_filepath(
            [__class__.OFFLINE_OUTPUT_DIR, workspace_id, unique_id]
        )

        create_directory(experiment_remote_path, delete_dir=True)

        logger.debug(
            "upload data to remote storage (offline). [%s -> %s]",
            experiment_source_path,
            experiment_remote_path,
        )

        # do copy experiment data
        shutil.copytree(
            experiment_source_path, experiment_remote_path, dirs_exist_ok=True
        )

    def remove_experiment(self, workspace_id: str, unique_id: str):
        # make paths
        experiment_remote_path = join_filepath(
            [__class__.OFFLINE_OUTPUT_DIR, workspace_id, unique_id]
        )

        logger.debug(
            "remove data from remote storage (offline). [%s]",
            experiment_remote_path,
        )

        # do remove experiment data
        if os.path.isdir(experiment_remote_path):
            shutil.rmtree(experiment_remote_path)
