import hashlib
import logging
import logging.config
import os
import platform
import traceback
from contextvars import ContextVar
from typing import Optional

import yaml

from studio.app.common.core.mode import MODE
from studio.app.common.core.platform_metadata import ECS_TASK_ID
from studio.app.common.core.utils.datetime_utils import get_current_datetime_formatted
from studio.app.dir_path import DIRPATH

LOGGING_CLIENT_ID_KEY = "client_id"

# Context variable for storing client_id per request/context
_client_id_context: ContextVar[Optional[str]] = ContextVar(
    LOGGING_CLIENT_ID_KEY, default=None
)

VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class LoggingConfigHelper:
    """
    Common utility class for logging configuration processing

    Note:
      - This class may be used in forked repositories.
        Therefore, care must be taken when changing the interface of this class
        (logging configuration) to avoid breaking changes.
    """

    @staticmethod
    def load_and_configure_logging_config(
        config_file: str,
        base_dir: str,
        apply_concurrent: bool = True,
        filename_modifier: callable = None,
    ) -> dict:
        """
        Load logging config from YAML file and apply standard configurations

        This is a unified method that combines:
        1. Loading YAML config
        2. Applying concurrent handler if supported
        3. Adjusting log file path and creating directory

        Args:
            config_file: Path to logging config YAML file
            base_dir: Base directory for log files (e.g., DIRPATH.DATA_DIR)
            apply_concurrent: Whether to apply concurrent handler (default: True)
            filename_modifier: Optional callable to modify filename
                              Function signature: (filename: str) -> str

        Returns:
            Configured logging configuration dictionary
        """
        # Load logging config from YAML
        with open(config_file, encoding="utf-8") as f:
            logging_config = yaml.safe_load(f.read())

        # Apply filename modifier if provided
        if filename_modifier:
            log_file = (
                logging_config.get("handlers", {})
                .get("rotating_file", {})
                .get("filename")
            )
            if log_file:
                modified_filename = filename_modifier(log_file)
                logging_config["handlers"]["rotating_file"][
                    "filename"
                ] = modified_filename

        # Apply concurrent handler configuration and cleanup
        logging_config = LoggingConfigHelper._apply_concurrent_handler_if_supported(
            logging_config, apply_concurrent
        )

        # Adjust log file path and create directory
        logging_config = LoggingConfigHelper._adjust_log_file_path(
            logging_config, base_dir
        )

        return logging_config

    @staticmethod
    def _is_native_windows() -> bool:
        """
        Check if running on native Windows (not WSL)

        Returns:
            True if running on native Windows, False otherwise
        """
        if platform.system() != "Windows":
            return False

        # Check WSL Platform
        if "WSL_DISTRO_NAME" in os.environ or "WSL_INTEROP" in os.environ:
            return False

        return True

    @staticmethod
    def _apply_concurrent_handler_if_supported(
        logging_config: dict, apply_concurrent: bool = True
    ) -> dict:
        """
        Apply concurrent rotating file handler if supported by the platform
        and cleanup temporary handler configuration

        Args:
            logging_config: Logging configuration dictionary
            apply_concurrent: Whether to apply concurrent handler (default: True)

        Returns:
            Modified logging configuration dictionary
        """
        logging_handlers = logging_config.get("handlers", {})

        # Switch rotating_file to concurrent handler for multi-process support
        if apply_concurrent:
            if LoggingConfigHelper._is_native_windows():
                # ATTENTION:
                # On the Windows Native Platform, "rotating_file_concurrency"
                # is currently not supported because pywin32 is required to use
                # concurrent_log_handler. (which is not installed in the conda env).
                pass
            else:
                if ("rotating_file" in logging_handlers) and (
                    "rotating_file_concurrency" in logging_handlers
                ):
                    logging_config["handlers"]["rotating_file"] = logging_config[
                        "handlers"
                    ]["rotating_file_concurrency"]

        # Delete unnecessary items (always cleanup, regardless of apply_concurrent)
        if "rotating_file_concurrency" in logging_handlers:
            del logging_config["handlers"]["rotating_file_concurrency"]

        return logging_config

    @staticmethod
    def _adjust_log_file_path(logging_config: dict, base_dir: str) -> dict:
        """
        Adjust log file path to absolute path and create log directory if needed

        Args:
            logging_config: Logging configuration dictionary
            base_dir: Base directory for log files (e.g., DIRPATH.DATA_DIR)

        Returns:
            Modified logging configuration dictionary
        """
        log_file = (
            logging_config.get("handlers", {}).get("rotating_file", {}).get("filename")
        )
        if log_file:
            # Adjust to absolute path
            log_file = f"{base_dir}/{log_file}"
            logging_config["handlers"]["rotating_file"]["filename"] = log_file

            # Create log output directory if it doesn't exist
            # ※ Must be done before logging.config.dictConfig()
            log_dir = os.path.dirname(log_file)
            if log_dir and not os.path.isdir(log_dir):
                os.makedirs(log_dir, exist_ok=True)

        return logging_config

    @staticmethod
    def _apply_log_level_override(logging_config: dict, log_level: str) -> dict:
        """Override all logger and handler levels with the given level.

        Applied once at startup; requires a process restart to change.
        In ECS, LOG_LEVEL is set in the task definition (compute.tf /
        background_service.tf). Locally, use --log-level or the env var.
        """
        level = log_level.upper()
        if level not in VALID_LOG_LEVELS:
            import warnings

            warnings.warn(f"Invalid LOG_LEVEL '{log_level}', using YAML defaults")
            return logging_config

        if "root" in logging_config:
            logging_config["root"]["level"] = level

        for logger_cfg in logging_config.get("loggers", {}).values():
            logger_cfg["level"] = level

        for handler_cfg in logging_config.get("handlers", {}).values():
            if "level" in handler_cfg:
                handler_cfg["level"] = level

        return logging_config


