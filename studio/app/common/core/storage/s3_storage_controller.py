import os
import re
import shutil

import boto3

from studio.app.common.core.logger import AppLogger
from studio.app.common.core.storage.remote_storage_controller import (
    BaseRemoteStorageController,
    RemoteSyncAction,
    RemoteSyncStatusFileUtil,
)
from studio.app.common.core.utils.filepath_creater import join_filepath
from studio.app.dir_path import DIRPATH

logger = AppLogger.get_logger()


class S3StorageController(BaseRemoteStorageController):
    """
    S3 Storage Controller
    """

    S3_INPUT_DIR = "input"
    S3_OUTPUT_DIR = "output"

    def __init__(self, bucket_name: str):
        # init s3 bucket attributes
        assert bucket_name, "S3 bucket name is not defined."
        self.__s3_storage_bucket = bucket_name
        self.__s3_storage_url = f"s3://{bucket_name}"
        logger.debug(f"Init S3StorageController: {bucket_name=}")

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

    def download_all_experiments_metas(self) -> bool:
        """
        NOTE:
          - この処理（config yaml files の S3 からのダウンロード）では、
            ダウンロード対象ファイルリストの取得に、python module (boto3) ではなく、
            外部コマンド (aws cli) を利用している
          - aws cli を利用する事由
            1. boto3 では、取得対象のファイルリストの Server(AWS) Side でのfilterをサポートしていない（2024.7時点）
                - 「Prefix配下のファイルリストをすべて取得 → Client Side でのFilter」の操作手順となる
            2. また 1. の操作を行う場合、Pagination の考慮も必要となる
          - 上記のため、ファイルリストの取得には、aws cli を利用する形式としている
            - aws cli (`aws s3 ls`) も、基本的には Server(AWS) Side のファイルリストfilterには非対応だが、
              `aws s3 sync` の利用により、間接的に Server(AWS) Side での filterが利用可能
            - なお aws cli の利用は暫定的な対応であり、最終的には boto3 でfilterを実現できることが望ましい
        """

        # ----------------------------------------
        # make paths
        # ----------------------------------------

        import subprocess
        import tempfile

        target_files = []
        with tempfile.TemporaryDirectory() as tempdir:
            """
            # CLI Command Description
            - Use `aws s3 sync`
                - Specify --dryrun to get file list (no actual sync)
            - search target files
                - Experiment Metadata Files
                    - DIRPATH.EXPERIMENT_YML
                    - DIRPATH.WORKFLOW_YML
            - command result (stdout) format
                > (dryrun) download: s3://{FILE_URL} to {DOWNLOAD_LOCAL_PATH}
                > ... (repeat above)
            """
            aws_s3_sync_command = (
                f"aws s3 sync {self.__s3_storage_url} {tempdir} "
                "--dryrun --exclude '*' "
                f"--include '*/{DIRPATH.EXPERIMENT_YML}' "
                f"--include '*/{DIRPATH.WORKFLOW_YML}' "
            )

            # run aws cli command
            cmd_ret = subprocess.run(
                aws_s3_sync_command,
                shell=True,
                capture_output=True,
                text=True,
                check=True,
                env={
                    "PATH": os.environ.get("PATH"),
                    "AWS_ACCESS_KEY_ID": os.environ.get("AWS_ACCESS_KEY_ID"),
                    "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY"),
                    "AWS_DEFAULT_REGION": os.environ.get("AWS_DEFAULT_REGION"),
                },
            )
            assert (
                cmd_ret.returncode == 0
            ), f"Fail aws_s3_sync_command. {cmd_ret.stderr}"

            # extract target files paths from command's stdout
            target_files_str = re.sub(
                "^.*(s3://[^ ]*) .*$", r"\1", cmd_ret.stdout, flags=(re.MULTILINE)
            ).strip()
            target_files = target_files_str.split("\n")

            logger.debug(
                "aws s3 sync result: [returncode:%d][len:%d]",
                cmd_ret.returncode,
                len(target_files),
            )

        logger.debug(
            "download all medata from remote storage (s3). [%s] [count: %d]",
            self.__s3_storage_bucket,
            len(target_files),
        )

        # ----------------------------------------
        # exec downloading
        # ----------------------------------------

        # do copy data from remote storage
        target_files_count = len(target_files)
        for index, remote_config_yml_abs_path in enumerate(target_files):
            relative_config_yml_path = remote_config_yml_abs_path.replace(
                f"{self.__s3_storage_url}/", ""
            )
            remote_config_yml_path = relative_config_yml_path
            local_config_yml_path = f"{DIRPATH.DATA_DIR}/{relative_config_yml_path}"
            local_config_yml_dir = os.path.dirname(local_config_yml_path)

            if not os.path.isfile(local_config_yml_path):
                logger.debug(
                    f"copy config_yml: {relative_config_yml_path} "
                    f"({index+1}/{target_files_count})"
                )

                os.makedirs(local_config_yml_dir, exist_ok=True)

                # do download config file
                self.__s3_client.download_file(
                    self.__s3_storage_bucket,
                    remote_config_yml_path,
                    local_config_yml_path,
                )

            else:
                logger.debug(
                    f"skip copy config_yml: {relative_config_yml_path} "
                    f"({index+1}/{target_files_count})"
                )
                continue

        return True

    def download_experiment(self, workspace_id: str, unique_id: str) -> bool:
        # make paths
        experiment_local_path = self.make_experiment_local_path(workspace_id, unique_id)
        experiment_remote_path = self.make_experiment_remote_path(
            workspace_id, unique_id
        )

        logger.debug(
            "download data from remote storage (S3). [%s] [%s -> %s]",
            self.__s3_storage_bucket,
            experiment_local_path,
            experiment_remote_path,
        )

        # ----------------------------------------
        # exec downloading
        # ----------------------------------------

        # clear remote_sync_status file.
        RemoteSyncStatusFileUtil.delete_sync_status_file(workspace_id, unique_id)

        # request s3 list_objects
        s3_list_objects = self.__s3_client.list_objects_v2(
            Bucket=self.__s3_storage_bucket, Prefix=experiment_remote_path
        )

        # check copy source directory
        if not s3_list_objects or s3_list_objects.get("KeyCount", 0) == 0:
            logger.warn(
                "remote data is not exists. [%s] [%s]",
                self.__s3_storage_bucket,
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
                f"download data from S3 [{self.__s3_storage_bucket}] "
                f"({index+1}/{target_files_count}) "
                f"{s3_file_path} ({file_size:,} bytes)"
            )

            # create local directory before downloading
            if not os.path.exists(local_abs_dir):
                os.makedirs(local_abs_dir)

            # do download experiment files
            self.__s3_client.download_file(
                self.__s3_storage_bucket, s3_file_path, local_abs_path
            )

        # creating remote_sync_status file.
        RemoteSyncStatusFileUtil.create_sync_status_file_for_success(
            self.__s3_storage_bucket, workspace_id, unique_id, RemoteSyncAction.DOWNLOAD
        )

        return True

    def upload_experiment(
        self, workspace_id: str, unique_id: str, target_files: list = None
    ) -> bool:
        # make paths
        experiment_local_path = self.make_experiment_local_path(workspace_id, unique_id)
        experiment_remote_path = self.make_experiment_remote_path(
            workspace_id, unique_id
        )

        logger.debug(
            "upload data to remote storage (S3). [%s] [%s -> %s]",
            self.__s3_storage_bucket,
            experiment_local_path,
            experiment_remote_path,
        )

        # ----------------------------------------
        # exec uploading
        # ----------------------------------------

        # clear remote_sync_status file.
        RemoteSyncStatusFileUtil.delete_sync_status_file(workspace_id, unique_id)

        # make target files path list
        # 1) Obtain target file path in absolute path format.
        target_abs_paths = []
        if target_files:  # Target specified files.
            target_abs_paths = [f"{experiment_local_path}/{f}" for f in target_files]
        else:  # Target all files.
            for root, dirs, files in os.walk(experiment_local_path):
                for filename in files:
                    local_abs_path = os.path.join(root, filename)
                    target_abs_paths.append(local_abs_path)

        # make target files path list
        # 2) Obtain target file path in transfer format to S3.
        adjusted_target_files = []
        for local_abs_path in target_abs_paths:
            local_relative_path = os.path.relpath(local_abs_path, experiment_local_path)

            s3_file_path = join_filepath(
                [
                    __class__.S3_OUTPUT_DIR,
                    workspace_id,
                    unique_id,
                    local_relative_path,
                ]
            )

            file_size = os.path.getsize(local_abs_path)
            adjusted_target_files.append([local_abs_path, s3_file_path, file_size])

        # do upload data to remote storage
        target_files_count = len(adjusted_target_files)
        for index, (local_abs_path, s3_file_path, file_size) in enumerate(
            adjusted_target_files
        ):
            logger.debug(
                f"upload data to S3 [{self.__s3_storage_bucket}] "
                f"({index+1}/{target_files_count}) "
                f"{s3_file_path} ({file_size:,} bytes)"
            )

            # do upload experiment files
            self.__s3_client.upload_file(
                local_abs_path, self.__s3_storage_bucket, s3_file_path
            )

        # creating remote_sync_status file.
        RemoteSyncStatusFileUtil.create_sync_status_file_for_success(
            self.__s3_storage_bucket, workspace_id, unique_id, RemoteSyncAction.UPLOAD
        )

        return True

    def delete_experiment(self, workspace_id: str, unique_id: str) -> bool:
        # make paths
        experiment_remote_path = self.make_experiment_remote_path(
            workspace_id, unique_id
        )

        logger.debug(
            "delete data from remote storage (s3). [%s] [%s]",
            self.__s3_storage_bucket,
            experiment_remote_path,
        )

        # ----------------------------------------
        # exec deleting
        # ----------------------------------------

        # do delete data from remote storage
        self.__s3_resource.Bucket(self.__s3_storage_bucket).objects.filter(
            Prefix=experiment_remote_path
        ).delete()

        return True
