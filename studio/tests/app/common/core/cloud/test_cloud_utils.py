"""
Unit tests for cloud_utils.py

Tests cover:
- calculate_limit_warning() - 5 warning cases with subscription lifecycle states
- _is_storage_data_fresh() - Date parsing and timezone handling
- get_current_user_storage_usage() - Hybrid caching logic
- _get_fallback_storage_quota() - Subscription plan determination
"""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from studio.app.common.core.cloud.cloud_utils import (
    _ensure_user_bucket_exists_impl,
    calculate_limit_warning,
)
from studio.app.common.core.cloud.storage_tracking import (
    _get_fallback_storage_quota,
    _is_storage_data_fresh,
    get_current_user_storage_usage,
)
from studio.app.common.core.subscription.constants import (
    AlertType,
    PlanName,
    StorageQuota,
    StorageSize,
    SubscriptionPeriods,
)
from studio.app.common.core.utils.datetime_utils import get_current_datetime

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_db():
    """Mock database session"""
    db = Mock()
    db.execute = Mock()
    db.add = Mock()
    db.commit = Mock()
    return db


@pytest.fixture
def mock_subscription_free():
    """Mock free plan subscription result"""
    result = Mock()
    result.plan_name = PlanName.FREE
    return result


@pytest.fixture
def mock_subscription_premium():
    """Mock premium plan subscription result"""
    result = Mock()
    result.plan_name = PlanName.PREMIUM
    return result


@pytest.fixture
def mock_storage_info_fresh():
    """Mock storage info with fresh timestamp (within 20 minutes)"""
    return {
        "user_id": 1,
        "storage_usage_bytes": 1_000_000_000,  # 1 GB
        "storage_quota_bytes": 5_000_000_000,  # 5 GB
        "storage_usage_percent": 20.0,
        "last_updated": get_current_datetime() - timedelta(minutes=10),
    }


@pytest.fixture
def mock_storage_info_stale():
    """Mock storage info with stale timestamp (over 20 minutes old)"""
    return {
        "user_id": 1,
        "storage_usage_bytes": 1_000_000_000,
        "storage_quota_bytes": 5_000_000_000,
        "storage_usage_percent": 20.0,
        "last_updated": get_current_datetime() - timedelta(minutes=30),
    }


# ============================================================================
# Tests for _get_fallback_storage_quota()
# ============================================================================


def test_get_fallback_storage_quota_free_plan():
    """Test fallback quota for free plan user"""
    user_id = 1

    with patch(
        "studio.app.common.core.cloud.storage_tracking.session_scope"
    ) as mock_scope:
        mock_db = Mock()
        mock_scope.return_value.__enter__.return_value = mock_db

        # Mock query result for free plan
        mock_result = Mock()
        mock_result.plan_name = PlanName.FREE
        mock_db.execute.return_value.first.return_value = mock_result

        result = _get_fallback_storage_quota(user_id)

        assert result["user_id"] == user_id
        assert result["storage_quota_bytes"] == StorageQuota.FREE * StorageSize.GB
        assert result["storage_usage_bytes"] == 0
        assert result["storage_usage_percent"] == 0.0
        assert result["last_updated"] is None


def test_get_fallback_storage_quota_premium_plan():
    """Test fallback quota for premium plan user"""
    user_id = 2

    with patch(
        "studio.app.common.core.cloud.storage_tracking.session_scope"
    ) as mock_scope:
        mock_db = Mock()
        mock_scope.return_value.__enter__.return_value = mock_db

        # Mock query result for premium plan
        mock_result = Mock()
        mock_result.plan_name = PlanName.PREMIUM
        mock_db.execute.return_value.first.return_value = mock_result

        result = _get_fallback_storage_quota(user_id)

        assert result["user_id"] == user_id
        assert result["storage_quota_bytes"] == StorageQuota.PREMIUM * StorageSize.GB
        assert result["storage_usage_bytes"] == 0


def test_get_fallback_storage_quota_no_subscription():
    """Test fallback quota when user has no subscription"""
    user_id = 3

    with patch(
        "studio.app.common.core.cloud.storage_tracking.session_scope"
    ) as mock_scope:
        mock_db = Mock()
        mock_scope.return_value.__enter__.return_value = mock_db

        # Mock query result with no plan
        mock_db.execute.return_value.first.return_value = None

        result = _get_fallback_storage_quota(user_id)

        # Should default to free plan
        assert result["storage_quota_bytes"] == StorageQuota.FREE * StorageSize.GB


def test_get_fallback_storage_quota_database_error():
    """Test fallback quota when database error occurs"""
    user_id = 4

    with patch(
        "studio.app.common.core.cloud.storage_tracking.session_scope"
    ) as mock_scope:
        mock_scope.side_effect = Exception("Database connection failed")

        result = _get_fallback_storage_quota(user_id)

        # Should fallback to free plan
        assert result["storage_quota_bytes"] == StorageQuota.FREE * StorageSize.GB


# ============================================================================
# Tests for _is_storage_data_fresh()
# ============================================================================


def test_is_storage_data_fresh_within_cache_window():
    """Test that fresh data (within cache window) returns True"""
    storage_info = {"last_updated": get_current_datetime() - timedelta(minutes=10)}

    result = _is_storage_data_fresh(
        storage_info, SubscriptionPeriods.MAX_CACHE_AGE_MINUTES
    )

    assert result is True


def test_is_storage_data_fresh_outside_cache_window():
    """Test that stale data (outside cache window) returns False"""
    storage_info = {"last_updated": get_current_datetime() - timedelta(minutes=30)}

    result = _is_storage_data_fresh(
        storage_info, SubscriptionPeriods.MAX_CACHE_AGE_MINUTES
    )

    assert result is False


def test_is_storage_data_fresh_missing_last_updated():
    """Test that missing last_updated field returns False"""
    storage_info = {"storage_usage_bytes": 1000}

    result = _is_storage_data_fresh(
        storage_info, SubscriptionPeriods.MAX_CACHE_AGE_MINUTES
    )

    assert result is False


def test_is_storage_data_fresh_string_format():
    """Test that ISO string format timestamps work correctly"""
    # Create timestamp as ISO string
    timestamp = (get_current_datetime() - timedelta(minutes=5)).isoformat()
    storage_info = {"last_updated": timestamp}

    result = _is_storage_data_fresh(
        storage_info, SubscriptionPeriods.MAX_CACHE_AGE_MINUTES
    )

    assert result is True


