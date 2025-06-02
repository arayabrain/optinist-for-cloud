from datetime import datetime
from glob import glob
from typing import Optional

from studio.app.common.core.experiment.experiment import ExptConfig
from studio.app.common.core.experiment.experiment_reader import ExptConfigReader
from studio.app.const import DATE_FORMAT


class ExptUtils:
    @classmethod
    def get_last_experiment(cls, workspace_id: str):
        last_expt_config: Optional[ExptConfig] = None
        config_paths = glob(
            ExptConfigReader.get_experiment_yaml_wild_path(workspace_id)
        )

        for path in config_paths:
            config = ExptConfigReader.read_from_path(path)
            if not last_expt_config:
                last_expt_config = config
            elif datetime.strptime(config.started_at, DATE_FORMAT) > datetime.strptime(
                last_expt_config.started_at, DATE_FORMAT
            ):
                last_expt_config = config

        return last_expt_config
