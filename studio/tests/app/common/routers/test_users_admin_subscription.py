"""
Tests for PUT /admin/users/{user_id}/subscription endpoint.

Tests the admin subscription update feature including:
- Schema validation (Pydantic)
- Business logic validation (CRUD)
- Audit log creation
- Edge cases
"""

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from studio.app.common.core.users import crud_users
from studio.app.common.models import User as UserModel
from studio.app.common.models.subscription import (
    SubscriptionAuditLog,
    UserStorageUsage,
    UserSubscription,
)
from studio.app.common.schemas.users import (
    SubscriptionAuditSnapshot,
    UserSubscriptionUpdate,
)
from studio.tests.app.common.sqlite_harness import sqlite_session

FREE_PLAN = 1
PREMIUM_PLAN = 2

# ============================================================================
# Schema Validation Tests (UserSubscriptionUpdate)
# ============================================================================


class TestUserSubscriptionUpdateSchema:
    """Tests for UserSubscriptionUpdate Pydantic model validation."""

    def test_valid_premium_update(self):
        """Valid Premium plan update with all fields."""
        data = UserSubscriptionUpdate(
            plan_id=2,
            expiration=datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
            storage_quota_bytes=214748364800,
            reason="Trial extension per support ticket #123",
        )
        assert data.plan_id == 2
        assert data.storage_quota_bytes == 214748364800
        assert data.reason == "Trial extension per support ticket #123"

    def test_valid_free_update_no_expiration(self):
        """Valid Free plan update without expiration."""
        data = UserSubscriptionUpdate(
            plan_id=1,
            expiration=None,
            storage_quota_bytes=5368709120,
            reason="Downgrade to free",
        )
        assert data.plan_id == 1
        assert data.expiration is None

    def test_storage_quota_must_be_positive(self):
        """storage_quota_bytes must be > 0."""
        with pytest.raises(ValidationError) as exc_info:
            UserSubscriptionUpdate(
                plan_id=1,
                storage_quota_bytes=0,
                reason="test",
            )
        assert "storage_quota_bytes" in str(exc_info.value)

    def test_storage_quota_rejects_negative(self):
        """storage_quota_bytes rejects negative values."""
        with pytest.raises(ValidationError) as exc_info:
            UserSubscriptionUpdate(
                plan_id=1,
                storage_quota_bytes=-1,
                reason="test",
            )
        assert "storage_quota_bytes" in str(exc_info.value)

    def test_reason_cannot_be_empty(self):
        """reason must be non-empty."""
        with pytest.raises(ValidationError) as exc_info:
            UserSubscriptionUpdate(
                plan_id=1,
                storage_quota_bytes=5368709120,
                reason="",
            )
        assert "reason" in str(exc_info.value)

    def test_reason_is_required(self):
        """reason field is required."""
        with pytest.raises(ValidationError):
            UserSubscriptionUpdate(
                plan_id=1,
                storage_quota_bytes=5368709120,
            )

    def test_plan_id_is_required(self):
        """plan_id field is required."""
        with pytest.raises(ValidationError):
            UserSubscriptionUpdate(
                storage_quota_bytes=5368709120,
                reason="test",
            )

    def test_storage_quota_is_required(self):
        """storage_quota_bytes field is required."""
        with pytest.raises(ValidationError):
            UserSubscriptionUpdate(
                plan_id=1,
                reason="test",
            )

    def test_plan_id_must_be_int(self):
        """plan_id rejects non-integer values."""
        with pytest.raises(ValidationError):
            UserSubscriptionUpdate(
                plan_id="invalid",
                storage_quota_bytes=5368709120,
                reason="test",
            )

    def test_expiration_accepts_iso_string(self):
        """expiration field parses ISO 8601 strings."""
        data = UserSubscriptionUpdate(
            plan_id=2,
            expiration="2026-12-31T23:59:59Z",
            storage_quota_bytes=5368709120,
            reason="test",
        )
        assert data.expiration is not None
        assert data.expiration.year == 2026

    def test_expiration_rejects_invalid_string(self):
        """expiration rejects non-datetime strings."""
        with pytest.raises(ValidationError):
            UserSubscriptionUpdate(
                plan_id=2,
                expiration="not-a-date",
                storage_quota_bytes=5368709120,
                reason="test",
            )


# ============================================================================
# SubscriptionAuditSnapshot Tests
# ============================================================================