def test_is_storage_data_fresh_string_format_with_z():
    """Test that ISO string with 'Z' suffix (UTC) works correctly"""
    # Create timestamp with Z suffix (common in JSON APIs)
    timestamp = (
        (get_current_datetime() - timedelta(minutes=5))
        .isoformat()
        .replace("+00:00", "Z")
    )
    storage_info = {"last_updated": timestamp}

    result = _is_storage_data_fresh(
        storage_info, SubscriptionPeriods.MAX_CACHE_AGE_MINUTES
    )

    assert result is True


def test_is_storage_data_fresh_invalid_string_format():
    """Test that invalid date string returns False"""
    storage_info = {"last_updated": "invalid-date-string"}

    result = _is_storage_data_fresh(
        storage_info, SubscriptionPeriods.MAX_CACHE_AGE_MINUTES
    )

    assert result is False


def test_is_storage_data_fresh_exactly_at_boundary():
    """Test boundary condition: exactly at max_cache_age_minutes"""
    storage_info = {
        "last_updated": get_current_datetime()
        - timedelta(minutes=SubscriptionPeriods.MAX_CACHE_AGE_MINUTES)
    }

    result = _is_storage_data_fresh(
        storage_info, SubscriptionPeriods.MAX_CACHE_AGE_MINUTES
    )

    # Implementation uses < (not <=), so data exactly at boundary is stale
    assert result is False


def test_is_storage_data_fresh_timezone_naive_datetime():
    """Test that timezone-naive datetime objects (from MySQL DateTime) work correctly

    This regression test ensures that datetime objects from MySQL DateTime columns
    (which are timezone-naive) are handled correctly and don't cause the error:
    "can't subtract offset-naive and offset-aware datetimes"
    """
    # Simulate what SQLAlchemy returns from MySQL DateTime column (timezone-naive)
    # Create naive datetime by stripping timezone info (MySQL DateTime is naive)
    naive_datetime = get_current_datetime().replace(tzinfo=None) - timedelta(minutes=10)
    assert naive_datetime.tzinfo is None  # Verify it's timezone-naive

    storage_info = {"last_updated": naive_datetime}

    result = _is_storage_data_fresh(
        storage_info, SubscriptionPeriods.MAX_CACHE_AGE_MINUTES
    )

    # Should work without exception and return True (data is fresh)
    assert result is True


def test_is_storage_data_fresh_timezone_naive_datetime_stale():
    """Test that stale timezone-naive datetime is correctly identified as stale"""
    # Timezone-naive datetime older than cache window
    naive_datetime = get_current_datetime().replace(tzinfo=None) - timedelta(minutes=30)
    assert naive_datetime.tzinfo is None

    storage_info = {"last_updated": naive_datetime}

    result = _is_storage_data_fresh(
        storage_info, SubscriptionPeriods.MAX_CACHE_AGE_MINUTES
    )

    # Should work without exception and return False (data is stale)
    assert result is False


# ============================================================================
# Tests for get_current_user_storage_usage()
# ============================================================================


@pytest.mark.asyncio
async def test_get_current_user_storage_usage_fresh_cache_hit(
    mock_storage_info_fresh,
):
    """Test that fresh cached data is returned without recalculation"""
    user_id = 1

    with patch(
        "studio.app.common.core.cloud.storage_tracking.get_user_storage_usage"
    ) as mock_get_storage:
        mock_get_storage.return_value = mock_storage_info_fresh

        result = await get_current_user_storage_usage(user_id, force_live=False)

        assert result == mock_storage_info_fresh["storage_usage_bytes"]
        # Should not call live calculation
        mock_get_storage.assert_called_once_with(user_id)


@pytest.mark.asyncio
async def test_get_current_user_storage_usage_stale_cache_recalculates(
    mock_storage_info_stale,
):
    """Test that stale cached data triggers recalculation"""
    user_id = 1
    live_usage = 2_000_000_000  # 2 GB

    with patch(
        "studio.app.common.core.cloud.storage_tracking.get_user_storage_usage"
    ) as mock_get_storage:
        with patch(
            "studio.app.common.core.cloud.storage_tracking."
            "_calculate_live_storage_usage"
        ) as mock_live_calc:
            with patch(
                "studio.app.common.core.cloud.storage_tracking."
                "update_user_storage_usage"
            ) as mock_update:
                mock_get_storage.return_value = mock_storage_info_stale
                mock_live_calc.return_value = live_usage

                result = await get_current_user_storage_usage(user_id, force_live=False)

                assert result == live_usage
                mock_live_calc.assert_called_once_with(user_id)
                mock_update.assert_called_once_with(user_id, live_usage)


@pytest.mark.asyncio
async def test_get_current_user_storage_usage_force_live():
    """Test that force_live=True always calculates live usage"""
    user_id = 1
    live_usage = 3_000_000_000

    with patch(
        "studio.app.common.core.cloud.storage_tracking._calculate_live_storage_usage"
    ) as mock_live_calc:
        with patch(
            "studio.app.common.core.cloud.storage_tracking.update_user_storage_usage"
        ):
            mock_live_calc.return_value = live_usage

            result = await get_current_user_storage_usage(user_id, force_live=True)

            assert result == live_usage
            mock_live_calc.assert_called_once_with(user_id)


@pytest.mark.asyncio
async def test_get_current_user_storage_usage_calculation_fails_fallback():
    """Test fallback to database when live calculation fails"""
    user_id = 1
    cached_usage = 1_000_000_000

    with patch(
        "studio.app.common.core.cloud.storage_tracking." "get_user_storage_usage"
    ) as mock_get_storage:
        with patch(
            "studio.app.common.core.cloud.storage_tracking."
            "_calculate_live_storage_usage"
        ) as mock_live_calc:
            mock_get_storage.return_value = {"storage_usage_bytes": cached_usage}
            mock_live_calc.side_effect = Exception("S3 connection failed")

            result = await get_current_user_storage_usage(user_id, force_live=True)

            # Should fallback to cached value
            assert result == cached_usage


# ============================================================================
# Tests for calculate_limit_warning()
# ============================================================================


@pytest.mark.asyncio
async def test_calculate_limit_warning_free_user_no_warning():
    """
    Case 1: Free user, no storage limit exceeded → No warning
    """
    user_id = 1

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        with patch(
            "studio.app.common.core.cloud.cloud_utils.get_user_storage_usage"
        ) as mock_get_storage:
            with patch(
                "studio.app.common.core.cloud.cloud_utils._is_storage_data_fresh"
            ) as mock_fresh:
                mock_db = Mock()
                mock_scope.return_value.__enter__.return_value = mock_db

                # Mock storage info: 2GB used of 5GB (40% - under limit)
                mock_get_storage.return_value = {
                    "storage_usage_bytes": 2_000_000_000,
                    "storage_quota_bytes": 5_000_000_000,
                }
                mock_fresh.return_value = True

                # Mock no subscription (free user)
                mock_db.execute.return_value.all.return_value = []

                result = await calculate_limit_warning(user_id)

                assert result is None  # No warning for free user within limits


