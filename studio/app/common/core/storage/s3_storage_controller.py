import os

import boto3

from studio.app.common.core.logger import AppLogger
from studio.app.common.core.storage.remote_storage_controller import (
    BaseRemoteStorageController,
)
from studio.app.common.core.utils.filepath_creater import join_filepath
from studio.app.dir_path import DIRPATH

logger = AppLogger.get_logger()


class S3StorageController(BaseRemoteStorageController):
    """
    S3 Storage Controller
    """

    # TODO: 認証情報の管理方式の組み込み
    # - 現段階では、awscli で事前認証した状態で、利用可能としている

    # TODO: S3 bucket は、最終的にユーザー単位での区画分けとなる想定
    S3_STORAGE_URL = os.environ.get("S3_STORAGE_URL")
    S3_STORAGE_BUCKET = S3_STORAGE_URL.split("//")[-1]

    S3_INPUT_DIR = "input"
    S3_OUTPUT_DIR = "output"

    def __init__(self):
        self.__s3_client = boto3.client("s3")
        self.__s3_resource = boto3.resource("s3")

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
            [__class__.S3_OUTPUT_DIR, workspace_id, unique_id]
        )

        logger.debug(
            "upload data to remote storage (S3). [%s -> %s]",
            experiment_source_path,
            experiment_remote_path,
        )

        target_files = []
        for root, dirs, files in os.walk(experiment_source_path):
            for filename in files:
                # construct paths
                local_abs_path = os.path.join(root, filename)
                local_relative_path = os.path.relpath(
                    local_abs_path, experiment_source_path
                )

                s3_file_path = join_filepath(
                    [
                        __class__.S3_OUTPUT_DIR,
                        workspace_id,
                        unique_id,
                        local_relative_path,
                    ]
                )

                file_size = os.path.getsize(local_abs_path)
                target_files.append([local_relative_path, s3_file_path, file_size])

        # do upload data to remote storage
        target_files_count = len(target_files)
        for index, (local_relative_path, s3_file_path, file_size) in enumerate(
            target_files
        ):
            logger.debug(
                f"upload data to S3 ({index+1}/{target_files_count}) "
                f"{s3_file_path} ({file_size:,} bytes)"
            )

            # do upload experiment files
            self.__s3_client.upload_file(
                local_abs_path, __class__.S3_STORAGE_BUCKET, s3_file_path
            )

    def remove_experiment(self, workspace_id: str, unique_id: str):
        # make paths
        experiment_remote_path = join_filepath(
            [__class__.S3_OUTPUT_DIR, workspace_id, unique_id]
        )

        logger.debug(
            "remove data from remote storage (s3). [%s]",
            experiment_remote_path,
        )

        # do remove data from remote storage
        self.__s3_resource.Bucket(__class__.S3_STORAGE_BUCKET).objects.filter(
            Prefix=experiment_remote_path
        ).delete()
