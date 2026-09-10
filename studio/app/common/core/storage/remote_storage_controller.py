import asyncio
import fcntl
import json
import os
import shutil
import time
from abc import ABCMeta, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import timedelta
from enum import Enum
from typing import Dict, List

from studio.app.common.core.logger import AppLogger
from studio.app.common.core.snakemake.smk_utils import SmkUtils
from studio.app.common.core.utils.datetime_utils import (
    datetime_from_timestamp,
    get_current_datetime,
)
from studio.app.common.core.utils.filepath_creater import join_filepath
from studio.app.const import ThumbnailType
from studio.app.dir_path import DIRPATH

logger = AppLogger.get_logger()


class RemoteStorageType(Enum):
    NO_USE = "0"
    MOCK = "1"
    S3 = "2"

    @classmethod
    def get_activated_type(cls) -> "RemoteStorageType":
        remote_storage_type_str = os.environ.get(
            "REMOTE_STORAGE_TYPE", cls.NO_USE.value
        )

        try:
            result = RemoteStorageType(remote_storage_type_str)
        except ValueError:
            result = RemoteStorageType.NO_USE

        return result


class RemoteSyncStatus(Enum):
    SUCCESS = "success"
    PROCESSING = "processing"
    ERROR = "error"


class RemoteSyncAction(Enum):
    DOWNLOAD = "download"
    UPLOAD = "upload"
    DELETE = "delete"


class RemoteExperimentSyncMode(Enum):
    ALL = "all"
    METADATA_ONLY = "metadata_only"
    VISUALIZATION = "visualization"
    ESSENTIAL_ONLY = "essential_only"
    THUMBNAILS_ONLY = "thumbnails_only"


class StorageDirectoryType(Enum):
    INPUT = "input"
    OUTPUT = "output"


class RemoteStorageLockError(Exception):
    def __init__(self, workspace_id: str, unique_id: str):
        self.workspace_id = workspace_id
        self.unique_id = unique_id

        message = "Remote data is temporary locked. " f"[{workspace_id}/{unique_id}]"
        super().__init__(message)

    def __reduce__(self):
        """
        Code required to unpack pickled data on multiprocessing
        (specifying the unpacking process)
        """
        return (self.__class__, (self.workspace_id, self.unique_id))


class RemoteStorageBucketNotFoundError(Exception):
    """Raised when user's S3 bucket does not exist."""

    def __init__(self, bucket_name: str):
        self.bucket_name = bucket_name
        message = f"S3 bucket does not exist: {bucket_name}"
        super().__init__(message)


class RemoteExperimentNotFoundError(Exception):
    """Raised when specified experiment data does not exist."""

    def __init__(self, workspace_id: str, unique_id: str):
        self.workspace_id = workspace_id
        self.unique_id = unique_id
        message = f"Remote experiment data does not exist: ({workspace_id}/{unique_id})"
        super().__init__(message)


@dataclass
class RemoteSyncStatusData:
    remote_bucket_name: str
    remote_storage_type: str  # RemoteStorageType
    sync_action: str  # RemoteSyncAction
    sync_status: str  # RemoteSyncStatus
    timestamp: str

    @classmethod
    def from_dict(cls, data: dict) -> "RemoteSyncStatusData":
        return cls(
            remote_bucket_name=data.get("remote_bucket_name", ""),
            remote_storage_type=data.get("remote_storage_type", ""),
            sync_action=data.get("sync_action", ""),
            sync_status=data.get("sync_status", ""),
            timestamp=data.get("timestamp", ""),
        )


