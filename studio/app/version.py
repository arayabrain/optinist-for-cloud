import json
import logging
import os
import re

from studio.app.dir_path import DIRPATH

logger = logging.getLogger(__name__)


def get_app_version_from_pyproject() -> str:
    """Extract version from pyproject.toml."""
    # Look for pyproject.toml in parent directories
    pyproject_path = os.path.join(DIRPATH.ROOT_DIR, "pyproject.toml")

    # Read and parse the version from pyproject.toml
    with open(pyproject_path, "r") as f:
        content = f.read()

    # Use regex to extract version
    version_match = re.search(r'version\s*=\s*["\']([^"\']+)["\']', content)
    if version_match:
        version = version_match.group(1)
        return version
    else:
        return "1.0.0"


def _load_build_info() -> dict:
    """Load build metadata written by the Dockerfile at build time."""
    build_info_path = os.path.join(DIRPATH.ROOT_DIR, "BUILD_INFO")
    try:
        with open(build_info_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.debug(f"Could not load BUILD_INFO: {e}")
        return {}


def _field(data: dict, key: str) -> str:
    """Read a BUILD_INFO field, normalising 'not recorded' to an empty string.

    A field can be missing (BUILD_INFO from an older image), empty (the build
    genuinely had no branch or no tag) or the literal "unknown" (the Docker ARG
    default, i.e. the build never passed the value in). All of these mean the
    same thing to a reader, so they collapse to "".

    "HEAD" collapses too: images built before the branch was resolved with
    `git symbolic-ref` recorded that string for every tag checkout, and it
    names no ref at all.

    Anything that is not a string collapses as well, so a hand-edited or
    truncated BUILD_INFO cannot put a non-string onto a class attribute the
    rest of the code treats as one.
    """
    value = data.get(key, "")
    if not isinstance(value, str) or value in ("", "unknown", "HEAD"):
        return ""
    return value


def _derive_git_ref(commit: str, branch: str, tag: str) -> str:
    """Summarise which git ref the image was built from.

    A tag checkout leaves HEAD detached, so a tag-based build records a tag and
    no branch, while a branch build usually records the opposite:

        v1.1.10              built from a tag
        develop-main         built from a branch
        develop-main (v1.1.10)   built from a branch whose HEAD also has a tag
        detached@0cf95d0d    detached HEAD with no tag on it

    The third case is why the branch is not simply dropped when a tag exists:
    reporting a bare "v1.1.10" there would misread as a tag checkout, which is
    the exact confusion this whole field is meant to remove.

    Derived on read rather than stored in BUILD_INFO on purpose: BUILD_INFO is
    written once into an image and can never be corrected, so baking a derived
    value in would freeze this rule alongside every image ever built.
    """
    if branch and tag:
        return f"{branch} ({tag})"
    if tag:
        return tag
    if branch:
        return branch
    if commit:
        return f"detached@{commit[:8]}"
    return "N/A"


class Version:
    APP_VERSION = get_app_version_from_pyproject()


class BuildInfo:
    _data = _load_build_info()
    GIT_COMMIT = _data.get("git_commit", "N/A")
    GIT_BRANCH = _field(_data, "git_branch")
    GIT_TAG = _field(_data, "git_tag")
    GIT_REF = _derive_git_ref(_field(_data, "git_commit"), GIT_BRANCH, GIT_TAG)
    BUILD_TIMESTAMP = _data.get("build_timestamp", "N/A")
