from typing import Dict, Union

from studio.app.common.core.experiment.experiment import ExptConfig, ExptFunction
from studio.app.common.core.utils.config_handler import ConfigReader
from studio.app.common.core.utils.filepath_creater import join_filepath
from studio.app.common.core.workflow.workflow import NodeRunStatus, OutputPath
from studio.app.dir_path import DIRPATH


class ExptConfigReader:
    @classmethod
    def read(cls, file: Union[str, bytes]) -> ExptConfig:
        config = ConfigReader.read(file)
        assert config, f"Invalid config yaml file: [{file}] [{config}]"

        return ExptConfig(
            workspace_id=config["workspace_id"],
            unique_id=config["unique_id"],
            name=config["name"],
            started_at=config["started_at"],
            finished_at=config.get("finished_at"),
            success=config.get("success", NodeRunStatus.RUNNING.value),
            hasNWB=config["hasNWB"],
            function=cls.read_function(config["function"]),
            nwb=config.get("nwb"),
            snakemake=config.get("snakemake"),
            data_usage=config.get("data_usage"),
        )

    @staticmethod
    def read_raw(workspace_id: str, unique_id: str) -> dict:
        config = ConfigReader.read(
            join_filepath(
                [DIRPATH.OUTPUT_DIR, workspace_id, unique_id, DIRPATH.EXPERIMENT_YML]
            )
        )
        return config

    @classmethod
    def read_function(cls, config) -> Dict[str, ExptFunction]:
        return {
            key: ExptFunction(
                unique_id=value["unique_id"],
                name=value["name"],
                started_at=value.get("started_at"),
                finished_at=value.get("finished_at"),
                success=value.get("success", NodeRunStatus.RUNNING.value),
                hasNWB=value["hasNWB"],
                message=value.get("message"),
                outputPaths=cls.read_output_paths(value.get("outputPaths")),
            )
            for key, value in config.items()
        }

    @classmethod
    def read_output_paths(cls, config) -> Dict[str, OutputPath]:
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
