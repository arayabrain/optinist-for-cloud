"""
Unit tests for SubscriptionService

Tests for:
- get_users_for_expiration_deletion()
"""

from datetime import timedelta
from unittest.mock import Mock

import pytest

from studio.app.common.core.subscription.subscription_service import SubscriptionService
from studio.app.common.core.utils.datetime_utils import get_current_datetime


class TestExpirationDeletion:
    """Expiration deletion eligibility tests."""

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session"""
        db = Mock()
        db.query = Mock()
        return db

    def test_no_users_for_expiration_deletion(self, mock_db):
        """Should return empty list when no users are eligible."""
        chain = mock_db.query.return_value.join.return_value.join.return_value
        chain.filter.return_value.order_by.return_value.all.return_value = []

        result = SubscriptionService.get_users_for_expiration_deletion(mock_db)

        assert result == []

    def test_finds_user_for_expiration_deletion(self, mock_db):
        """Should find users past grace period with storage over free quota."""
        from studio.app.common.core.subscription.constants import (
            ExpirationDeletion,
            SubscriptionPeriods,
        )

        current_time = get_current_datetime()

        mock_sub = Mock()
        mock_sub.expiration = current_time - timedelta(
            days=SubscriptionPeriods.GRACE_PERIOD_DAYS + 1
        )
        mock_sub.deletion_processed_at = None

        mock_user = Mock()
        mock_user.id = 123
        mock_user.uid = "user_uid_123"

        mock_storage = Mock()
        mock_storage.storage_usage_bytes = ExpirationDeletion.FREE_QUOTA_BYTES * 2

        chain = mock_db.query.return_value.join.return_value.join.return_value
        chain.filter.return_value.order_by.return_value.all.return_value = [
            (mock_sub, mock_user, mock_storage)
        ]

        result = SubscriptionService.get_users_for_expiration_deletion(mock_db)

        assert len(result) == 1
        assert result[0]["user_id"] == 123
        assert result[0]["excess_bytes"] > 0


class TestDetermineLifecycle:
    """SubscriptionService.determine_lifecycle() classification tests."""

    @staticmethod
    def _mock_db(expiration):
        db = Mock()
        subscription = Mock()
        subscription.expiration = expiration
        db.execute.return_value.all.return_value = [[subscription]]
        return db

    def test_none_expiration_returns_none(self):
        assert SubscriptionService.determine_lifecycle(self._mock_db(None), 1) is None

    def test_no_premium_rows_is_free(self):
        from studio.app.common.core.subscription.constants import (
            SubscriptionLifecycleStatus,
        )
        from studio.app.common.core.subscription.subscription_service import (
            SubscriptionLifecycle,
        )

        db = Mock()
        db.execute.return_value.all.return_value = []

        result = SubscriptionService.determine_lifecycle(db, 1)

        assert isinstance(result, SubscriptionLifecycle)
        assert result.status == SubscriptionLifecycleStatus.FREE
