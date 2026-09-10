import os
from dataclasses import asdict
from typing import Dict, Optional

import yaml

from studio.app.common.core.experiment.experiment import (
    ExptConfig,
    ExptFunction,
    ExptOutputPathIds,
)
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.storage.remote_storage_controller import (
    RemoteExperimentSyncMode,
    RemoteStorageReader,
)
from studio.app.common.core.utils.config_handler import ConfigReader
from studio.app.common.core.utils.datetime_utils import TIMEZONE_KEY
from studio.app.common.core.utils.filepath_creater import join_filepath
from studio.app.common.core.workflow.workflow import (
    NodeRunStatus,
    OutputPath,
    WorkflowRunStatus,
)
from studio.app.dir_path import DIRPATH

logger = AppLogger.get_logger()


class ExptConfigReader:
    @classmethod
    def get_config_yaml_path(cls, workspace_id: str, unique_id: str) -> str:
        path = join_filepath(
            [DIRPATH.OUTPUT_DIR, workspace_id, unique_id, DIRPATH.EXPERIMENT_YML]
        )
        return path

    @classmethod
    def get_config_yaml_wild_path(cls, workspace_id: str) -> str:
        path = join_filepath(
            [DIRPATH.OUTPUT_DIR, workspace_id, "*", DIRPATH.EXPERIMENT_YML]
        )
        return path

    @classmethod
    def get_local_experiment_uids(cls, workspace_id: str) -> set:
        """Return set of experiment UIDs that exist locally on filesystem.

        Uses glob to find experiment.yaml files and extracts UIDs from paths.
        """
        from glob import glob

        config_paths = glob(cls.get_config_yaml_wild_path(workspace_id))
        uids = set()
        for path in config_paths:
            try:
                ids = ExptOutputPathIds(os.path.dirname(path))
                if ids.unique_id:
                    uids.add(ids.unique_id)
            except Exception:
                pass
        return uids

    @classmethod
    async def ensure_synced_async(
        cls,
        workspace_id: str,
        unique_id: str,
        remote_bucket_name: str,
    ) -> bool:
        """
        Ensure experiment metadata exists locally, syncing from S3 if needed.

        Call this before read() in async contexts to handle cross-instance
        scenarios where a user has been migrated to a new instance.

        Args:
            workspace_id: Workspace ID containing the experiment
            unique_id: Unique ID of the experiment
            remote_bucket_name: S3 bucket name for the user

        Returns:
            True if config exists (or was synced), False if sync failed
        """
        from studio.app.common.core.storage.remote_storage_controller import (
            RemoteStorageController,
        )

        config_path = cls.get_config_yaml_path(workspace_id, unique_id)

        # If config exists locally, no sync needed
        if os.path.exists(config_path):
            return True

        # Check if remote storage is available
        if not RemoteStorageController.is_available():
            return False

        logger.info(
            f"Experiment config not found locally, syncing from S3: "
            f"[{workspace_id}/{unique_id}]"
        )

        try:
            async with RemoteStorageReader(
                remote_bucket_name,
                workspace_id,
                unique_id,
                sync_mode=RemoteExperimentSyncMode.METADATA_ONLY,
            ) as controller:
                # Download only this specific experiment's metadata (efficient)
                await controller.download_experiment_meta(workspace_id, unique_id)
            return os.path.exists(config_path)
        except Exception as e:
            logger.warning(f"Failed to sync experiment from S3: {e}", exc_info=True)
            return False

    @classmethod
    def read(cls, workspace_id: str, unique_id: str) -> ExptConfig:
        filepath = cls.get_config_yaml_path(workspace_id, unique_id)
        config = ConfigReader.read(filepath)
        assert config, f"Invalid config yaml file: [{filepath}] [{config}]"

        return cls._create_experiment_config(config)

    @classmethod
    def read_from_path(cls, filepath: str) -> ExptConfig:
        ids = ExptOutputPathIds(os.path.dirname(filepath))
        return cls.read(ids.workspace_id, ids.unique_id)

    @classmethod
    def create_empty_experiment_config(cls) -> ExptConfig:
        return ExptConfig(
            workspace_id=None,
            unique_id=None,
            name=None,
            started_at=None,
            finished_at=None,
            success=None,
            hasNWB=None,
            function=None,
            procs=None,
            nwb=None,
            snakemake=None,
            data_usage=None,
            timezone=None,
        )

    @classmethod
    def _create_experiment_config(cls, config: dict) -> ExptConfig:
        return ExptConfig(
            workspace_id=config["workspace_id"],
            unique_id=config["unique_id"],
            name=config["name"],
            started_at=config["started_at"],
            finished_at=config.get("finished_at"),
            success=config.get("success", NodeRunStatus.RUNNING.value),
            hasNWB=config["hasNWB"],
            function=cls.convert_function(config["function"]),
            procs=cls.convert_function(config.get("procs")),
            nwb=config.get("nwb"),
            snakemake=config.get("snakemake"),
            data_usage=config.get("data_usage"),
            timezone=config.get(TIMEZONE_KEY),
        )

    @classmethod
    def validate_experiment_config(cls, config: ExptConfig) -> bool:
        """
        ExptConfig content validation
        """
        config_dict = asdict(config)
        for field in ExptConfig.required_fields():
            if config_dict.get(field) is None:
                logger.warning(
                    f"ExptConfig.{field} is missing or None in experiment config"
                )
                return False

        return True

    @classmethod
    def convert_function(cls, config: dict) -> Dict[str, ExptFunction]:
        # Assuming the case where an empty value is specified, check here.
        # (For backward compatibility with config yaml)
        if not config:
            return {}

        return {
            key: ExptFunction(
                unique_id=value["unique_id"],
                name=value["name"],
                started_at=value.get("started_at"),
                finished_at=value.get("finished_at"),
                success=value.get("success", NodeRunStatus.RUNNING.value),
                hasNWB=value["hasNWB"],
                message=value.get("message"),
                outputPaths=cls.convert_output_paths(value.get("outputPaths")),
            )
            for key, value in config.items()
        }

    @classmethod
    def convert_output_paths(cls, config: dict) -> Dict[str, OutputPath]:
        if config:
            return {
                key: OutputPath(
                    path=value["path"],
                    type=value["type"],
                    max_index=value["max_index"],
                    data_shape=value.get("data_shape"),
                )
                for key, value in config.items()
            }
        else:
            return None

    @classmethod
    def read_experiment_status(
        cls, workspace_id: str, unique_id: str
    ) -> Optional[WorkflowRunStatus]:
        try:
            config = cls.read(workspace_id=workspace_id, unique_id=unique_id)
            if config.success is None:
                return None
            return WorkflowRunStatus(config.success)
        except AssertionError:
            # A missing or empty experiment.yaml means "no status yet",
            # not corruption.
            logger.debug(
                f"experiment.yaml is missing or empty: [{workspace_id}/{unique_id}]"
            )
            return None
        except (ValueError, yaml.YAMLError) as e:
            logger.warning(
                f"experiment config read error: [{workspace_id}/{unique_id}] {e}"
            )
            return None
