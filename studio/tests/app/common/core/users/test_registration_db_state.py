"""Database state written by ``POST /api/register``.

After registration the ``users`` row exists and is active, and the user starts
on Free.

``test_registrations_api_contract.py`` covers the response shape only, so
nothing asserted what ``create_user`` actually writes. The rows it inserts are
what every later tier decision reads: ``get_user_with_context`` derives Free
from the absence of a premium ``subscription_users`` row, and the storage
warnings compare against ``user_storage_usage.storage_quota_bytes``.

Firebase is mocked at ``firebase_auth``, the boundary the other suites in this
tree already mock. No network, no database.
"""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from studio.app.common.core.subscription.constants import (
    StorageQuota,
    StorageSize,
    SubscriptionPlanIds,
)
from studio.app.common.core.users import crud_users
from studio.app.common.models import Organization
from studio.app.common.models import User as UserModel
from studio.app.common.models.subscription import UserStorageUsage, UserSubscription
from studio.app.common.schemas.users import UserCreate, UserRole
from studio.app.const import DEFAULT_ORGANIZATION_ID

MODULE = "studio.app.common.core.users.crud_users"

FIREBASE_UID = "firebase-uid-abc123"
REGISTERED_EMAIL = "new.user@example.com"


def _added_of_type(db, model):
    """Every instance of ``model`` handed to ``db.add`` during registration."""
    return [
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], model)
    ]


async def _register(role_id=UserRole.operator.value):
    """Run the registration path and return the mock session it wrote to."""
    db = MagicMock()

    # UserModel.id is assigned by the DB; create_user reads it after flush() to
    # build the dependent rows, so give the flush a visible effect. refresh()
    # loads the organization relationship the response model requires.
    def _flush():
        setattr(_added_of_type(db, UserModel)[0], "id", 4321)

    def _refresh(instance):
        instance.organization = Organization(
            id=DEFAULT_ORGANIZATION_ID, name="Test Organization"
        )

    db.flush.side_effect = _flush
    db.refresh.side_effect = _refresh

    firebase_user = Mock(uid=FIREBASE_UID, email=REGISTERED_EMAIL)

    with patch(f"{MODULE}.firebase_auth") as firebase, patch(
        f"{MODULE}.RemoteStorageController.is_available", return_value=False
    ), patch(f"{MODULE}.AuthEmailService.send_verification_email"), patch(
        f"{MODULE}.set_role", new_callable=AsyncMock
    ) as set_role:
        firebase.create_user.return_value = firebase_user
        await crud_users.create_user(
            db,
            UserCreate(
                email=REGISTERED_EMAIL,
                password="Str0ng-Passw0rd!",
                name="New User",
                role_id=role_id,
            ),
            organization_id=DEFAULT_ORGANIZATION_ID,
            verified=False,
        )

    return db, firebase, set_role


class TestRegistrationWritesAnActiveUser:
    """The ``users`` row exists with ``active = 1``."""

    @pytest.mark.asyncio
    async def test_user_row_is_active(self):
        db, _, _ = await _register()

        users = _added_of_type(db, UserModel)
        assert len(users) == 1, "registration must insert exactly one users row"
        assert users[0].active is True

    @pytest.mark.asyncio
    async def test_user_row_takes_its_identity_from_firebase(self):
        """The uid and email come from the created Firebase record, not from the
        request body. A user row whose uid does not match Firebase can never
        authenticate, because login resolves the uid from the verified token."""
        db, _, _ = await _register()

        user = _added_of_type(db, UserModel)[0]
        assert user.uid == FIREBASE_UID
        assert user.email == REGISTERED_EMAIL

    @pytest.mark.asyncio
    async def test_firebase_account_is_created_unverified(self):
        """The unverified flag is what makes login fail until the email is
        confirmed."""
        _, firebase, _ = await _register()

        assert firebase.create_user.call_args.kwargs["email_verified"] is False

    @pytest.mark.asyncio
    async def test_self_registration_cannot_claim_the_admin_role(self):
        """A request body asking for ``role_id: 1`` is overwritten with operator.

        The only guard is ``if not verified: data.role_id = operator`` at the top
        of ``create_user``; ``/api/register`` always passes ``verified=False``.
        Without it, anyone could register straight into the Account Manager.
        """
        _, _, set_role = await _register(role_id=UserRole.admin.value)

        assert set_role.await_args.kwargs["role_id"] == UserRole.operator.value


class TestRegistrationStartsTheUserOnFree:
    """The new user's starting subscription and storage state."""

    @pytest.mark.asyncio
    async def test_subscription_row_is_the_free_plan(self):
        db, _, _ = await _register()

        subscriptions = _added_of_type(db, UserSubscription)
        assert len(subscriptions) == 1
        assert subscriptions[0].plan_id == SubscriptionPlanIds.FREE
        assert subscriptions[0].user_id == 4321

    @pytest.mark.asyncio
    async def test_storage_quota_is_the_free_quota_not_the_premium_one(self):
        """5GB, not 200GB. An over-generous default would silently suppress every
        storage warning for the account's whole lifetime."""
        db, _, _ = await _register()

        usage = _added_of_type(db, UserStorageUsage)
        assert len(usage) == 1
        assert usage[0].storage_usage_bytes == 0
        # Literal 5, not `StorageQuota.FREE * GB`, which restates production's
        # own expression and passes if the constant itself is wrong.
        assert usage[0].storage_quota_bytes == 5 * StorageSize.GB
        assert StorageQuota.FREE == 5

    @pytest.mark.asyncio
    async def test_all_three_rows_land_in_one_commit(self):
        """A user committed without their plan or quota row reads as an unknown
        tier, so the three inserts must not be split across transactions."""
        db, _, _ = await _register()

        assert db.commit.call_count == 1
