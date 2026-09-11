"""join_filepath is the single chokepoint every request path passes through.

Guarding it there rather than at each router means a new endpoint cannot
forget the check. These tests pin what it accepts, what it refuses, and the
string it returns -- 185 call sites depend on the last one.
"""
import os

import pytest

from studio.app.common.core.utils.filepath_creater import (
    InvalidPathError,
    join_filepath,
)
from studio.app.dir_path import DIRPATH

OUT = DIRPATH.OUTPUT_DIR


def test_a_plain_join_is_unchanged():
    assert join_filepath([OUT, "1", "abc123"]) == f"{OUT}/1/abc123"


def test_a_relative_join_stays_relative():
    """Callers build workspace-relative keys as well as absolute paths."""
    assert join_filepath(["1", "uid", "node", "x.pkl"]) == "1/uid/node/x.pkl"


def test_a_single_string_passes_through():
    assert join_filepath(f"{OUT}/1/uid") == f"{OUT}/1/uid"


def test_an_absolute_path_split_into_parts_is_rebuilt():
    """PickleWriter passes `path.split("/")[:-1]`, whose first element is the
    empty string left by the leading separator."""
    parts = f"{OUT}/1/uid/node/x.pkl".split("/")[:-1]
    assert parts[0] == ""
    assert join_filepath(parts) == f"{OUT}/1/uid/node"


def test_nested_segments_are_allowed():
    """The input tree has subdirectories; only traversal is refused."""
    assert join_filepath([OUT, "1", "a/b/c.json"]) == f"{OUT}/1/a/b/c.json"


@pytest.mark.parametrize(
    "parts",
    [
        [OUT, "1", "../../etc/passwd"],
        [OUT, "..", "etc"],
        [OUT, "1/../../etc", "x"],
        ["1", "..", "2"],
    ],
)
def test_traversal_out_of_the_base_is_refused(parts):
    with pytest.raises(InvalidPathError):
        join_filepath(parts)


def test_a_sideways_move_into_another_workspace_is_refused():
    """`../other` normalises back inside OUTPUT_DIR, so containment alone
    would allow it. It still leaves workspace 1, which is why ".." is refused
    outright rather than merely contained."""
    with pytest.raises(InvalidPathError):
        join_filepath([OUT, "1", "../other/expt"])


def test_an_absolute_component_cannot_reset_the_base():
    """ "/".join, not os.path.join: an absolute second component is appended
    rather than replacing what came before."""
    assert join_filepath([OUT, "/etc/passwd"]) == f"{OUT}/etc/passwd"


def test_the_result_is_normalised():
    assert join_filepath([OUT, "", "1", "uid"]) == f"{OUT}/1/uid"
    assert join_filepath([OUT, ".", "1"]) == f"{OUT}/1"


def test_invalid_path_error_is_a_value_error():
    """The snakemake rule processes import this module in conda environments
    without FastAPI, so the exception must not depend on it."""
    assert issubclass(InvalidPathError, ValueError)


def test_the_guard_matches_what_the_filesystem_would_do():
    """A sanity check that the accepted form is the path callers then open."""
    built = join_filepath([OUT, "1", "uid", "f.json"])
    assert built == os.path.normpath(built)
