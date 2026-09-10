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
    """Load build metadata written by the Dockerfile at build time.

    Always returns a mapping. `json.load` also succeeds on `[]`, `null` and a
    bare string, and BuildInfo below reads its fields in the class body — which
    runs on import, from __main_unit__, at startup. Letting a non-object
    through would turn a damaged BUILD_INFO into an import-time crash of the
    whole application rather than a degraded log line.
    """
    build_info_path = os.path.join(DIRPATH.ROOT_DIR, "BUILD_INFO")
    try:
        with open(build_info_path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.debug(f"Could not load BUILD_INFO: {e}")
        return {}


# Placeholder strings a field is known to have carried in some already-built
# image, listed per field rather than globally. `unknown` and `HEAD` are both
# creatable ref names (`git tag unknown`, `git tag HEAD`), so collapsing them
# everywhere would discard a real value:
#
#   git_commit      "unknown" was the Docker ARG default, and a commit hash can
#                   never be that string, so there is no real value to lose.
#   git_branch      "unknown" was the ARG default; "HEAD" is what
#                   `git rev-parse --abbrev-ref` recorded for every tag
#                   checkout before this change. A branch genuinely named
#                   `unknown` or `HEAD` is collapsed too — accepted, because
#                   images carrying the old sentinels exist and such branches
#                   do not.
#   git_tag         introduced by this change and never shipped with a
#                   placeholder, so every non-empty value is a real tag.
#   build_timestamp "unknown" is still the ARG default today.
_PLACEHOLDERS = {
    "git_commit": ("unknown",),
    "git_branch": ("unknown", "HEAD"),
    "git_tag": (),
    "build_timestamp": ("unknown",),
}


def _field(data: dict, key: str) -> str:
    """Read a BUILD_INFO field, normalising 'not recorded' to an empty string.

    A field can be missing (BUILD_INFO from an older image), empty (the build
    genuinely had no branch or no tag) or hold one of the placeholders listed
    in `_PLACEHOLDERS` for that field. All of these mean the same thing to a
    reader, so they collapse to "".

    Anything that is not a string collapses as well, so a hand-edited or
    truncated BUILD_INFO cannot put a non-string onto a class attribute the
    rest of the code treats as one.
    """
    value = data.get(key, "")
    if not isinstance(value, str) or value == "":
        return ""
    return "" if value in _PLACEHOLDERS.get(key, ()) else value


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
    # Every field goes through _field, so a damaged BUILD_INFO cannot put a
    # non-string onto an attribute the startup log formats as one, and so the
    # commit cannot read "unknown" on one log line while the ref derived from
    # that same commit reads "N/A" on the next.
    _data = _load_build_info()
    GIT_COMMIT = _field(_data, "git_commit") or "N/A"
    GIT_BRANCH = _field(_data, "git_branch")
    GIT_TAG = _field(_data, "git_tag")
    GIT_REF = _derive_git_ref(_field(_data, "git_commit"), GIT_BRANCH, GIT_TAG)
    BUILD_TIMESTAMP = _field(_data, "build_timestamp") or "N/A"