@pytest.mark.asyncio
async def test_calculate_limit_warning_free_user_storage_exceeded():
    """
    Case 2: Free user, storage limit exceeded → Storage warning
    """
    user_id = 1
    excess_bytes = 750_000_000  # 0.75 GB excess

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        with patch(
            "studio.app.common.core.cloud.cloud_utils.get_user_storage_usage"
        ) as mock_get_storage:
            with patch(
                "studio.app.common.core.cloud.cloud_utils._is_storage_data_fresh"
            ) as mock_fresh:
                mock_db = Mock()
                mock_scope.return_value.__enter__.return_value = mock_db

                # Mock storage info: 5.75GB used of 5GB (115% - over limit)
                mock_get_storage.return_value = {
                    "storage_usage_bytes": 5_750_000_000,
                    "storage_quota_bytes": 5_000_000_000,
                }
                mock_fresh.return_value = True

                # Mock no subscription (free user)
                mock_db.execute.return_value.all.return_value = []

                result = await calculate_limit_warning(user_id)

                assert result is not None
                assert result.has_alert is True
                assert result.alert_type == AlertType.STORAGE.value
                assert result.days_remaining == SubscriptionPeriods.STORAGE_WARNING_DAYS
                assert result.excess_data_bytes == excess_bytes
                assert "exceeds the free plan limit" in result.message


@pytest.mark.asyncio
async def test_calculate_limit_warning_premium_active_storage_exceeded():
    """
    Case 3: Premium user (active), storage limit exceeded → Storage warning only
    """
    user_id = 1

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        with patch(
            "studio.app.common.core.cloud.cloud_utils.get_user_storage_usage"
        ) as mock_get_storage:
            with patch(
                "studio.app.common.core.cloud.cloud_utils._is_storage_data_fresh"
            ) as mock_fresh:
                mock_db = Mock()
                mock_scope.return_value.__enter__.return_value = mock_db

                # Mock storage info: 205GB used of 200GB (over premium limit)
                mock_get_storage.return_value = {
                    "storage_usage_bytes": 205_000_000_000,
                    "storage_quota_bytes": 200_000_000_000,
                }
                mock_fresh.return_value = True

                # Mock active premium subscription (expires in future)
                mock_subscription = Mock()
                mock_subscription.expiration = get_current_datetime() + timedelta(
                    days=30
                )
                mock_db.execute.return_value.all.return_value = [[mock_subscription]]

                result = await calculate_limit_warning(user_id)

                assert result is not None
                assert result.has_alert is True
                assert result.alert_type == AlertType.STORAGE.value
                assert result.days_remaining == SubscriptionPeriods.STORAGE_WARNING_DAYS
                assert "unable to run workflows" in result.message


@pytest.mark.asyncio
async def test_calculate_limit_warning_premium_warning_storage_ok():
    """
    Case 4: Premium user in WARNING period, storage OK -> Grace warning.
    Even when storage is under the free tier limit, a subscription
    expiration warning is shown.
    """
    user_id = 1
    grace_period = SubscriptionPeriods.GRACE_PERIOD_DAYS

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        with patch(
            "studio.app.common.core.cloud.cloud_utils.get_user_storage_usage"
        ) as mock_get_storage:
            with patch(
                "studio.app.common.core.cloud.cloud_utils._is_storage_data_fresh"
            ) as mock_fresh:
                mock_db = Mock()
                mock_scope.return_value.__enter__.return_value = mock_db

                mock_get_storage.return_value = {
                    "storage_usage_bytes": 2_000_000_000,
                    "storage_quota_bytes": 5_000_000_000,
                }
                mock_fresh.return_value = True

                expiration_date = get_current_datetime() - timedelta(
                    days=grace_period + 10
                )
                mock_subscription = Mock()
                mock_subscription.expiration = expiration_date
                mock_db.execute.return_value.all.return_value = [[mock_subscription]]

                result = await calculate_limit_warning(user_id)

                # Grace warning shown for expired premium users
                assert result is not None
                assert result.has_alert is True
                assert result.alert_type == AlertType.GRACE.value
                assert result.days_remaining >= 0
                assert "expired" in result.message.lower()


@pytest.mark.asyncio
async def test_calculate_limit_warning_premium_warning_storage_exceeded():
    """
    Case 5: Premium user in WARNING period, storage exceeded → Combined warning
    Note: After grace period expires, user falls back to FREE quota (5GB)
    """
    user_id = 1
    grace_period = SubscriptionPeriods.GRACE_PERIOD_DAYS
    warning_period = SubscriptionPeriods.WARNING_PERIOD_DAYS

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        with patch(
            "studio.app.common.core.cloud.cloud_utils.get_user_storage_usage"
        ) as mock_get_storage:
            with patch(
                "studio.app.common.core.cloud.cloud_utils._is_storage_data_fresh"
            ) as mock_fresh:
                mock_db = Mock()
                mock_scope.return_value.__enter__.return_value = mock_db

                # Mock storage info: 8GB used of 5GB (over FREE limit after downgrade)
                mock_get_storage.return_value = {
                    "storage_usage_bytes": 8_000_000_000,
                    "storage_quota_bytes": 5_000_000_000,
                }
                mock_fresh.return_value = True

                # Mock expired subscription in WARNING period
                expiration_date = get_current_datetime() - timedelta(
                    days=grace_period + 5
                )
                deletion_date = expiration_date + timedelta(
                    days=grace_period + warning_period
                )

                mock_subscription = Mock()
                mock_subscription.expiration = expiration_date
                mock_db.execute.return_value.all.return_value = [[mock_subscription]]

                result = await calculate_limit_warning(user_id)

                assert result is not None
                assert result.has_alert is True
                assert result.alert_type == AlertType.GRACE.value
                # days_remaining is (deletion_date - now).days
                expected_days = (deletion_date - get_current_datetime()).days
                assert (
                    result.days_remaining >= expected_days - 1
                )  # Allow 1 day variance
                assert result.days_remaining <= expected_days + 1
                assert "expired" in result.message
                msg_lower = result.message.lower()
                assert "remove" in msg_lower or "upgrade" in msg_lower
                # Verify excess is calculated correctly
                # Note: Uses binary GB (1 GB = 1024^3 bytes), so:
                # 8,000,000,000 bytes = 7.45 GB, quota = 5.0 GB, excess = 2.45 GB
                assert result.excess_data_gb >= 2.4
                assert result.excess_data_gb <= 3.0


