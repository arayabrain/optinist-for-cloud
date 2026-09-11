import os
import shutil

from studio.app.dir_path import DIRPATH


class InvalidPathError(ValueError):
    """A path built from request data would leave the directory it belongs to.

    A ValueError subclass so the snakemake rule processes, which import this
    module in conda environments without FastAPI, see an ordinary exception.
    __main_unit__ maps it to a 400 for requests.
    """


def join_filepath(path_list):
    if isinstance(path_list, str):
        joined = path_list
        base = path_list
    elif isinstance(path_list, list):
        assert path_list, "Path is empty"
        joined = "/".join(path_list)
        # A leading empty element comes from splitting an absolute path
        # ("/a/b".split("/") -> ["", "a", "b"]); its base is the root.
        base = path_list[0] or os.sep
    else:
        assert False, "Path is not list"

    # Reject ".." outright rather than merely containing it. A segment that
    # normalises back inside the base can still cross into another workspace:
    # [OUTPUT_DIR, "1", "../other/expt"] stays under OUTPUT_DIR but leaves
    # workspace 1.
    if ".." in joined.split("/"):
        raise InvalidPathError(f"path contains '..': {joined!r}")

    # "/".join, not os.path.join, so an absolute component never resets the
    # base; with ".." gone the result cannot escape. The normpath + startswith
    # pair is also the shape CodeQL's py/path-injection query recognises as a
    # sanitizer, which is what clears the alerts at every call site at once.
    normalized = os.path.normpath(joined)
    if not normalized.startswith(os.path.normpath(base)):
        raise InvalidPathError(f"path escapes its base directory: {joined!r}")

    return normalized


def create_filepath(dirname, filename):
    create_directory(dirname)

    return join_filepath([dirname, filename])


def get_pickle_file(workspace_id, unique_id, node_id, algo_name):
    return join_filepath([workspace_id, unique_id, node_id, f"{algo_name}.pkl"])


def create_directory(dirpath, delete_dir=False):
    if delete_dir and os.path.exists(dirpath):
        try:
            shutil.rmtree(dirpath)
        except FileNotFoundError:
            # Handle race condition where directory/files are deleted
            # by another process or due to filesystem delays
            pass

    os.makedirs(dirpath, exist_ok=True)


def normalize_output_path(path: str) -> str:
    """
    Convert absolute output path to relative path.

    Handles paths like:
    - /tmp/studio/output/93/tutorial1/... → 93/tutorial1/...
    - /app/studio_data/output/93/tutorial1/... → 93/tutorial1/...

    Args:
        path: The path to normalize (may be absolute or relative)

    Returns:
        Relative path without output directory prefix
    """
    if not path:
        return path

    # Strip OUTPUT_DIR prefix if present
    if path.startswith(DIRPATH.OUTPUT_DIR):
        return path[len(DIRPATH.OUTPUT_DIR) :].lstrip("/")

    return path


def resolve_absolute_output_path(filepath: str) -> str:
    """Normalize and convert to absolute path for filesystem operations."""
    filepath = normalize_output_path(filepath)
    return join_filepath([DIRPATH.OUTPUT_DIR, filepath])
