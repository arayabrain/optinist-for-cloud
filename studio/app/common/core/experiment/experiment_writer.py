import asyncio
import glob
import os
import pickle
import re
import shutil
from dataclasses import asdict
from typing import Dict

import numpy as np
import yaml
from filelock import FileLock

from studio.app.common.core.experiment.experiment import ExptConfig, ExptFunction
from studio.app.common.core.experiment.experiment_builder import ExptConfigBuilder
from studio.app.common.core.experiment.experiment_reader import ExptConfigReader
from studio.app.common.core.experiment.experiment_record_services import (
    ExperimentRecordService,
)
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.storage.remote_storage_controller import (
    RemoteStorageController,
    RemoteStorageDeleter,
    RemoteStorageWriter,
    RemoteSyncLockFileUtil,
)
from studio.app.common.core.utils.config_handler import (
    ConfigWriter,
    differential_deep_merge,
)
from studio.app.common.core.utils.datetime_utils import (
    get_datetime_for_timezone_formatted,
)
from studio.app.common.core.utils.filelock_handler import FileLockUtils
from studio.app.common.core.utils.filepath_creater import join_filepath
from studio.app.common.core.workflow.workflow import (
    NodeRunStatus,
    ProcessType,
    WorkflowRunStatus,
)
from studio.app.common.core.workflow.workflow_reader import WorkflowConfigReader
from studio.app.const import DATE_FORMAT
from studio.app.dir_path import DIRPATH

logger = AppLogger.get_logger()

# S3 deletion retry configuration
S3_DELETION_MAX_RETRIES = 3
S3_DELETION_BASE_DELAY_SECONDS = 1.0


class ExptConfigWriter:
    def __init__(
        self,
        workspace_id: str,
        unique_id: str,
        name: str = None,
        nwbfile: Dict = {},
        snakemake: Dict = {},
        timezone: str = None,
    ) -> None:
        self.workspace_id = workspace_id
        self.unique_id = unique_id
        self.name = name
        self.nwbfile = nwbfile
        self.snakemake = snakemake
        self.timezone = timezone  # User's browser timezone (IANA format)
        self.builder = ExptConfigBuilder()

    def write(self) -> None:
        expt_filepath = ExptConfigReader.get_config_yaml_path(
            self.workspace_id, self.unique_id
        )
        if os.path.exists(expt_filepath):
            try:
                expt_config = ExptConfigReader.read(self.workspace_id, self.unique_id)
                self.builder.set_config(expt_config)
                self.add_run_info()
            except (AssertionError, ValueError, KeyError, yaml.YAMLError):
                # An existing but empty/corrupt experiment.yaml would otherwise
                # abort the run; recreate it from scratch instead.
                logger.warning(
                    f"experiment.yaml for {self.workspace_id}/{self.unique_id} "
                    f"is empty or invalid; recreating it"
                )
                self.create_config()
        else:
            self.create_config()

        self.build_function_from_nodeDict()
        self.build_procs()

        # Write EXPERIMENT_YML
        self._write_raw(
            self.workspace_id, self.unique_id, config=asdict(self.builder.build())
        )

    @classmethod
    def _write_raw(
        cls, workspace_id: str, unique_id: str, config: dict, auto_file_lock=True
    ) -> None:
        ConfigWriter.write(
            dirname=join_filepath([DIRPATH.OUTPUT_DIR, workspace_id, unique_id]),
            filename=DIRPATH.EXPERIMENT_YML,
            config=config,
            auto_file_lock=auto_file_lock,
        )

    def overwrite(self, update_params: dict) -> None:
        expt_filepath = ExptConfigReader.get_config_yaml_path(
            self.workspace_id, self.unique_id
        )

        # Exclusive control for parallel updates from multiple processes.
        lock_path = FileLockUtils.get_lockfile_path(expt_filepath)
        with FileLock(lock_path, ConfigWriter.FILE_LOCK_TIMEOUT):
            # Read experiment config
            config = ExptConfigReader.read(self.workspace_id, self.unique_id)

            # Merge overwrite params
            config_merged = differential_deep_merge(asdict(config), update_params)

            # Overwrite experiment config
            __class__._write_raw(
                self.workspace_id, self.unique_id, config_merged, auto_file_lock=False
            )

    def create_config(self) -> ExptConfig:
        return (
            self.builder.set_workspace_id(self.workspace_id)
            .set_unique_id(self.unique_id)
            .set_name(self.name)
            .set_started_at(
                get_datetime_for_timezone_formatted(self.timezone, DATE_FORMAT)
            )
            .set_success(WorkflowRunStatus.RUNNING.value)
            .set_nwbfile(self.nwbfile)
            .set_snakemake(self.snakemake)
            .set_timezone(self.timezone)
            .build()
        )

    def add_run_info(self) -> ExptConfig:
        return (
            self.builder.set_started_at(
                get_datetime_for_timezone_formatted(self.timezone, DATE_FORMAT)
            )  # Update time
            .set_success(WorkflowRunStatus.RUNNING.value)
            # rewritten with started_at above, or the pair disagrees on a rerun
            .set_timezone(self.timezone)
            .build()
        )

    def build_function_from_nodeDict(self) -> ExptConfig:
        func_dict: Dict[str, ExptFunction] = {}
        node_dict = WorkflowConfigReader.read(
            self.workspace_id,
            self.unique_id,
        ).nodeDict

        for node in node_dict.values():
            func_dict[node.id] = ExptFunction(
                unique_id=node.id,
                name=node.data.label,
                hasNWB=False,
                success=NodeRunStatus.RUNNING.value,
            )
            if node.data.type == "input":
                timestamp = get_datetime_for_timezone_formatted(
                    self.timezone, DATE_FORMAT
                )
                func_dict[node.id].started_at = timestamp
                func_dict[node.id].finished_at = timestamp
                func_dict[node.id].success = NodeRunStatus.SUCCESS.value

        return self.builder.set_function(func_dict).build()

    def build_procs(self) -> ExptConfig:
        target_procs = [ProcessType.POST_PROCESS]
        func_dict: Dict[str, ExptFunction] = {}

        for proc in target_procs:
            func_dict[proc.id] = ExptFunction(
                unique_id=proc.id, name=proc.label, hasNWB=False, success="running"
            )

        return self.builder.set_procs(func_dict).build()