@pytest.mark.asyncio
async def test_calculate_limit_warning_premium_overdue():
    """
    Test: Premium user OVERDUE (past deletion date) → Overdue warning
    """
    user_id = 1
    grace_period = SubscriptionPeriods.GRACE_PERIOD_DAYS
    warning_period = SubscriptionPeriods.WARNING_PERIOD_DAYS

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        with patch(
            "studio.app.common.core.cloud.cloud_utils.get_user_storage_usage"
        ) as mock_get_storage:
            with patch(
                "studio.app.common.core.cloud.cloud_utils._is_storage_data_fresh"
            ) as mock_fresh:
                mock_db = Mock()
                mock_scope.return_value.__enter__.return_value = mock_db

                # Mock storage info
                mock_get_storage.return_value = {
                    "storage_usage_bytes": 6_000_000_000,
                    "storage_quota_bytes": 5_000_000_000,
                }
                mock_fresh.return_value = True

                # Mock expired subscription past deletion date
                expiration_date = get_current_datetime() - timedelta(
                    days=grace_period + warning_period + 5
                )
                mock_subscription = Mock()
                mock_subscription.expiration = expiration_date
                mock_db.execute.return_value.all.return_value = [[mock_subscription]]

                result = await calculate_limit_warning(user_id)

                assert result is not None
                assert result.has_alert is True
                assert result.alert_type == AlertType.OVERDUE.value
                assert result.days_remaining == 0


@pytest.mark.asyncio
async def test_calculate_limit_warning_premium_active_no_storage_issue():
    """
    Test: Premium user active with no storage issue → No warning
    """
    user_id = 1

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        with patch(
            "studio.app.common.core.cloud.cloud_utils.get_user_storage_usage"
        ) as mock_get_storage:
            with patch(
                "studio.app.common.core.cloud.cloud_utils._is_storage_data_fresh"
            ) as mock_fresh:
                mock_db = Mock()
                mock_scope.return_value.__enter__.return_value = mock_db

                # Mock storage info: 50GB used of 200GB (within limits)
                mock_get_storage.return_value = {
                    "storage_usage_bytes": 50_000_000_000,
                    "storage_quota_bytes": 200_000_000_000,
                }
                mock_fresh.return_value = True

                # Mock active premium subscription
                mock_subscription = Mock()
                mock_subscription.expiration = get_current_datetime() + timedelta(
                    days=30
                )
                mock_db.execute.return_value.all.return_value = [[mock_subscription]]

                result = await calculate_limit_warning(user_id)

                assert result is None  # No warning


@pytest.mark.asyncio
async def test_calculate_limit_warning_premium_in_grace_period():
    """
    Case 6: Premium user in GRACE period with storage within FREE
    quota limits -> Grace warning shown (subscription expired).
    """
    user_id = 1

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        with patch(
            "studio.app.common.core.cloud.cloud_utils.get_user_storage_usage"
        ) as mock_get_storage:
            with patch(
                "studio.app.common.core.cloud.cloud_utils._is_storage_data_fresh"
            ) as mock_fresh:
                mock_db = Mock()
                mock_scope.return_value.__enter__.return_value = mock_db

                mock_get_storage.return_value = {
                    "storage_usage_bytes": 2_000_000_000,
                    "storage_quota_bytes": 5_000_000_000,
                }
                mock_fresh.return_value = True

                mock_subscription = Mock()
                mock_subscription.expiration = get_current_datetime() - timedelta(
                    days=3
                )
                mock_db.execute.return_value.all.return_value = [[mock_subscription]]

                result = await calculate_limit_warning(user_id)

                # Grace period warning always shown for expired premium users
                assert result is not None
                assert result.has_alert is True
                assert result.alert_type == AlertType.GRACE.value
                assert result.days_remaining >= 0
                assert "expired" in result.message.lower()


@pytest.mark.asyncio
async def test_calculate_limit_warning_with_stale_cache():
    """
    Test that stale cache triggers live calculation in calculate_limit_warning
    """
    user_id = 1
    live_usage = 4_500_000_000  # 4.5 GB

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        with patch(
            "studio.app.common.core.cloud.cloud_utils.get_user_storage_usage"
        ) as mock_get_storage:
            with patch(
                "studio.app.common.core.cloud.cloud_utils._is_storage_data_fresh"
            ) as mock_fresh:
                with patch(
                    "studio.app.common.core.cloud.cloud_utils."
                    "get_current_user_storage_usage"
                ) as mock_live:
                    mock_db = Mock()
                    mock_scope.return_value.__enter__.return_value = mock_db

                    # Mock stale cache
                    mock_get_storage.return_value = {
                        "storage_usage_bytes": 1_000_000_000,
                        "storage_quota_bytes": 5_000_000_000,
                    }
                    mock_fresh.return_value = False
                    mock_live.return_value = live_usage

                    # Mock no subscription
                    mock_db.execute.return_value.all.return_value = []

                    await calculate_limit_warning(user_id)

                    # Should use live calculation when cache is stale
                    mock_live.assert_called_once_with(user_id, force_live=True)


@pytest.mark.asyncio
async def test_calculate_limit_warning_exception_handling():
    """
    Test that exceptions are handled gracefully and return None
    """
    user_id = 1

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        mock_scope.side_effect = Exception("Database connection failed")

        result = await calculate_limit_warning(user_id)

        assert result is None  # Should return None on exception


# ============================================================================
# Regression Tests - Bug Fixes
# ============================================================================


@pytest.mark.asyncio
async def test_calculate_limit_warning_query_filters_premium_only():
    """
    REGRESSION TEST: Verify subscription query filters for premium
    plans only.
    """
    user_id = 1

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        with patch(
            "studio.app.common.core.cloud.cloud_utils." "get_user_storage_usage"
        ) as mock_get_storage:
            with patch(
                "studio.app.common.core.cloud.cloud_utils." "_is_storage_data_fresh"
            ) as mock_fresh:
                mock_db = Mock()
                mock_scope.return_value.__enter__.return_value = mock_db

                mock_get_storage.return_value = {
                    "storage_usage_bytes": 2_000_000_000,
                    "storage_quota_bytes": 5_000_000_000,
                }
                mock_fresh.return_value = True
                mock_db.execute.return_value.all.return_value = []

                await calculate_limit_warning(user_id)

                assert mock_db.execute.called

                call_args = mock_db.execute.call_args
                query = call_args[0][0]
                query_str = str(query)
                assert "plan_id" in query_str, (
                    "Query must filter by plan_id to only fetch "
                    "premium subscriptions. "
                    f"Query was: {query_str}"
                )


