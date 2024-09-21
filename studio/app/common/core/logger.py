import logging
import logging.config
import os
import sys

import watchtower  # Import watchtower for CloudWatch logging
import yaml

from studio.app.common.core.mode import MODE
from studio.app.dir_path import DIRPATH


class AppLogger:
    """
    Generic Application Logger
    """

    LOGGER_NAME = "optinist"

    @staticmethod
    def init_logger():
        """
        Note #1.
            At the time of starting to use this Logger,
            the logging initialization process has already been performed
            at the following location,
            so no explicit initialization process is required.

            - logger initialization location
              - Web App ... studio.__main_unit__
              - Batch App ... studio.app.optinist.core.expdb.batch_runner

        Note #2.
            However, only in the case of the snakemake process,
            the initialization process is required because it is a separate process.
        """

        log_config_file = (
            f"{DIRPATH.CONFIG_DIR}/logging.yaml"
            if MODE.IS_STANDALONE
            else f"{DIRPATH.CONFIG_DIR}/logging.multiuser.yaml"
        )

        if os.path.exists(log_config_file):
            with open(log_config_file) as file:
                log_config = yaml.load(file.read(), yaml.FullLoader)

                # Create log output directory (if none exists)
                log_file = (
                    log_config.get("handlers", {})
                    .get("rotating_file", {})
                    .get("filename")
                )
                if log_file:
                    log_dir = os.path.dirname(log_file)
                    if not os.path.isdir(log_dir):
                        os.makedirs(log_dir)

                logging.config.dictConfig(log_config)
        else:
            # Fallback configuration if the YAML file is not found
            logging.basicConfig(
                format=(
                    "[%(asctime)s] %(levelname)s in %(module)s:"
                    "%(funcName)s:%(lineno)d: %(message)s"
                ),
                datefmt="%Y-%m-%d %H:%M:%S",
                stream=sys.stderr,
                level=logging.INFO,
            )

        # Configure CloudWatch logging
        logging.getLogger().addHandler(
            watchtower.CloudWatchLogHandler(
                log_group=os.environ.get(
                    "CLOUDWATCH_LOG_GROUP", "/ecs/optinist-cloud-taskdef"
                ),
                stream_name=os.environ.get(
                    "CLOUDWATCH_STREAM_NAME", "optinist-cloud-stream"
                ),
            )
        )

    @staticmethod
    def get_logger():
        logger = logging.getLogger(__class__.LOGGER_NAME)

        # If before initialization, call init
        if not logger.handlers:
            __class__.init_logger()

        return logger
