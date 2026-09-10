import asyncio
import io
import json
import logging
import traceback
from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi import HTTPException

import studio.app.common.core.auth.auth as auth_module
import studio.app.common.core.auth.firebase_email_sender as firebase_email_sender
from studio.app.common.core.auth.auth import _extract_firebase_error

FAKE_KEY = "AIzaFAKEKEY123"


def make_response(status=400, body=b'{"error": {"message": "EMAIL_NOT_FOUND"}}'):
    response = requests.models.Response()
    response.status_code = status
    response._content = body
    response.url = (
        "https://identitytoolkit.googleapis.com/v1/"
        f"accounts:sendOobCode?key={FAKE_KEY}"
    )
    return response


def pyrebase_http_error(message):
    # pyrebase raises HTTPError(original_error, response_text); the key lives
    # in the original error's URL, the clean message in the wrapped body.
    original = requests.exceptions.HTTPError(
        "400 Client Error: Bad Request for url: "
        f"https://identitytoolkit.googleapis.com/v1/accounts:lookup?key={FAKE_KEY}"
    )
    return requests.exceptions.HTTPError(
        original, json.dumps({"error": {"message": message}})
    )


@pytest.fixture
def auth_log():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    auth_module.logger.addHandler(handler)
    yield stream
    auth_module.logger.removeHandler(handler)


@pytest.fixture
def sender_log():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    firebase_email_sender.logger.addHandler(handler)
    with patch.object(firebase_email_sender, "FIREBASE_API_KEY", FAKE_KEY):
        yield stream
    firebase_email_sender.logger.removeHandler(handler)


def assert_no_key_anywhere(exc, log_stream):
    assert FAKE_KEY not in str(exc)
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    assert FAKE_KEY not in rendered
    assert FAKE_KEY not in log_stream.getvalue()


def test_extract_firebase_error_requests_shape():
    # requests raise_for_status: key is in str(e), message is in response body
    with pytest.raises(requests.exceptions.HTTPError) as exc_info:
        make_response().raise_for_status()
    assert FAKE_KEY in str(exc_info.value)
    assert _extract_firebase_error(exc_info.value) == "EMAIL_NOT_FOUND"


def test_extract_firebase_error_pyrebase_shape():
    # pyrebase raises HTTPError(original_error, response_text)
    original = requests.exceptions.HTTPError(f"400 Client Error: url ?key={FAKE_KEY}")
    error = requests.exceptions.HTTPError(
        original, '{"error": {"message": "INVALID_LOGIN_CREDENTIALS"}}'
    )
    assert _extract_firebase_error(error) == "INVALID_LOGIN_CREDENTIALS"


def test_extract_firebase_error_no_response():
    error = requests.exceptions.ConnectionError(f"url?key={FAKE_KEY}")
    assert _extract_firebase_error(error) == "authentication failed"


def test_password_reset_sender_does_not_leak_key(sender_log):
    with patch.object(
        firebase_email_sender.requests, "post", return_value=make_response()
    ):
        with pytest.raises(Exception) as exc_info:
            firebase_email_sender.send_password_reset_email_via_firebase(
                "user@example.com"
            )
    assert "EMAIL_NOT_FOUND" in str(exc_info.value)
    assert_no_key_anywhere(exc_info.value, sender_log)
    assert "EMAIL_NOT_FOUND" in sender_log.getvalue()


def test_verification_sender_does_not_leak_key(sender_log):
    fake_user = type("FakeUser", (), {"email_verified": False, "uid": "uid1"})()
    with patch.object(
        firebase_email_sender.firebase_auth, "get_user_by_email", return_value=fake_user
    ), patch.object(
        firebase_email_sender.firebase_auth, "create_custom_token", return_value=b"tok"
    ), patch.object(
        firebase_email_sender.requests,
        "post",
        return_value=make_response(body=b'{"error": {"message": "UNKNOWN_CODE"}}'),
    ):
        with pytest.raises(ValueError) as exc_info:
            firebase_email_sender.send_verification_email_via_firebase(
                "user@example.com"
            )
    assert "rate" in str(exc_info.value).lower()
    assert_no_key_anywhere(exc_info.value, sender_log)


def test_authenticate_user_does_not_leak_key(auth_log):
    firebase = MagicMock()
    firebase.auth.return_value.sign_in_with_email_and_password.side_effect = (
        pyrebase_http_error("INVALID_LOGIN_CREDENTIALS")
    )
    data = MagicMock(email="user@example.com", password="wrong")
    with patch.object(auth_module, "pyrebase_app", firebase):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(auth_module.authenticate_user(MagicMock(), data))
    assert exc_info.value.detail == "INVALID_LOGIN_CREDENTIALS"
    assert FAKE_KEY not in auth_log.getvalue()
    assert "INVALID_LOGIN_CREDENTIALS" in auth_log.getvalue()


def test_refresh_token_does_not_leak_key(auth_log):
    firebase = MagicMock()
    firebase.auth.return_value.refresh.side_effect = pyrebase_http_error(
        "TOKEN_EXPIRED"
    )
    with patch.object(auth_module, "pyrebase_app", firebase), patch.object(
        auth_module, "validate_refresh_token", return_value=({"sub": "rt"}, None)
    ):
        with pytest.raises(HTTPException):
            asyncio.run(auth_module.refresh_current_user_token("refresh-token"))
    assert FAKE_KEY not in auth_log.getvalue()
    assert "TOKEN_EXPIRED" in auth_log.getvalue()


def test_send_reset_password_mail_does_not_leak_key(auth_log):
    firebase = MagicMock()
    firebase.auth.return_value.send_password_reset_email.side_effect = (
        pyrebase_http_error("EMAIL_NOT_FOUND")
    )
    with patch.object(auth_module, "pyrebase_app", firebase):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                auth_module.send_reset_password_mail(MagicMock(), "user@example.com")
            )
    assert exc_info.value.detail == "EMAIL_NOT_FOUND"
    assert FAKE_KEY not in auth_log.getvalue()
    assert "EMAIL_NOT_FOUND" in auth_log.getvalue()
