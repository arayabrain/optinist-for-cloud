"""
Unit tests for user deletion functionality.

Tests cover:
- Firebase deletion ordering with two-phase commit
- DeletionStep tracking
- UserDeletionRecord state management
- Recovery from incomplete deletions
"""

from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException
from firebase_admin.exceptions import FirebaseError

from studio.app.common.core.users.crud_users import (
    check_firebase_account_exists,
    delete_user,
    recover_incomplete_deletions,
    resume_deletion_from_step,
)
from studio.app.common.core.utils.datetime_utils import get_current_datetime
from studio.app.common.models.subscription import (
    DeletionStatus,
    DeletionStep,
    UserDeletionRecord,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_db():
    """Mock database session."""
    db = Mock()
    db.query = Mock()
    db.add = Mock()
    db.commit = Mock()
    db.rollback = Mock()
    return db


@pytest.fixture
def mock_user():
    """Create a mock user object."""
    user = Mock()
    user.id = 1
    user.uid = "test-firebase-uid"
    user.email = "test@example.com"
    user.name = "Test User"
    user.active = True
    user.organization_id = 1
    user.remote_bucket_name = "test-bucket"
    return user


@pytest.fixture
def mock_deletion_record():
    """Create a mock deletion record."""
    record = Mock(spec=UserDeletionRecord)
    record.id = 1
    record.user_id = 1
    record.user_uid = "test-firebase-uid"
    record.step = DeletionStep.STARTED.value
    record.status = DeletionStatus.IN_PROGRESS.value
    record.error = None
    record.started_at = get_current_datetime() - timedelta(hours=2)
    record.completed_at = None
    return record


# ============================================================================
# Tests: DeletionStep Enum
# ============================================================================


def test_deletion_step_enum_values():
    """Verify DeletionStep enum has expected values."""
    assert DeletionStep.STARTED.value == "started"
    assert DeletionStep.FIREBASE_PENDING.value == "firebase_pending"
    assert DeletionStep.FIREBASE_DELETED.value == "firebase_deleted"
    assert DeletionStep.STRIPE_CANCELLED.value == "stripe_cancelled"
    assert DeletionStep.S3_DELETED.value == "s3_deleted"
    assert DeletionStep.WORKSPACES_DELETED.value == "workspaces_deleted"
    assert DeletionStep.COMPLETED.value == "completed"


def test_deletion_step_ordering():
    """Verify all expected deletion steps exist."""
    expected_steps = {
        "started",
        "firebase_pending",
        "firebase_deleted",
        "stripe_cancelled",
        "s3_deleted",
        "workspaces_deleted",
        "completed",
    }
    actual_steps = {s.value for s in DeletionStep}
    assert actual_steps == expected_steps


def test_deletion_status_enum_values():
    """Verify DeletionStatus enum has expected values."""
    assert DeletionStatus.IN_PROGRESS.value == "in_progress"
    assert DeletionStatus.COMPLETED.value == "completed"
    assert DeletionStatus.FAILED.value == "failed"


# ============================================================================
# Tests: UserDeletionRecord Model
# ============================================================================


def test_user_deletion_record_creation():
    """Test UserDeletionRecord can be created with required fields."""
    record = UserDeletionRecord(
        user_id=1,
        user_uid="test-uid",
        step=DeletionStep.STARTED.value,
        status=DeletionStatus.IN_PROGRESS.value,
    )
    assert record.user_id == 1
    assert record.user_uid == "test-uid"
    assert record.step == DeletionStep.STARTED.value
    assert record.status == DeletionStatus.IN_PROGRESS.value


def test_user_deletion_record_defaults():
    """Test UserDeletionRecord has correct defaults."""
    record = UserDeletionRecord(user_id=1, user_uid="test-uid")
    assert record.step == DeletionStep.STARTED.value
    assert record.status == DeletionStatus.IN_PROGRESS.value
    assert record.error is None
    assert record.completed_at is None


# ============================================================================
# Tests: delete_user - Successful Flow
# ============================================================================


@pytest.mark.asyncio
async def test_delete_user_success(mock_db, mock_user):
    """Test successful user deletion with correct ordering."""
    mock_workspace = Mock()
    mock_workspace.id = 7
    mock_query_result = Mock()
    mock_query_result.filter.return_value = mock_query_result
    mock_query_result.first.return_value = mock_user
    mock_query_result.all.return_value = [mock_workspace]
    mock_db.query.return_value = mock_query_result

    steps_executed = []

    def track_commit():
        # Track the deletion record step at each commit
        if hasattr(mock_db, "_deletion_record") and mock_db._deletion_record:
            steps_executed.append(mock_db._deletion_record.step)

    mock_db.commit.side_effect = track_commit

    deletion_record = None

    def capture_add(record):
        nonlocal deletion_record
        if hasattr(record, "step"):
            deletion_record = record
            mock_db._deletion_record = record

    mock_db.add.side_effect = capture_add

    with patch("studio.app.common.core.users.crud_users.firebase_auth") as mock_fb:
        with patch(
            "studio.app.common.core.users.crud_users.StripeService"
        ) as mock_stripe:
            mock_stripe.handle_cancel_user_subscription = AsyncMock()
            with patch(
                "studio.app.common.core.users.crud_users.RemoteStorageController"
            ) as mock_storage:
                mock_storage.is_available.return_value = False
                with patch(
                    "studio.app.common.core.users.crud_users.WorkspaceService"
                ) as mock_workspaces:
                    mock_workspaces.initiate_workspace_deletion = AsyncMock(
                        return_value=(True, "ok")
                    )

                    result = await delete_user(mock_db, 1, 1)

    assert result is True
    mock_fb.delete_user.assert_called_once_with(mock_user.uid)
    mock_stripe.handle_cancel_user_subscription.assert_called_once()
    mock_workspaces.initiate_workspace_deletion.assert_awaited_once_with(
        mock_db, mock_user.remote_bucket_name, 7, 1
    )
    assert mock_user.active is False
    assert deletion_record.status == DeletionStatus.COMPLETED.value
    assert steps_executed == [
        DeletionStep.STARTED.value,
        DeletionStep.FIREBASE_PENDING.value,
        DeletionStep.FIREBASE_DELETED.value,
        DeletionStep.STRIPE_CANCELLED.value,
        DeletionStep.S3_DELETED.value,
        DeletionStep.WORKSPACES_DELETED.value,
        DeletionStep.COMPLETED.value,
    ]


@pytest.mark.asyncio
async def test_delete_user_firebase_deleted_first(mock_db, mock_user):
    """Test that Firebase is deleted BEFORE any other operations."""
    mock_query_result = Mock()
    mock_query_result.filter.return_value = mock_query_result
    mock_query_result.first.return_value = mock_user
    mock_query_result.all.return_value = []
    mock_db.query.return_value = mock_query_result

    call_order = []

    with patch("studio.app.common.core.users.crud_users.firebase_auth") as mock_fb:
        mock_fb.delete_user.side_effect = lambda uid: call_order.append("firebase")

        with patch(
            "studio.app.common.core.users.crud_users.StripeService"
        ) as mock_stripe:

            async def stripe_side_effect(*args, **kwargs):
                call_order.append("stripe")

            mock_stripe.handle_cancel_user_subscription = AsyncMock(
                side_effect=stripe_side_effect
            )

            with patch(
                "studio.app.common.core.users.crud_users.RemoteStorageController"
            ) as mock_storage:
                mock_storage.is_available.return_value = False

                await delete_user(mock_db, 1, 1)

    # Firebase must be first
    assert call_order[0] == "firebase"
    assert "stripe" in call_order


@pytest.mark.asyncio
async def test_delete_user_creates_deletion_record(mock_db, mock_user):
    """Test that deletion record is created at start."""
    mock_query_result = Mock()
    mock_query_result.filter.return_value = mock_query_result
    mock_query_result.first.return_value = mock_user
    mock_query_result.all.return_value = []
    mock_db.query.return_value = mock_query_result

    added_objects = []
    mock_db.add.side_effect = lambda obj: added_objects.append(obj)

    with patch("studio.app.common.core.users.crud_users.firebase_auth"):
        with patch(
            "studio.app.common.core.users.crud_users.StripeService"
        ) as mock_stripe:
            mock_stripe.handle_cancel_user_subscription = AsyncMock()
            with patch(
                "studio.app.common.core.users.crud_users.RemoteStorageController"
            ) as mock_storage:
                mock_storage.is_available.return_value = False

                await delete_user(mock_db, 1, 1)

    # Should have added a UserDeletionRecord
    deletion_records = [
        obj for obj in added_objects if isinstance(obj, UserDeletionRecord)
    ]
    assert len(deletion_records) == 1
    assert deletion_records[0].user_id == 1
    assert deletion_records[0].user_uid == mock_user.uid


# ============================================================================
# Tests: delete_user - Firebase Failure Handling
# ============================================================================


@pytest.mark.asyncio
async def test_delete_user_firebase_failure_aborts(mock_db, mock_user):
    """Test that Firebase failure aborts deletion before other changes."""
    mock_query_result = Mock()
    mock_query_result.filter.return_value = mock_query_result
    mock_query_result.first.return_value = mock_user
    mock_query_result.all.return_value = []
    mock_db.query.return_value = mock_query_result

    with patch("studio.app.common.core.users.crud_users.firebase_auth") as mock_fb:
        mock_fb.delete_user.side_effect = FirebaseError(code=500, message="Auth error")

        with patch(
            "studio.app.common.core.users.crud_users.StripeService"
        ) as mock_stripe:
            mock_stripe.handle_cancel_user_subscription = AsyncMock()

            with pytest.raises(HTTPException) as exc_info:
                await delete_user(mock_db, 1, 1)

    assert exc_info.value.status_code == 400
    assert "Firebase" in exc_info.value.detail

    # Stripe should NOT have been called
    mock_stripe.handle_cancel_user_subscription.assert_not_called()

    # User should still be active
    assert mock_user.active is True


@pytest.mark.asyncio
async def test_delete_user_firebase_failure_records_error(mock_db, mock_user):
    """Test that Firebase failure is recorded in deletion record."""
    mock_query_result = Mock()
    mock_query_result.filter.return_value = mock_query_result
    mock_query_result.first.return_value = mock_user
    mock_query_result.all.return_value = []
    mock_db.query.return_value = mock_query_result

    deletion_record = None

    def capture_add(obj):
        nonlocal deletion_record
        if isinstance(obj, UserDeletionRecord):
            deletion_record = obj

    mock_db.add.side_effect = capture_add

    with patch("studio.app.common.core.users.crud_users.firebase_auth") as mock_fb:
        mock_fb.delete_user.side_effect = FirebaseError(code=500, message="Auth error")

        with pytest.raises(HTTPException):
            await delete_user(mock_db, 1, 1)

    assert deletion_record is not None
    assert deletion_record.status == DeletionStatus.FAILED.value
    assert deletion_record.error is not None


# ============================================================================
# Tests: delete_user - Two-Phase Firebase Commit
# ============================================================================


@pytest.mark.asyncio
async def test_delete_user_firebase_pending_marked_before_call(mock_db, mock_user):
    """Test firebase_pending is marked BEFORE calling Firebase API."""
    mock_query_result = Mock()
    mock_query_result.filter.return_value = mock_query_result
    mock_query_result.first.return_value = mock_user
    mock_query_result.all.return_value = []
    mock_db.query.return_value = mock_query_result

    deletion_record = None
    step_when_firebase_called = None

    def capture_add(obj):
        nonlocal deletion_record
        if isinstance(obj, UserDeletionRecord):
            deletion_record = obj

    mock_db.add.side_effect = capture_add

    with patch("studio.app.common.core.users.crud_users.firebase_auth") as mock_fb:

        def capture_step_on_firebase_call(uid):
            nonlocal step_when_firebase_called
            step_when_firebase_called = deletion_record.step

        mock_fb.delete_user.side_effect = capture_step_on_firebase_call

        with patch(
            "studio.app.common.core.users.crud_users.StripeService"
        ) as mock_stripe:
            mock_stripe.handle_cancel_user_subscription = AsyncMock()
            with patch(
                "studio.app.common.core.users.crud_users.RemoteStorageController"
            ) as mock_storage:
                mock_storage.is_available.return_value = False

                await delete_user(mock_db, 1, 1)

    # When Firebase was called, step should have been firebase_pending
    assert step_when_firebase_called == DeletionStep.FIREBASE_PENDING.value


@pytest.mark.asyncio
async def test_delete_user_firebase_deleted_marked_after_call(mock_db, mock_user):
    """Test firebase_deleted is marked AFTER successful Firebase call."""
    mock_query_result = Mock()
    mock_query_result.filter.return_value = mock_query_result
    mock_query_result.first.return_value = mock_user
    mock_query_result.all.return_value = []
    mock_db.query.return_value = mock_query_result

    deletion_record = None
    steps_after_commits = []

    def capture_add(obj):
        nonlocal deletion_record
        if isinstance(obj, UserDeletionRecord):
            deletion_record = obj

    mock_db.add.side_effect = capture_add

    def track_commit():
        if deletion_record:
            steps_after_commits.append(deletion_record.step)

    mock_db.commit.side_effect = track_commit

    with patch("studio.app.common.core.users.crud_users.firebase_auth"):
        with patch(
            "studio.app.common.core.users.crud_users.StripeService"
        ) as mock_stripe:
            mock_stripe.handle_cancel_user_subscription = AsyncMock()
            with patch(
                "studio.app.common.core.users.crud_users.RemoteStorageController"
            ) as mock_storage:
                mock_storage.is_available.return_value = False

                await delete_user(mock_db, 1, 1)

    # Should have: started, firebase_pending, firebase_deleted, ...
    assert DeletionStep.FIREBASE_PENDING.value in steps_after_commits
    pending_idx = steps_after_commits.index(DeletionStep.FIREBASE_PENDING.value)
    deleted_idx = steps_after_commits.index(DeletionStep.FIREBASE_DELETED.value)
    assert deleted_idx > pending_idx


# ============================================================================
# Tests: delete_user - User Not Found
# ============================================================================


@pytest.mark.asyncio
async def test_delete_user_not_found_raises_404(mock_db):
    """Test user not found raises 404."""
    mock_query_result = Mock()
    mock_query_result.filter.return_value = mock_query_result
    mock_query_result.first.return_value = None
    mock_db.query.return_value = mock_query_result

    with pytest.raises(HTTPException) as exc_info:
        await delete_user(mock_db, 999, 1)

    assert exc_info.value.status_code == 404
    assert "not found" in exc_info.value.detail.lower()


# ============================================================================
# Tests: delete_user - Graceful Failure Handling
# ============================================================================


@pytest.mark.asyncio
async def test_delete_user_stripe_failure_continues(mock_db, mock_user):
    """Test that Stripe failure doesn't abort deletion."""
    mock_query_result = Mock()
    mock_query_result.filter.return_value = mock_query_result
    mock_query_result.first.return_value = mock_user
    mock_query_result.all.return_value = []
    mock_db.query.return_value = mock_query_result

    with patch("studio.app.common.core.users.crud_users.firebase_auth"):
        with patch(
            "studio.app.common.core.users.crud_users.StripeService"
        ) as mock_stripe:
            mock_stripe.handle_cancel_user_subscription = AsyncMock(
                side_effect=Exception("Stripe error")
            )
            with patch(
                "studio.app.common.core.users.crud_users.RemoteStorageController"
            ) as mock_storage:
                mock_storage.is_available.return_value = False

                # Should not raise - Stripe failure is non-fatal
                result = await delete_user(mock_db, 1, 1)

    assert result is True
    assert mock_user.active is False


@pytest.mark.asyncio
async def test_delete_user_s3_failure_continues(mock_db, mock_user):
    """Test that S3 failure doesn't abort deletion."""
    mock_query_result = Mock()
    mock_query_result.filter.return_value = mock_query_result
    mock_query_result.first.return_value = mock_user
    mock_query_result.all.return_value = []
    mock_db.query.return_value = mock_query_result

    with patch("studio.app.common.core.users.crud_users.firebase_auth"):
        with patch(
            "studio.app.common.core.users.crud_users.StripeService"
        ) as mock_stripe:
            mock_stripe.handle_cancel_user_subscription = AsyncMock()
            with patch(
                "studio.app.common.core.users.crud_users.RemoteStorageController"
            ) as mock_storage:
                mock_storage.is_available.return_value = True
            with patch(
                "studio.app.common.core.users.crud_users.RemoteStorageSimpleWriter"
            ) as mock_writer:
                mock_context = AsyncMock()
                mock_context.delete_bucket = AsyncMock(
                    side_effect=Exception("S3 error")
                )
                mock_writer.return_value.__aenter__.return_value = mock_context

                # Should not raise - S3 failure is non-fatal
                result = await delete_user(mock_db, 1, 1)

    assert result is True
    assert mock_user.active is False


# ============================================================================
# Tests: check_firebase_account_exists
# ============================================================================


@pytest.mark.asyncio
async def test_check_firebase_account_exists_returns_true():
    """Test returns True when account exists."""
    with patch("studio.app.common.core.users.crud_users.firebase_auth") as mock_fb:
        mock_fb.get_user.return_value = Mock()

        result = await check_firebase_account_exists("test-uid")

    assert result is True
    mock_fb.get_user.assert_called_once_with("test-uid")


@pytest.mark.asyncio
async def test_check_firebase_account_exists_returns_false():
    """Test returns False when account doesn't exist."""
    from firebase_admin import auth as real_firebase_auth

    with patch("studio.app.common.core.users.crud_users.firebase_auth") as mock_fb:
        # Use the real UserNotFoundError type
        mock_fb.get_user.side_effect = real_firebase_auth.UserNotFoundError("Not found")
        mock_fb.UserNotFoundError = real_firebase_auth.UserNotFoundError

        result = await check_firebase_account_exists("test-uid")

    assert result is False


@pytest.mark.asyncio
async def test_check_firebase_account_exists_raises_on_error():
    """Test raises exception on Firebase error."""
    from firebase_admin import auth as real_firebase_auth

    with patch("studio.app.common.core.users.crud_users.firebase_auth") as mock_fb:
        mock_fb.get_user.side_effect = RuntimeError("API error")
        mock_fb.UserNotFoundError = real_firebase_auth.UserNotFoundError

        with pytest.raises(RuntimeError):
            await check_firebase_account_exists("test-uid")


# ============================================================================
# Tests: recover_incomplete_deletions
# ============================================================================


@pytest.mark.asyncio
async def test_recover_incomplete_deletions_finds_old_records(mock_db):
    """Test recovery finds records older than 1 hour."""
    old_record = Mock(spec=UserDeletionRecord)
    old_record.user_id = 1
    old_record.user_uid = "test-uid"
    old_record.step = DeletionStep.FIREBASE_DELETED.value
    old_record.status = DeletionStatus.IN_PROGRESS.value
    old_record.started_at = get_current_datetime() - timedelta(hours=2)

    mock_query_result = Mock()
    mock_query_result.filter.return_value = mock_query_result
    mock_query_result.all.return_value = [old_record]
    mock_query_result.first.return_value = None  # User not found = completed
    mock_db.query.return_value = mock_query_result

    with patch(
        "studio.app.common.core.users.crud_users.resume_deletion_from_step",
        new_callable=AsyncMock,
    ) as mock_resume:
        mock_resume.return_value = True

        count = await recover_incomplete_deletions(mock_db)

    assert count == 1


@pytest.mark.asyncio
async def test_recover_incomplete_deletions_handles_firebase_pending(mock_db):
    """Test recovery checks Firebase state for pending records."""
    pending_record = Mock(spec=UserDeletionRecord)
    pending_record.user_id = 1
    pending_record.user_uid = "test-uid"
    pending_record.step = DeletionStep.FIREBASE_PENDING.value
    pending_record.status = DeletionStatus.IN_PROGRESS.value
    pending_record.started_at = get_current_datetime() - timedelta(hours=2)

    mock_query_result = Mock()
    mock_query_result.filter.return_value = mock_query_result
    mock_query_result.all.return_value = [pending_record]
    mock_db.query.return_value = mock_query_result

    with patch(
        "studio.app.common.core.users.crud_users.check_firebase_account_exists",
        new_callable=AsyncMock,
    ) as mock_check:
        mock_check.return_value = False  # Firebase account was deleted

        with patch(
            "studio.app.common.core.users.crud_users.resume_deletion_from_step",
            new_callable=AsyncMock,
        ):
            await recover_incomplete_deletions(mock_db)

    # Should have updated step to firebase_deleted
    assert pending_record.step == DeletionStep.FIREBASE_DELETED.value


@pytest.mark.asyncio
async def test_recover_firebase_pending_still_exists_marks_failed(mock_db):
    """Test marks failed if Firebase still exists in pending state."""
    pending_record = Mock(spec=UserDeletionRecord)
    pending_record.user_id = 1
    pending_record.user_uid = "test-uid"
    pending_record.step = DeletionStep.FIREBASE_PENDING.value
    pending_record.status = DeletionStatus.IN_PROGRESS.value
    pending_record.started_at = get_current_datetime() - timedelta(hours=2)
    pending_record.error = None

    mock_query_result = Mock()
    mock_query_result.filter.return_value = mock_query_result
    mock_query_result.all.return_value = [pending_record]
    mock_db.query.return_value = mock_query_result

    with patch(
        "studio.app.common.core.users.crud_users.check_firebase_account_exists",
        new_callable=AsyncMock,
    ) as mock_check:
        mock_check.return_value = True  # Firebase still exists

        await recover_incomplete_deletions(mock_db)

    # Should be marked as failed
    assert pending_record.status == DeletionStatus.FAILED.value
    assert "still exists" in pending_record.error


# ============================================================================
# Tests: resume_deletion_from_step
# ============================================================================


@pytest.mark.asyncio
async def test_resume_deletion_completes_if_user_not_found(mock_db):
    """Test resume completes if user no longer exists."""
    record = Mock(spec=UserDeletionRecord)
    record.user_id = 1
    record.step = DeletionStep.STRIPE_CANCELLED.value
    record.status = DeletionStatus.IN_PROGRESS.value

    mock_query_result = Mock()
    mock_query_result.filter.return_value = mock_query_result
    mock_query_result.first.return_value = None  # User not found
    mock_db.query.return_value = mock_query_result

    result = await resume_deletion_from_step(record, mock_db)

    assert result is True
    assert record.status == DeletionStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_resume_deletion_skips_completed_steps(mock_db, mock_user):
    """Test resume skips already completed steps."""
    record = Mock(spec=UserDeletionRecord)
    record.user_id = 1
    record.step = DeletionStep.S3_DELETED.value  # Start after S3

    mock_query_result = Mock()
    mock_query_result.filter.return_value = mock_query_result
    mock_query_result.first.return_value = mock_user
    mock_query_result.all.return_value = []
    mock_db.query.return_value = mock_query_result

    with patch("studio.app.common.core.users.crud_users.StripeService") as mock_stripe:
        mock_stripe.handle_cancel_user_subscription = AsyncMock()

        with patch(
            "studio.app.common.core.users.crud_users.RemoteStorageController"
        ) as mock_storage:
            mock_storage.is_available.return_value = False

            await resume_deletion_from_step(record, mock_db)

    # Stripe should NOT have been called (already past that step)
    mock_stripe.handle_cancel_user_subscription.assert_not_called()
    # User should be marked inactive
    assert mock_user.active is False


# ============================================================================
# Contract Tests: Cases 26-27 - Stripe/S3 Deletion Guarantees
# ============================================================================


class TestUserDeletionContract:
    """Contract tests for user deletion guarantees."""

    @pytest.mark.asyncio
    async def test_contract_firebase_deleted_blocks_login(self, mock_db, mock_user):
        """If Firebase deleted, user cannot authenticate."""
        mock_query_result = Mock()
        mock_query_result.filter.return_value = mock_query_result
        mock_query_result.first.return_value = mock_user
        mock_query_result.all.return_value = []
        mock_db.query.return_value = mock_query_result

        firebase_deleted = False

        def track_firebase_delete(uid):
            nonlocal firebase_deleted
            firebase_deleted = True

        with patch("studio.app.common.core.users.crud_users.firebase_auth") as mock_fb:
            mock_fb.delete_user.side_effect = track_firebase_delete

            with patch(
                "studio.app.common.core.users.crud_users.StripeService"
            ) as mock_stripe:
                mock_stripe.handle_cancel_user_subscription = AsyncMock()
                with patch(
                    "studio.app.common.core.users.crud_users.RemoteStorageController"
                ) as mock_storage:
                    mock_storage.is_available.return_value = False

                    await delete_user(mock_db, 1, 1)

        # Firebase must be deleted
        assert firebase_deleted is True
        # Verify Firebase was called with correct UID
        mock_fb.delete_user.assert_called_with(mock_user.uid)

    @pytest.mark.asyncio
    async def test_contract_stripe_failure_does_not_resurrect_user(
        self, mock_db, mock_user
    ):
        """Stripe failure should not prevent user from being deleted."""
        mock_query_result = Mock()
        mock_query_result.filter.return_value = mock_query_result
        mock_query_result.first.return_value = mock_user
        mock_query_result.all.return_value = []
        mock_db.query.return_value = mock_query_result

        with patch("studio.app.common.core.users.crud_users.firebase_auth"):
            with patch(
                "studio.app.common.core.users.crud_users.StripeService"
            ) as mock_stripe:
                mock_stripe.handle_cancel_user_subscription = AsyncMock(
                    side_effect=Exception("Stripe API error")
                )
                with patch(
                    "studio.app.common.core.users.crud_users.RemoteStorageController"
                ) as mock_storage:
                    mock_storage.is_available.return_value = False

                    result = await delete_user(mock_db, 1, 1)

        # User must still be marked inactive
        assert result is True
        assert mock_user.active is False

    @pytest.mark.asyncio
    async def test_contract_s3_failure_does_not_resurrect_user(
        self, mock_db, mock_user
    ):
        """S3 failure should not prevent user from being deleted."""
        mock_query_result = Mock()
        mock_query_result.filter.return_value = mock_query_result
        mock_query_result.first.return_value = mock_user
        mock_query_result.all.return_value = []
        mock_db.query.return_value = mock_query_result

        with patch("studio.app.common.core.users.crud_users.firebase_auth"):
            with patch(
                "studio.app.common.core.users.crud_users.StripeService"
            ) as mock_stripe:
                mock_stripe.handle_cancel_user_subscription = AsyncMock()
                with patch(
                    "studio.app.common.core.users.crud_users.RemoteStorageController"
                ) as mock_storage:
                    mock_storage.is_available.return_value = True
                with patch(
                    "studio.app.common.core.users.crud_users.RemoteStorageSimpleWriter"
                ) as mock_writer:
                    mock_context = AsyncMock()
                    mock_context.delete_bucket = AsyncMock(
                        side_effect=Exception("S3 API error")
                    )
                    mock_writer.return_value.__aenter__.return_value = mock_context

                    result = await delete_user(mock_db, 1, 1)

        # User must still be marked inactive
        assert result is True
        assert mock_user.active is False

    @pytest.mark.asyncio
    async def test_contract_incomplete_deletion_recoverable_from_any_step(
        self, mock_db, mock_user
    ):
        """Incomplete deletion must be recoverable from any step."""
        # Test recovery from each deletion step
        recoverable_steps = [
            DeletionStep.FIREBASE_DELETED,  # After Firebase deleted
            DeletionStep.STRIPE_CANCELLED,  # After Stripe cancelled
            DeletionStep.S3_DELETED,  # After S3 deleted
            DeletionStep.WORKSPACES_DELETED,  # After workspaces deleted
        ]

        for step in recoverable_steps:
            record = Mock(spec=UserDeletionRecord)
            record.user_id = 1
            record.user_uid = mock_user.uid
            record.step = step.value
            record.status = DeletionStatus.IN_PROGRESS.value

            mock_query_result = Mock()
            mock_query_result.filter.return_value = mock_query_result
            mock_query_result.first.return_value = mock_user
            mock_query_result.all.return_value = []
            mock_db.query.return_value = mock_query_result

            with patch(
                "studio.app.common.core.users.crud_users.StripeService"
            ) as mock_stripe:
                mock_stripe.handle_cancel_user_subscription = AsyncMock()
                with patch(
                    "studio.app.common.core.users.crud_users.RemoteStorageController"
                ) as mock_storage:
                    mock_storage.is_available.return_value = False

                    result = await resume_deletion_from_step(record, mock_db)

            # Must reach completed state
            assert result is True, f"Recovery failed from step: {step.value}"
            assert record.status == DeletionStatus.COMPLETED.value

    @pytest.mark.asyncio
    async def test_contract_deletion_order_is_firebase_first(self, mock_db, mock_user):
        """Firebase must be deleted before any other cleanup."""
        mock_query_result = Mock()
        mock_query_result.filter.return_value = mock_query_result
        mock_query_result.first.return_value = mock_user
        mock_query_result.all.return_value = []
        mock_db.query.return_value = mock_query_result

        operation_order = []

        with patch("studio.app.common.core.users.crud_users.firebase_auth") as mock_fb:
            mock_fb.delete_user.side_effect = lambda uid: operation_order.append(
                "firebase"
            )

            with patch(
                "studio.app.common.core.users.crud_users.StripeService"
            ) as mock_stripe:

                async def stripe_op(*args, **kwargs):
                    operation_order.append("stripe")

                mock_stripe.handle_cancel_user_subscription = AsyncMock(
                    side_effect=stripe_op
                )
                with patch(
                    "studio.app.common.core.users.crud_users.RemoteStorageController"
                ) as mock_storage:
                    mock_storage.is_available.return_value = True
                with patch(
                    "studio.app.common.core.users.crud_users.RemoteStorageSimpleWriter"
                ) as mock_writer:
                    mock_context = AsyncMock()

                    async def s3_op(*args, **kwargs):
                        operation_order.append("s3")

                    mock_context.delete_bucket = AsyncMock(side_effect=s3_op)
                    mock_writer.return_value.__aenter__.return_value = mock_context

                    await delete_user(mock_db, 1, 1)

        # Firebase must be first
        assert len(operation_order) >= 1
        assert operation_order[0] == "firebase"
        # Other operations should follow
        if len(operation_order) >= 2:
            assert operation_order[1] == "stripe"

    @pytest.mark.asyncio
    async def test_contract_no_orphaned_state_on_failure(self, mock_db, mock_user):
        """User should not be in orphaned state on any failure path."""
        mock_query_result = Mock()
        mock_query_result.filter.return_value = mock_query_result
        mock_query_result.first.return_value = mock_user
        mock_query_result.all.return_value = []
        mock_db.query.return_value = mock_query_result

        added_records = []
        mock_db.add.side_effect = lambda obj: added_records.append(obj)

        with patch("studio.app.common.core.users.crud_users.firebase_auth") as mock_fb:
            mock_fb.delete_user.side_effect = FirebaseError(
                code=500, message="Auth error"
            )

            with pytest.raises(HTTPException):
                await delete_user(mock_db, 1, 1)

        # Find deletion record
        deletion_records = [
            r for r in added_records if isinstance(r, UserDeletionRecord)
        ]
        assert len(deletion_records) == 1
        deletion_record = deletion_records[0]

        # Record must have proper status for recovery
        assert deletion_record.status == DeletionStatus.FAILED.value
        # Error must be recorded
        assert deletion_record.error is not None
        # User must still be active (nothing was deleted)
        assert mock_user.active is True


# ============================================================================
# Tests: deleting a user who owns data, against a real database
# ============================================================================


class TestDeleteUserWhoOwnsData:
    """``delete_user`` over a real session, with rows to lose.

    Everything else in this file drives a ``Mock`` session, where the user stays
    active, the workspace query answers ``[]`` and no subscription row exists.
    Three claims are only provable against a database: the user ends inactive,
    the deletion record reaches ``completed``, and the billing history is left
    standing.
    """

    PLAN_PREMIUM = 2

    @pytest.fixture()
    def db(self):
        from studio.app.common.models import Organization as OrganizationModel
        from studio.app.common.models import User as UserModel
        from studio.app.common.models import Workspace
        from studio.app.common.models.subscription import (
            SubscriptionUserPurchase,
            UserSubscription,
        )
        from studio.app.common.models.user_preferences import UserPreferences
        from studio.tests.app.common.sqlite_harness import sqlite_session

        tables = [
            OrganizationModel.__table__,
            UserModel.__table__,
            Workspace.__table__,
            UserPreferences.__table__,
            UserSubscription.__table__,
            SubscriptionUserPurchase.__table__,
            UserDeletionRecord.__table__,
        ]
        with sqlite_session(tables) as session:
            session.add(OrganizationModel(id=1, name="Test Org"))
            session.add(
                UserModel(
                    id=1,
                    organization_id=1,
                    uid="uid-owns-data",
                    name="Owner",
                    email="owner@example.com",
                    attributes={},
                    active=True,
                )
            )
            session.commit()
            session.add(
                UserSubscription(
                    id=11,
                    user_id=1,
                    plan_id=self.PLAN_PREMIUM,
                    expiration=get_current_datetime() + timedelta(days=20),
                )
            )
            session.add(
                SubscriptionUserPurchase(id=21, user_id=1, plan_id=self.PLAN_PREMIUM)
            )
            session.add(
                Workspace(id=31, name="Owned workspace", user_id=1, deleted=False)
            )
            session.add(UserPreferences(id=41, user_id=1))
            session.commit()
            yield session

    @pytest.fixture()
    def externals(self):
        """Firebase, Stripe, S3 and the workspace pipeline stubbed at the seam."""
        with patch(
            "studio.app.common.core.users.crud_users.firebase_auth"
        ) as firebase, patch(
            "studio.app.common.core.users.crud_users.StripeService"
        ) as stripe, patch(
            "studio.app.common.core.users.crud_users.RemoteStorageController"
        ) as storage, patch(
            "studio.app.common.core.users.crud_users.WorkspaceService"
        ) as workspaces:
            stripe.handle_cancel_user_subscription = AsyncMock()
            storage.is_available.return_value = False
            workspaces.initiate_workspace_deletion = AsyncMock(
                return_value=(True, "ok")
            )
            yield Mock(
                firebase=firebase, stripe=stripe, storage=storage, workspaces=workspaces
            )

    @pytest.mark.asyncio
    async def test_owned_workspace_is_handed_to_the_deletion_pipeline(
        self, db, externals
    ):
        assert await delete_user(db, 1, 1) is True

        externals.workspaces.initiate_workspace_deletion.assert_awaited_once()
        args = externals.workspaces.initiate_workspace_deletion.await_args.args
        assert args[2] == 31
        assert args[3] == 1

    @pytest.mark.asyncio
    async def test_the_user_ends_inactive_and_the_record_reaches_completed(
        self, db, externals
    ):
        await delete_user(db, 1, 1)

        from studio.app.common.models import User as UserModel

        user = db.get(UserModel, 1)
        assert user.active is False

        record = db.query(UserDeletionRecord).one()
        assert record.step == DeletionStep.COMPLETED.value
        assert record.status == DeletionStatus.COMPLETED.value
        assert record.completed_at is not None
        assert record.error is None

    @pytest.mark.asyncio
    async def test_user_preferences_are_removed(self, db, externals):
        from studio.app.common.models.user_preferences import UserPreferences

        await delete_user(db, 1, 1)

        assert db.query(UserPreferences).count() == 0

    @pytest.mark.asyncio
    async def test_the_subscription_and_purchase_history_survive_unchanged(
        self, db, externals
    ):
        """Billing history is the audit trail for a paid account and
        must outlive the account itself."""
        from studio.app.common.models.subscription import (
            SubscriptionUserPurchase,
            UserSubscription,
        )

        columns = [
            "id",
            "user_id",
            "plan_id",
            "expiration",
            "scheduled_downgrade",
            "sync_status",
            "deletion_processed_at",
        ]
        before_subscription = {
            c: getattr(db.query(UserSubscription).one(), c) for c in columns
        }
        purchase_columns = ["id", "user_id", "plan_id", "created_at"]
        before_purchase = {
            c: getattr(db.query(SubscriptionUserPurchase).one(), c)
            for c in purchase_columns
        }

        await delete_user(db, 1, 1)
        db.expire_all()

        assert db.query(UserSubscription).count() == 1
        assert db.query(SubscriptionUserPurchase).count() == 1
        after_subscription = {
            c: getattr(db.query(UserSubscription).one(), c) for c in columns
        }
        after_purchase = {
            c: getattr(db.query(SubscriptionUserPurchase).one(), c)
            for c in purchase_columns
        }
        assert after_subscription == before_subscription
        assert after_purchase == before_purchase
