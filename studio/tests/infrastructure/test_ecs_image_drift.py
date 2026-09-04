"""Tests for the label rules in check_ecs_image_drift.

The drift verdict itself is a digest comparison and is not exercised here.
What is exercised is the naming layer built on top of it: which tag is allowed
to stand for a build, and the promise that a failed lookup degrades the report
rather than ending it.
"""

from unittest.mock import patch

import check_ecs_image_drift as drift
import pytest


class TestTargetLabel:
    """Which name the report gives the build it is checking against."""

    def test_mutable_tag_defers_to_its_alias(self):
        # The substitution the feature exists for: `latest` names no build.
        assert drift.target_label_for("latest", ["v1.1.10"]) == "v1.1.10"

    def test_explicit_tag_survives_its_own_alias(self):
        # ecr_build_push.sh puts `latest` on the same digest, so an explicit
        # request always has `latest` among its aliases. Substituting there
        # replaced the version asked for with the mutable tag.
        assert drift.target_label_for("v1.1.10", ["latest"]) == "v1.1.10"

    def test_mutable_tag_with_no_alias_falls_back_to_the_request(self):
        assert drift.target_label_for("latest", []) == "latest"


class TestLabelFor:
    """One name per build, with the rest kept out of the sentence."""

    def test_prefers_an_immutable_tag_over_a_mutable_one(self):
        assert drift.label_for(["latest", "v1.1.10"], None) == "v1.1.10 (also latest)"

    def test_single_tag_carries_no_aside(self):
        assert drift.label_for(["v1.1.9"], None) == "v1.1.9"

    def test_all_mutable_still_yields_a_name(self):
        assert drift.label_for(["latest"], None) == "latest"

    def test_no_tags_falls_back(self):
        assert drift.label_for([], "fallback") == "fallback"


class TestResolveVersion:
    """The lookup must degrade the report, never end it."""

    def test_failed_lookup_returns_none_and_is_cached(self):
        cache = {}
        with patch.object(drift, "aws_optional", return_value=None) as call:
            assert drift.resolve_version("r", "repo", "sha256:dead", cache) is None
            assert drift.resolve_version("r", "repo", "sha256:dead", cache) is None
        # Cached negatively: a repo that expired an image must not cost one
        # failing call per task.
        assert call.call_count == 1
        assert cache == {"sha256:dead": None}

    def test_successful_lookup_calls_once_and_caches(self):
        cache = {}
        with patch.object(drift, "aws_optional", return_value=["v1.1.9"]) as call:
            assert drift.resolve_version("r", "repo", "sha256:beef", cache) == "v1.1.9"
            assert drift.resolve_version("r", "repo", "sha256:beef", cache) == "v1.1.9"
        assert call.call_count == 1


class TestAwsOptional:
    """Every failure of a presentational call has to be absorbed."""

    @pytest.mark.parametrize(
        "boom", [RuntimeError("non-zero exit"), ValueError("non-JSON stdout")]
    )
    def test_absorbs_both_failure_modes(self, boom):
        # A JSONDecodeError (a ValueError) escaped the wrapper and every
        # handler in __main__, exiting 1 — this script's "drift detected" code.
        with patch.object(drift, "aws", side_effect=boom):
            assert drift.aws_optional("r", "ecr", "describe-images") is None