class RemoteSyncStatusFileUtil:
    REMOTE_SYNC_STATUS_FILE = "remote_sync_stat.json"

    @classmethod
    def __make_sync_status_file_path(cls, workspace_id: str, unique_id: str) -> str:
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
    def check_sync_status_file(
        cls, workspace_id: str, unique_id: str
    ) -> RemoteSyncStatus:
        """
        check remote storage sync status file.
        """
        remote_sync_status_file_path = cls.__make_sync_status_file_path(
            workspace_id, unique_id
        )

        sync_status = None
        if os.path.isfile(remote_sync_status_file_path):
            with open(remote_sync_status_file_path) as f:
                sync_status_data = RemoteSyncStatusData.from_dict(json.load(f))
                sync_status_str = sync_status_data.sync_status.upper()
                if sync_status_str in RemoteSyncStatus.__members__:
                    sync_status = RemoteSyncStatus[sync_status_str]

        return sync_status

    @classmethod
    def check_sync_status_success(cls, workspace_id: str, unique_id: str) -> bool:
        """
        check remote storage sync status file. (is success)
        """
        sync_status = cls.check_sync_status_file(workspace_id, unique_id)

        return sync_status == RemoteSyncStatus.SUCCESS

    @classmethod
    def check_sync_status_unsynced(cls, workspace_id: str, unique_id: str) -> bool:
        """
        check remote storage sync status file. (is unsynced)
        """
        sync_status = cls.check_sync_status_file(workspace_id, unique_id)

        return sync_status != RemoteSyncStatus.SUCCESS

    @classmethod
    def create_sync_status_file(
        cls,
        remote_bucket_name: str,
        workspace_id: str,
        unique_id: str,
        sync_action: RemoteSyncAction,
        sync_status: RemoteSyncStatus,
    ) -> None:
        """
        create remote storage sync status file.
        """
        remote_sync_status_file_path = cls.__make_sync_status_file_path(
            workspace_id, unique_id
        )

        os.makedirs(
            os.path.dirname(remote_sync_status_file_path),
            exist_ok=True,
        )
        with open(remote_sync_status_file_path, "w") as f:
            sync_status_data = RemoteSyncStatusData(
                remote_bucket_name=remote_bucket_name,
                remote_storage_type=RemoteStorageType.get_activated_type().value,
                sync_action=sync_action.value,
                sync_status=sync_status.value,
                timestamp=str(get_current_datetime()),
            )
            json.dump(asdict(sync_status_data), f, indent=2)

    @classmethod
    def create_sync_status_file_for_success(
        cls,
        remote_bucket_name: str,
        workspace_id: str,
        unique_id: str,
        sync_action: RemoteSyncAction,
    ) -> None:
        cls.create_sync_status_file(
            remote_bucket_name,
            workspace_id,
            unique_id,
            sync_action,
            RemoteSyncStatus.SUCCESS,
        )

    @classmethod
    def create_sync_status_file_for_processing(
        cls,
        remote_bucket_name: str,
        workspace_id: str,
        unique_id: str,
        sync_action: RemoteSyncAction,
    ) -> None:
        cls.create_sync_status_file(
            remote_bucket_name,
            workspace_id,
            unique_id,
            sync_action,
            RemoteSyncStatus.PROCESSING,
        )

    @classmethod
    def create_sync_status_file_for_error(
        cls,
        remote_bucket_name: str,
        workspace_id: str,
        unique_id: str,
        sync_action: RemoteSyncAction,
    ) -> None:
        cls.create_sync_status_file(
            remote_bucket_name,
            workspace_id,
            unique_id,
            sync_action,
            RemoteSyncStatus.ERROR,
        )

    @classmethod
    def delete_sync_status_file(cls, workspace_id: str, unique_id: str) -> None:
        """
        delete remote storage sync status file.
        """
        remote_sync_status_file_path = cls.__make_sync_status_file_path(
            workspace_id, unique_id
        )

        if os.path.isfile(remote_sync_status_file_path):
            os.remove(remote_sync_status_file_path)

    @classmethod
    def get_remote_bucket_name(cls, workspace_id: str, unique_id: str) -> None:
        """
        get remote_bucket_name from sync status file.
        """
        remote_sync_status_file_path = cls.__make_sync_status_file_path(
            workspace_id, unique_id
        )

        remote_bucket_name = None
        if os.path.isfile(remote_sync_status_file_path):
            with open(remote_sync_status_file_path) as f:
                sync_status_data = RemoteSyncStatusData.from_dict(json.load(f))
                remote_bucket_name = sync_status_data.remote_bucket_name
        else:
            logger.warning(
                f"remote_sync_status_file not found. [{remote_sync_status_file_path}]"
            )

        assert remote_bucket_name, f"Invalid remote_bucket_name: {remote_bucket_name}"

        return remote_bucket_name


