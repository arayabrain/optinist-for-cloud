"""The BUILD_INFO reading rules in studio.app.version.

The fields themselves are written into the image at build time (see
studio/config/docker/Dockerfile) and can never be corrected once an image
exists. The rule that turns them into a readable ref therefore lives on the
reading side, where it stays changeable — which only works if it is pinned.

The end-to-end path that produces those fields is covered separately by
studio/tests/infrastructure/test_git_ref_info.py.
"""

import importlib

import pytest

import studio.app.version as version_module
from studio.app.dir_path import DIRPATH
from studio.app.version import BuildInfo, _derive_git_ref, _field, _load_build_info


class TestField:
    """What counts as "this was not recorded"."""

    def test_reads_a_recorded_value(self):
        assert _field({"git_tag": "v1.1.10"}, "git_tag") == "v1.1.10"

    def test_missing_key_is_absent(self):
        # BUILD_INFO written by an image built before git_tag existed.
        assert _field({"git_commit": "0cf95d0d"}, "git_tag") == ""

    def test_empty_string_is_absent(self):
        # The build looked, and the checkout genuinely had no tag.
        assert _field({"git_tag": ""}, "git_tag") == ""

    def test_unknown_is_absent(self):
        # The ARG default: the value was never passed into the build.
        assert _field({"git_branch": "unknown"}, "git_branch") == ""
        assert _field({"git_commit": "unknown"}, "git_commit") == ""
        assert _field({"build_timestamp": "unknown"}, "build_timestamp") == ""

    def test_literal_head_is_absent(self):
        # The defect this whole change exists to fix. `git rev-parse
        # --abbrev-ref HEAD` reported "HEAD" for every tag checkout, so images
        # built before the fix carry it. It names no ref, and reporting a
        # branch called HEAD is worse than reporting nothing.
        assert _field({"git_branch": "HEAD"}, "git_branch") == ""

    @pytest.mark.parametrize("name", ["unknown", "HEAD"])
    def test_a_tag_that_looks_like_a_placeholder_survives(self, name):
        # `git tag unknown` and `git tag HEAD` both succeed, so these are real
        # tag names. git_tag is introduced by this change and never shipped
        # with a placeholder, so nothing about it is a sentinel and collapsing
        # these would silently discard the value the field exists to carry.
        assert _field({"git_tag": name}, "git_tag") == name
        assert _derive_git_ref("0cf95d0d", "", name) == name

    def test_non_string_is_absent(self):
        # A truncated or hand-edited BUILD_INFO must not put a non-string onto
        # a class attribute the rest of the code treats as one.
        assert _field({"git_tag": None}, "git_tag") == ""
        assert _field({"git_tag": 17}, "git_tag") == ""


class TestDeriveGitRef:
    """Which single name stands for the build."""

    def test_tag_checkout_reports_the_tag(self):
        # A tag checkout detaches HEAD, so there is no branch to report.
        assert _derive_git_ref("0cf95d0d", "", "v1.1.10") == "v1.1.10"

    def test_branch_checkout_reports_the_branch(self):
        assert _derive_git_ref("0cf95d0d", "develop-main", "") == "develop-main"

    def test_branch_carrying_a_tag_reports_both(self):
        # Reporting a bare "v1.1.10" here would read as a tag checkout, which
        # is the exact confusion this field exists to remove. The branch is how
        # the checkout was made; the tag is what version that commit is.
        assert (
            _derive_git_ref("0cf95d0d", "develop-main", "v1.1.10")
            == "develop-main (v1.1.10)"
        )

    def test_detached_without_a_tag_reports_the_commit(self):
        assert _derive_git_ref("0cf95d0dd37e4b9b", "", "") == "detached@0cf95d0d"

    def test_nothing_recorded_reports_na(self):
        # A plain `docker build` with no --build-arg, or no BUILD_INFO at all.
        assert _derive_git_ref("", "", "") == "N/A"


class TestLoadBuildInfo:
    """Nothing a damaged BUILD_INFO contains may reach the caller as non-dict.

    BuildInfo reads its fields in the class body, which runs on import from
    __main_unit__ at startup, so anything this lets through becomes an
    import-time crash of the application rather than a degraded log line.
    """

    @pytest.fixture
    def build_info(self, tmp_path, monkeypatch):
        """Point the loader at a directory this test controls.

        Without this the assertions would really be about the ambient image:
        the test image happens not to write /app/BUILD_INFO, but one built from
        the production Dockerfile does.
        """
        monkeypatch.setattr(DIRPATH, "ROOT_DIR", str(tmp_path))

        def write(content: str):
            (tmp_path / "BUILD_INFO").write_text(content)

        return write

    def test_missing_file_yields_an_empty_record(self, build_info):
        # Running from source, where only a Docker build writes the file.
        assert _load_build_info() == {}

    def test_reads_a_well_formed_record(self, build_info):
        build_info('{"git_tag": "v1.1.10"}')
        assert _load_build_info() == {"git_tag": "v1.1.10"}

    def test_truncated_json_yields_an_empty_record(self, build_info):
        build_info("{truncated")
        assert _load_build_info() == {}

    @pytest.mark.parametrize("content", ["[]", "null", '"just-a-string"', "17"])
    def test_valid_json_that_is_not_an_object_yields_an_empty_record(
        self, build_info, content
    ):
        # json.load succeeds on all of these, and BuildInfo would then call
        # .get() on a list/None/str and raise at import time.
        build_info(content)
        assert _load_build_info() == {}


