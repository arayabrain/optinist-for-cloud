import asyncio
import os
import re
import time
import uuid
from subprocess import CalledProcessError
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

import aioboto3
import boto3
from sqlmodel import select

from studio.app.common import models as common_model

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

    from studio.app.const import ThumbnailType

from botocore.exceptions import ClientError

from studio.app.common.core.logger import AppLogger
from studio.app.common.core.storage.file_filter import FileSyncFilter
from studio.app.common.core.storage.remote_storage_controller import (
    BaseRemoteStorageController,
    InputFileLock,
    RemoteExperimentNotFoundError,
    RemoteExperimentSyncMode,
    RemoteStorageBucketNotFoundError,
    RemoteSyncLockFileUtil,
    RemoteSyncStatusFileUtil,
    StorageDirectoryType,
)
from studio.app.common.core.utils.filepath_creater import join_filepath
from studio.app.common.db.database import session_scope
from studio.app.const import ThumbnailConst
from studio.app.dir_path import DIRPATH

# NOTE: cloud_utils imports are kept inside functions to avoid circular imports:
# cloud_utils.py → s3_storage_monitor.py → s3_storage_controller.py → cloud_utils.py

logger = AppLogger.get_logger()


def is_no_such_bucket_error(e: Exception) -> bool:
    """Check if exception is a NoSuchBucket error from S3."""
    if isinstance(e, ClientError):
        return e.response.get("Error", {}).get("Code") == "NoSuchBucket"
    return "NoSuchBucket" in str(type(e).__name__)