@pytest.mark.asyncio
async def test_calculate_limit_warning_free_plan_no_premium_warning():
    """
    REGRESSION TEST: User with only FREE plan subscription should
    NOT get premium expired warning.
    """
    user_id = 1

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        with patch(
            "studio.app.common.core.cloud.cloud_utils." "get_user_storage_usage"
        ) as mock_get_storage:
            with patch(
                "studio.app.common.core.cloud.cloud_utils." "_is_storage_data_fresh"
            ) as mock_fresh:
                mock_db = Mock()
                mock_scope.return_value.__enter__.return_value = mock_db

                mock_get_storage.return_value = {
                    "storage_usage_bytes": 2_000_000_000,
                    "storage_quota_bytes": 5_000_000_000,
                }
                mock_fresh.return_value = True
                # Empty because FREE plan records are filtered out
                mock_db.execute.return_value.all.return_value = []

                result = await calculate_limit_warning(user_id)

                assert result is None, (
                    "User with only FREE plan subscription should "
                    "not get any warning when within storage limits"
                )


# ============================================================================
# Edge Case Tests - Alert Visibility
# ============================================================================


@pytest.mark.asyncio
async def test_calculate_limit_warning_just_expired_grace_period():
    """
    Premium subscription JUST expired (day 1 of grace period)
    with storage under free limit should show grace warning.
    """
    user_id = 1

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        with patch(
            "studio.app.common.core.cloud.cloud_utils." "get_user_storage_usage"
        ) as mock_get_storage:
            with patch(
                "studio.app.common.core.cloud.cloud_utils." "_is_storage_data_fresh"
            ) as mock_fresh:
                mock_db = Mock()
                mock_scope.return_value.__enter__.return_value = mock_db

                mock_get_storage.return_value = {
                    "storage_usage_bytes": 2_000_000_000,
                    "storage_quota_bytes": 5_000_000_000,
                }
                mock_fresh.return_value = True

                mock_subscription = Mock()
                mock_subscription.expiration = get_current_datetime() - timedelta(
                    days=1
                )
                mock_db.execute.return_value.all.return_value = [[mock_subscription]]

                result = await calculate_limit_warning(user_id)

                assert result is not None
                assert result.has_alert is True
                assert result.alert_type == AlertType.GRACE.value
                assert "expired" in result.message.lower()


@pytest.mark.asyncio
async def test_calculate_limit_warning_last_day_of_grace():
    """
    Premium subscription on last day of grace period with storage
    under free limit should show grace warning.
    """
    user_id = 1
    grace_period = SubscriptionPeriods.GRACE_PERIOD_DAYS

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        with patch(
            "studio.app.common.core.cloud.cloud_utils." "get_user_storage_usage"
        ) as mock_get_storage:
            with patch(
                "studio.app.common.core.cloud.cloud_utils." "_is_storage_data_fresh"
            ) as mock_fresh:
                mock_db = Mock()
                mock_scope.return_value.__enter__.return_value = mock_db

                mock_get_storage.return_value = {
                    "storage_usage_bytes": 2_000_000_000,
                    "storage_quota_bytes": 5_000_000_000,
                }
                mock_fresh.return_value = True

                mock_subscription = Mock()
                mock_subscription.expiration = get_current_datetime() - timedelta(
                    days=grace_period
                )
                mock_db.execute.return_value.all.return_value = [[mock_subscription]]

                result = await calculate_limit_warning(user_id)

                assert result is not None
                assert result.has_alert is True
                assert result.alert_type == AlertType.GRACE.value
                assert "expired" in result.message.lower()


@pytest.mark.asyncio
async def test_calculate_limit_warning_premium_expires_today():
    """
    Premium subscription expiring today (still active) should NOT
    show any warning if storage is within limits.
    """
    user_id = 1

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        with patch(
            "studio.app.common.core.cloud.cloud_utils." "get_user_storage_usage"
        ) as mock_get_storage:
            with patch(
                "studio.app.common.core.cloud.cloud_utils." "_is_storage_data_fresh"
            ) as mock_fresh:
                mock_db = Mock()
                mock_scope.return_value.__enter__.return_value = mock_db

                mock_get_storage.return_value = {
                    "storage_usage_bytes": 50_000_000_000,
                    "storage_quota_bytes": 200_000_000_000,
                }
                mock_fresh.return_value = True

                mock_subscription = Mock()
                mock_subscription.expiration = get_current_datetime() + timedelta(
                    hours=1
                )
                mock_db.execute.return_value.all.return_value = [[mock_subscription]]

                result = await calculate_limit_warning(user_id)

                assert result is None


@pytest.mark.asyncio
async def test_calculate_limit_warning_storage_exactly_at_limit():
    """
    Storage usage exactly at the limit should NOT trigger warning.
    """
    user_id = 1
    quota_bytes = 5_000_000_000

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        with patch(
            "studio.app.common.core.cloud.cloud_utils." "get_user_storage_usage"
        ) as mock_get_storage:
            with patch(
                "studio.app.common.core.cloud.cloud_utils." "_is_storage_data_fresh"
            ) as mock_fresh:
                mock_db = Mock()
                mock_scope.return_value.__enter__.return_value = mock_db

                mock_get_storage.return_value = {
                    "storage_usage_bytes": quota_bytes,
                    "storage_quota_bytes": quota_bytes,
                }
                mock_fresh.return_value = True
                mock_db.execute.return_value.all.return_value = []

                result = await calculate_limit_warning(user_id)

                assert result is None


@pytest.mark.asyncio
async def test_calculate_limit_warning_storage_one_byte_over():
    """
    Storage usage 1 byte over the limit should trigger warning.
    """
    user_id = 1
    quota_bytes = 5_000_000_000

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        with patch(
            "studio.app.common.core.cloud.cloud_utils." "get_user_storage_usage"
        ) as mock_get_storage:
            with patch(
                "studio.app.common.core.cloud.cloud_utils." "_is_storage_data_fresh"
            ) as mock_fresh:
                mock_db = Mock()
                mock_scope.return_value.__enter__.return_value = mock_db

                mock_get_storage.return_value = {
                    "storage_usage_bytes": quota_bytes + 1,
                    "storage_quota_bytes": quota_bytes,
                }
                mock_fresh.return_value = True
                mock_db.execute.return_value.all.return_value = []

                result = await calculate_limit_warning(user_id)

                assert result is not None
                assert result.has_alert is True
                assert result.alert_type == AlertType.STORAGE.value


