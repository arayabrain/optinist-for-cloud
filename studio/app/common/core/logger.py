import logging
import logging.config
import os
import sys
import uuid

import watchtower
import yaml

from studio.app.common.core.mode import MODE
from studio.app.dir_path import DIRPATH


class AppLogger:
    """
    Generic Application Logger
    """

    LOGGER_NAME = "optinist"
    _cloudwatch_configured = False

    @classmethod
    def init_logger(cls):
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
        log_group = os.environ.get(
            "CLOUDWATCH_LOG_GROUP", "/ecs/optinist-cloud-taskdef"
        )
        stream_name = os.environ.get("CLOUDWATCH_STREAM_NAME", "optinist-cloud-stream")
        # Create a unique stream name if not provided
        if stream_name == "optinist-cloud-stream":
            stream_name = f"optinist-cloud-stream-{uuid.uuid4()}"
        if not cls._cloudwatch_configured:
            try:
                cloudwatch_handler = watchtower.CloudWatchLogHandler(
                    log_group=log_group,
                    stream_name=stream_name,
                    create_log_group=True,  # Create the log group if it doesn't exist
                )
                logging.getLogger().addHandler(cloudwatch_handler)
                logging.info("CloudWatch logging configured.")
                logging.info(f"Log Group: {log_group}, Stream: {stream_name}")
                cls._cloudwatch_configured = True
            except Exception as e:
                logging.error(f"Failed to configure CloudWatch logging: {str(e)}")

    @classmethod
    def get_logger(cls):
        logger = logging.getLogger(__class__.LOGGER_NAME)

        # If before initialization, call init
        if not logger.handlers:
            __class__.init_logger()

        return logger