class RemoteSyncLockFileUtil:
    REMOTE_SYNC_LOCK_FILE = "remote_sync.lock"
    LOCK_FILE_EXPIRE_MINUTES = 60  # Fixed at 60 minutes

    LOCK_WAIT_MAX_SECONDS = 300
    LOCK_WAIT_POLL_INTERVAL = 2

    # Retry policy for operations that re-attempt on a lock conflict
    # (RemoteStorageLockError) rather than waiting on the lock file.
    # Used when an exclusive access is contended by another process and the
    # operation is idempotent, so a short bounded retry can succeed.
    LOCK_CONFLICT_RETRY_MAX = 3
    LOCK_CONFLICT_RETRY_BACKOFF_SECONDS = 5

    @classmethod
    def __make_sync_lock_file_path(cls, workspace_id: str, unique_id: str) -> str:
        """
        make remote storage sync lock file path.
        """
        experiment_local_path = join_filepath(
            [DIRPATH.OUTPUT_DIR, workspace_id, unique_id]
        )
        remote_sync_lock_file_path = os.path.join(
            experiment_local_path, cls.REMOTE_SYNC_LOCK_FILE
        )
        return remote_sync_lock_file_path

    @classmethod
    def check_sync_lock_file(
        cls, workspace_id: str, unique_id: str, raise_error: bool = False
    ) -> bool:
        """
        check remote storage sync status file.
        """
        remote_sync_lock_file_path = cls.__make_sync_lock_file_path(
            workspace_id, unique_id
        )

        is_locked = False
        if os.path.isfile(remote_sync_lock_file_path):
            # Get lock file's modified time
            threshold_min = cls.LOCK_FILE_EXPIRE_MINUTES
            threshold_time = get_current_datetime() - timedelta(minutes=threshold_min)
            file_modified_time = datetime_from_timestamp(
                os.path.getmtime(remote_sync_lock_file_path)
            )

            # If the lock file is old, delete it.
            if file_modified_time <= threshold_time:
                cls.delete_sync_lock_file(workspace_id, unique_id)
                is_locked = False
            else:
                is_locked = True

        if is_locked and raise_error:
            raise RemoteStorageLockError(workspace_id, unique_id)

        return is_locked

    @classmethod
    def create_sync_lock_file(
        cls,
        workspace_id: str,
        unique_id: str,
    ) -> None:
        """
        create remote storage sync lock file.
        """
        remote_sync_lock_file_path = cls.__make_sync_lock_file_path(
            workspace_id, unique_id
        )

        os.makedirs(
            os.path.dirname(remote_sync_lock_file_path),
            exist_ok=True,
        )
        with open(remote_sync_lock_file_path, "w") as f:
            file_data = {
                "workspace_id": workspace_id,
                "unique_id": unique_id,
                "timestamp": get_current_datetime(),
            }
            json.dump(file_data, f, default=str, indent=2)

            # force fsync
            os.fsync(f.fileno())

    @classmethod
    def delete_sync_lock_file(cls, workspace_id: str, unique_id: str) -> None:
        """
        delete remote storage sync lock file.
        """
        remote_sync_lock_file_path = cls.__make_sync_lock_file_path(
            workspace_id, unique_id
        )

        if os.path.isfile(remote_sync_lock_file_path):
            os.remove(remote_sync_lock_file_path)

    @classmethod
    def wait_for_lock_release(cls, workspace_id: str, unique_id: str) -> bool:
        """Wait for post_process upload lock to release."""
        if not cls.check_sync_lock_file(workspace_id, unique_id):
            return True

        experiment = f"{workspace_id}/{unique_id}"
        logger.info(f"Waiting for remote storage lock on [{experiment}]")
        start = time.monotonic()

        while time.monotonic() - start < cls.LOCK_WAIT_MAX_SECONDS:
            time.sleep(cls.LOCK_WAIT_POLL_INTERVAL)
            if not cls.check_sync_lock_file(workspace_id, unique_id):
                elapsed = time.monotonic() - start
                logger.info(f"Lock released on [{experiment}] " f"after {elapsed:.1f}s")
                return True

        elapsed = time.monotonic() - start
        logger.warning(
            f"Lock wait timeout on [{experiment}] "
            f"after {elapsed:.1f}s, proceeding anyway"
        )
        return False