@pytest.mark.asyncio
async def test_calculate_limit_warning_zero_storage_usage():
    """
    User with zero storage usage should never get storage warning.
    """
    user_id = 1

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        with patch(
            "studio.app.common.core.cloud.cloud_utils." "get_user_storage_usage"
        ) as mock_get_storage:
            with patch(
                "studio.app.common.core.cloud.cloud_utils." "_is_storage_data_fresh"
            ) as mock_fresh:
                mock_db = Mock()
                mock_scope.return_value.__enter__.return_value = mock_db

                mock_get_storage.return_value = {
                    "storage_usage_bytes": 0,
                    "storage_quota_bytes": 5_000_000_000,
                }
                mock_fresh.return_value = True
                mock_db.execute.return_value.all.return_value = []

                result = await calculate_limit_warning(user_id)

                assert result is None


@pytest.mark.asyncio
async def test_calculate_limit_warning_multiple_subscriptions():
    """
    User with multiple subscription records should use the most
    recent to determine status.
    """
    user_id = 1

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        with patch(
            "studio.app.common.core.cloud.cloud_utils." "get_user_storage_usage"
        ) as mock_get_storage:
            with patch(
                "studio.app.common.core.cloud.cloud_utils." "_is_storage_data_fresh"
            ) as mock_fresh:
                mock_db = Mock()
                mock_scope.return_value.__enter__.return_value = mock_db

                mock_get_storage.return_value = {
                    "storage_usage_bytes": 50_000_000_000,
                    "storage_quota_bytes": 200_000_000_000,
                }
                mock_fresh.return_value = True

                mock_sub_active = Mock()
                mock_sub_active.expiration = get_current_datetime() + timedelta(days=30)
                mock_sub_old = Mock()
                mock_sub_old.expiration = get_current_datetime() - timedelta(days=60)
                mock_db.execute.return_value.all.return_value = [
                    [mock_sub_active],
                    [mock_sub_old],
                ]

                result = await calculate_limit_warning(user_id)

                assert result is None


@pytest.mark.asyncio
async def test_calculate_limit_warning_expired_premium_high_storage():
    """
    Expired premium user with storage exceeding FREE quota should
    see combined warning.
    """
    user_id = 1

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        with patch(
            "studio.app.common.core.cloud.cloud_utils." "get_user_storage_usage"
        ) as mock_get_storage:
            with patch(
                "studio.app.common.core.cloud.cloud_utils." "_is_storage_data_fresh"
            ) as mock_fresh:
                mock_db = Mock()
                mock_scope.return_value.__enter__.return_value = mock_db

                mock_get_storage.return_value = {
                    "storage_usage_bytes": 100_000_000_000,
                    "storage_quota_bytes": 5_000_000_000,
                }
                mock_fresh.return_value = True

                mock_subscription = Mock()
                mock_subscription.expiration = get_current_datetime() - timedelta(
                    days=5
                )
                mock_db.execute.return_value.all.return_value = [[mock_subscription]]

                result = await calculate_limit_warning(user_id)

                assert result is not None
                assert result.has_alert is True
                assert result.alert_type == AlertType.GRACE.value
                assert result.excess_data_bytes > 0
                assert (
                    "expired" in result.message.lower()
                    or "upgrade" in result.message.lower()
                )


# Failed Storage Operations Retry Tests


class TestProcessFailedStorageOperations:
    """Failed storage decrement queue processing."""

    def test_process_no_failed_operations(self):
        """Should return 0 when no failed operations exist."""
        from studio.app.common.core.cloud.storage_operations import (
            process_failed_storage_operations,
        )

        with patch(
            "studio.app.common.core.cloud.storage_operations.session_scope"
        ) as mock_scope:
            mock_db = Mock()
            mock_scope.return_value.__enter__.return_value = mock_db
            mock_db.execute.return_value.all.return_value = []

            result = process_failed_storage_operations()

            assert result == 0

    def test_process_failed_operation_success(self):
        """Should retry failed operation and mark as completed."""
        from studio.app.common.core.cloud.storage_operations import (
            process_failed_storage_operations,
        )
        from studio.app.common.models.subscription import (
            StorageOperationStatus,
            StorageOperationType,
        )

        with patch(
            "studio.app.common.core.cloud.storage_operations.session_scope"
        ) as mock_scope:
            mock_db = Mock()
            mock_scope.return_value.__enter__.return_value = mock_db

            mock_op = Mock()
            mock_op.id = 1
            mock_op.user_id = 123
            mock_op.operation_type = StorageOperationType.DECREMENT.value
            mock_op.bytes_delta = 1000
            mock_op.retry_count = 0
            mock_op.status = StorageOperationStatus.FAILED.value

            mock_usage = Mock()
            mock_usage.storage_usage_bytes = 5000
            mock_usage.last_updated = None

            mock_db.execute.return_value.all.return_value = [(mock_op, mock_usage)]

            process_failed_storage_operations()

            assert mock_op.status == StorageOperationStatus.COMPLETED.value
            assert mock_op.retry_count == 1

    def test_respects_max_retry_limit(self):
        """Should not retry operations that exceeded max retries."""
        from studio.app.common.core.cloud.storage_operations import (
            process_failed_storage_operations,
        )

        with patch(
            "studio.app.common.core.cloud.storage_operations.session_scope"
        ) as mock_scope:
            mock_db = Mock()
            mock_scope.return_value.__enter__.return_value = mock_db
            mock_db.execute.return_value.all.return_value = []

            result = process_failed_storage_operations(max_retries=5)

            assert result == 0


# ============================================================================
# _ensure_user_bucket_exists_impl — short-circuit & merge behaviour
# ============================================================================


def _make_db_with_user(attributes):
    user = MagicMock()
    user.id = 42
    user.attributes = attributes
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = user
    return db, user


def test_ensure_bucket_short_circuits_when_attribute_set(monkeypatch):
    """When the DB already holds a bucket name, no S3 CreateBucket call."""
    db, user = _make_db_with_user({"remote_bucket_name": "optinist-user-42-existing00"})

    create_bucket_mock = AsyncMock()

    class FakeWriter:
        def __init__(self, name):
            self.name = name

        async def __aenter__(self):
            inner = MagicMock()
            inner.create_bucket = create_bucket_mock
            return inner

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(
        "studio.app.common.core.storage."
        "remote_storage_controller.RemoteStorageSimpleWriter",
        FakeWriter,
    )

    result = asyncio.run(_ensure_user_bucket_exists_impl(42, db, auto_commit=False))

    assert result == "optinist-user-42-existing00"
    create_bucket_mock.assert_not_called()
    db.commit.assert_not_called()


