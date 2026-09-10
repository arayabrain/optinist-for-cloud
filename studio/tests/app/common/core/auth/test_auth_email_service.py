"""Which sender each configuration selects, and which of them put mail on the wire.

The reason this matters beyond the branch coverage: the e2e workflow sets
`USE_FIREBASE_EMAIL=False` so a CI run cannot mail a real inbox. That only holds
if every configuration reachable with the flag off ends in the development branch,
which logs the link and sends nothing.

`USE_FIREBASE_EMAIL` and `FIREBASE_EMAIL_AVAILABLE` are read at import time into
module globals, so the tests patch the globals rather than the environment.
"""

from unittest.mock import patch

import pytest

from studio.app.common.core.auth import auth_email_service
from studio.app.common.core.auth.auth_email_service import AuthEmailService


@pytest.fixture()
def senders():
    """Every outbound path stubbed, so a test can assert which one was chosen and
    that the others stayed silent."""
    with patch.object(
        auth_email_service, "send_verification_email_via_firebase"
    ) as verify_via_firebase, patch.object(
        auth_email_service, "send_password_reset_email_via_firebase"
    ) as reset_via_firebase, patch.object(
        auth_email_service, "pyrebase_app"
    ) as pyrebase, patch.object(
        auth_email_service, "firebase_auth"
    ) as admin_sdk:
        admin_sdk.generate_email_verification_link.return_value = "https://verify"
        admin_sdk.generate_password_reset_link.return_value = "https://reset"
        yield {
            "verify_via_firebase": verify_via_firebase,
            "reset_via_firebase": reset_via_firebase,
            "pyrebase": pyrebase,
            "admin_sdk": admin_sdk,
        }


def configure(use_firebase_email, firebase_available, pyrebase_configured, senders):
    if not pyrebase_configured:
        senders["pyrebase"] = None
    return patch.multiple(
        auth_email_service,
        USE_FIREBASE_EMAIL=use_firebase_email,
        FIREBASE_EMAIL_AVAILABLE=firebase_available,
        pyrebase_app=senders["pyrebase"],
    )


def mail_was_sent(senders):
    """The development branch only logs; every other branch hands the address to
    a service that delivers."""
    return (
        senders["verify_via_firebase"].called
        or senders["reset_via_firebase"].called
        or (senders["pyrebase"] is not None and senders["pyrebase"].auth.called)
    )


class TestVerificationEmail:
    def test_the_firebase_sender_is_used_when_configured(self, senders):
        with configure(True, True, True, senders):
            assert AuthEmailService.send_verification_email("who@example.com") is True

        senders["verify_via_firebase"].assert_called_once_with("who@example.com")
        senders["admin_sdk"].generate_email_verification_link.assert_not_called()

    @pytest.mark.parametrize(
        "use_firebase_email,firebase_available,pyrebase_configured",
        [
            (False, True, True),
            (False, True, False),
            (False, False, True),
            (True, False, True),
            (True, False, False),
        ],
    )
    def test_every_other_configuration_only_logs_the_link(
        self, senders, use_firebase_email, firebase_available, pyrebase_configured
    ):
        """`(False, True, True)` is the configuration that used to reach a
        Pyrebase branch and mail the address for real."""
        with configure(
            use_firebase_email, firebase_available, pyrebase_configured, senders
        ):
            assert AuthEmailService.send_verification_email("who@example.com") is True

        senders["admin_sdk"].generate_email_verification_link.assert_called_once()
        assert not mail_was_sent(senders)

    def test_a_sender_failure_is_raised_rather_than_swallowed(self, senders):
        """`/register` rolls the new user back on this exception. Returning False
        instead would leave an account that can never verify."""
        senders["verify_via_firebase"].side_effect = RuntimeError("firebase is down")

        with configure(True, True, True, senders):
            with pytest.raises(RuntimeError):
                AuthEmailService.send_verification_email("who@example.com")


class TestPasswordResetEmail:
    def test_the_firebase_sender_is_used_when_configured(self, senders):
        with configure(True, True, True, senders):
            assert AuthEmailService.send_password_reset_email("who@example.com") is True

        senders["reset_via_firebase"].assert_called_once_with("who@example.com")

    def test_pyrebase_covers_a_missing_firebase_sender_module(self, senders):
        """The flag is on and the operator expects mail to go out, but the sender
        module would not import. Pyrebase can do password reset directly, so this
        deployment keeps working."""
        with configure(True, False, True, senders):
            assert AuthEmailService.send_password_reset_email("who@example.com") is True

        senders[
            "pyrebase"
        ].auth.return_value.send_password_reset_email.assert_called_once_with(
            "who@example.com"
        )
        senders["admin_sdk"].generate_password_reset_link.assert_not_called()

    @pytest.mark.parametrize(
        "firebase_available,pyrebase_configured",
        [(True, True), (True, False), (False, True), (False, False)],
    )
    def test_the_flag_off_sends_no_mail_by_any_route(
        self, senders, firebase_available, pyrebase_configured
    ):
        """Pyrebase is gated on the flag as well, so nothing reaches an inbox in
        CI. Without that gate, `(True, True)` mails the address for real."""
        with configure(False, firebase_available, pyrebase_configured, senders):
            assert AuthEmailService.send_password_reset_email("who@example.com") is True

        senders["admin_sdk"].generate_password_reset_link.assert_called_once()
        assert not mail_was_sent(senders)

    def test_an_http_error_is_logged_without_its_url(self, senders):
        """The Firebase REST URL carries the API key as a query parameter, so the
        default `str(e)` would put the key in the log."""
        from requests.exceptions import HTTPError

        senders["reset_via_firebase"].side_effect = HTTPError(
            "400 Client Error for url: "
            "https://identitytoolkit.googleapis.com/v1/x?key=SECRET_KEY"
        )

        with configure(True, True, True, senders):
            with patch.object(auth_email_service, "logger") as logger:
                with pytest.raises(HTTPError):
                    AuthEmailService.send_password_reset_email("who@example.com")

        logged = " ".join(str(call) for call in logger.error.call_args_list)
        assert "SECRET_KEY" not in logged