class AppLogger:
    """
    Generic Application Logger
    """

    LOGGER_NAME = "optinist"
    _initialized = False  # Flag to track if logger has been initialized

    class ClientIdFilter(logging.Filter):
        """
        Logging filter to inject client_id and ecs_task_id
        from context into log records.
        """

        # Alternate text to log if client_id is not obtained
        NO_CLIENT_ID_DEFAULT_VALUE = "default"

        def filter(self, record):
            # Get client_id from context, default to "none" if not set
            client_id = _client_id_context.get()
            record.client_id = (
                client_id
                if client_id is not None
                else __class__.NO_CLIENT_ID_DEFAULT_VALUE
            )
            record.ecs_task_id = ECS_TASK_ID
            return True

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
                (optinist-for-server)

        Note #2.
            However, only in the case of the snakemake process,
            the initialization process is required because it is a separate process.
        """

        # Skip if already initialized
        if cls._initialized:
            return

        # read logging config
        logging_config = cls.get_logging_config()

        # set logging config
        logging.config.dictConfig(logging_config)

        # Mark as initialized
        cls._initialized = True

    @staticmethod
    def get_logging_config() -> dict:
        logging_config_file = (
            f"{DIRPATH.CONFIG_DIR}/logging.yaml"
            if MODE.IS_STANDALONE
            else f"{DIRPATH.CONFIG_DIR}/logging.multiuser.yaml"
        )

        # Load and configure logging config (using unified utility method)
        logging_config = LoggingConfigHelper.load_and_configure_logging_config(
            config_file=logging_config_file,
            base_dir=DIRPATH.DATA_DIR,
            apply_concurrent=True,
        )

        # Add ClientIdFilter configuration to filters section
        CLIENT_ID_FILTER_NAME = "client_id_filter"
        if "filters" not in logging_config:
            logging_config["filters"] = {}
        logging_config["filters"][CLIENT_ID_FILTER_NAME] = {
            "()": "studio.app.common.core.logger.AppLogger.ClientIdFilter"
        }

        # Add client_id_filter to all handlers
        for handler_config in logging_config.get("handlers", {}).values():
            if "filters" not in handler_config:
                handler_config["filters"] = []
            if CLIENT_ID_FILTER_NAME not in handler_config["filters"]:
                handler_config["filters"].append(CLIENT_ID_FILTER_NAME)

        # NOTE: LOG_LEVEL env var controls the frontend log viewer filter,
        # not the Python logging level. Python always uses YAML defaults
        # (DEBUG for optinist/snakemake) so all logs reach CloudWatch.

        return logging_config

    @classmethod
    def get_logger(cls) -> logging.Logger:
        logger = logging.getLogger(cls.LOGGER_NAME)

        # If not initialized yet, call init
        if not cls._initialized:
            cls.init_logger()

        return logger

    @staticmethod
    def set_client_id(client_id: Optional[str]):
        """
        Set client_id in the current context for logging
        """
        _client_id_context.set(client_id)

    @staticmethod
    def get_client_id() -> Optional[str]:
        """
        Get client_id from the current context
        """
        return _client_id_context.get()

    @staticmethod
    def generate_client_id(uid: str, auto_refresh: bool = False) -> str:
        """
        Generate a client_id for logging based on the specified uid.

        Args:
            uid: User ID to generate client_id from
            auto_refresh: If true, refresh id at period interval (weekly)

        Returns:
            Generated client_id (16 character hash)
        """
        if not uid:
            return uid

        if auto_refresh:
            datetime_str = get_current_datetime_formatted(
                "%Y-%m-%d-%w"
            )  # Refresh by period (weekly)
            hash_source = f"{uid}-{datetime_str}"
        else:
            hash_source = uid

        # SHA-256 rather than MD5: the digest is derived from a user id, and
        # truncating to 16 hex chars keeps the existing id length.
        client_id = hashlib.sha256(hash_source.encode()).hexdigest()
        client_id = client_id[0:16]

        return client_id

    @staticmethod
    def format_exc_traceback(e: Exception):
        return "{}: {}\n{}".format(type(e), e, traceback.format_exc())
