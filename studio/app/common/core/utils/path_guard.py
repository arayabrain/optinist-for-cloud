import os

from fastapi import HTTPException, status

from studio.app.common.core.utils.filepath_creater import (
    join_filepath,
    normalize_output_path,
)
from studio.app.dir_path import DIRPATH

_INVALID_PATH_MESSAGE = "Invalid path parameter"

# Request parameters reach the filesystem through join_filepath(), which uses
# "/".join() rather than os.path.join(), so an absolute component cannot reset
# the base directory and ".." is the only way out of it. The helpers below
# close that off at the router layer, where the request value first arrives,
# instead of at each of the file operations downstream.
#
# Both normalise the path and then check the result with startswith(): that is
# the shape CodeQL's py/path-injection query recognises as a sanitizer, so the
# guard is visible to static analysis as well as effective at runtime.


def secure_component(value: str) -> str:
    """Validate a single path segment (workspace_id, unique_id, node_id, ...).

    Returns the segment unchanged, or raises 400 when it is empty, nested or
    would traverse out of its parent directory.
    """
    normalized = os.path.normpath(os.path.join(os.sep, value))
    if not normalized.startswith(os.sep):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=_INVALID_PATH_MESSAGE
        )

    component = normalized[len(os.sep) :]
    # Anything normalisation had to rewrite ("..", "//", a leading separator)
    # was not a plain segment to begin with, so reject rather than silently
    # accept the rewritten value.
    if not component or component != value or os.sep in component:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=_INVALID_PATH_MESSAGE
        )

    return component


def secure_relpath(base: str, relpath: str) -> str:
    """Validate a multi-segment relative path used under `base`.

    Returns the path relative to `base`, or raises 400 when it escapes `base`.
    """
    prefix = os.path.normpath(base) + os.sep
    normalized = os.path.normpath(os.path.join(base, relpath))
    if not normalized.startswith(prefix):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=_INVALID_PATH_MESSAGE
        )

    return normalized[len(prefix) :]


def secure_output_relpath(path: str) -> str:
    """Validate a client-supplied visualisation path.

    Accepts the absolute forms older DB records still carry, and returns the
    path relative to OUTPUT_DIR so callers can keep using join_filepath().
    """
    return secure_relpath(DIRPATH.OUTPUT_DIR, normalize_output_path(path))


def secure_input_relpath(workspace_id: str, path: str) -> str:
    """Validate a client-supplied input path, relative to the workspace.

    `workspace_id` must already have passed secure_component(); it is part of
    the base the path is checked against, so an unchecked value would let the
    path land in another workspace.
    """
    return secure_relpath(join_filepath([DIRPATH.INPUT_DIR, workspace_id]), path)
