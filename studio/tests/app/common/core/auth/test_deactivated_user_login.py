"""A deleted user cannot log back in.

Deletion is a soft delete: the `users` row survives with `active = 0`, and the
Firebase account is deleted separately. The row-level guard is therefore the
`active.is_(True)` conjunct in `authenticate_user`'s lookup, and it is the only
thing standing between a deactivated account and a working session if the
Firebase half ever survives - a partially completed deletion, or a Firebase
account recreated at the same address.

Asserting that `delete_user` was called does not cover this: that a deletion was
requested says nothing about whether a later login is refused.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from firebase_admin.auth import UserNotFoundError

from studio.app.common.core.auth import auth as auth_module
from studio.app.common.models import Organization as OrganizationModel
from studio.app.common.models import User as UserModel
from studio.app.common.schemas.auth import UserAuth
from studio.tests.app.common.sqlite_harness import sqlite_session

UID = "firebase-uid-deleted-user"
CREDENTIALS = UserAuth(email="deleted@example.com", password="e2ePass!1")


@pytest.fixture()
def db():
    """In-memory SQLite session holding one organization and its users."""
    with sqlite_session([OrganizationModel.__table__, UserModel.__table__]) as session:
        session.add(OrganizationModel(id=1, name="Test Org"))
        session.commit()
        yield session


def seed(db, active):
    user = UserModel(
        organization_id=1,
        uid=UID,
        name="Deleted User",
        email=CREDENTIALS.email,
        attributes={},
        active=active,
    )
    db.add(user)
    db.commit()


@pytest.fixture()
def firebase_accepts_the_password():
    """Firebase authenticates the credentials, so anything that refuses the
    login afterwards is our own guard rather than Firebase's."""
    signed_in = {
        "localId": UID,
        "idToken": "id-token",
        "refreshToken": "refresh-token",
    }
    with patch.object(auth_module, "pyrebase_app") as pyrebase, patch.object(
        auth_module, "auth"
    ) as firebase_admin_auth:
        pyrebase.auth.return_value = MagicMock(
            **{"sign_in_with_email_and_password.return_value": signed_in}
        )
        firebase_admin_auth.get_user.return_value = SimpleNamespace(
            uid=UID, email_verified=True
        )
        # The real class, or `except auth.UserNotFoundError` raises TypeError and
        # every path through this function fails for the wrong reason
        firebase_admin_auth.UserNotFoundError = UserNotFoundError
        yield pyrebase


@pytest.mark.asyncio
async def test_an_active_user_logs_in(db, firebase_accepts_the_password):
    """The positive control: without it, the refusal below could come from the
    harness rather than from the `active` guard."""
    seed(db, active=True)

    token, user = await auth_module.authenticate_user(db, CREDENTIALS)

    assert token.access_token == "id-token"
    assert user.uid == UID


@pytest.mark.asyncio
async def test_a_deactivated_user_is_refused(db, firebase_accepts_the_password):
    seed(db, active=False)

    with pytest.raises(HTTPException) as raised:
        await auth_module.authenticate_user(db, CREDENTIALS)

    # 404 rather than 403: the lookup filters deactivated rows out, so by the
    # time anything could report on the account there is no row left to report on
    assert raised.value.status_code == 404
    assert raised.value.detail == "User not found"


@pytest.mark.asyncio
async def test_the_active_filter_is_what_refuses_them(
    db, firebase_accepts_the_password
):
    """Firebase still holds the password for a deactivated account and still
    verifies it, so the sign-in is refused by the database lookup alone. Asserting
    that keeps the test honest about where the guard lives: remove the `active`
    filter and this user logs in with a valid password."""
    seed(db, active=False)

    with pytest.raises(HTTPException):
        await auth_module.authenticate_user(db, CREDENTIALS)

    sign_in = (
        firebase_accepts_the_password.auth.return_value
    ).sign_in_with_email_and_password
    sign_in.assert_called_once_with(CREDENTIALS.email, CREDENTIALS.password)
