"""Tests for the premium expiration -> release backstop sweep job (issue #629 P3)."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from studio.app.common.core.background.premium_expiration_sweep_job import (
    PremiumExpirationSweepJob,
)
from studio.app.common.core.subscription.constants import (
    PremiumExpirationSweep,
    SubscriptionPeriods,
)

MODULE = "studio.app.common.core.background.premium_expiration_sweep_job"


def _self_returning_query(rows):
    """A query mock where join/filter/order_by/limit chain back to itself."""
    q = MagicMock()
    q.join.return_value = q
    q.filter.return_value = q
    q.distinct.return_value = q
    q.order_by.return_value = q
    q.limit.return_value = q
    q.all.return_value = rows
    return q


def _mock_session_scope(mock_db):
    """Patch target for session_scope() used as a context manager."""
    scope = MagicMock()
    scope.return_value.__enter__ = MagicMock(return_value=mock_db)
    scope.return_value.__exit__ = MagicMock(return_value=False)
    return scope


class TestRun:
    @patch.object(PremiumExpirationSweepJob, "_find_dangling_assignments")
    @pytest.mark.asyncio
    async def test_no_candidates_no_release(self, mock_find):
        mock_find.return_value = []
        with patch(
            f"{MODULE}.premium_assignment_service.release_premium_user",
            new=AsyncMock(),
        ) as mock_release:
            await PremiumExpirationSweepJob.run()
        mock_release.assert_not_awaited()

    @patch.object(PremiumExpirationSweepJob, "_find_dangling_assignments")
    @pytest.mark.asyncio
    async def test_releases_each_candidate_hard(self, mock_find):
        mock_find.return_value = [(1, "uid_a"), (2, "uid_b")]
        with patch(
            f"{MODULE}.premium_assignment_service.release_premium_user",
            new=AsyncMock(return_value={"success": True, "message": "released"}),
        ) as mock_release:
            await PremiumExpirationSweepJob.run()

        assert mock_release.await_count == 2
        timeout = PremiumExpirationSweep.RELEASE_TIMEOUT_SECONDS
        mock_release.assert_any_await(
            user_id=1, user_uid="uid_a", hard=True, timeout=timeout
        )
        mock_release.assert_any_await(
            user_id=2, user_uid="uid_b", hard=True, timeout=timeout
        )

    @patch.object(PremiumExpirationSweepJob, "_find_dangling_assignments")
    @pytest.mark.asyncio
    async def test_continues_after_release_error(self, mock_find):
        """One failing release must not stop the rest of the batch."""
        mock_find.return_value = [(1, "uid_a"), (2, "uid_b")]
        with patch(
            f"{MODULE}.premium_assignment_service.release_premium_user",
            new=AsyncMock(
                side_effect=[Exception("boom"), {"success": True, "message": "ok"}]
            ),
        ) as mock_release:
            # Should not raise.
            await PremiumExpirationSweepJob.run()

        assert mock_release.await_count == 2


class TestFindDanglingAssignments:
    @patch(f"{MODULE}.get_current_datetime")
    @patch(f"{MODULE}.session_scope")
    def test_returns_deduped_candidates(self, mock_scope, mock_now):
        from datetime import datetime

        mock_now.return_value = datetime(2026, 5, 22)
        mock_db = MagicMock()
        # Duplicate user_id should be collapsed to a single candidate.
        mock_db.query.return_value = _self_returning_query(
            [(1, "uid_a"), (1, "uid_a"), (2, "uid_b")]
        )
        mock_scope.return_value = _mock_session_scope(mock_db).return_value

        result = PremiumExpirationSweepJob._find_dangling_assignments()

        assert result == [(1, "uid_a"), (2, "uid_b")]

    @patch(f"{MODULE}.get_current_datetime")
    @patch(f"{MODULE}.session_scope")
    def test_returns_empty_when_no_rows(self, mock_scope, mock_now):
        from datetime import datetime

        mock_now.return_value = datetime(2026, 5, 22)
        mock_db = MagicMock()
        mock_db.query.return_value = _self_returning_query([])
        mock_scope.return_value = _mock_session_scope(mock_db).return_value

        result = PremiumExpirationSweepJob._find_dangling_assignments()

        assert result == []


class TestExpiryLeavesTheAssignmentDangling:
    """Expiry alone must not release the assignment.

    When the cancellation webhook never fires - or expiry is applied straight in
    the DB - the assignment is left dangling, and this job is the only thing
    that releases it, but not until GRACE_PERIOD_DAYS have passed. The session
    is mocked here, so the predicates are the observable.
    """

    @patch(f"{MODULE}.get_current_datetime")
    @patch(f"{MODULE}.session_scope")
    def test_sweeps_only_assignments_expired_past_the_grace_period(
        self, mock_scope, mock_now
    ):
        now = datetime(2026, 5, 22)
        mock_now.return_value = now
        mock_db = MagicMock()
        query = _self_returning_query([])
        mock_db.query.return_value = query
        mock_scope.return_value = _mock_session_scope(mock_db).return_value

        PremiumExpirationSweepJob._find_dangling_assignments()

        rendered = [
            str(clause.compile(compile_kwargs={"literal_binds": True}))
            for clause in query.filter.call_args[0]
        ]
        grace_cutoff = now - timedelta(days=SubscriptionPeriods.GRACE_PERIOD_DAYS)
        # A subscription that expired yesterday is outside the cutoff, so its
        # assignment row is left dangling rather than released.
        assert f"subscription_users.expiration <= '{grace_cutoff}'" in rendered
        # Re-subscribers are excluded by a currently-active subscription.
        assert any(
            f"subscription_users_1.expiration > '{now}'" in clause
            for clause in rendered
        )
        # Standby placeholder rows are not user assignments.
        assert "premium_user_assignments.status = 'active'" in rendered
        assert "premium_user_assignments.is_standby = false" in rendered


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
