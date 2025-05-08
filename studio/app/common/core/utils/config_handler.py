import os
from typing import Union

import yaml
from filelock import FileLock

from studio.app.common.core.utils.filelock_handler import FileLockUtils
from studio.app.common.core.utils.filepath_creater import (
    create_directory,
    join_filepath,
)


class ConfigReader:
    @classmethod
    def read(cls, file: Union[str, bytes]):
        config = {}

        if file is None:
            return config

        if isinstance(file, bytes):
            config = yaml.safe_load(file)
        elif isinstance(file, str) and os.path.exists(file):
            with open(file) as f:
                config = yaml.safe_load(f)
        else:
            assert False, f"Invalid file [{file}]"

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