class TestSubscriptionAuditSnapshot:
    """Tests for audit log snapshot model."""

    def test_snapshot_with_expiration(self):
        """Snapshot captures all fields including expiration."""
        snapshot = SubscriptionAuditSnapshot(
            plan_id=2,
            expiration="2026-12-31T23:59:59+00:00",
            storage_quota_bytes=214748364800,
        )
        assert snapshot.plan_id == 2
        assert snapshot.expiration == "2026-12-31T23:59:59+00:00"
        assert snapshot.storage_quota_bytes == 214748364800

    def test_snapshot_without_expiration(self):
        """Snapshot allows null expiration."""
        snapshot = SubscriptionAuditSnapshot(
            plan_id=1,
            expiration=None,
            storage_quota_bytes=5368709120,
        )
        assert snapshot.expiration is None

    def test_snapshot_dict_serialization(self):
        """Snapshot serializes to dict for JSON storage."""
        snapshot = SubscriptionAuditSnapshot(
            plan_id=2,
            expiration="2026-12-31T23:59:59+00:00",
            storage_quota_bytes=214748364800,
        )
        dumped = snapshot.dict()
        assert isinstance(dumped, dict)
        assert dumped["plan_id"] == 2
        assert dumped["expiration"] == "2026-12-31T23:59:59+00:00"
        assert dumped["storage_quota_bytes"] == 214748364800

    def test_snapshot_all_fields_null(self):
        """Snapshot allows every field null (record did not exist before)."""
        snapshot = SubscriptionAuditSnapshot()
        assert snapshot.plan_id is None
        assert snapshot.expiration is None
        assert snapshot.storage_quota_bytes is None
        assert snapshot.dict() == {
            "plan_id": None,
            "expiration": None,
            "storage_quota_bytes": None,
        }

    def test_snapshot_mixed_null(self):
        """Snapshot allows one row present and the other absent."""
        snapshot = SubscriptionAuditSnapshot(plan_id=1, storage_quota_bytes=None)
        assert snapshot.plan_id == 1
        assert snapshot.storage_quota_bytes is None


# ============================================================================
# CRUD Tests (update_user_subscription_admin) — real DB session
# ============================================================================


@pytest.fixture()
def db():
    """In-memory SQLite session with all tables created."""
    with sqlite_session(
        [
            # Only the tables this logic touches - the full metadata includes
            # tables with MySQL-specific DDL that SQLite cannot create.
            UserModel.__table__,
            UserSubscription.__table__,
            UserStorageUsage.__table__,
            SubscriptionAuditLog.__table__,
        ]
    ) as session:
        yield session


