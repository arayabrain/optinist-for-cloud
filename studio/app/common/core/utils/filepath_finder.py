import datetime
import os
import re
from glob import glob
from typing import Optional

from studio.app.common.core.utils.filepath_creater import join_filepath
from studio.app.dir_path import CORE_PARAM_PATH, DIRPATH


def find_filepath(name, category) -> Optional[str]:
    name, _ = os.path.splitext(name)

    filepaths = glob(
        join_filepath(
            [DIRPATH.APP_DIR, "*", "wrappers", "**", category, f"{name}.yaml"]
        ),
        recursive=True,
    )
    return filepaths[0] if len(filepaths) > 0 else None


def find_param_filepath(name: str):
    if name in CORE_PARAM_PATH.__members__:
        return CORE_PARAM_PATH[name].value
    else:
        return find_filepath(name, "params")


def find_condaenv_filepath(name: str):
    return find_filepath(name, "conda")


def find_recent_updated_files(
    find_root_dir: str,
    threshold_minutes: int,
    exclude_files: list = [],
    do_relative_path: bool = False,
):
    """
    Search for files that have been updated since the specified time.
    """
    time_threshold = datetime.datetime.now() - datetime.timedelta(
        minutes=threshold_minutes
    )
    recent_files = []

    # search under directories
    for root, dirs, files in os.walk(find_root_dir):
        for file in files:
            file_path = os.path.join(root, file)

            # check exclude file
            is_exclude = False
            for exclude_file in exclude_files:
                if exclude_file in file_path:
                    is_exclude = True
                    break
            if is_exclude:
                continue

            # get mtime
            file_mtime = os.path.getmtime(file_path)
            file_mtime_dt = datetime.datetime.fromtimestamp(file_mtime)

            # compare mtime
            if file_mtime_dt > time_threshold:
                result_file_path = (
                    re.sub(f"^{find_root_dir}/", "", file_path)
                    if do_relative_path
                    else file_path
                )
                recent_files.append(result_file_path)

    return recent_files