class S3StorageController(BaseRemoteStorageController):
    """
    S3 Storage Controller
    """

    S3_BASE_PATH = os.environ.get("S3_BASE_PATH", "/app/studio_data").lstrip("/")
    S3_INPUT_DIR = "input"
    S3_OUTPUT_DIR = "output"
    VALIDATION_MAX_OBJECTS = 500

    def __init__(self, bucket_name: str):
        # init s3 bucket attributes
        assert bucket_name, "S3 bucket name is not defined."
        self.__s3_storage_bucket = bucket_name
        self.__s3_storage_url = f"s3://{bucket_name}"
        logger.debug(f"Init S3StorageController: {bucket_name=}")

    def __get_s3_client(self):
        region = os.environ.get("AWS_DEFAULT_REGION")
        return aioboto3.Session().client("s3", region_name=region)

    def __get_s3_resource(self):
        region = os.environ.get("AWS_DEFAULT_REGION")
        return aioboto3.Session().resource("s3", region_name=region)

    def _make_input_data_local_path(self, workspace_id: str, filename: str) -> str:
        input_data_local_path = join_filepath(
            [DIRPATH.INPUT_DIR, workspace_id, filename]
        )
        return input_data_local_path

    def _make_input_data_remote_path(self, workspace_id: str, filename: str) -> str:
        # Include app/studio_data path to match Snakemake's expected S3 structure
        # Snakemake expects: /app/studio_data/input/{workspace_id}/{filename}
        # S3 mapping: s3://bucket/app/studio_data/input/{workspace_id}/{filename}
        input_data_remote_path = join_filepath(
            [__class__.S3_BASE_PATH, __class__.S3_INPUT_DIR, workspace_id, filename]
        )
        return input_data_remote_path

    def _make_experiment_local_path(self, workspace_id: str, unique_id: str) -> str:
        experiment_local_path = join_filepath(
            [DIRPATH.OUTPUT_DIR, workspace_id, unique_id]
        )
        return experiment_local_path

    def _make_experiment_remote_path(self, workspace_id: str, unique_id: str) -> str:
        # Include app/studio_data path to match Snakemake's expected S3 structure
        # Snakemake expects: /app/studio_data/output/{workspace_id}/{unique_id}
        # S3 mapping: s3://bucket/app/studio_data/output/{workspace_id}/{unique_id}
        experiment_remote_path = join_filepath(
            [__class__.S3_BASE_PATH, __class__.S3_OUTPUT_DIR, workspace_id, unique_id]
        )
        logger.info(
            f"S3 experiment path: {experiment_remote_path} "
            f"(workspace_id='{workspace_id}', unique_id='{unique_id}')"
        )
        return experiment_remote_path

    @staticmethod
    def make_s3_output_prefix(workspace_id: str = None, unique_id: str = None) -> str:
        """
        Build S3 prefix path for experiment output data.

        Args:
            workspace_id: Optional workspace identifier
            unique_id: Optional experiment unique identifier

        Returns:
            S3 prefix like "app/studio_data/output/{workspace_id}/{unique_id}/"
        """
        parts = [S3StorageController.S3_BASE_PATH, S3StorageController.S3_OUTPUT_DIR]
        if workspace_id:
            parts.append(workspace_id)
        if unique_id:
            parts.append(unique_id)
        return "/".join(parts) + "/"

    @staticmethod
    def make_s3_input_prefix(workspace_id: str = None) -> str:
        """
        Build S3 prefix path for input data.

        Args:
            workspace_id: Optional workspace identifier

        Returns:
            S3 prefix like "app/studio_data/input/{workspace_id}/"
        """
        parts = [S3StorageController.S3_BASE_PATH, S3StorageController.S3_INPUT_DIR]
        if workspace_id:
            parts.append(workspace_id)
        return "/".join(parts) + "/"

    @property
    def bucket_name(self) -> str:
        return self.__s3_storage_bucket

    # ----------------------------------------
    # Common helper methods
    # ----------------------------------------

    async def _list_s3_objects_paginated(
        self,
        s3_client,
        prefix: str,
        max_files: int = None,
    ) -> List[Dict]:
        """List S3 objects under a prefix with pagination.

        Args:
            s3_client: Active S3 client
            prefix: S3 prefix to list
            max_files: If set, raises RuntimeError when object count exceeds this limit

        Returns:
            List of S3 object dicts containing 'Key', 'Size', etc.
        """
        all_objects = []
        continuation_token = None

        while True:
            list_params = {
                "Bucket": self.bucket_name,
                "Prefix": prefix,
            }
            if continuation_token:
                list_params["ContinuationToken"] = continuation_token

            response = await s3_client.list_objects_v2(**list_params)

            if not response or response.get("KeyCount", 0) == 0:
                break

            all_objects.extend(response.get("Contents", []))

            if max_files and len(all_objects) > max_files:
                raise RuntimeError(
                    f"S3 object count exceeds the limit of {max_files}. "
                    f"prefix={prefix}, found={len(all_objects)}+ objects"
                )

            if response.get("IsTruncated"):
                continuation_token = response.get("NextContinuationToken")
            else:
                break

        return all_objects

    async def _download_s3_with_update_check(
        self,
        s3_client,
        s3_file_path: str,
        local_file_path: str,
        file_size: int,
        progress_info: str = "",
    ) -> bool:
        """Download a single file from S3, skipping if already exists with correct size.

        Args:
            s3_client: Active S3 client
            s3_file_path: S3 object key
            local_file_path: Local destination path
            file_size: Expected file size in bytes
            progress_info: Optional progress string for logging (e.g. "(3/10)")

        Returns:
            True if file was downloaded, False if skipped
        """
        # NFS keeps a negative-dentry cache that stat() doesn't bypass — a peer
        # that just released the InputFileLock may have a fresh file on the
        # server we still see as missing. READDIR via listdir invalidates it.
        try:
            os.listdir(os.path.dirname(local_file_path))
        except OSError:
            pass

        if os.path.isfile(local_file_path):
            local_size = os.path.getsize(local_file_path)
            if local_size == file_size:
                logger.debug(
                    f"Skip download (already exists): {s3_file_path} "
                    f"({file_size:,} bytes)"
                )
                return False

        progress_str = f"{progress_info} " if progress_info else ""
        logger.debug(
            f"Download data from S3 [{self.bucket_name}] "
            f"{progress_str}{s3_file_path} ({file_size:,} bytes)"
        )

        os.makedirs(os.path.dirname(local_file_path), exist_ok=True)

        # Download to a sibling tmp path then atomic-rename, so a concurrent
        # reader on shared storage (e.g. EFS) never sees a partial file at
        # local_file_path. rename(2) is atomic within a single filesystem.
        tmp_path = f"{local_file_path}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        try:
            await s3_client.download_file(self.bucket_name, s3_file_path, tmp_path)
            os.replace(tmp_path, local_file_path)
        except BaseException:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise

        logger.debug(
            f"Finish download data from S3 [{self.bucket_name}] " f"{s3_file_path}"
        )

        return True

    @staticmethod
    async def _delete_s3_objects_batched(bucket, keys_to_delete: list) -> None:
        """Delete S3 objects in batches of 1000 (S3 API limit).

        Args:
            bucket: S3 Bucket resource
            keys_to_delete: List of dicts with 'Key' (e.g. [{"Key": "path/to/file"}])
        """
        if not keys_to_delete:
            return
        batch_size = 1000
        for i in range(0, len(keys_to_delete), batch_size):
            batch = keys_to_delete[i : i + batch_size]
            await bucket.delete_objects(Delete={"Objects": batch})

    # ----------------------------------------
    # Bucket operations
    # ----------------------------------------

    async def create_bucket(self) -> bool:
        """
        Note:
        - About public access settings for bucket
            - Public access permission by ACL is not allowed after 2023/4.
                - https://aws.amazon.com/jp/about-aws/whats-new/2022/12/
                    amazon-s3-automatically-enable-block-public-access-disable-
                    access-control-lists-buckets-april-2023/
            - The above requires that public access be configured via bucket policy.
        """

        create_config = {
            "LocationConstraint": os.environ.get("AWS_DEFAULT_REGION"),
        }

        async with self.__get_s3_client() as __s3_client:
            await __s3_client.create_bucket(
                Bucket=self.bucket_name,
                CreateBucketConfiguration=create_config,
            )

        logger.info(f"S3 bucket was successfully created. [{self.bucket_name}]")

        return True

    async def delete_bucket(self, force_delete=False) -> str:
        async with self.__get_s3_resource() as __s3_resource:
            bucket = await __s3_resource.Bucket(self.bucket_name)

            if force_delete:
                await bucket.objects.all().delete()

            await bucket.delete()

        logger.info(f"S3 bucket was successfully deleted. [{self.bucket_name}]")

        return True

    async def download_input_data(self, workspace_id: str, filename: str) -> bool:
        # make paths
        input_data_local_path = self._make_input_data_local_path(workspace_id, filename)
        input_data_remote_path = self._make_input_data_remote_path(
            workspace_id, filename
        )

        logger.info(
            "Download input data from remote storage (S3). [%s] [%s -> %s]",
            self.bucket_name,
            input_data_remote_path,
            input_data_local_path,
        )

        MAX_DOWNLOAD_FILES = 1000

        # Serialize same-(workspace, file) downloads across EFS-shared processes.
        async with InputFileLock.acquire(workspace_id, filename):
            async with self.__get_s3_client() as __s3_client:
                all_s3_objects = await self._list_s3_objects_paginated(
                    __s3_client, input_data_remote_path, max_files=MAX_DOWNLOAD_FILES
                )

                if not all_s3_objects:
                    logger.warning(
                        "Remote data does not exist. [%s] [%s]",
                        self.bucket_name,
                        input_data_remote_path,
                    )
                    return False

                # Sort by directory depth (shallower files first)
                all_s3_objects.sort(key=lambda obj: obj["Key"].count("/"))

                target_files_count = len(all_s3_objects)
                s3_prefix = __class__.make_s3_input_prefix()

                for index, s3_object in enumerate(all_s3_objects):
                    s3_file_path = s3_object["Key"]
                    file_size = s3_object["Size"]

                    # Compute local path from S3 path
                    # s3_file_path: app/studio_data/input/{workspace_id}/{relative_path}
                    # local path:   {INPUT_DIR}/{workspace_id}/{relative_path}
                    relative_path = s3_file_path.replace(s3_prefix, "")
                    local_file_path = os.path.join(DIRPATH.INPUT_DIR, relative_path)

                    await self._download_s3_with_update_check(
                        __s3_client,
                        s3_file_path,
                        local_file_path,
                        file_size,
                        progress_info=f"({index+1}/{target_files_count})",
                    )

        return True

    async def upload_input_data(self, workspace_id: str, filename: str) -> bool:
        # make paths
        input_data_local_path = self._make_input_data_local_path(workspace_id, filename)
        input_data_remote_path = self._make_input_data_remote_path(
            workspace_id, filename
        )

        logger.info(
            "Upload data to remote storage (S3). [%s] [%s -> %s]",
            self.bucket_name,
            input_data_local_path,
            input_data_remote_path,
        )

        # ----------------------------------------
        # exec uploading
        # ----------------------------------------

        file_size = os.path.getsize(input_data_local_path)

        logger.info(
            f"Upload data to S3 [{self.bucket_name}] "
            f"{input_data_remote_path} ({file_size:,} bytes)"
        )

        try:
            # Use synchronous boto3 to avoid aioboto3 asyncio compatibility issues
            # Run in thread pool to maintain async interface
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: boto3.client("s3").upload_file(
                    input_data_local_path, self.bucket_name, input_data_remote_path
                ),
            )
        except Exception as e:
            logger.error(f"Failed to upload input data: {e}")
            return False

        logger.info(
            f"Finish upload data from S3 [{self.bucket_name}] "
            f"{input_data_remote_path}"
        )

        return True

    async def list_input_data_objects(self, workspace_id: str) -> List[Dict]:
        """List all input data objects in S3 for a workspace.

        Uses pagination to handle workspaces with >1000 files.
        Returns empty list if bucket does not exist.
        """
        prefix = self.make_s3_input_prefix(workspace_id)
        objects = []

        try:
            async with self.__get_s3_client() as s3_client:
                all_s3_objects = await self._list_s3_objects_paginated(
                    s3_client, prefix
                )

                for obj in all_s3_objects:
                    key = obj["Key"]
                    filename = key.replace(prefix, "")
                    if filename and not filename.endswith("/"):
                        objects.append(
                            {
                                "filename": filename,
                                "size": obj["Size"],
                                "last_modified": obj["LastModified"].isoformat(),
                            }
                        )
        except Exception as e:
            if is_no_such_bucket_error(e):
                logger.warning(f"Bucket does not exist: {self.bucket_name}")
                return []
            raise

        return objects

    async def delete_input_data(self, workspace_id: str, filename: str) -> bool:
        # make paths
        input_data_remote_path = self._make_input_data_remote_path(
            workspace_id, filename
        )

        logger.info(
            "Delete input data from remote storage (S3). [%s]",
            input_data_remote_path,
        )

        async with self.__get_s3_resource() as __s3_resource:
            bucket = await __s3_resource.Bucket(self.bucket_name)
            objects_to_delete = bucket.objects.filter(Prefix=input_data_remote_path)
            keys_to_delete = [{"Key": obj.key} async for obj in objects_to_delete]
            await self._delete_s3_objects_batched(bucket, keys_to_delete)

        return True

    async def download_all_experiments_metas(self, workspace_ids: list = None) -> bool:
        # Whether to use AWS CLI to download user metadata
        USE_AWS_CLI_FOR_DOWNLOADING = (
            False  # Currently fixed as False (aws cli is not used)
        )

        if USE_AWS_CLI_FOR_DOWNLOADING:
            self.__download_all_experiments_metas_via_aws_cli()
        else:
            await self.__download_all_experiments_metas_via_boto3(workspace_ids)

    async def __download_all_experiments_metas_via_boto3(
        self, workspace_ids: list = None
    ) -> bool:
        """
        Download experiments metadata (yaml files) from S3

        # Specifications
        - Scan the directory structure on S3 below and download only the metadata files.
          - Structure of data storage on S3
            - bucket1
              - outputs/
                - workspace1
                  - experiment1
                    - experiment.yaml
                    - workflow.yaml
                    - ...
                  - experiment2
                  - ...
                - workspace2
                - ...
            - bucket2
            - ...
        """

        # Search workspaces directories listing on S3
        try:
            async with self.__get_s3_client() as __s3_client:
                workspaces_response = await __s3_client.list_objects_v2(
                    Bucket=self.bucket_name,
                    Prefix=__class__.make_s3_output_prefix(),
                    Delimiter="/",
                )
        except Exception as e:
            if is_no_such_bucket_error(e):
                logger.warning(f"Bucket does not exist: {self.bucket_name}")
                raise RemoteStorageBucketNotFoundError(self.bucket_name) from e
            logger.error(
                "S3 list_objects_v2 failed: bucket=%s, prefix=%s, "
                "region=%s, error=%s",
                self.bucket_name,
                __class__.make_s3_output_prefix(),
                os.environ.get("AWS_DEFAULT_REGION", "(not set)"),
                e,
            )
            raise

        if "CommonPrefixes" not in workspaces_response:
            logger.warning(
                "No workspaces dirs found in S3 "
                f"[{self.bucket_name}][{__class__.S3_OUTPUT_DIR}]"
            )
            return False

        # Extract workspace directory listing
        all_workspaces_dirs = [
            v["Prefix"] for v in workspaces_response["CommonPrefixes"]
        ]

        # filter target workspaces_dirs
        if workspace_ids:
            re_ids = "|".join(str(wid) for wid in workspace_ids)
            re_ids = f"({re_ids})"
            workspaces_dirs = [
                w for w in all_workspaces_dirs if re.search(f"/{re_ids}/$", w)
            ]
        else:
            workspaces_dirs = all_workspaces_dirs

        logger.info(
            "Download all metadata from remote storage (S3). [%s] workspaces: %s",
            self.bucket_name,
            workspaces_dirs,
        )

        metadata_filenames = [
            DIRPATH.EXPERIMENT_YML,
            DIRPATH.SNAKEMAKE_CONFIG_YML,
            DIRPATH.WORKFLOW_YML,
        ]

        # Scan workspaces directories
        async with self.__get_s3_client() as __s3_client:
            for workspace_dir in workspaces_dirs:
                # Search experiments directories listing on S3
                experiments_response = await __s3_client.list_objects_v2(
                    Bucket=self.bucket_name, Prefix=workspace_dir, Delimiter="/"
                )

                if "CommonPrefixes" not in experiments_response:
                    "No experiments dirs found in S3"
                    f"[{self.bucket_name}][{workspace_dir}]"
                    continue

                # Extract experiments directory listing
                experiments_dirs = [
                    v["Prefix"] for v in experiments_response["CommonPrefixes"]
                ]

                # Scan experiments directories
                for experiment_dir in experiments_dirs:
                    # Download metadata files
                    for metadata_filename in metadata_filenames:
                        file_remote_path = experiment_dir + metadata_filename
                        flie_local_path = os.path.join(
                            DIRPATH.DATA_DIR, experiment_dir, metadata_filename
                        )

                        # Fix duplicate path issue
                        dup_path = (
                            f"/{__class__.S3_BASE_PATH}/{__class__.S3_BASE_PATH}/"
                        )
                        if dup_path in flie_local_path:
                            flie_local_path = flie_local_path.replace(
                                dup_path, f"/{__class__.S3_BASE_PATH}/"
                            )

                        if not os.path.isfile(flie_local_path):
                            try:
                                # create local directory
                                os.makedirs(
                                    os.path.dirname(flie_local_path), exist_ok=True
                                )

                                # Check for the existence of the remote file
                                #   before executing download_file
                                #   (to avoid creating an empty file locally)
                                await __s3_client.head_object(
                                    Bucket=self.bucket_name, Key=file_remote_path
                                )

                                # download file
                                await __s3_client.download_file(
                                    self.bucket_name,
                                    file_remote_path,
                                    flie_local_path,
                                )
                            except Exception as e:
                                logger.warning(
                                    f"Failed to download [{self.bucket_name}]"
                                    f"[{file_remote_path}]: {e}"
                                )
                        else:
                            logger.debug(f"Skip download: {file_remote_path}")
                            continue

        return True

    async def download_experiment_meta(self, workspace_id: str, unique_id: str) -> bool:
        """
        Download metadata files (yaml) for a single experiment from remote
        storage. More efficient than download_all_experiments_metas when only
        one experiment is needed.

        Downloads: experiment.yaml, workflow.yaml, snakemake_config.yaml
        """
        metadata_filenames = [
            DIRPATH.EXPERIMENT_YML,
            DIRPATH.SNAKEMAKE_CONFIG_YML,
            DIRPATH.WORKFLOW_YML,
        ]

        # Construct the S3 path for this specific experiment
        experiment_prefix = __class__.make_s3_output_prefix(workspace_id, unique_id)

        logger.debug(
            f"Downloading experiment metadata from S3: [{self.bucket_name}]"
            f"[{workspace_id}/{unique_id}]"
        )

        downloaded_count = 0
        async with self.__get_s3_client() as __s3_client:
            for metadata_filename in metadata_filenames:
                file_remote_path = experiment_prefix + metadata_filename
                file_local_path = os.path.join(
                    DIRPATH.DATA_DIR,
                    __class__.S3_OUTPUT_DIR,
                    workspace_id,
                    unique_id,
                    metadata_filename,
                )

                # Skip if file already exists locally
                if os.path.isfile(file_local_path):
                    logger.debug(f"Skip download (exists): {file_remote_path}")
                    continue

                try:
                    # Create local directory if needed
                    os.makedirs(os.path.dirname(file_local_path), exist_ok=True)

                    # Check for the existence of the remote file
                    #   before executing download_file
                    #   (to avoid creating an empty file locally)
                    await __s3_client.head_object(
                        Bucket=self.bucket_name, Key=file_remote_path
                    )

                    # Download file from S3
                    await __s3_client.download_file(
                        self.bucket_name,
                        file_remote_path,
                        file_local_path,
                    )
                    downloaded_count += 1
                    logger.debug(f"Downloaded: {file_remote_path}")
                except Exception as e:
                    # File may not exist in S3 - this is OK for optional files
                    logger.warning(
                        f"Could not download [{self.bucket_name}]"
                        f"[{file_remote_path}]: {e}"
                    )

        logger.debug(
            f"Downloaded {downloaded_count} metadata files for "
            f"[{workspace_id}/{unique_id}]"
        )
        return True

    async def experiment_prefix_exists(
        self, workspace_id: str, unique_id: str
    ) -> Tuple[Optional[bool], Optional[str]]:
        """
        Check whether any object exists under an experiment's S3 output prefix.

        Returns a tri-state so callers can distinguish confirmed absence from
        an unverifiable check:
            - True: at least one object present (error is None)
            - False: prefix confirmed empty
            - None: could not check (transient/S3 error)
        """
        prefix = self.make_s3_output_prefix(workspace_id, unique_id)
        try:
            async with self.__get_s3_client() as client:
                result = await client.list_objects_v2(
                    Bucket=self.bucket_name, Prefix=prefix, MaxKeys=5
                )
                if result.get("KeyCount", 0) == 0:
                    return False, f"No data found in S3 for {workspace_id}/{unique_id}"
        except Exception as e:
            logger.error(
                f"S3 existence check error for {workspace_id}/{unique_id}: {e}"
            )
            return None, f"Could not verify S3 data: {str(e)}"

        return True, None

    async def validate_experiment_in_s3(
        self, workspace_id: str, unique_id: str
    ) -> dict:
        """
        Validate that an experiment exists in S3 without downloading.

        Checks for required files (experiment.yaml, workflow.yaml) via
        ListObjectsV2. Used by the background sync job to update DB
        status without wasting bandwidth on downloads.

        Returns:
            dict with keys:
                valid (bool): True if required files exist
                has_thumbnails (bool): True if thumbnail PNGs found
                error (str|None): Error message if validation failed
        """
        prefix = self.make_s3_output_prefix(workspace_id, unique_id)
        required = {DIRPATH.EXPERIMENT_YML, DIRPATH.WORKFLOW_YML}

        try:
            found_files = set()
            has_thumbnails = False

            async with self.__get_s3_client() as s3_client:
                continuation_token = None
                total_listed = 0

                while True:
                    params = {
                        "Bucket": self.bucket_name,
                        "Prefix": prefix,
                    }
                    if continuation_token:
                        params["ContinuationToken"] = continuation_token

                    resp = await s3_client.list_objects_v2(**params)

                    if not resp or resp.get("KeyCount", 0) == 0:
                        break

                    for obj in resp.get("Contents", []):
                        key = obj["Key"]
                        filename = key.rsplit("/", 1)[-1]
                        file_size = obj.get("Size", 0)

                        if filename in required:
                            if file_size > 0:
                                found_files.add(filename)
                            else:
                                logger.warning(
                                    f"Required file {filename}"
                                    f" is 0 bytes in S3: {key}"
                                )

                        if filename.endswith("_thumb.png"):
                            has_thumbnails = True

                    total_listed += resp.get("KeyCount", 0)
                    if total_listed >= self.VALIDATION_MAX_OBJECTS:
                        break

                    if resp.get("IsTruncated"):
                        continuation_token = resp.get("NextContinuationToken")
                    else:
                        break

            if not found_files:
                return {
                    "valid": False,
                    "has_thumbnails": False,
                    "error": "No objects found in S3",
                }

            missing = required - found_files
            if missing:
                return {
                    "valid": False,
                    "has_thumbnails": has_thumbnails,
                    "error": f"Missing required files: {sorted(missing)}",
                }

            return {
                "valid": True,
                "has_thumbnails": has_thumbnails,
                "error": None,
            }

        except Exception as e:
            return {
                "valid": False,
                "has_thumbnails": False,
                "error": f"S3 validation error: {e}",
            }

    async def __download_all_experiments_metas_via_aws_cli(self) -> bool:
        """
        NOTE:
          - この処理（config yaml files の S3 からのダウンロード）では、
            ダウンロード対象ファイルリストの取得に、python module (boto3) ではなく、
            外部コマンド (aws cli) を利用している
          - aws cli を利用する事由
            1. boto3 では、取得対象のファイルリストの Server(AWS) Side でのfilterをサポートしていない（2024.7時点）
                - 「Prefix配下のファイルリストをすべて取得 → Client Side でのFilter」の操作手順となる
            2. また 1. の操作を行う場合、Pagination の考慮も必要となる
          - 上記のため、ファイルリストの取得には、aws cli (`aws s3 sync`) を利用する形式を、この関数では用意している
            - `aws s3 sync` の利用により、特定のファイルのみのfilterが、簡潔に利用可能となる
            - しかし実際には、`aws s3 sync` も内部で Client Side でのFilter を行っている様であるため、性能面の課題は残る
            - 最終的には、 S3 APIでsyncオプションが用意され、boto3 でfilterを実現可能となることが望ましい
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
                f"--include '*/{DIRPATH.SNAKEMAKE_CONFIG_YML}' "
                f"--include '*/{DIRPATH.WORKFLOW_YML}' "
            )

            # run aws cli command
            try:
                # inherit the process env so the ECS Task Role container credentials
                # reach the aws CLI
                cmd_ret = subprocess.run(
                    aws_s3_sync_command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    check=True,
                )

            except CalledProcessError as e:
                logger.error(e)
                logger.error(e.stderr)
                raise e

            # extract target files paths from command's stdout
            if len(str(cmd_ret.stdout).strip()) > 0:
                target_files_str = re.sub(
                    "^.*(s3://[^ ]*) .*$", r"\1", cmd_ret.stdout, flags=(re.MULTILINE)
                ).strip()
                target_files = target_files_str.split("\n")
            else:
                target_files = []

        # ----------------------------------------
        # exec downloading
        # ----------------------------------------

        # do copy data from remote storage
        async with self.__get_s3_client() as __s3_client:
            target_files_count = len(target_files)
            for index, remote_config_yml_abs_path in enumerate(target_files):
                relative_config_yml_path = remote_config_yml_abs_path.replace(
                    f"{self.__s3_storage_url}/", ""
                )
                remote_config_yml_path = relative_config_yml_path
                local_config_yml_path = f"{DIRPATH.DATA_DIR}/{relative_config_yml_path}"
                local_config_yml_dir = os.path.dirname(local_config_yml_path)

                if not os.path.isfile(local_config_yml_path):
                    os.makedirs(local_config_yml_dir, exist_ok=True)

                    # do download config file
                    await __s3_client.download_file(
                        self.bucket_name,
                        remote_config_yml_path,
                        local_config_yml_path,
                    )

                else:
                    logger.debug(
                        f"Skip copy config_yml: {relative_config_yml_path} "
                        f"({index+1}/{target_files_count})"
                    )
                    continue

        return True

    async def download_experiment(
        self,
        workspace_id: str,
        unique_id: str,
        sync_mode: RemoteExperimentSyncMode = RemoteExperimentSyncMode.ALL,
    ) -> bool:
        """
        Download experiment from S3 to local storage.

        Args:
            workspace_id: Workspace identifier
            unique_id: Unique experiment identifier
            sync_mode: RemoteExperimentSyncMode enum value
                - ALL: sync everything (default)
                - ESSENTIAL_ONLY: skip large files, sync yaml/json (for dataview)
                - VISUALIZATION: sync only json and tiff (for viewing results)

        Returns:
            True if download successful, False otherwise
        """
        # make paths
        experiment_local_path = self._make_experiment_local_path(
            workspace_id, unique_id
        )
        experiment_remote_path = self._make_experiment_remote_path(
            workspace_id, unique_id
        )
        logger.info(
            "Download data from remote storage (S3). [%s] [%s -> %s] sync_mode=%s",
            self.bucket_name,
            experiment_local_path,
            experiment_remote_path,
            sync_mode,
        )

        file_filter = FileSyncFilter()
        MAX_DOWNLOAD_EXPERIMENT_FILES = 5000

        async with self.__get_s3_client() as __s3_client:
            all_s3_objects = await self._list_s3_objects_paginated(
                __s3_client,
                experiment_remote_path,
                max_files=MAX_DOWNLOAD_EXPERIMENT_FILES,
            )

            if not all_s3_objects:
                logger.warning(
                    "Remote data does not exist. [%s] [%s]",
                    self.bucket_name,
                    experiment_remote_path,
                )
                raise RemoteExperimentNotFoundError(workspace_id, unique_id)

            # Sort by directory depth (shallower files first)
            all_s3_objects.sort(key=lambda obj: obj["Key"].count("/"))

            logger.debug(
                f"Listed {len(all_s3_objects)} objects from S3 "
                f"[{self.bucket_name}] [{experiment_remote_path}]"
            )

            # cleaning data from local path (only for full sync, not partial syncs)
            # Partial syncs (visualization, essential_only) should preserve existing
            # files to avoid redundant downloads
            if sync_mode == RemoteExperimentSyncMode.ALL and os.path.isdir(
                experiment_local_path
            ):
                await self._clear_local_experiment_data(experiment_local_path)

            target_files_count = len(all_s3_objects)

            # Coordination files that should not be downloaded from S3
            coordination_files = {
                RemoteSyncLockFileUtil.REMOTE_SYNC_LOCK_FILE,
                RemoteSyncStatusFileUtil.REMOTE_SYNC_STATUS_FILE,
            }

            for index, s3_object in enumerate(all_s3_objects):
                s3_file_path = s3_object["Key"]
                file_size = s3_object["Size"]

                # skip directory on s3
                if s3_file_path.endswith("/"):
                    continue

                # skip coordination files - they are local-only
                filename = os.path.basename(s3_file_path)
                if filename in coordination_files:
                    logger.debug(
                        f"Skipping coordination file from S3 download: {filename}"
                    )
                    continue

                # Apply file filtering for selective sync
                should_sync, reason = file_filter.should_sync_file(
                    s3_file_path, sync_mode
                )
                if not should_sync:
                    logger.debug(
                        f"Skipping {s3_file_path}: {reason} ({file_size:,} bytes)"
                    )
                    continue

                # make paths
                local_abs_path = os.path.join(
                    os.path.dirname(DIRPATH.OUTPUT_DIR), s3_file_path.lstrip("/")
                )

                # Fix duplicate path issue
                dup_path = f"/{__class__.S3_BASE_PATH}/{__class__.S3_BASE_PATH}/"
                if dup_path in local_abs_path:
                    local_abs_path = local_abs_path.replace(
                        dup_path, f"/{__class__.S3_BASE_PATH}/"
                    )

                await self._download_s3_with_update_check(
                    __s3_client,
                    s3_file_path,
                    local_abs_path,
                    file_size,
                    progress_info=f"({index+1}/{target_files_count})",
                )

        return True

    async def upload_experiment(
        self, workspace_id: str, unique_id: str, target_files: list = None
    ) -> bool:
        # make paths
        experiment_local_path = self._make_experiment_local_path(
            workspace_id, unique_id
        )
        experiment_remote_path = self._make_experiment_remote_path(
            workspace_id, unique_id
        )
        logger.info(
            "Upload data to remote storage (S3). [%s] [%s -> %s]",
            self.bucket_name,
            experiment_local_path,
            experiment_remote_path,
        )

        # ----------------------------------------
        # exec uploading
        # ----------------------------------------

        # make target files path list
        # 1) Obtain target file path in absolute path format.
        target_abs_paths = []
        if target_files:  # Target specified files.
            target_abs_paths = [f"{experiment_local_path}/{f}" for f in target_files]
        else:  # Target all files.
            # Exclude coordination files - they are local-only and should not be in S3
            coordination_files = {
                RemoteSyncLockFileUtil.REMOTE_SYNC_LOCK_FILE,
                RemoteSyncStatusFileUtil.REMOTE_SYNC_STATUS_FILE,
            }
            # Exclude internal directories that should never be uploaded
            for root, dirs, files in os.walk(experiment_local_path):
                # Skip excluded directories (modifies dirs in-place to prevent descent)
                dirs[:] = [
                    d for d in dirs if d not in self.UPLOAD_EXPERIMENT_EXCLUDED_DIRS
                ]
                for filename in files:
                    # Skip coordination files
                    if filename in coordination_files:
                        logger.debug(
                            f"Skipping coordination file from S3 upload: {filename}"
                        )
                        continue
                    local_abs_path = os.path.join(root, filename)
                    target_abs_paths.append(local_abs_path)

        # make target files path list
        # 2) Obtain target file path in transfer format to S3.
        adjusted_target_files = []
        for local_abs_path in target_abs_paths:
            local_relative_path = os.path.relpath(local_abs_path, experiment_local_path)

            s3_file_path = join_filepath(
                [
                    __class__.S3_BASE_PATH,
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
        loop = asyncio.get_event_loop()
        total_bytes_uploaded = 0

        for index, (local_abs_path, s3_file_path, file_size) in enumerate(
            adjusted_target_files
        ):
            logger.debug(
                f"Upload data to S3 [{self.bucket_name}] "
                f"({index+1}/{target_files_count}) "
                f"{s3_file_path} ({file_size:,} bytes)"
            )

            try:
                # Use synchronous boto3 to avoid aioboto3 asyncio compatibility issues
                # Run in thread pool to maintain async interface
                def upload_file(local_path, s3_path):
                    s3_client: "S3Client" = boto3.client("s3")
                    return s3_client.upload_file(local_path, self.bucket_name, s3_path)

                await loop.run_in_executor(
                    None, upload_file, local_abs_path, s3_file_path
                )
                total_bytes_uploaded += file_size
            except Exception as e:
                logger.error(f"Failed to upload experiment file {s3_file_path}: {e}")
                return False

        # Update user storage with the total bytes uploaded (incremental approach)
        # Uses idempotent operation to prevent double-counting on retries
        if total_bytes_uploaded > 0:
            try:
                from studio.app.common.core.cloud.storage_operations import (
                    increment_storage_idempotent,
                )

                workspace_id_int = int(workspace_id)
                with session_scope() as db:
                    query_result = db.execute(
                        select(common_model.Workspace.user_id).where(
                            common_model.Workspace.id == workspace_id_int
                        )
                    )
                    result_row = query_result.first()
                    user_id = result_row[0] if result_row else None

                if user_id:
                    # Use idempotent key to prevent double-counting
                    idempotency_key = (
                        f"exp_upload_{workspace_id}"
                        f"_{unique_id}"
                        f"_{int(time.time())}"
                    )
                    success = increment_storage_idempotent(
                        user_id, total_bytes_uploaded, idempotency_key
                    )
                    if success:
                        logger.debug(
                            f"Incremented storage for user {user_id} by "
                            f"{total_bytes_uploaded:,} bytes after upload"
                        )
                    else:
                        logger.warning(
                            f"Storage increment returned false for user {user_id} "
                            f"(key: {idempotency_key}). Will be retried by "
                            "reconciliation job."
                        )
            except Exception as storage_error:
                logger.warning(
                    f"Failed to update storage after upload: {storage_error}. "
                    "Pending operation will be retried by reconciliation job."
                )
                # Don't fail the upload if storage tracking fails
                # The pending operation will be recovered by
                # process_stale_pending_operations()

        return True

    async def delete_experiment(self, workspace_id: str, unique_id: str) -> bool:
        # make paths
        experiment_remote_path = self._make_experiment_remote_path(
            workspace_id, unique_id
        )

        # ----------------------------------------
        # exec deleting
        # ----------------------------------------

        # Track total bytes deleted for storage update
        total_bytes_deleted = 0

        # do delete data from remote storage
        try:
            async with self.__get_s3_resource() as __s3_resource:
                bucket = await __s3_resource.Bucket(self.bucket_name)

                objects_to_delete = bucket.objects.filter(Prefix=experiment_remote_path)

                # Collect keys and sizes before deletion
                keys_to_delete = []
                async for obj in objects_to_delete:
                    keys_to_delete.append({"Key": obj.key})
                    # Track size for storage update
                    total_bytes_deleted += await obj.size

                if keys_to_delete:
                    logger.debug(
                        f"Deleting {len(keys_to_delete)} objects "
                        f"({total_bytes_deleted:,} bytes) "
                        f"from {experiment_remote_path}"
                    )
                    await self._delete_s3_objects_batched(bucket, keys_to_delete)
        except Exception as e:
            if (
                hasattr(e, "response")
                and e.response.get("Error", {}).get("Code") == "NoSuchBucket"
            ):
                logger.warning(
                    f"[S3] Bucket '{self.bucket_name}' does not exist, "
                    f"skipping experiment deletion for '{unique_id}'"
                )
                return True
            raise

        # Update user storage with the total bytes deleted (incremental approach)
        # Uses idempotent operation to prevent double-counting on retries
        if total_bytes_deleted > 0:
            try:
                from studio.app.common.core.cloud.storage_operations import (
                    decrement_storage_idempotent,
                )

                workspace_id_int = int(workspace_id)
                with session_scope() as db:
                    query_result = db.execute(
                        select(common_model.Workspace.user_id).where(
                            common_model.Workspace.id == workspace_id_int
                        )
                    )
                    result_row = query_result.first()
                    user_id = result_row[0] if result_row else None

                if user_id:
                    # Use idempotent key to prevent double-counting
                    idempotency_key = f"exp_delete_{workspace_id}_{unique_id}"
                    success = decrement_storage_idempotent(
                        user_id, total_bytes_deleted, idempotency_key
                    )
                    if success:
                        logger.debug(
                            f"Decremented storage for user {user_id} by "
                            f"{total_bytes_deleted:,} bytes after deletion"
                        )
                    else:
                        logger.warning(
                            f"Storage decrement returned false for user {user_id} "
                            f"(key: {idempotency_key}). Will be retried by "
                            "reconciliation job."
                        )
            except Exception as storage_error:
                logger.warning(
                    f"Failed to update storage after deletion: {storage_error}. "
                    "Pending operation will be retried by reconciliation job."
                )
                # Don't fail the deletion if storage tracking fails
                # The pending operation will be recovered by
                # process_stale_pending_operations()

        return True

    async def download_thumbnail_source(
        self,
        workspace_id: str,
        unique_id: str,
        original_path: str,
        thumb_type: "ThumbnailType",
    ) -> bool:
        """
        Download the source file needed to generate a thumbnail.

        For INPUT thumbnails: downloads the input TIFF file
        For ROI thumbnails: downloads experiment files with visualization sync mode

        Args:
            workspace_id: Workspace identifier
            unique_id: Experiment unique identifier
            original_path: Path to original file
            thumb_type: ThumbnailType.INPUT or ThumbnailType.ROI

        Returns:
            True if download succeeded, False otherwise
        """
        from studio.app.const import ThumbnailType

        try:
            if thumb_type == ThumbnailType.INPUT:
                filename = os.path.basename(original_path)
                await self.download_input_data(workspace_id, filename)
            else:
                await self.download_experiment(
                    workspace_id,
                    unique_id,
                    sync_mode=RemoteExperimentSyncMode.VISUALIZATION,
                )
            return True
        except Exception as e:
            logger.warning(f"Failed to download thumbnail source: {e}")
            return False

    async def upload_thumbnail(
        self, workspace_id: str, unique_id: str, thumbnail_path: str
    ) -> bool:
        """
        Upload a generated thumbnail PNG to S3 for persistence.

        This allows thumbnails generated lazily on one instance to be
        available to other instances without regeneration.

        Args:
            workspace_id: Workspace identifier
            unique_id: Experiment unique identifier
            thumbnail_path: Local path to the thumbnail PNG file

        Returns:
            True if upload successful, False otherwise
        """
        if not os.path.exists(thumbnail_path):
            logger.warning(f"Thumbnail file not found: {thumbnail_path}")
            return False

        # Construct S3 path
        filename = os.path.basename(thumbnail_path)
        s3_path = join_filepath(
            [
                self.make_s3_output_prefix(workspace_id, unique_id).rstrip("/"),
                ThumbnailConst.DIRNAME,
                filename,
            ]
        )

        file_size = os.path.getsize(thumbnail_path)
        logger.info(f"Uploading thumbnail to S3: {s3_path} ({file_size:,} bytes)")

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: boto3.client("s3").upload_file(
                    thumbnail_path, self.bucket_name, s3_path
                ),
            )
            logger.info(f"Successfully uploaded thumbnail: {s3_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to upload thumbnail: {e}")
            return False

    async def delete_workspace(
        self, workspace_id: str, directory_type: StorageDirectoryType
    ) -> bool:
        try:
            logger.info(
                f"[S3]Delete workspace '{workspace_id}'"
                f" for category '{directory_type.value}'"
            )

            # Validate category
            if directory_type not in [
                StorageDirectoryType.OUTPUT,
                StorageDirectoryType.INPUT,
            ]:
                logger.error(f"Invalid category specified: {directory_type.value}")
                return False

            prefix = f"{self.S3_BASE_PATH}/" f"{directory_type.value}/{workspace_id}/"

            async with self.__get_s3_resource() as s3_resource:
                bucket = await s3_resource.Bucket(self.bucket_name)
                objects_to_delete = bucket.objects.filter(Prefix=prefix)
                keys_to_delete = [{"Key": obj.key} async for obj in objects_to_delete]

                if keys_to_delete:
                    await self._delete_s3_objects_batched(bucket, keys_to_delete)
                    logger.info(f"[S3] Deleted S3 objects under prefix: {prefix}")
                else:
                    logger.warning(f"[S3] No objects found for prefix: {prefix}")

            return True

        except Exception as e:
            if (
                hasattr(e, "response")
                and e.response.get("Error", {}).get("Code") == "NoSuchBucket"
            ):
                logger.warning(
                    f"[S3] Bucket '{self.bucket_name}' does not exist, "
                    f"skipping workspace deletion for '{workspace_id}'"
                )
                return True
            logger.error(f"[S3] Failed to delete workspace: {e}", exc_info=True)
            return False
