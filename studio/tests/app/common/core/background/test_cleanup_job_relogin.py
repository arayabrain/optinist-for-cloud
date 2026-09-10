"""
Tests for cleanup job re-login detection.
Verifies that the cleanup job properly detects when a user logs back in
and aborts cleanup to prevent race conditions.
"""
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

MODULE = "studio.app.common.core.background.cleanup_job"


class TestCleanupJobReloginDetection:
    """Tests for cleanup job re-login detection."""

    @patch(f"{MODULE}.session_scope")
    def test_check_user_relogin_returns_true_when_logged_back_in(self, mock_session):
        """Should return True if user has logged back in (logged_out_at is NULL)."""
        from studio.app.common.core.background.cleanup_job import DataCleanupJob

        mock_db = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_db

        # User logged back in - logged_out_at is None
        mock_assignment = MagicMock()
        mock_assignment.logged_out_at = None
        mock_result = MagicMock()
        mock_result.first.return_value = (mock_assignment,)
        mock_db.execute.return_value = mock_result

        result = DataCleanupJob._check_user_relogin(user_id="123")

        assert result is True

    @patch(f"{MODULE}.session_scope")
    def test_check_user_relogin_returns_false_when_still_logged_out(self, mock_session):
        """Should return False if user is still logged out."""
        from studio.app.common.core.background.cleanup_job import DataCleanupJob

        mock_db = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_db

        # User still logged out - logged_out_at has a value
        mock_assignment = MagicMock()
        mock_assignment.logged_out_at = datetime(2026, 2, 5, 10, 0, 0)
        mock_result = MagicMock()
        mock_result.first.return_value = (mock_assignment,)
        mock_db.execute.return_value = mock_result

        result = DataCleanupJob._check_user_relogin(user_id="123")

        assert result is False

    @patch(f"{MODULE}.session_scope")
    def test_check_user_relogin_returns_false_when_no_assignment(self, mock_session):
        """Should return False if assignment record doesn't exist."""
        from studio.app.common.core.background.cleanup_job import DataCleanupJob

        mock_db = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_db

        # No assignment record
        mock_result = MagicMock()
        mock_result.first.return_value = None
        mock_db.execute.return_value = mock_result

        result = DataCleanupJob._check_user_relogin(user_id="123")

        assert result is False


class TestCleanupJobMainLoopReloginCheck:
    """Tests for re-login check in main cleanup loop."""

    @patch(f"{MODULE}.DataCleanupJob._check_user_relogin")
    @patch(f"{MODULE}.DataCleanupJob._cleanup_user_data")
    @patch(f"{MODULE}.DataCleanupJob._get_users_for_cleanup")
    @patch(f"{MODULE}.DataCleanupJob._handle_orphaned_data")
    @pytest.mark.asyncio
    async def test_cleanup_skips_user_if_logged_back_in(
        self,
        mock_orphaned,
        mock_get_users,
        mock_cleanup_data,
        mock_check_relogin,
    ):
        """Should skip cleanup if user logged back in before starting."""
        from studio.app.common.core.background.cleanup_job import DataCleanupJob

        mock_get_users.return_value = [("user1", ["ws1", "ws2"])]
        mock_check_relogin.return_value = True  # User logged back in

        await DataCleanupJob.run()

        # Cleanup should not have been called
        mock_cleanup_data.assert_not_called()


class TestCleanupUserDataReloginCheck:
    """Tests for re-login check during workspace cleanup."""

    @patch(f"{MODULE}.DataCleanupJob._check_user_relogin")
    @patch(f"{MODULE}.os.path.exists")
    def test_cleanup_aborts_mid_process_if_relogin(
        self, mock_exists, mock_check_relogin
    ):
        """Should abort cleanup if user logs back in during processing."""
        from studio.app.common.core.background.cleanup_job import (
            CleanupOutcome,
            DataCleanupJob,
        )

        # User logs back in after first check
        mock_check_relogin.side_effect = [False, True]
        mock_exists.return_value = False

        result = DataCleanupJob._cleanup_user_data(
            user_id="123",
            workspace_ids=["ws1", "ws2", "ws3"],
        )

        # Cleanup aborted because the user returned → KEPT (not an error)
        assert result == CleanupOutcome.KEPT


class TestVerifyNoActiveWorkflows:
    """Tests for workflow verification."""

    @patch(f"{MODULE}.session_scope")
    def test_verify_no_active_workflows_returns_true_when_zero(self, mock_session):
        """Should return True when no active workflows."""
        from studio.app.common.core.background.cleanup_job import DataCleanupJob

        mock_db = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_db

        mock_assignment = MagicMock()
        mock_assignment.active_workflow_count = 0
        mock_result = MagicMock()
        mock_result.first.return_value = (mock_assignment,)
        mock_db.execute.return_value = mock_result

        result = DataCleanupJob._verify_no_active_workflows(user_id="123")

        assert result is True

    @patch(f"{MODULE}.session_scope")
    def test_verify_no_active_workflows_returns_false_when_running(self, mock_session):
        """Should return False when workflows are running."""
        from studio.app.common.core.background.cleanup_job import DataCleanupJob

        mock_db = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_db

        mock_assignment = MagicMock()
        mock_assignment.active_workflow_count = 2
        mock_result = MagicMock()
        mock_result.first.return_value = (mock_assignment,)
        mock_db.execute.return_value = mock_result

        result = DataCleanupJob._verify_no_active_workflows(user_id="123")

        assert result is False

    @patch(f"{MODULE}.session_scope")
    def test_verify_no_active_workflows_returns_true_when_no_assignment(
        self, mock_session
    ):
        """Should return True when no assignment exists."""
        from studio.app.common.core.background.cleanup_job import DataCleanupJob

        mock_db = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_db

        mock_result = MagicMock()
        mock_result.first.return_value = None
        mock_db.execute.return_value = mock_result

        result = DataCleanupJob._verify_no_active_workflows(user_id="123")

        assert result is True
