"""
Integration tests for free user logout endpoint.

Tests logout tracking and cleanup scheduling.
"""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.dialects import mysql

from studio.app.common.core.subscription.constants import SubscriptionType
from studio.app.common.models.instance_usage import UsageTier
from studio.app.common.routers.users_me import logout_free_user

MODULE = "studio.app.common.routers.users_me"


def _compiled_updates(mock_db):
    """Return ``[(sql, params), ...]`` for every statement passed to ``execute``."""
    statements = [c.args[0] for c in mock_db.execute.call_args_list]
    out = []
    for statement in statements:
        compiled = statement.compile(dialect=mysql.dialect())
        out.append((" ".join(str(compiled).split()), compiled.params))
    return out


class TestLogoutFreeUser:
    """Test free user logout endpoint"""

    @pytest.mark.asyncio
    async def test_logout_free_user_success(self):
        mock_db = MagicMock()
        mock_free_user = MagicMock()
        mock_free_user.id = 123
        mock_free_user.subscription_type = SubscriptionType.FREE.value
        """Test successful logout for free tier user"""
        mock_db.execute.return_value.rowcount = 1

        result = await logout_free_user(current_user=mock_free_user, db=mock_db)

        assert result["logged_out"] is True
        assert result["cleanup_after_minutes"] == 60
        assert mock_db.execute.call_count == 2
        mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_logout_stamps_the_assignment_and_closes_only_the_open_free_session(
        self,
    ):
        """The two UPDATEs are the whole endpoint.

        Losing ``ended_at IS NULL`` would re-close every historical usage row for
        the user, which is invisible to a ``call_count`` assertion.
        """
        mock_db = MagicMock()
        mock_free_user = MagicMock()
        mock_free_user.id = 123
        mock_free_user.subscription_type = SubscriptionType.FREE.value
        mock_db.execute.return_value.rowcount = 1

        await logout_free_user(current_user=mock_free_user, db=mock_db)

        assignment_sql, assignment_params = _compiled_updates(mock_db)[0]
        assert assignment_sql.startswith(
            "UPDATE free_user_assignments SET logged_out_at="
        )
        assert "free_user_assignments.user_id = " in assignment_sql
        assert assignment_params["user_id_1"] == 123
        assert assignment_params["logged_out_at"] is not None

        usage_sql, usage_params = _compiled_updates(mock_db)[1]
        assert usage_sql.startswith("UPDATE instance_usage_log SET ended_at=")
        assert "instance_usage_log.user_id = " in usage_sql
        assert "instance_usage_log.tier = " in usage_sql
        assert "instance_usage_log.ended_at IS NULL" in usage_sql
        assert usage_params["user_id_1"] == 123
        assert usage_params["tier_1"] == UsageTier.FREE
        assert usage_params["ended_at"] is not None

        # Lambda SQL matches these on the literal, never through the enum.
        assert UsageTier.FREE == "free"
        assert UsageTier.PREMIUM == "premium"

    @pytest.mark.asyncio
    async def test_logout_premium_user_no_effect(self):
        mock_db = MagicMock()
        mock_premium_user = MagicMock()
        mock_premium_user.id = 456
        mock_premium_user.subscription_type = SubscriptionType.PREMIUM.value
        """Test logout for premium user has no effect"""
        result = await logout_free_user(current_user=mock_premium_user, db=mock_db)

        assert result["logged_out"] is False
        assert "only applies to free tier" in result["message"]
        mock_db.add.assert_not_called()
        mock_db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_logout_no_assignment_found(self):
        """A zero-rowcount update still reports success, but warns instead of
        claiming a cleanup was scheduled."""
        mock_db = MagicMock()
        mock_free_user = MagicMock()
        mock_free_user.id = 123
        mock_free_user.subscription_type = SubscriptionType.FREE.value
        mock_db.execute.return_value.rowcount = 0

        with patch(f"{MODULE}.logger") as mock_logger:
            result = await logout_free_user(current_user=mock_free_user, db=mock_db)

        assert result["logged_out"] is True
        assert (
            "No assignment found for free user 123"
            in mock_logger.warning.call_args[0][0]
        )
        mock_logger.info.assert_not_called()

    @pytest.mark.asyncio
    async def test_logout_with_assignment_logs_cleanup_scheduled(self):
        mock_db = MagicMock()
        mock_free_user = MagicMock()
        mock_free_user.id = 123
        mock_free_user.subscription_type = SubscriptionType.FREE.value
        mock_db.execute.return_value.rowcount = 1

        with patch(f"{MODULE}.logger") as mock_logger:
            await logout_free_user(current_user=mock_free_user, db=mock_db)

        assert (
            "data cleanup scheduled" in mock_logger.info.call_args[0][0]
        ), mock_logger.info.call_args
        mock_logger.warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_logout_updates_timestamp(self):
        mock_db = MagicMock()
        mock_free_user = MagicMock()
        mock_free_user.id = 123
        mock_free_user.subscription_type = SubscriptionType.FREE.value
        """Test that logout updates logged_out_at timestamp"""
        mock_db.execute.return_value.rowcount = 1

        result = await logout_free_user(current_user=mock_free_user, db=mock_db)

        assert result["logged_out"] is True
        assert mock_db.execute.call_count == 2
        mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_logout_handles_exception_gracefully(self):
        mock_db = MagicMock()
        mock_free_user = MagicMock()
        mock_free_user.id = 123
        mock_free_user.subscription_type = SubscriptionType.FREE.value
        mock_assignment = MagicMock()
        mock_assignment.user_id = "123"
        mock_assignment.logged_out_at = None
        """Test logout handles exceptions gracefully"""
        # Mock execute() to return a row-like tuple
        mock_db.execute.return_value.first.return_value = (mock_assignment,)
        mock_db.commit.side_effect = Exception("Database error")

        result = await logout_free_user(current_user=mock_free_user, db=mock_db)

        assert result["logged_out"] is False
        assert "error" in result
