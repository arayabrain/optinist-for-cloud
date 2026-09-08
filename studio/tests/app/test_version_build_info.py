"""The BUILD_INFO reading rules in studio.app.version.

The fields themselves are written into the image at build time (see
studio/config/docker/Dockerfile) and can never be corrected once an image
exists. The rule that turns them into a readable ref therefore lives on the
reading side, where it stays changeable — which only works if it is pinned.

The end-to-end path that produces those fields is covered separately by
studio/tests/infrastructure/test_git_ref_info.py.
"""

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

    def test_literal_head_is_absent(self):
        # The defect this whole change exists to fix. `git rev-parse
        # --abbrev-ref HEAD` reported "HEAD" for every tag checkout, so images
        # built before the fix carry it. It names no ref, and reporting a
        # branch called HEAD is worse than reporting nothing.
        assert _field({"git_branch": "HEAD"}, "git_branch") == ""

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
    """BUILD_INFO is absent outside a built image, and that is not an error."""

    def test_missing_file_yields_an_empty_record(self):
        # The source checkout has no BUILD_INFO; only the Docker build writes
        # one. Running from source must not raise.
        assert _load_build_info() == {}


class TestBuildInfo:
    """The class attributes the startup log reads."""

    def test_every_attribute_is_a_string(self):
        for name in ("GIT_COMMIT", "GIT_BRANCH", "GIT_TAG", "GIT_REF"):
            assert isinstance(getattr(BuildInfo, name), str), name

    def test_git_ref_is_derived_from_the_other_fields(self):
        # Pins the wiring, not the value: this runs from source, where all
        # three inputs are absent.
        assert BuildInfo.GIT_REF == _derive_git_ref(
            _field(BuildInfo._data, "git_commit"),
            BuildInfo.GIT_BRANCH,
            BuildInfo.GIT_TAG,
        )