def test_ensure_bucket_creates_and_merges_when_missing(monkeypatch):
    """When attributes lacks the key, create bucket and merge (preserve others)."""
    db, user = _make_db_with_user({"some_other_key": "preserved"})

    create_bucket_mock = AsyncMock()

    class FakeWriter:
        def __init__(self, name):
            self.name = name

        async def __aenter__(self):
            inner = MagicMock()
            inner.create_bucket = create_bucket_mock
            return inner

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(
        "studio.app.common.core.storage."
        "remote_storage_controller.RemoteStorageSimpleWriter",
        FakeWriter,
    )
    monkeypatch.setenv("S3_USER_BUCKET_SECRET", "test-secret")

    result = asyncio.run(_ensure_user_bucket_exists_impl(42, db, auto_commit=True))

    assert result.startswith("optinist-user-42-")
    create_bucket_mock.assert_awaited_once()
    # Merge preserved the other key
    assert user.attributes["some_other_key"] == "preserved"
    assert user.attributes["remote_bucket_name"] == result
    db.commit.assert_called_once()


def test_ensure_bucket_swallows_already_owned_error(monkeypatch):
    """BucketAlreadyOwnedByYou should not propagate; function still returns name."""
    db, user = _make_db_with_user({})

    class FakeWriter:
        def __init__(self, name):
            self.name = name

        async def __aenter__(self):
            inner = MagicMock()
            inner.create_bucket = AsyncMock(
                side_effect=Exception("BucketAlreadyOwnedByYou")
            )
            return inner

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(
        "studio.app.common.core.storage."
        "remote_storage_controller.RemoteStorageSimpleWriter",
        FakeWriter,
    )
    monkeypatch.setenv("S3_USER_BUCKET_SECRET", "test-secret")

    result = asyncio.run(_ensure_user_bucket_exists_impl(42, db, auto_commit=False))

    assert result.startswith("optinist-user-42-")


def test_ensure_bucket_propagates_unexpected_errors(monkeypatch):
    """Errors that are not BucketAlreadyOwnedByYou must propagate."""
    db, user = _make_db_with_user({})

    class FakeWriter:
        def __init__(self, name):
            self.name = name

        async def __aenter__(self):
            inner = MagicMock()
            inner.create_bucket = AsyncMock(side_effect=Exception("AccessDenied"))
            return inner

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(
        "studio.app.common.core.storage."
        "remote_storage_controller.RemoteStorageSimpleWriter",
        FakeWriter,
    )
    monkeypatch.setenv("S3_USER_BUCKET_SECRET", "test-secret")

    with pytest.raises(Exception, match="AccessDenied"):
        asyncio.run(_ensure_user_bucket_exists_impl(42, db, auto_commit=False))


