import os
import shutil

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

    def make_experiment_local_path(self, workspace_id: str, unique_id: str) -> str:
        experiment_local_path = join_filepath(
            [DIRPATH.OUTPUT_DIR, workspace_id, unique_id]
        )
        return experiment_local_path

    def make_experiment_remote_path(self, workspace_id: str, unique_id: str) -> str:
        experiment_remote_path = join_filepath(
            [__class__.S3_OUTPUT_DIR, workspace_id, unique_id]
        )
        return experiment_remote_path

    def download_experiment_metas(self, workspace_id: str, unique_id: str) -> bool:
        # TODO: Implementation is required
        pass

    def download_experiment(self, workspace_id: str, unique_id: str) -> bool:
        # make paths
        experiment_local_path = self.make_experiment_local_path(workspace_id, unique_id)
        experiment_remote_path = self.make_experiment_remote_path(
            workspace_id, unique_id
        )

        # request s3 list_objects
        s3_list_objects = self.__s3_client.list_objects_v2(
            Bucket=__class__.S3_STORAGE_BUCKET, Prefix=experiment_remote_path
        )

        # check copy source directory
        if not s3_list_objects or s3_list_objects.get("KeyCount", 0) == 0:
            logger.warn(
                "remote data is not exists. [%s][%s]",
                __class__.S3_STORAGE_BUCKET,
                experiment_remote_path,
            )
            return False

        # cleaning data from local path
        if os.path.isdir(experiment_local_path):
            shutil.rmtree(experiment_local_path)

        # do download data from remote storage
        target_files_count = len(s3_list_objects["Contents"])
        for index, s3_object in enumerate(s3_list_objects["Contents"]):
            s3_file_path = s3_object["Key"]
            file_size = s3_object["Size"]

            # skip directory on s3
            if s3_file_path.endswith("/"):
                continue

            # make paths
            local_abs_path = os.path.join(
                os.path.dirname(DIRPATH.OUTPUT_DIR), s3_file_path
            )
            local_abs_dir = os.path.dirname(local_abs_path)

            logger.debug(
                f"download data to S3 ({index+1}/{target_files_count}) "
                f"{s3_file_path} ({file_size:,} bytes)"
            )

            # create local directory before downloading
            if not os.path.exists(local_abs_dir):
                os.makedirs(local_abs_dir)

            # do download experiment files
            self.__s3_client.download_file(
                __class__.S3_STORAGE_BUCKET, s3_file_path, local_abs_path
            )

        # TODO: creating sync-status file.

        return True

    def upload_experiment(self, workspace_id: str, unique_id: str) -> bool:
        # make paths
        experiment_local_path = self.make_experiment_local_path(workspace_id, unique_id)
        experiment_remote_path = self.make_experiment_remote_path(
            workspace_id, unique_id
        )

        logger.debug(
            "upload data to remote storage (S3). [%s -> %s]",
            experiment_local_path,
            experiment_remote_path,
        )

        target_files = []
        for root, dirs, files in os.walk(experiment_local_path):
            for filename in files:
                # construct paths
                local_abs_path = os.path.join(root, filename)
                local_relative_path = os.path.relpath(
                    local_abs_path, experiment_local_path
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
                target_files.append([local_abs_path, s3_file_path, file_size])

        # do upload data to remote storage
        target_files_count = len(target_files)
        for index, (local_abs_path, s3_file_path, file_size) in enumerate(target_files):
            logger.debug(
                f"upload data to S3 ({index+1}/{target_files_count}) "
                f"{s3_file_path} ({file_size:,} bytes)"
            )

            # do upload experiment files
            self.__s3_client.upload_file(
                local_abs_path, __class__.S3_STORAGE_BUCKET, s3_file_path
            )

        return True

    def remove_experiment(self, workspace_id: str, unique_id: str) -> bool:
        # make paths
        experiment_remote_path = self.make_experiment_remote_path(
            workspace_id, unique_id
        )

        logger.debug(
            "remove data from remote storage (s3). [%s]",
            experiment_remote_path,
        )

        # do remove data from remote storage
        self.__s3_resource.Bucket(__class__.S3_STORAGE_BUCKET).objects.filter(
            Prefix=experiment_remote_path
        ).delete()

        return True