class InputFileLock:
    """Per-(workspace, file) flock so concurrent processes sharing INPUT_DIR
    over EFS don't redundantly download the same input. On timeout we fall
    through unlocked — tmp+rename in the download keeps partial reads
    impossible regardless."""

    LOCKS_DIRNAME = ".locks"
    LOCK_WAIT_MAX_SECONDS = 300
    LOCK_WAIT_POLL_INTERVAL = 0.1

    @classmethod
    def _lock_path(cls, workspace_id: str, filename: str) -> str:
        return join_filepath(
            [DIRPATH.INPUT_DIR, workspace_id, cls.LOCKS_DIRNAME, f"{filename}.lock"]
        )

    @classmethod
    @asynccontextmanager
    async def acquire(cls, workspace_id: str, filename: str):
        lock_path = cls._lock_path(workspace_id, filename)
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)

        fd = await asyncio.to_thread(os.open, lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:

            def _try_lock() -> bool:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return True
                except BlockingIOError:
                    return False

            start = time.monotonic()
            while True:
                if await asyncio.to_thread(_try_lock):
                    break
                elapsed = time.monotonic() - start
                if elapsed >= cls.LOCK_WAIT_MAX_SECONDS:
                    logger.warning(
                        f"InputFileLock acquire timeout after {elapsed:.1f}s "
                        f"for {workspace_id}/{filename}; proceeding without "
                        "exclusive lock"
                    )
                    break
                await asyncio.sleep(cls.LOCK_WAIT_POLL_INTERVAL)

            yield
        finally:
            # close releases the flock; off-loop in case it blocks on NFS.
            await asyncio.to_thread(os.close, fd)


class BaseRemoteStorageController(metaclass=ABCMeta):
    # Directories to be excluded when uploading experimental data to remote storage
    UPLOAD_EXPERIMENT_EXCLUDED_DIRS = {".snakemake"}

    @staticmethod
    def create_upload_experiment_ignore_function(excluded_dirs: set = None):
        """
        Create an ignore function for shutil.copytree used in upload_experiment.
        Excludes specified directories from being uploaded to remote storage.

        Args:
            excluded_dirs: Set of directory names to exclude.
                          If None, uses UPLOAD_EXPERIMENT_EXCLUDED_DIRS.

        Returns:
            A function suitable for use with shutil.copytree's ignore parameter.
        """
        if excluded_dirs is None:
            excluded_dirs = BaseRemoteStorageController.UPLOAD_EXPERIMENT_EXCLUDED_DIRS

        def ignore_excluded(dir_path, files):
            # Get the basename of current directory
            dir_name = os.path.basename(dir_path)
            # If current directory is in excluded list, ignore all contents
            if dir_name in excluded_dirs:
                return files
            # Otherwise, ignore only subdirectories that match excluded_dirs
            return [f for f in files if f in excluded_dirs]

        return ignore_excluded

    @abstractmethod
    def _make_input_data_local_path(self, workspace_id: str, filename: str) -> str:
        """
        make input data directory local path.
        """

    @abstractmethod
    def _make_input_data_remote_path(self, workspace_id: str, filename: str) -> str:
        """
        make input data directory remote path.
        """

    @abstractmethod
    def _make_experiment_local_path(self, workspace_id: str, unique_id: str) -> str:
        """
        make experiment data directory local path.
        """

    @abstractmethod
    def _make_experiment_remote_path(self, workspace_id: str, unique_id: str) -> str:
        """
        make experiment data directory remote path.
        """

    @property
    @abstractmethod
    def bucket_name(self) -> str:
        """
        return current remotes storage bucket_name.
        """

    @abstractmethod
    def download_input_data(self, workspace_id: str, filename: str) -> bool:
        """
        download input data from remote storage.
        """

    @abstractmethod
    def upload_input_data(self, workspace_id: str, filename: str) -> bool:
        """
        upload input data to remote storage.
        """

    @abstractmethod
    def delete_input_data(self, workspace_id: str, filename: str) -> bool:
        """
        delete input data from remote storage.
        """

    @abstractmethod
    def list_input_data_objects(self, workspace_id: str) -> List[Dict]:
        """
        List all input data objects in remote storage for a workspace.
        Returns list of dicts with filename, size, last_modified.
        """

    @abstractmethod
    def download_all_experiments_metas(self, workspace_ids: list = None) -> bool:
        """
        download all experiment metadata from remote storage.
        """

    @abstractmethod
    def download_experiment_meta(self, workspace_id: str, unique_id: str) -> bool:
        """
        Download metadata files (yaml) for a single experiment from remote storage.
        More efficient than download_all_experiments_metas when only one
        experiment is needed.
        """

    @abstractmethod
    def download_experiment(
        self,
        workspace_id: str,
        unique_id: str,
        sync_mode: RemoteExperimentSyncMode = RemoteExperimentSyncMode.ALL,
    ) -> bool:
        """
        download experiment data from remote storage.
        """

    @abstractmethod
    def upload_experiment(
        self, workspace_id: str, unique_id: str, target_files: list = None
    ) -> bool:
        """
        upload experiment data to remote storage.

        Args:
            target_files list:
                Specify files to be uploaded (By default, all files are targeted)
        """

    @abstractmethod
    def delete_experiment(self, workspace_id: str, unique_id: str) -> bool:
        """
        delete experiment data from remote storage.
        """

    @abstractmethod
    def download_thumbnail_source(
        self,
        workspace_id: str,
        unique_id: str,
        original_path: str,
        thumb_type: ThumbnailType,
    ) -> bool:
        """
        Download the source file needed to generate a thumbnail.
        """

    @abstractmethod
    async def upload_thumbnail(
        self, workspace_id: str, unique_id: str, thumbnail_path: str
    ) -> bool:
        """
        Upload a generated thumbnail PNG to remote storage for persistence.
        """

    @abstractmethod
    async def delete_workspace(
        self, workspace_id: str, directory_type: StorageDirectoryType
    ) -> bool:
        """
        Delete workspace data.
        """

    async def _clear_local_experiment_data(self, experiment_local_path: str):
        """
        Clean existing local experiment data
        - Delete all data except for some files (lockfile, etc.)
        """
        source_dir_path = experiment_local_path
        deleting_temp_dir = "_deleting_tmp"
        deleting_temp_dir_path = os.path.join(source_dir_path, deleting_temp_dir)
        exclude_files = [
            deleting_temp_dir,
            RemoteSyncLockFileUtil.REMOTE_SYNC_LOCK_FILE,
            RemoteSyncStatusFileUtil.REMOTE_SYNC_STATUS_FILE,
        ]

        if os.path.isdir(deleting_temp_dir_path):
            shutil.rmtree(deleting_temp_dir_path)

        os.makedirs(deleting_temp_dir_path)

        # Move files to be deleted to a temporary folder
        for filename in os.listdir(source_dir_path):
            source_path = os.path.join(source_dir_path, filename)

            if filename not in exclude_files:
                source_path = os.path.join(source_dir_path, filename)
                dest_path = os.path.join(deleting_temp_dir_path, filename)

                shutil.move(source_path, dest_path)

        # Delete temporary folders for deletion
        shutil.rmtree(deleting_temp_dir_path)


class RemoteStorageController(BaseRemoteStorageController):
    def __init__(self, bucket_name: str):
        remote_storage_type = RemoteStorageType.get_activated_type()

        if remote_storage_type == RemoteStorageType.MOCK:
            from studio.app.common.core.storage.mock_storage_controller import (
                MockStorageController,
            )

            self.__controller = MockStorageController()
        elif remote_storage_type == RemoteStorageType.S3:
            from studio.app.common.core.storage.s3_storage_controller import (
                S3StorageController,
            )

            self.__controller = S3StorageController(bucket_name)
        else:
            assert False, f"Invalid remote_storage_type: {remote_storage_type}"

    @staticmethod
    def is_available():
        """
        Determine if remote storage is available
        """
        remote_storage_type = RemoteStorageType.get_activated_type()
        is_available = remote_storage_type in [
            RemoteStorageType.MOCK,
            RemoteStorageType.S3,
        ]
        return is_available

    def _make_input_data_local_path(self, workspace_id: str, filename: str) -> str:
        return self.__controller._make_input_data_local_path(workspace_id, filename)

    def _make_input_data_remote_path(self, workspace_id: str, filename: str) -> str:
        return self.__controller._make_input_data_remote_path(workspace_id, filename)

    def _make_experiment_local_path(self, workspace_id: str, unique_id: str) -> str:
        return self.__controller._make_experiment_local_path(workspace_id, unique_id)

    def _make_experiment_remote_path(self, workspace_id: str, unique_id: str) -> str:
        return self.__controller._make_experiment_remote_path(workspace_id, unique_id)

    @staticmethod
    def create_user_bucket_name(id: int, prefix: str = "optinist-user") -> str:
        # Deterministic per (secret, id): repeated calls for the same user
        # produce the same bucket name, so retries are idempotent and
        # BucketAlreadyOwnedByYou is a real safety net rather than masking
        # drift.
        import hashlib
        import os

        secret = os.environ.get("S3_USER_BUCKET_SECRET", "")
        hash_src = f"{secret}-{id}"
        hash_value = hashlib.md5(hash_src.encode()).hexdigest()[0:10]
        return f"{prefix}-{id}-{hash_value}"

    @property
    def bucket_name(self) -> str:
        return self.__controller.bucket_name

    async def create_bucket(self) -> bool:
        remote_storage_type = RemoteStorageType.get_activated_type()

        if remote_storage_type == RemoteStorageType.S3:
            await self.__controller.create_bucket()
        elif remote_storage_type == RemoteStorageType.MOCK:
            logger.info(
                "This remote_storage_type does not use bucket: "
                f"{remote_storage_type.value}"
            )
            pass
        else:
            assert False, (
                "This remote_storage_type does not support bucket: "
                f"{remote_storage_type.value}"
            )

        return True

    async def delete_bucket(self, force_delete=False) -> bool:
        remote_storage_type = RemoteStorageType.get_activated_type()
        if remote_storage_type == RemoteStorageType.S3:
            await self.__controller.delete_bucket(force_delete)
        elif remote_storage_type == RemoteStorageType.MOCK:
            logger.info(
                "This remote_storage_type does not support bucket: "
                f"{remote_storage_type.value}"
            )
            pass
        else:
            assert False, (
                "This remote_storage_type does not use bucket: "
                f"{remote_storage_type.value}"
            )

        return True

    async def download_input_data(self, workspace_id: str, filename: str) -> bool:
        return await self.__controller.download_input_data(workspace_id, filename)

    async def upload_input_data(self, workspace_id: str, filename: str) -> bool:
        return await self.__controller.upload_input_data(workspace_id, filename)

    async def delete_input_data(self, workspace_id: str, filename: str) -> bool:
        return await self.__controller.delete_input_data(workspace_id, filename)

    async def list_input_data_objects(self, workspace_id: str) -> List[Dict]:
        return await self.__controller.list_input_data_objects(workspace_id)

    async def download_all_experiments_metas(self, workspace_ids: list = None) -> bool:
        """
        Args:
          workspace_ids:
            List of workspace ids to be downloaded. if none, all workspaces are targeted
        """
        return await self.__controller.download_all_experiments_metas(workspace_ids)

    async def download_experiment_meta(self, workspace_id: str, unique_id: str) -> bool:
        """
        Download metadata files (yaml) for a single experiment from remote
        storage. More efficient than download_all_experiments_metas when only
        one experiment is needed.
        """
        return await self.__controller.download_experiment_meta(workspace_id, unique_id)

    async def download_experiment(
        self,
        workspace_id: str,
        unique_id: str,
        sync_mode: RemoteExperimentSyncMode = RemoteExperimentSyncMode.ALL,
    ) -> bool:
        """
        Download experiment from remote storage.

        Args:
            workspace_id: Workspace identifier
            unique_id: Experiment identifier
            sync_mode: RemoteExperimentSyncMode.ALL (full sync),
                       RemoteExperimentSyncMode.VISUALIZATION (json/tiff only),
                       or RemoteExperimentSyncMode.ESSENTIAL_ONLY (yaml/json only)

        Note: Only sync_mode=ALL updates the sync status file and downloads
        input data. Partial syncs (visualization, essential_only) leave the
        status unchanged so that a full sync can still be triggered later.
        """
        is_full_sync = sync_mode == RemoteExperimentSyncMode.ALL

        sync_status_params = {
            "remote_bucket_name": self.bucket_name,
            "workspace_id": workspace_id,
            "unique_id": unique_id,
            "sync_action": RemoteSyncAction.DOWNLOAD,
        }
        result = False

        try:
            # Only update sync status for full syncs
            if is_full_sync:
                RemoteSyncStatusFileUtil.create_sync_status_file_for_processing(
                    **sync_status_params
                )

            # download experiment data
            result = await self.__controller.download_experiment(
                workspace_id, unique_id, sync_mode=sync_mode
            )

            # Only download input data and mark success for full syncs
            if is_full_sync:
                # Download the input data related to the experiment data as well.
                try:
                    input_filenames = SmkUtils.get_datatypes_inputs(
                        workspace_id, unique_id, apply_basename=True
                    )
                    for input_filename in input_filenames:
                        await self.download_input_data(workspace_id, input_filename)
                except (AssertionError, KeyError) as e:
                    # snakemake.yaml may be empty or missing required keys
                    logger.warning(f"snakemake yaml read error: {e}")
                    pass

                # update sync status file
                RemoteSyncStatusFileUtil.create_sync_status_file_for_success(
                    **sync_status_params
                )
        except Exception as e:
            # Only update error status for full syncs
            if is_full_sync:
                RemoteSyncStatusFileUtil.create_sync_status_file_for_error(
                    **sync_status_params
                )
            raise e

        return result

    async def upload_experiment(
        self, workspace_id: str, unique_id: str, target_files: list = None
    ) -> bool:
        sync_status_params = {
            "remote_bucket_name": self.bucket_name,
            "workspace_id": workspace_id,
            "unique_id": unique_id,
            "sync_action": RemoteSyncAction.UPLOAD,
        }
        result = False

        try:
            # create sync status file
            RemoteSyncStatusFileUtil.create_sync_status_file_for_processing(
                **sync_status_params
            )

            result = await self.__controller.upload_experiment(
                workspace_id, unique_id, target_files
            )

            # update sync status file based on result
            if result:
                RemoteSyncStatusFileUtil.create_sync_status_file_for_success(
                    **sync_status_params
                )
            else:
                RemoteSyncStatusFileUtil.create_sync_status_file_for_error(
                    **sync_status_params
                )
        except Exception as e:
            RemoteSyncStatusFileUtil.create_sync_status_file_for_error(
                **sync_status_params
            )
            raise e

        return result

    async def delete_experiment(self, workspace_id: str, unique_id: str) -> bool:
        sync_status_params = {
            "remote_bucket_name": self.bucket_name,
            "workspace_id": workspace_id,
            "unique_id": unique_id,
            "sync_action": RemoteSyncAction.DELETE,
        }
        result = False

        try:
            RemoteSyncStatusFileUtil.create_sync_status_file_for_processing(
                **sync_status_params
            )

            result = await self.__controller.delete_experiment(workspace_id, unique_id)

            # update sync status file based on result
            if result:
                RemoteSyncStatusFileUtil.create_sync_status_file_for_success(
                    **sync_status_params
                )
            else:
                RemoteSyncStatusFileUtil.create_sync_status_file_for_error(
                    **sync_status_params
                )
        except Exception as e:
            RemoteSyncStatusFileUtil.create_sync_status_file_for_error(
                **sync_status_params
            )
            raise e

        return result

    async def download_thumbnail_source(
        self,
        workspace_id: str,
        unique_id: str,
        original_path: str,
        thumb_type: ThumbnailType,
    ) -> bool:
        """
        Download the source file needed to generate a thumbnail.
        """
        return await self.__controller.download_thumbnail_source(
            workspace_id, unique_id, original_path, thumb_type
        )

    async def upload_thumbnail(
        self, workspace_id: str, unique_id: str, thumbnail_path: str
    ) -> bool:
        """
        Upload a generated thumbnail PNG to remote storage for persistence.
        """
        return await self.__controller.upload_thumbnail(
            workspace_id, unique_id, thumbnail_path
        )

    async def delete_workspace(
        self, workspace_id: str, directory_type: StorageDirectoryType
    ) -> bool:
        try:
            logger.info(
                f"[AWS] Delete WID '{workspace_id}'"
                f"for category '{directory_type.value}'"
            )

            # Delegate the call to the actual backend (S3, Mock, etc.)
            return await self.__controller.delete_workspace(
                workspace_id, directory_type
            )

        except Exception as e:
            logger.exception(f"[AWS] Failed to delete workspace '{workspace_id}': {e}")
            return False


class BaseRemoteStorageSimpleReaderWriter(metaclass=ABCMeta):
    """
    Simplified Reader/Writer wrapper for RemoteStorageController
    - This Reader class does not implement exclusive control of experiment data.
      - It is used to access remote data (such as input data or a single file)
        without specifying the experiment data key (workspace_id, unique_id).
    - params: Specify only bucket_name
    """

    def __init__(self, bucket_name: str, sync_action: RemoteSyncAction):
        # Note: This Reader class does not implement exclusive control of data.

        self.bucket_name = bucket_name
        self.sync_action = sync_action
        self.__controller = RemoteStorageController(bucket_name)

    async def __aenter__(self) -> RemoteStorageController:
        return self.__controller

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass  # do nothing.


class RemoteStorageSimpleReader(BaseRemoteStorageSimpleReaderWriter):
    """
    Simplified Reader wrapper for RemoteStorageController
    """

    def __init__(self, bucket_name: str):
        super().__init__(bucket_name, RemoteSyncAction.DOWNLOAD)


class RemoteStorageSimpleWriter(BaseRemoteStorageSimpleReaderWriter):
    """
    Simplified Writer wrapper for RemoteStorageController
    """

    def __init__(self, bucket_name: str):
        super().__init__(bucket_name, RemoteSyncAction.UPLOAD)


class BaseRemoteStorageReaderWriter(metaclass=ABCMeta):
    """
    Reader/Writer wrapper for RemoteStorageController
    - ContextManager class supporting async
    - Provides exclusive control for the same experiment data
    - params: bucket_name, workspace_id, unique_id, are specified
    """

    def __init__(
        self,
        bucket_name: str,
        workspace_id: str,
        unique_id: str,
        sync_action: RemoteSyncAction,
        sync_mode: RemoteExperimentSyncMode = RemoteExperimentSyncMode.ALL,
    ):
        self.bucket_name = bucket_name
        self.workspace_id = workspace_id
        self.unique_id = unique_id
        self.sync_action = sync_action
        self.sync_mode = sync_mode

        is_locked = RemoteSyncLockFileUtil.check_sync_lock_file(workspace_id, unique_id)
        if is_locked:
            logger.warning("This data is locked because it is being processed.")
            raise RemoteStorageLockError(workspace_id, unique_id)

        # generate remote-sync-lock-file
        RemoteSyncLockFileUtil.create_sync_lock_file(workspace_id, unique_id)

        # generate remote-sync-status-file (for pendding)
        # *updates are only made when self.sync_mode==ALL (full sync)
        if self.sync_mode == RemoteExperimentSyncMode.ALL:
            RemoteSyncStatusFileUtil.create_sync_status_file_for_processing(
                bucket_name,
                workspace_id,
                unique_id,
                self.sync_action,
            )

        self.__controller = RemoteStorageController(bucket_name)

    async def __aenter__(self) -> RemoteStorageController:
        return self.__controller

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # update remote-sync-status-file
        # *updates are only made when self.sync_mode==ALL (full sync)
        if self.sync_mode == RemoteExperimentSyncMode.ALL:
            if not exc_type:  # Processing success
                RemoteSyncStatusFileUtil.create_sync_status_file_for_success(
                    self.bucket_name,
                    self.workspace_id,
                    self.unique_id,
                    self.sync_action,
                )
            else:  # Processing error
                RemoteSyncStatusFileUtil.create_sync_status_file_for_error(
                    self.bucket_name,
                    self.workspace_id,
                    self.unique_id,
                    self.sync_action,
                )

        # delete lock file
        RemoteSyncLockFileUtil.delete_sync_lock_file(self.workspace_id, self.unique_id)


class RemoteStorageReader(BaseRemoteStorageReaderWriter):
    """
    Reader wrapper for RemoteStorageController
    """

    def __init__(
        self,
        bucket_name: str,
        workspace_id: str,
        unique_id: str,
        sync_mode: RemoteExperimentSyncMode = RemoteExperimentSyncMode.ALL,
    ):
        super().__init__(
            bucket_name, workspace_id, unique_id, RemoteSyncAction.DOWNLOAD, sync_mode
        )


class RemoteStorageWriter(BaseRemoteStorageReaderWriter):
    """
    Writer wrapper for RemoteStorageController
    """

    def __init__(self, bucket_name: str, workspace_id: str, unique_id: str):
        super().__init__(bucket_name, workspace_id, unique_id, RemoteSyncAction.UPLOAD)


class RemoteStorageDeleter(BaseRemoteStorageReaderWriter):
    """
    Deleter wrapper for RemoteStorageController
    """

    def __init__(self, bucket_name: str, workspace_id: str, unique_id: str):
        super().__init__(bucket_name, workspace_id, unique_id, RemoteSyncAction.DELETE)


async def upload_experiment_wrapper(
    remote_bucket_name: str, workspace_id: str, unique_id
):
    """
    Utility functions for uploading input data
    This function combines multiple steps into one function
      so that it can be executed via asyncio.gather.
    """
    async with RemoteStorageWriter(
        remote_bucket_name, workspace_id, unique_id
    ) as remote_storage_controller:
        await remote_storage_controller.upload_experiment(workspace_id, unique_id)


async def upload_input_data_wrapper(
    remote_bucket_name: str, workspace_id: str, input_data_name: str
):
    """
    Utility functions for uploading experiments data
    This function combines multiple steps into one function
      so that it can be executed via asyncio.gather.
    """
    async with RemoteStorageSimpleWriter(
        remote_bucket_name
    ) as remote_storage_controller:
        await remote_storage_controller.upload_input_data(workspace_id, input_data_name)


class RemoteStorageDownloadUtils:
    """Utility class for downloading input files from remote storage.

    Exclusive control:
        This class does NOT implement exclusive locking.
        Input files are workspace-scoped (INPUT_DIR/{workspace_id}/), so the
        experiment-level lock in BaseRemoteStorageReaderWriter does not apply.

        Concurrent downloads of the same file may occur, but input files are
        immutable, so the practical risk is limited to redundant API calls.
        Both methods use os.path.exists() to skip already-downloaded files.

        Ideally, workspace-level locking should be introduced in the future.
    """

    @staticmethod
    async def sync_experiment_input_files(
        workspace_id: str, unique_id: str, remote_bucket_name: str
    ) -> None:
        """Sync input files (TIFF/CSV) for an experiment to local storage.

        Input files live in a separate directory from experiment outputs and are
        not covered by download_experiment(sync_mode="visualization"). This must
        be called after the visualization tier download completes.

        Files that already exist locally are skipped.
        """
        if not RemoteStorageController.is_available():
            return

        try:
            input_filenames = SmkUtils.get_datatypes_inputs(
                workspace_id, unique_id, apply_basename=True
            )
            if not input_filenames:
                return

            controller = RemoteStorageController(remote_bucket_name)
            for input_filename in input_filenames:
                input_path = join_filepath(
                    [DIRPATH.INPUT_DIR, workspace_id, input_filename]
                )
                if not os.path.exists(input_path):
                    await controller.download_input_data(workspace_id, input_filename)
        except (AssertionError, KeyError):
            # snakemake_config.yaml missing or incomplete -- skip silently
            pass
        except Exception as e:
            logger.warning(
                f"Input file download failed for {workspace_id}/{unique_id}: {e}"
            )

    @staticmethod
    async def ensure_input_file_synced(
        workspace_id: str,
        filename: str,
        remote_bucket_name: str,
    ) -> bool:
        """On-demand sync for a single input file from remote storage.

        Returns True if file exists locally after sync attempt.
        Raises Exception for remote storage errors
        (caller should handle HTTP response).
        """
        input_path = join_filepath([DIRPATH.INPUT_DIR, workspace_id, filename])

        # Already exists locally
        if os.path.exists(input_path):
            return True

        # Check if remote storage is available
        if not RemoteStorageController.is_available():
            return False

        logger.info(f"On-demand sync for input file: {workspace_id}/{filename}")

        # Lock is held inside download_input_data, covering all entry points.
        controller = RemoteStorageController(remote_bucket_name)
        downloaded = await controller.download_input_data(workspace_id, filename)
        if not downloaded:
            logger.warning(
                f"Input file not found in remote storage: " f"{workspace_id}/{filename}"
            )
            return False
        return os.path.exists(input_path)