def test_ensure_bucket_returns_none_when_user_not_found():
    """If the user row doesn't exist, return None and don't touch S3."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    result = asyncio.run(_ensure_user_bucket_exists_impl(999, db, auto_commit=False))

    assert result is None
    db.commit.assert_not_called()


# ============================================================================
# Tests for get_effective_quota_bytes() - shared enforcement quota (issue #721)
# ============================================================================

FREE_QUOTA_BYTES = StorageQuota.FREE * StorageSize.GB
PREMIUM_QUOTA_BYTES = StorageQuota.PREMIUM * StorageSize.GB


def _mock_lifecycle_db(expiration):
    """Build a mocked session_scope whose lifecycle query returns one premium row."""
    mock_db = Mock()
    mock_subscription = Mock()
    mock_subscription.expiration = expiration
    mock_db.execute.return_value.all.return_value = [[mock_subscription]]
    return mock_db


def _run_effective_quota(expiration, raw_quota_bytes):
    from studio.app.common.core.cloud.cloud_utils import get_effective_quota_bytes

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        mock_scope.return_value.__enter__.return_value = _mock_lifecycle_db(expiration)
        return get_effective_quota_bytes(
            1, storage_info={"storage_quota_bytes": raw_quota_bytes}
        )


def test_effective_quota_overdue_premium_downgraded_to_free():
    """Overdue premium with a still-raw premium record enforces the free limit."""
    grace = SubscriptionPeriods.GRACE_PERIOD_DAYS
    warning = SubscriptionPeriods.WARNING_PERIOD_DAYS
    expiration = get_current_datetime() - timedelta(days=grace + warning + 5)

    assert _run_effective_quota(expiration, PREMIUM_QUOTA_BYTES) == FREE_QUOTA_BYTES


def test_effective_quota_grace_downgraded_to_free():
    """A user just inside the grace period is held to the 5GB free limit."""
    expiration = get_current_datetime() - timedelta(days=1)

    assert _run_effective_quota(expiration, PREMIUM_QUOTA_BYTES) == FREE_QUOTA_BYTES


def test_effective_quota_warning_downgraded_to_free():
    """A user in the post-grace warning window is held to the 5GB free limit."""
    grace = SubscriptionPeriods.GRACE_PERIOD_DAYS
    expiration = get_current_datetime() - timedelta(days=grace + 1)

    assert _run_effective_quota(expiration, PREMIUM_QUOTA_BYTES) == FREE_QUOTA_BYTES


def test_effective_quota_active_premium_uses_raw():
    """An active premium user keeps the full raw quota - no downgrade."""
    expiration = get_current_datetime() + timedelta(days=30)

    assert _run_effective_quota(expiration, PREMIUM_QUOTA_BYTES) == PREMIUM_QUOTA_BYTES


def test_effective_quota_free_user_uses_raw():
    """A user who never had premium (no rows) enforces their raw quota (5GB)."""
    from studio.app.common.core.cloud.cloud_utils import get_effective_quota_bytes

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        mock_db = Mock()
        mock_db.execute.return_value.all.return_value = []  # never had premium
        mock_scope.return_value.__enter__.return_value = mock_db

        result = get_effective_quota_bytes(
            1, storage_info={"storage_quota_bytes": FREE_QUOTA_BYTES}
        )

    assert result == FREE_QUOTA_BYTES


def test_effective_quota_zero_raw_quota_returns_zero():
    """Unknown/disabled quota (<=0) returns 0 so callers skip enforcement."""
    from studio.app.common.core.cloud.cloud_utils import get_effective_quota_bytes

    assert get_effective_quota_bytes(1, storage_info={"storage_quota_bytes": 0}) == 0
    assert get_effective_quota_bytes(1, storage_info={}) == 0


def test_effective_quota_malformed_row_fails_open_to_raw():
    """A premium row with None expiration cannot be classified -> enforce raw quota."""
    from studio.app.common.core.cloud.cloud_utils import get_effective_quota_bytes

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        mock_scope.return_value.__enter__.return_value = _mock_lifecycle_db(None)

        result = get_effective_quota_bytes(
            1, storage_info={"storage_quota_bytes": PREMIUM_QUOTA_BYTES}
        )

    assert result == PREMIUM_QUOTA_BYTES


# ============================================================================
# Run gate uses the effective quota (issue #721)
# ============================================================================


def test_run_gate_blocks_overdue_user_over_effective_quota():
    """End-to-end: _check_storage_quota raises 403 for an overdue user over the
    free effective quota even though their raw quota record still reads premium.

    get_effective_quota_bytes is NOT stubbed, so this proves the gate is wired to
    the effective quota (the fix), not the raw storage_quota_bytes.
    """
    from fastapi import HTTPException

    from studio.app.common.routers.run import _check_storage_quota

    used = 6_886_941_631  # 6.4 GB - the issue's reproduction value
    grace = SubscriptionPeriods.GRACE_PERIOD_DAYS
    warning = SubscriptionPeriods.WARNING_PERIOD_DAYS
    overdue_expiration = get_current_datetime() - timedelta(days=grace + warning + 5)

    with patch(
        "studio.app.common.routers.run.get_current_user_storage_usage",
        new=AsyncMock(return_value=used),
    ), patch(
        "studio.app.common.routers.run.get_user_storage_usage",
        return_value={"storage_quota_bytes": PREMIUM_QUOTA_BYTES},
    ), patch(
        "studio.app.common.core.cloud.cloud_utils.session_scope"
    ) as mock_scope:
        mock_scope.return_value.__enter__.return_value = _mock_lifecycle_db(
            overdue_expiration
        )
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_check_storage_quota(13))

    assert exc.value.status_code == 403
    assert "Storage quota exceeded" in exc.value.detail


def test_run_gate_allows_active_user_under_raw_quota():
    """End-to-end: an active premium user under their raw quota is unaffected."""
    from studio.app.common.routers.run import _check_storage_quota

    active_expiration = get_current_datetime() + timedelta(days=30)

    with patch(
        "studio.app.common.routers.run.get_current_user_storage_usage",
        new=AsyncMock(return_value=50 * StorageSize.GB),
    ), patch(
        "studio.app.common.routers.run.get_user_storage_usage",
        return_value={"storage_quota_bytes": PREMIUM_QUOTA_BYTES},
    ), patch(
        "studio.app.common.core.cloud.cloud_utils.session_scope"
    ) as mock_scope:
        mock_scope.return_value.__enter__.return_value = _mock_lifecycle_db(
            active_expiration
        )
        # Should not raise (50GB well under the raw premium effective quota)
        asyncio.run(_check_storage_quota(1))


def test_effective_quota_db_error_fails_open_to_raw():
    """A DB error while resolving lifecycle must fail open to the raw quota,
    never propagate (which would 500 the run/upload)."""
    from studio.app.common.core.cloud.cloud_utils import get_effective_quota_bytes

    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        mock_scope.side_effect = Exception("DB connection lost")

        result = get_effective_quota_bytes(
            1, storage_info={"storage_quota_bytes": PREMIUM_QUOTA_BYTES}
        )

    assert result == PREMIUM_QUOTA_BYTES


# ============================================================================
# Upload gate uses the effective quota (issue #721)
# ============================================================================


def _call_create_file(user_id, filename="test.tiff"):
    """Invoke create_file for the given user. workspace_id/filename and the
    background_tasks/file/db/remote_bucket_name args are inert for the gate: it
    reads only current_user.id and raises before touching the rest."""
    from studio.app.common.routers.files import create_file

    return asyncio.run(
        create_file(
            workspace_id="1",
            filename=filename,
            background_tasks=Mock(),
            file=Mock(),
            current_user=Mock(id=user_id),
            db=Mock(),
            remote_bucket_name="",
        )
    )


def test_upload_gate_blocks_overdue_user_over_effective_quota():
    """End-to-end: create_file raises 403 for an overdue user over the free
    effective quota even though their raw quota record still reads premium.

    get_effective_quota_bytes is NOT stubbed, so this proves the upload gate is
    wired to the effective quota (the fix), not the raw storage_quota_bytes.
    """
    from fastapi import HTTPException

    used = 6_886_941_631  # 6.4 GB - the issue's reproduction value
    # Guard the premise: over the free limit but under the raw premium quota,
    # so a 403 can only mean the gate used the effective (downgraded) quota.
    assert FREE_QUOTA_BYTES < used < PREMIUM_QUOTA_BYTES
    grace = SubscriptionPeriods.GRACE_PERIOD_DAYS
    warning = SubscriptionPeriods.WARNING_PERIOD_DAYS
    overdue_expiration = get_current_datetime() - timedelta(days=grace + warning + 5)

    with patch(
        "studio.app.common.routers.files.get_current_user_storage_usage",
        new=AsyncMock(return_value=used),
    ), patch(
        "studio.app.common.routers.files.get_user_storage_usage",
        return_value={"storage_quota_bytes": PREMIUM_QUOTA_BYTES},
    ), patch(
        "studio.app.common.core.cloud.cloud_utils.session_scope"
    ) as mock_scope:
        mock_scope.return_value.__enter__.return_value = _mock_lifecycle_db(
            overdue_expiration
        )
        with pytest.raises(HTTPException) as exc:
            _call_create_file(13)

    assert exc.value.status_code == 403
    assert "Storage quota exceeded" in exc.value.detail


def test_upload_gate_allows_active_user_under_raw_quota():
    """End-to-end: an active premium user well under their raw quota is not
    blocked - proves active premium keeps the raw quota (not downgraded to free)
    through the real create_file gate.
    """
    active_expiration = get_current_datetime() + timedelta(days=30)

    with patch(
        "studio.app.common.routers.files.get_current_user_storage_usage",
        new=AsyncMock(return_value=50 * StorageSize.GB),
    ), patch(
        "studio.app.common.routers.files.get_user_storage_usage",
        return_value={"storage_quota_bytes": PREMIUM_QUOTA_BYTES},
    ), patch(
        "studio.app.common.core.cloud.cloud_utils.session_scope"
    ) as mock_scope, patch(
        "studio.app.common.routers.files.create_directory"
    ), patch(
        "studio.app.common.routers.files.open", create=True
    ), patch(
        "studio.app.common.routers.files.shutil"
    ), patch(
        "studio.app.common.routers.files.WorkspaceDataCapacityService.is_available",
        return_value=False,
    ), patch(
        "studio.app.common.routers.files.RemoteStorageController.is_available",
        return_value=False,
    ):
        mock_scope.return_value.__enter__.return_value = _mock_lifecycle_db(
            active_expiration
        )
        # 50GB is far under the raw premium quota; would only 403 if the gate
        # wrongly downgraded an active premium user to the free limit.
        result = _call_create_file(1, filename="test.csv")

    assert result == {"file_path": "test.csv"}
