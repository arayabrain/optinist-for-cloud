"""A host clock running behind Google's rejects a token the client just got.

The failure is `Token used too early, <now> < <iat>` on the request that
immediately follows a successful login, so every path that verifies a Firebase
token has to allow the same tolerance - one path without it is enough to 401 a
freshly signed-in user.
"""

from unittest.mock import MagicMock, patch

import pytest

from studio.app.common.core.auth import auth_helper

MODULE = "studio.app.common.core.auth.auth_helper"


@pytest.fixture
def verifier():
    """A cached uid short-circuits verification entirely, so a stale entry from
    another test would leave the verifier uncalled and this fixture blind."""
    auth_helper._token_cache.clear()
    with patch(f"{MODULE}.firebase_auth.verify_id_token") as mock:
        mock.return_value = {"uid": "uid-1"}
        yield mock
    auth_helper._token_cache.clear()


def skew_of(mock) -> int:
    assert mock.call_count == 1, f"expected one verification, got {mock.call_count}"
    return mock.call_args.kwargs.get("clock_skew_seconds", 0)


class TestEveryVerificationPathAllowsSkew:
    def test_the_sync_path_allows_skew(self, verifier):
        assert auth_helper._verify_firebase_token_sync("t") == "uid-1"
        assert skew_of(verifier) == auth_helper._CLOCK_SKEW_SECONDS

    def test_the_credential_path_allows_skew(self, verifier):
        credential = MagicMock(credentials="t")
        uid, err = auth_helper.extract_uid_from_firebase_credential(credential)
        assert (uid, err) == ("uid-1", None)
        assert skew_of(verifier) == auth_helper._CLOCK_SKEW_SECONDS

    def test_the_request_path_allows_skew(self, verifier):
        request = MagicMock()
        request.headers = {"Authorization": "Bearer t"}
        request.cookies = {}
        with patch(f"{MODULE}.AUTH_CONFIG") as config:
            config.USE_FIREBASE_TOKEN = True
            assert auth_helper.extract_uid_from_request(request) == "uid-1"
        assert skew_of(verifier) == auth_helper._CLOCK_SKEW_SECONDS


def test_the_tolerance_is_within_the_range_firebase_accepts():
    # Pinned separately: a test deriving its bound from the constant would
    # still pass if the constant went to 0 or past Firebase's ceiling of 60.
    assert 1 <= auth_helper._CLOCK_SKEW_SECONDS <= 60
