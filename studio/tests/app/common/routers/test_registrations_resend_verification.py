"""`POST /api/register/resend-verification`.

The endpoint has four outcomes and `test_registrations_api_contract.py` reaches
none of them: it builds dicts and validates their shape without a request. What
the two rows care about is which of the four a caller gets, because the frontend
keys its snackbar off `already_verified` and its "wait a moment" copy off the
429.

Firebase is patched on the real `firebase_admin.auth` module rather than replaced
wholesale, so `except firebase_auth.UserNotFoundError` still names a class.
"""

from unittest.mock import Mock, patch

import pytest
from firebase_admin import auth as firebase_auth

from studio.app.common.core.auth.auth_email_service import AuthEmailService

ENDPOINT = "/api/register/resend-verification"
EMAIL = "resend@example.com"


@pytest.fixture()
def firebase_user():
    """Patch the lookup and the send, yielding both so a test can set either."""
    with patch.object(firebase_auth, "get_user_by_email") as lookup, patch.object(
        AuthEmailService, "send_verification_email"
    ) as send:
        lookup.return_value = Mock(email_verified=False, uid="uid-resend")
        yield Mock(lookup=lookup, send=send)


class TestResendVerification:
    def test_an_unverified_address_is_sent_a_new_link(self, client, firebase_user):
        response = client.post(ENDPOINT, json={"email": EMAIL})

        assert response.status_code == 200
        assert response.json() == {
            "success": True,
            "message": "Verification email has been sent",
            "already_verified": False,
        }
        firebase_user.send.assert_called_once_with(EMAIL)

    def test_an_already_verified_address_is_reported_and_sent_nothing(
        self, client, firebase_user
    ):
        """`already_verified` is what the frontend uses to send the user to the
        login form instead of telling them to check their inbox, so a second
        email here is not merely wasteful: it contradicts the copy."""
        firebase_user.lookup.return_value = Mock(email_verified=True, uid="uid-resend")

        response = client.post(ENDPOINT, json={"email": EMAIL})

        assert response.status_code == 200
        assert response.json() == {
            "success": True,
            "message": "Email is already verified",
            "already_verified": True,
        }
        firebase_user.send.assert_not_called()

    def test_an_unknown_address_is_a_404(self, client, firebase_user):
        firebase_user.lookup.side_effect = firebase_auth.UserNotFoundError("no user")

        response = client.post(ENDPOINT, json={"email": EMAIL})

        assert response.status_code == 404
        assert response.json()["detail"] == "User not found"
        firebase_user.send.assert_not_called()

    def test_firebases_rate_limit_reaches_the_caller_as_a_429(
        self, client, firebase_user
    ):
        """Both rows warn that resending too soon is refused by Firebase. The
        message has to survive: a 500 would render as "something went wrong"
        rather than "wait a few minutes"."""
        firebase_user.send.side_effect = ValueError(
            "Too many verification emails sent. Please wait a few "
            "minutes before trying again."
        )

        response = client.post(ENDPOINT, json={"email": EMAIL})

        assert response.status_code == 429
        assert "wait a few" in response.json()["detail"]

    def test_an_unexpected_firebase_failure_is_a_500(self, client, firebase_user):
        """Separates the 429 above from the catch-all, which would otherwise be
        the same assertion twice."""
        firebase_user.send.side_effect = RuntimeError("firebase exploded")

        response = client.post(ENDPOINT, json={"email": EMAIL})

        assert response.status_code == 500
        assert response.json()["detail"] == "Failed to resend verification email"

    def test_a_malformed_address_never_reaches_firebase(self, client, firebase_user):
        response = client.post(ENDPOINT, json={"email": "not-an-email"})

        assert response.status_code == 422
        firebase_user.lookup.assert_not_called()
