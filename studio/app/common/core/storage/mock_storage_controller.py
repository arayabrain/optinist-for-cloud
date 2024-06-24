import os
import shutil

from studio.app.common.core.logger import AppLogger
from studio.app.common.core.storage.remote_storage_controller import (
    BaseRemoteStorageController,
)
from studio.app.common.core.utils.filepath_creater import (
    create_directory,
    join_filepath,
)
from studio.app.dir_path import DIRPATH

logger = AppLogger.get_logger()


class MockStorageController(BaseRemoteStorageController):
    """
    Mock Storage Controller (uses of development)
    """

    MOCK_STORAGE_DIR = (
        os.environ.get("MOCK_STORAGE_DIR")
        if "MOCK_STORAGE_DIR" in os.environ
        else "/tmp/studio/mock-storage"
    )
    MOCK_INPUT_DIR = f"{MOCK_STORAGE_DIR}/input"
    MOCK_OUTPUT_DIR = f"{MOCK_STORAGE_DIR}/output"

    def __init__(self):
        # initialization: create directories
        create_directory(__class__.MOCK_INPUT_DIR)
        create_directory(__class__.MOCK_OUTPUT_DIR)

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
            [__class__.MOCK_OUTPUT_DIR, workspace_id, unique_id]
        )

        create_directory(experiment_remote_path, delete_dir=True)

        logger.debug(
            "upload data to remote storage (mock). [%s -> %s]",
            experiment_source_path,
            experiment_remote_path,
        )

        # do copy data to remote storage
        shutil.copytree(
            experiment_source_path, experiment_remote_path, dirs_exist_ok=True
        )

    def remove_experiment(self, workspace_id: str, unique_id: str):
        # make paths
        experiment_remote_path = join_filepath(
            [__class__.MOCK_OUTPUT_DIR, workspace_id, unique_id]
        )

        logger.debug(
            "remove data from remote storage (mock). [%s]",
            experiment_remote_path,
        )

        # do remove data from remote storage
        if os.path.isdir(experiment_remote_path):
            shutil.rmtree(experiment_remote_path)