class ExptDataWriter:
    def __init__(
        self,
        remote_bucket_name: str,
        workspace_id: str,
        unique_id: str,
    ):
        self.remote_bucket_name = remote_bucket_name
        self.workspace_id = workspace_id
        self.unique_id = unique_id

    async def delete_data(self) -> bool:
        """
        Delete experiment data from S3 and local filesystem.
        Handles local filesystem deletion failure separately from S3.
        """
        experiment_path = join_filepath(
            [DIRPATH.OUTPUT_DIR, self.workspace_id, self.unique_id]
        )

        s3_result = True  # Default to True if S3 not available
        local_result = False

        try:
            # Check the expt is running or if don't have status it will return None
            status = ExptConfigReader.read_experiment_status(
                self.workspace_id, self.unique_id
            )
            # If the experiment is running or has no status, skip deletion
            # no status means the experiemnt yaml is not created yet
            if status is None:
                pass
            elif status == WorkflowRunStatus.RUNNING:
                logger.warning(
                    f"Skipping deletion of running experiment '{self.unique_id}'"
                )
                return False

            # Operate remote storage data with retry logic
            if RemoteStorageController.is_available():
                # Check for remote-sync-lock-file
                # - If lock file exists, an exception is raised (raise_error=True)
                RemoteSyncLockFileUtil.check_sync_lock_file(
                    self.workspace_id, self.unique_id, raise_error=True
                )

                # Delete remote data with retry for transient failures
                s3_result = await self._delete_remote_data_with_retry()

                if not s3_result:
                    logger.error(
                        f"S3 deletion failed after {S3_DELETION_MAX_RETRIES} retries "
                        f"for experiment '{self.unique_id}'"
                    )
                    # Continue with local deletion anyway - reconciliation job
                    # will clean up S3 later

            # Handle local deletion separately
            try:
                if os.path.exists(experiment_path):
                    shutil.rmtree(experiment_path)
                    logger.info(f"Deleted experiment data at: {experiment_path}")
                else:
                    logger.debug(f"Local path already deleted: {experiment_path}")
                local_result = True
            except PermissionError as pe:
                logger.error(
                    f"Permission denied deleting local data for '{self.unique_id}': "
                    f"{pe}. S3 deletion status: {s3_result}"
                )
                local_result = False
            except OSError as oe:
                logger.error(
                    f"OS error deleting local data for '{self.unique_id}': {oe}. "
                    f"S3 deletion status: {s3_result}"
                )
                local_result = False
            except Exception as local_error:
                logger.error(
                    f"Failed to delete local data for '{self.unique_id}': "
                    f"{local_error}. S3 deletion status: {s3_result}"
                )
                local_result = False

            # If S3 succeeded but local failed, return success since S3 is authoritative
            if s3_result and not local_result:
                logger.warning(
                    f"S3 deletion succeeded but local deletion failed for "
                    f"'{self.unique_id}'. Local data may remain orphaned at "
                    f"{experiment_path}"
                )
                # Return True since the important S3 data is deleted
                # Local cleanup can happen later via cleanup job
                return True

            return s3_result and local_result

        except Exception as e:
            logger.error(
                f"Failed to delete experiment '{self.unique_id}': {e}",
                exc_info=True,
            )
            return False

    async def _delete_remote_data_with_retry(self) -> bool:
        """
        Delete remote (S3) data with exponential backoff retry.

        Retries transient S3 failures up to S3_DELETION_MAX_RETRIES times
        with exponential backoff between attempts.

        Returns:
            True if deletion succeeded, False if all retries exhausted
        """
        for attempt in range(S3_DELETION_MAX_RETRIES):
            try:
                async with RemoteStorageDeleter(
                    self.remote_bucket_name, self.workspace_id, self.unique_id
                ) as remote_storage_controller:
                    result = await remote_storage_controller.delete_experiment(
                        self.workspace_id, self.unique_id
                    )
                    if result:
                        if attempt > 0:
                            logger.info(
                                f"S3 deletion succeeded on attempt {attempt + 1} "
                                f"for experiment '{self.unique_id}'"
                            )
                        return True
                    else:
                        logger.warning(
                            f"S3 deletion attempt {attempt + 1} returned False "
                            f"for '{self.unique_id}'"
                        )

            except Exception as e:
                if attempt < S3_DELETION_MAX_RETRIES - 1:
                    delay = S3_DELETION_BASE_DELAY_SECONDS * (2**attempt)
                    logger.warning(
                        f"S3 deletion attempt {attempt + 1} failed for "
                        f"'{self.unique_id}': {e}. Retrying in {delay}s..."
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        f"S3 deletion failed after {S3_DELETION_MAX_RETRIES} "
                        f"attempts for '{self.unique_id}': {e}"
                    )

        return False

    async def rename(self, new_name: str) -> ExptConfig:
        # Operate remote storage data.
        if RemoteStorageController.is_available():
            # Check for remote-sync-lock-file
            # - If lock file exists, an exception is raised (raise_error=True)
            RemoteSyncLockFileUtil.check_sync_lock_file(
                self.workspace_id, self.unique_id, raise_error=True
            )

        # validate params
        new_name = "" if new_name is None else new_name  # filter None

        # Overwrite experiment config
        update_params = {"name": new_name}
        ExptConfigWriter(self.workspace_id, self.unique_id).overwrite(update_params)

        # Update ExperimentRecord
        if ExperimentRecordService.is_available():
            ExperimentRecordService.update_name(
                self.workspace_id, self.unique_id, new_name
            )

        # Operate remote storage data.
        if RemoteStorageController.is_available():
            # upload latest EXPERIMENT_YML
            async with RemoteStorageWriter(
                self.remote_bucket_name, self.workspace_id, self.unique_id
            ) as remote_storage_controller:
                await remote_storage_controller.upload_experiment(
                    self.workspace_id, self.unique_id, [DIRPATH.EXPERIMENT_YML]
                )

        return ExptConfigReader.read(self.workspace_id, self.unique_id)

    async def copy_data(self, new_unique_id: str, new_name: str) -> bool:
        logger = AppLogger.get_logger()

        try:
            if RemoteStorageController.is_available():
                # Check for remote-sync-lock-file
                # - If lock file exists, an exception is raised (raise_error=True)
                RemoteSyncLockFileUtil.check_sync_lock_file(
                    self.workspace_id, self.unique_id, raise_error=True
                )

            # Define file paths
            output_dir = join_filepath(
                [DIRPATH.OUTPUT_DIR, self.workspace_id, self.unique_id]
            )
            new_output_dir = join_filepath(
                [DIRPATH.OUTPUT_DIR, self.workspace_id, new_unique_id]
            )

            # Copy directory
            shutil.copytree(output_dir, new_output_dir)

            # Update experiment configuration and unique IDs
            if not self.__copy_data_update_experiment_config_name(
                self.workspace_id, new_unique_id, new_name
            ):
                logger.error("Failed to update experiment.yml after copying.")
                return False

            if not self.__copy_data_replace_unique_id(
                new_output_dir, self.unique_id, new_unique_id
            ):
                logger.error("Failed to update unique_id in files.")
                return False

            # Operate remote storage data.
            if RemoteStorageController.is_available():
                # Upload a new data with new unique id to remote data
                async with RemoteStorageWriter(
                    self.remote_bucket_name, self.workspace_id, new_unique_id
                ) as remote_storage_controller:
                    await remote_storage_controller.upload_experiment(
                        self.workspace_id, new_unique_id
                    )

            logger.debug(f"Data successfully copied to {new_output_dir}")
            return True

        except Exception as e:
            logger.error(f"Error copying data: {e}")
            return False

    def __copy_data_replace_unique_id(
        self, directory: str, old_id: str, new_id: str
    ) -> bool:
        logger = AppLogger.get_logger()

        try:
            targeted_files = {
                "yaml": glob.glob(
                    os.path.join(directory, "**", "*.yaml"), recursive=True
                ),
                "npy": glob.glob(
                    os.path.join(directory, "**", "*.npy"), recursive=True
                ),
                "pkl": glob.glob(
                    os.path.join(directory, "**", "*.pkl"), recursive=True
                ),
            }

            for file_type, files in targeted_files.items():
                for file in files:
                    if file_type == "yaml":
                        self.__copy_data_update_yaml(file, old_id, new_id)
                    elif file_type == "pkl":
                        self.__copy_data_update_pickle(file, old_id, new_id)
                    elif file_type == "npy":
                        self.__copy_data_update_npy(file, old_id, new_id)

            logger.debug("All relevant files updated successfully.")
            return True

        except Exception as e:
            logger.error(f"Error replacing unique_id in files: {e}")
            return False

    def __copy_data_update_yaml(self, file_path: str, old_id: str, new_id: str) -> None:
        logger = AppLogger.get_logger()
        try:
            with open(file_path, "r", encoding="utf-8") as file:
                content = file.read()

            # Use the provided regex pattern for replacement
            pattern = re.compile(rf"(?:\b|\/){re.escape(old_id)}(?:\b|\/)(?:[^\s]*)")
            updated_content = pattern.sub(
                lambda match: match.group(0).replace(old_id, new_id), content
            )

            with open(file_path, "w", encoding="utf-8") as file:
                file.write(updated_content)

            logger.debug(f"Updated YAML: {file_path}")
        except Exception as e:
            logger.warning(f"Failed to update YAML {file_path}: {e}")

    def __copy_data_update_pickle(
        self, file_path: str, old_id: str, new_id: str
    ) -> None:
        logger = AppLogger.get_logger()
        try:
            with open(file_path, "rb") as file:
                data = pickle.load(file)

            updated_data = self.__replace_ids_recursive(data, old_id, new_id)
            with open(file_path, "wb") as file:
                pickle.dump(updated_data, file)

            logger.debug(f"Updated Pickle: {file_path}")
        except Exception as e:
            logger.warning(f"Failed to update Pickle {file_path}: {e}")

    def __copy_data_update_npy(self, file_path: str, old_id: str, new_id: str) -> None:
        logger = AppLogger.get_logger()
        try:
            with open(file_path, "rb") as file:
                data = np.load(file, allow_pickle=True)

            updated_data = self.__replace_ids_recursive(data, old_id, new_id)
            with open(file_path, "wb") as file:
                np.save(file, updated_data, allow_pickle=True)

            logger.debug(f"Updated NPY: {file_path}")
        except Exception as e:
            logger.warning(f"Failed to update NPY {file_path}: {e}")

    def __replace_ids_recursive(
        self, obj: object, old_id: str, new_id: str, visited: set = None
    ):
        if visited is None:
            visited = set()

        obj_id = id(obj)
        if obj_id in visited:
            return obj
        visited.add(obj_id)

        if isinstance(obj, dict):
            return {
                key: self.__replace_ids_recursive(value, old_id, new_id, visited)
                for key, value in obj.items()
            }
        elif isinstance(obj, list):
            return [
                self.__replace_ids_recursive(item, old_id, new_id, visited)
                for item in obj
            ]
        elif isinstance(obj, tuple):
            # Convert tuple to list, process, and convert back to tuple
            return tuple(
                self.__replace_ids_recursive(item, old_id, new_id, visited)
                for item in obj
            )
        elif isinstance(obj, np.ndarray):
            if obj.ndim == 0:  # Handle 0D (scalar-like) arrays
                return self.__replace_ids_recursive(obj.item(), old_id, new_id, visited)
            else:  # Handle 1D or higher-dimensional arrays
                return np.array(
                    [
                        self.__replace_ids_recursive(item, old_id, new_id, visited)
                        for item in obj
                    ]
                )
        elif isinstance(obj, str) and old_id in obj:
            # Use the custom regex pattern for strings
            pattern = re.compile(rf"(?:\b|\/){re.escape(old_id)}(?:\b|\/)(?:[^\s]*)")
            return pattern.sub(
                lambda match: match.group(0).replace(old_id, new_id), obj
            )
        elif hasattr(obj, "__dict__"):
            # Process custom objects
            for attr, value in obj.__dict__.items():
                setattr(
                    obj,
                    attr,
                    self.__replace_ids_recursive(value, old_id, new_id, visited),
                )
            return obj
        elif obj is None:
            return None
        else:
            return obj

    def __copy_data_update_experiment_config_name(
        self, workspace_id: str, unique_id: str, new_name: str
    ) -> bool:
        logger = AppLogger.get_logger()

        try:
            # Overwrite experiment config
            update_params = {"name": new_name}
            ExptConfigWriter(workspace_id, unique_id).overwrite(update_params)

            logger.info(f"Updated experiment.yml: {workspace_id}/{unique_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to update experiment.yml: {e}")
            return False