@pytest.fixture()
def seeded_user(db):
    """An active user; returns its id. No subscription/storage rows."""
    user = UserModel(
        organization_id=1,
        uid="uid-test",
        name="Test User",
        email="test@example.com",
        attributes={},
        active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user.id


@pytest.fixture()
def admin_user():
    return SimpleNamespace(id=99, organization=SimpleNamespace(id=1))


def _call(db, user_id, data, admin_user):
    # get_user_with_context re-queries with joins irrelevant to this logic;
    # stub it so the test focuses on the upsert + audit behavior.
    async def _stub(_db, _uid):
        return _uid

    original = crud_users.get_user_with_context
    crud_users.get_user_with_context = _stub
    try:
        return asyncio.run(
            crud_users.update_user_subscription_admin(db, user_id, data, admin_user)
        )
    finally:
        crud_users.get_user_with_context = original


class TestUpdateUserSubscriptionAdminCRUD:
    """Business-logic + audit coverage for the admin subscription upsert."""

    def test_creates_both_rows_when_missing(self, db, seeded_user, admin_user, caplog):
        data = UserSubscriptionUpdate(
            plan_id=PREMIUM_PLAN,
            expiration=datetime(2026, 12, 31, tzinfo=timezone.utc),
            storage_quota_bytes=214748364800,
            reason="grant premium",
        )
        with caplog.at_level(logging.WARNING):
            _call(db, seeded_user, data, admin_user)

        # Both rows were missing -> the warning names both tables.
        assert "creating missing" in caplog.text
        assert "subscription_users" in caplog.text
        assert "user_storage_usage" in caplog.text

        sub = (
            db.query(UserSubscription)
            .filter(UserSubscription.user_id == seeded_user)
            .one()
        )
        storage = (
            db.query(UserStorageUsage)
            .filter(UserStorageUsage.user_id == seeded_user)
            .one()
        )
        assert sub.plan_id == PREMIUM_PLAN
        assert sub.scheduled_downgrade is False
        assert storage.storage_quota_bytes == 214748364800

        log = db.query(SubscriptionAuditLog).one()
        assert log.old_value == {
            "plan_id": None,
            "expiration": None,
            "storage_quota_bytes": None,
        }
        assert log.new_value["plan_id"] == PREMIUM_PLAN
        assert log.new_value["storage_quota_bytes"] == 214748364800

    def test_creates_subscription_when_only_storage_exists(
        self, db, seeded_user, admin_user
    ):
        db.add(
            UserStorageUsage(
                user_id=seeded_user,
                storage_usage_bytes=0,
                storage_quota_bytes=5368709120,
            )
        )
        db.commit()

        data = UserSubscriptionUpdate(
            plan_id=PREMIUM_PLAN,
            expiration=datetime(2026, 12, 31, tzinfo=timezone.utc),
            storage_quota_bytes=214748364800,
            reason="upgrade",
        )
        _call(db, seeded_user, data, admin_user)

        assert (
            db.query(UserSubscription)
            .filter(UserSubscription.user_id == seeded_user)
            .one()
            .plan_id
            == PREMIUM_PLAN
        )
        log = db.query(SubscriptionAuditLog).one()
        # storage existed -> its old quota is recorded; subscription did not.
        assert log.old_value["plan_id"] is None
        assert log.old_value["storage_quota_bytes"] == 5368709120

    def test_updates_existing_rows(self, db, seeded_user, admin_user, caplog):
        db.add(
            UserSubscription(
                user_id=seeded_user,
                plan_id=PREMIUM_PLAN,
                expiration=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )
        db.add(
            UserStorageUsage(
                user_id=seeded_user,
                storage_usage_bytes=0,
                storage_quota_bytes=214748364800,
            )
        )
        db.commit()

        data = UserSubscriptionUpdate(
            plan_id=FREE_PLAN,
            expiration=None,
            storage_quota_bytes=5368709120,
            reason="downgrade to free",
        )
        with caplog.at_level(logging.WARNING):
            _call(db, seeded_user, data, admin_user)

        # Both rows existed -> no "creating missing" warning.
        assert "creating missing" not in caplog.text

        assert (
            db.query(UserSubscription)
            .filter(UserSubscription.user_id == seeded_user)
            .one()
            .plan_id
            == FREE_PLAN
        )
        log = db.query(SubscriptionAuditLog).one()
        assert log.old_value["plan_id"] == PREMIUM_PLAN
        assert log.old_value["storage_quota_bytes"] == 214748364800
        assert log.new_value["storage_quota_bytes"] == 5368709120

    def test_premium_requires_expiration(self, db, seeded_user, admin_user):
        from fastapi import HTTPException

        data = UserSubscriptionUpdate(
            plan_id=PREMIUM_PLAN,
            expiration=None,
            storage_quota_bytes=214748364800,
            reason="premium no expiration",
        )
        with pytest.raises(HTTPException) as exc:
            _call(db, seeded_user, data, admin_user)
        assert exc.value.status_code == 400
        assert db.query(UserSubscription).count() == 0

    def test_creates_storage_when_only_subscription_exists(
        self, db, seeded_user, admin_user, caplog
    ):
        db.add(
            UserSubscription(
                user_id=seeded_user,
                plan_id=PREMIUM_PLAN,
                expiration=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )
        db.commit()

        data = UserSubscriptionUpdate(
            plan_id=FREE_PLAN,
            expiration=None,
            storage_quota_bytes=5368709120,
            reason="repair storage",
        )
        with caplog.at_level(logging.WARNING):
            _call(db, seeded_user, data, admin_user)

        # Only storage was missing -> the warning names it and not the
        # subscription table (guards against inverted label/flag pairing).
        assert "user_storage_usage" in caplog.text
        assert "subscription_users" not in caplog.text

        assert (
            db.query(UserStorageUsage)
            .filter(UserStorageUsage.user_id == seeded_user)
            .one()
            .storage_quota_bytes
            == 5368709120
        )
        log = db.query(SubscriptionAuditLog).one()
        # subscription existed -> its old plan recorded; storage did not -> null.
        assert log.old_value["plan_id"] == PREMIUM_PLAN
        assert log.old_value["storage_quota_bytes"] is None


class TestInsertOrReselect:
    """The SAVEPOINT + IntegrityError -> re-select concurrency guard."""

    def test_returns_existing_row_on_unique_conflict(self, db, seeded_user):
        existing = UserSubscription(
            user_id=seeded_user,
            plan_id=PREMIUM_PLAN,
            expiration=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        db.add(existing)
        db.commit()

        # A concurrent writer's row would collide on the user_id unique
        # constraint; the guard must return the winner, not the duplicate.
        duplicate = UserSubscription(
            user_id=seeded_user,
            plan_id=FREE_PLAN,
            expiration=datetime(2027, 1, 1, tzinfo=timezone.utc),
        )
        result = crud_users._insert_or_reselect(
            db, duplicate, UserSubscription, seeded_user
        )

        assert result.plan_id == PREMIUM_PLAN
        assert (
            db.query(UserSubscription)
            .filter(UserSubscription.user_id == seeded_user)
            .count()
            == 1
        )

    def test_reraises_when_no_existing_row(self, db):
        from sqlalchemy.exc import IntegrityError

        # expiration is NOT NULL: this raises IntegrityError on flush for a
        # reason other than the unique constraint, and no row exists to
        # re-select, so the error must propagate rather than be swallowed.
        bad = UserSubscription(user_id=424242, plan_id=PREMIUM_PLAN, expiration=None)
        with pytest.raises(IntegrityError):
            crud_users._insert_or_reselect(db, bad, UserSubscription, 424242)
