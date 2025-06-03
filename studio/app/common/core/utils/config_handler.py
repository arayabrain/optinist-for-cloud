import os

import yaml
from filelock import FileLock

from studio.app.common.core.utils.filelock_handler import FileLockUtils
from studio.app.common.core.utils.filepath_creater import (
    create_directory,
    join_filepath,
)


class ConfigReader:
    @classmethod
    def read(cls, filepath: str) -> dict:
        config = {}

        if filepath is not None and os.path.exists(filepath):
            with open(filepath) as f:
                config = yaml.safe_load(f)

        return config

    @classmethod
    def read_from_bytes(cls, content: bytes) -> dict:
        config = yaml.safe_load(content)
        return config


class ConfigWriter:
    @classmethod
    def write(cls, dirname, filename, config):
        create_directory(dirname)

        config_path = join_filepath([dirname, filename])

        # Controls locking for simultaneous writing to yaml-file from multiple nodes.
        lock_path = FileLockUtils.get_lockfile_path(config_path)
        with FileLock(lock_path, timeout=10):
            with open(config_path, "w") as f:
                yaml.dump(config, f, sort_keys=False)
