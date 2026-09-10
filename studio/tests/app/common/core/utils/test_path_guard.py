import pytest
from fastapi import HTTPException

from studio.app.common.core.utils.path_guard import (
    secure_component,
    secure_output_relpath,
    secure_relpath,
)
from studio.app.dir_path import DIRPATH

BASE = f"{DIRPATH.OUTPUT_DIR}/1"


def assert_rejected(fn, *args):
    with pytest.raises(HTTPException) as excinfo:
        fn(*args)
    assert excinfo.value.status_code == 400


# ---------------------------------------------------------- secure_component


@pytest.mark.parametrize(
    "value",
    [
        "1",  # workspace_id
        "a1b2c3d4",  # unique_id: str(uuid.uuid4())[:8]
        "remote_storage_test",  # unique_id in the checked-in test data
        "input_0",  # node_id
        "suite2p_file_convert_pi2bgrsd6m",  # node_id
        "file.with.dots.tiff",
        "..leading-dots-are-fine",
    ],
)
def test_secure_component_accepts_real_identifiers(value):
    """The formats actually produced by the app must survive the guard."""
    assert secure_component(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "..",
        "../etc",
        "../../etc/passwd",
        "a/../../b",
        "/etc/passwd",  # absolute
        "a/b",  # nested: a component is one segment
        "",
        ".",
        "./x",
        "a//b",
    ],
)
def test_secure_component_rejects_traversal_and_nesting(value):
    assert_rejected(secure_component, value)


def test_secure_component_rejects_rather_than_rewrites():
    """`../etc` normalises to `etc`; returning that would silently act on a
    different file than the caller named."""
    assert_rejected(secure_component, "../etc")


def test_secure_component_leaves_percent_encoding_alone():
    """Starlette decodes before the handler sees the value, so a literal
    `%2e%2e` really is a directory of that name, not a traversal."""
    assert secure_component("%2e%2e") == "%2e%2e"


# ------------------------------------------------------------ secure_relpath


@pytest.mark.parametrize(
    "relpath,expected",
    [
        ("a.json", "a.json"),
        ("node/plot.json", "node/plot.json"),
        ("deep/nested/dir/file.tiff", "deep/nested/dir/file.tiff"),
        ("x/../y.json", "y.json"),  # normalised, still inside base
    ],
)
def test_secure_relpath_accepts_paths_inside_base(relpath, expected):
    assert secure_relpath(BASE, relpath) == expected


@pytest.mark.parametrize(
    "relpath",
    [
        "../2/secret.json",  # a sibling workspace
        "../../etc/passwd",
        "a/../../../etc/passwd",
        "/etc/passwd",
        "",  # resolves to base itself, not a path under it
        ".",
    ],
)
def test_secure_relpath_rejects_escapes(relpath):
    assert_rejected(secure_relpath, BASE, relpath)


def test_secure_relpath_rejects_a_sibling_with_a_shared_prefix():
    """`/output/1` must not accept `/output/12/x`: the check appends a
    separator so a common string prefix is not a common directory."""
    assert_rejected(secure_relpath, BASE, "../12/x.json")


# ----------------------------------------------------- secure_output_relpath


def test_secure_output_relpath_accepts_a_relative_path():
    assert secure_output_relpath("1/uid/node/plot.json") == "1/uid/node/plot.json"


def test_secure_output_relpath_accepts_the_absolute_form_in_older_records():
    """Some DB rows still hold the absolute path; normalize_output_path strips
    the OUTPUT_DIR prefix before the containment check."""
    absolute = f"{DIRPATH.OUTPUT_DIR}/1/uid/node/plot.json"
    assert secure_output_relpath(absolute) == "1/uid/node/plot.json"


@pytest.mark.parametrize(
    "path",
    [
        "../../etc/passwd",
        "1/../../../etc/passwd",
        "/etc/passwd",
    ],
)
def test_secure_output_relpath_rejects_escapes(path):
    assert_rejected(secure_output_relpath, path)