class TestBuildInfo:
    """The class attributes the startup log reads."""

    def test_every_attribute_is_a_string(self):
        for name in (
            "GIT_COMMIT",
            "GIT_BRANCH",
            "GIT_TAG",
            "GIT_REF",
            "BUILD_TIMESTAMP",
        ):
            assert isinstance(getattr(BuildInfo, name), str), name

    def test_git_ref_is_derived_from_the_other_fields(self):
        # Pins the wiring, not the value: this runs from source, where all
        # three inputs are absent.
        assert BuildInfo.GIT_REF == _derive_git_ref(
            _field(BuildInfo._data, "git_commit"),
            BuildInfo.GIT_BRANCH,
            BuildInfo.GIT_TAG,
        )


class TestBuildInfoAgainstARealRecord:
    """The class body, evaluated against BUILD_INFO files rather than {}.

    Everything above tests the helpers. These pin what the startup log actually
    prints, which is the class body reading a real file — the one step where a
    field can bypass the rules by being read with a raw `.get()`.
    """

    @pytest.fixture
    def build_info(self, tmp_path, monkeypatch):
        """Write a BUILD_INFO, re-evaluate the class body, hand back BuildInfo."""
        monkeypatch.setattr(DIRPATH, "ROOT_DIR", str(tmp_path))
        # Reloading re-evaluates Version.APP_VERSION too, which reads
        # pyproject.toml from the same ROOT_DIR.
        (tmp_path / "pyproject.toml").write_text('version = "9.9.9"\n')

        def load(content: str):
            (tmp_path / "BUILD_INFO").write_text(content)
            return importlib.reload(version_module).BuildInfo

        yield load
        # Restore the module for anything importing it after this test.
        monkeypatch.undo()
        importlib.reload(version_module)

    def test_a_tag_build_reports_the_tag(self, build_info):
        info = build_info(
            '{"git_commit": "0cf95d0dd37e4b9b", "git_branch": "", '
            '"git_tag": "v1.1.10", "build_timestamp": "2026-09-07T02:23:37Z"}'
        )
        assert info.GIT_REF == "v1.1.10"
        assert info.GIT_COMMIT == "0cf95d0dd37e4b9b"
        assert info.BUILD_TIMESTAMP == "2026-09-07T02:23:37Z"

    def test_a_legacy_image_degrades_instead_of_naming_a_ref(self, build_info):
        # An image built before this change: the branch reads "HEAD" and there
        # is no git_tag key at all.
        info = build_info(
            '{"git_commit": "0cf95d0dd37e4b9bda51601bbb12adf4d21173a4", '
            '"git_branch": "HEAD", "build_timestamp": "2026-09-07T02:23:37Z"}'
        )
        assert info.GIT_BRANCH == ""
        assert info.GIT_REF == "detached@0cf95d0d"

    def test_the_commit_and_the_ref_agree_when_nothing_was_recorded(self, build_info):
        # A plain `docker build` with no --build-arg. These are adjacent lines
        # of one log message, so "unknown" on one and "N/A" on the next gave
        # two names to a single state.
        info = build_info(
            '{"git_commit": "unknown", "git_branch": "", "git_tag": "", '
            '"build_timestamp": "unknown"}'
        )
        assert info.GIT_COMMIT == "N/A"
        assert info.GIT_REF == "N/A"
        assert info.BUILD_TIMESTAMP == "N/A"

    def test_no_field_reaches_an_attribute_as_a_non_string(self, build_info):
        # `.get(key, "N/A")` does not fire its default when the key exists
        # holding null, so a raw read put None and [] onto these two.
        info = build_info(
            '{"git_commit": null, "build_timestamp": [], '
            '"git_branch": 17, "git_tag": {}}'
        )
        for name in (
            "GIT_COMMIT",
            "GIT_BRANCH",
            "GIT_TAG",
            "GIT_REF",
            "BUILD_TIMESTAMP",
        ):
            value = getattr(info, name)
            assert isinstance(value, str), f"{name} = {value!r}"
        assert info.GIT_COMMIT == "N/A"
        assert info.BUILD_TIMESTAMP == "N/A"
