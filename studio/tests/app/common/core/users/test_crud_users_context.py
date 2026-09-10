"""
Unit tests for crud_users.py - get_user_with_context() function

``get_user_with_context()`` issues one multi-join query and then derives every
subscription and storage field from that single result row, via
``_transform_user_row``. The derivation is pure given the row, so the whole
tier ladder (Free / Premium / Limit Grace / Expired) and the storage-percent
arithmetic are reachable by returning a row tuple from a mocked ``db.execute``.

These cases carried ``@pytest.mark.skip("Requires integration test with real
DB")`` and so never ran, while the coverage map credited them with the
grace-period boundary. The skip reason was wrong - nothing here needs a
database - and the boundary cases below are new: unskipping alone left the
Limit Grace edge unasserted, so moving the operator still passed.

The two boundary cases address the window by ``GRACE_PERIOD_DAYS``, so they pin
which side of the comparison each edge falls on but not the window's length;
``test_the_grace_window_is_thirty_days`` pins that separately.
"""

from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from fastapi import HTTPException

from studio.app.common.core.subscription.constants import (
    PlanName,
    SubscriptionPeriods,
    SubscriptionPlanIds,
    SubscriptionStatus,
)
from studio.app.common.core.users.crud_users import get_user_with_context
from studio.app.common.core.utils.datetime_utils import get_current_datetime

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_db():
    """Mock database session"""
    db = Mock()
    db.execute = Mock()
    return db


@pytest.fixture
def mock_user_model():
    """Mock UserModel instance"""
    user = Mock()
    user.id = 1
    user.uid = "test-user-123"
    user.email = "test@example.com"
    user.name = "Test User"
    user.active = True
    user.organization_id = 1

    # Create mock organization
    mock_org = Mock()
    mock_org.id = 1
    mock_org.name = "Test Organization"
    user.organization = mock_org

    user.__dict__ = {
        "id": 1,
        "uid": "test-user-123",
        "email": "test@example.com",
        "name": "Test User",
        "active": True,
        "organization_id": 1,
        "organization": mock_org,
    }
    return user


def create_query_result(
    user,
    role_id=1,
    data_usage=0,
    subscription_plan_name=None,
    storage_usage_bytes=0,
    storage_quota_bytes=5_000_000_000,
    subscription_expiration=None,
    subscription_plan_id=None,
):
    """Helper to create query result tuple"""
    return (
        user,
        role_id,
        data_usage,
        subscription_plan_name,
        storage_usage_bytes,
        storage_quota_bytes,
        subscription_expiration,
        subscription_plan_id,
    )


# ============================================================================
# Tests for get_user_with_context() - Subscription Status
# ============================================================================


@pytest.mark.asyncio
async def test_get_user_with_context_free_user(mock_db, mock_user_model):
    """Test user context with no subscription (free user)"""
    query_result = create_query_result(
        mock_user_model,
        subscription_plan_name=None,
        subscription_expiration=None,
        subscription_plan_id=None,
    )

    mock_result = Mock()
    mock_result.first.return_value = query_result
    mock_db.execute.return_value = mock_result

    result = await get_user_with_context(mock_db, 1)

    assert result.subscription_status == SubscriptionStatus.FREE.value
    assert result.subscription_days_remaining is None
    assert result.subscription_plan_name == PlanName.FREE.value


@pytest.mark.asyncio
async def test_get_user_with_context_premium_active(mock_db, mock_user_model):
    """Test user context with active premium subscription (30 days remaining)"""
    future_date = get_current_datetime() + timedelta(days=30)

    query_result = create_query_result(
        mock_user_model,
        subscription_plan_name=PlanName.PREMIUM.value,
        subscription_expiration=future_date,
        subscription_plan_id=SubscriptionPlanIds.PREMIUM,
    )

    mock_result = Mock()
    mock_result.first.return_value = query_result
    mock_db.execute.return_value = mock_result

    result = await get_user_with_context(mock_db, 1)

    assert result.subscription_status == SubscriptionStatus.PREMIUM.value
    # Allow 1 day variance due to timing
    assert result.subscription_days_remaining >= 29
    assert result.subscription_days_remaining <= 30


def test_the_grace_window_is_thirty_days():
    """The boundary cases below derive their dates from GRACE_PERIOD_DAYS, so a
    change to the constant moves the test with the product. The billing grace
    the product promises is a specific length, so pin it here."""
    assert SubscriptionPeriods.GRACE_PERIOD_DAYS == 30


@pytest.mark.asyncio
async def test_get_user_with_context_premium_in_grace_period(mock_db, mock_user_model):
    """Test user context with subscription in grace period (5 days past expiration)"""
    past_date = get_current_datetime() - timedelta(days=5)

    query_result = create_query_result(
        mock_user_model,
        subscription_plan_name=PlanName.PREMIUM.value,
        subscription_expiration=past_date,
        subscription_plan_id=SubscriptionPlanIds.PREMIUM,
    )

    mock_result = Mock()
    mock_result.first.return_value = query_result
    mock_db.execute.return_value = mock_result

    result = await get_user_with_context(mock_db, 1)

    # In grace period: -GRACE_PERIOD_DAYS <= days_remaining <= -1
    assert result.subscription_status == SubscriptionStatus.LIMIT_GRACE.value
    # Days left in grace period = GRACE_PERIOD_DAYS - 5
    expected_grace_days = SubscriptionPeriods.GRACE_PERIOD_DAYS - 5
    assert result.subscription_days_remaining >= expected_grace_days - 1
    assert result.subscription_days_remaining <= expected_grace_days + 1


async def _status_at_days_past_expiry(mock_db, user, days_past: int):
    """Resolve the tier for a premium row that expired exactly ``days_past``
    days ago.

    ``_transform_user_row`` derives ``days_remaining`` as
    ``(expiration - get_current_datetime()).days``, which truncates toward
    negative infinity. Pinning ``now`` is what makes the boundary exact: with a
    live clock the expiration is always a few microseconds staler than the
    ``now`` the function reads, so ``-7 days`` truncates to ``-8`` and the
    boundary cannot be addressed at all. That is why the two boundary rows here
    previously had to hedge with ``in [LIMIT_GRACE, EXPIRED]``.
    """
    now = get_current_datetime()
    query_result = create_query_result(
        user,
        subscription_plan_name=PlanName.PREMIUM.value,
        subscription_expiration=now - timedelta(days=days_past),
        subscription_plan_id=SubscriptionPlanIds.PREMIUM,
    )

    mock_result = Mock()
    mock_result.first.return_value = query_result
    mock_db.execute.return_value = mock_result

    with patch(
        "studio.app.common.core.users.crud_users.get_current_datetime",
        return_value=now,
    ):
        result = await get_user_with_context(mock_db, 1)
    return result


@pytest.mark.asyncio
async def test_get_user_with_context_premium_grace_period_last_day(
    mock_db, mock_user_model
):
    """The final day of the grace window still grants Limit Grace access.

    On this day the user still keeps the premium storage quota, so shrinking the
    window silently drops a paying-but-lapsed user to 5GB and over quota.
    """
    result = await _status_at_days_past_expiry(
        mock_db, mock_user_model, SubscriptionPeriods.GRACE_PERIOD_DAYS
    )

    assert result.subscription_status == SubscriptionStatus.LIMIT_GRACE.value
    assert result.subscription_days_remaining == 0


@pytest.mark.asyncio
async def test_get_user_with_context_premium_one_day_past_grace_is_expired(
    mock_db, mock_user_model
):
    """One day beyond the window is Expired, not Limit Grace.

    The companion half of the boundary: without this, widening
    GRACE_PERIOD_DAYS passes every other case in this file.
    """
    result = await _status_at_days_past_expiry(
        mock_db, mock_user_model, SubscriptionPeriods.GRACE_PERIOD_DAYS + 1
    )

    assert result.subscription_status == SubscriptionStatus.EXPIRED.value
    assert result.subscription_days_remaining is None


@pytest.mark.asyncio
async def test_get_user_with_context_expiring_today_enters_grace(
    mock_db, mock_user_model
):
    """The upper boundary: ``days_remaining == 0`` is the *first* grace day.

    Premium requires ``days_remaining > 0``, so an expiration of exactly now
    lands in Limit Grace with the full window still ahead. Pins which side of
    the ``> 0`` / ``>= -GRACE`` split zero falls on, which the pre-existing
    ``in [PREMIUM, LIMIT_GRACE]`` assertion could not.
    """
    result = await _status_at_days_past_expiry(mock_db, mock_user_model, 0)

    assert result.subscription_status == SubscriptionStatus.LIMIT_GRACE.value
    assert result.subscription_days_remaining == SubscriptionPeriods.GRACE_PERIOD_DAYS


@pytest.mark.asyncio
async def test_get_user_with_context_premium_expired(mock_db, mock_user_model):
    """Test user context with expired subscription (past grace period)"""
    # Past the whole grace window
    past_date = get_current_datetime() - timedelta(
        days=SubscriptionPeriods.GRACE_PERIOD_DAYS + 5
    )

    query_result = create_query_result(
        mock_user_model,
        subscription_plan_name=PlanName.PREMIUM.value,
        subscription_expiration=past_date,
        subscription_plan_id=SubscriptionPlanIds.PREMIUM,
    )

    mock_result = Mock()
    mock_result.first.return_value = query_result
    mock_db.execute.return_value = mock_result

    result = await get_user_with_context(mock_db, 1)

    assert result.subscription_status == SubscriptionStatus.EXPIRED.value
    assert result.subscription_days_remaining is None


@pytest.mark.asyncio
async def test_get_user_with_context_free_plan_id(mock_db, mock_user_model):
    """Test user with FREE plan_id (not premium)"""
    future_date = get_current_datetime() + timedelta(days=30)

    query_result = create_query_result(
        mock_user_model,
        subscription_plan_name=PlanName.FREE.value,
        subscription_expiration=future_date,
        subscription_plan_id=SubscriptionPlanIds.FREE,
    )

    mock_result = Mock()
    mock_result.first.return_value = query_result
    mock_db.execute.return_value = mock_result

    result = await get_user_with_context(mock_db, 1)

    assert result.subscription_status == SubscriptionStatus.FREE.value
    assert result.subscription_days_remaining is None


# ============================================================================
# Tests for get_user_with_context() - Storage Usage
# ============================================================================


@pytest.mark.asyncio
async def test_get_user_with_context_storage_usage_calculation(
    mock_db, mock_user_model
):
    """Test storage usage percentage calculation"""
    # 2.5 GB used of 5 GB quota = 50%
    query_result = create_query_result(
        mock_user_model,
        storage_usage_bytes=2_500_000_000,
        storage_quota_bytes=5_000_000_000,
    )

    mock_result = Mock()
    mock_result.first.return_value = query_result
    mock_db.execute.return_value = mock_result

    result = await get_user_with_context(mock_db, 1)

    assert result.storage_usage_bytes == 2_500_000_000
    assert result.storage_quota_bytes == 5_000_000_000
    assert result.storage_usage_percent == 50.0


@pytest.mark.asyncio
async def test_get_user_with_context_storage_zero_usage(mock_db, mock_user_model):
    """Test storage with zero usage"""
    query_result = create_query_result(
        mock_user_model,
        storage_usage_bytes=0,
        storage_quota_bytes=5_000_000_000,
    )

    mock_result = Mock()
    mock_result.first.return_value = query_result
    mock_db.execute.return_value = mock_result

    result = await get_user_with_context(mock_db, 1)

    assert result.storage_usage_bytes == 0
    assert result.storage_usage_percent == 0.0


@pytest.mark.asyncio
async def test_get_user_with_context_storage_exceeded(mock_db, mock_user_model):
    """Test storage exceeding quota (115%)"""
    # 5.75 GB used of 5 GB quota = 115%
    query_result = create_query_result(
        mock_user_model,
        storage_usage_bytes=5_750_000_000,
        storage_quota_bytes=5_000_000_000,
    )

    mock_result = Mock()
    mock_result.first.return_value = query_result
    mock_db.execute.return_value = mock_result

    result = await get_user_with_context(mock_db, 1)

    assert result.storage_usage_bytes == 5_750_000_000
    assert result.storage_usage_percent == 115.0


@pytest.mark.asyncio
async def test_get_user_with_context_storage_null_values(mock_db, mock_user_model):
    """Test storage with null values in database"""
    query_result = create_query_result(
        mock_user_model,
        storage_usage_bytes=None,
        storage_quota_bytes=None,
    )

    mock_result = Mock()
    mock_result.first.return_value = query_result
    mock_db.execute.return_value = mock_result

    result = await get_user_with_context(mock_db, 1)

    # Should default to 0
    assert result.storage_usage_bytes == 0
    assert result.storage_quota_bytes == 0
    # 0 / 1 * 100 = 0.0 (uses 1 as denominator to avoid division by zero)
    assert result.storage_usage_percent == 0.0


# ============================================================================
# Tests for get_user_with_context() - Edge Cases
# ============================================================================


@pytest.mark.asyncio
async def test_get_user_with_context_user_not_found(mock_db):
    """Test behavior when user is not found"""
    mock_result = Mock()
    mock_result.first.return_value = None
    mock_db.execute.return_value = mock_result

    with pytest.raises(HTTPException) as exc_info:
        await get_user_with_context(mock_db, 999)

    assert exc_info.value.status_code == 404
    assert "User not found" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_get_user_with_context_timezone_naive_expiration(
    mock_db, mock_user_model
):
    """Test handling of timezone-naive expiration datetime"""
    # Create timezone-naive datetime (simulates MySQL DateTime column)
    future_date_naive = get_current_datetime().replace(tzinfo=None) + timedelta(days=30)

    query_result = create_query_result(
        mock_user_model,
        subscription_plan_name=PlanName.PREMIUM.value,
        subscription_expiration=future_date_naive,
        subscription_plan_id=SubscriptionPlanIds.PREMIUM,
    )

    mock_result = Mock()
    mock_result.first.return_value = query_result
    mock_db.execute.return_value = mock_result

    # Should not raise exception - code handles timezone-naive dates
    result = await get_user_with_context(mock_db, 1)

    assert result.subscription_status == SubscriptionStatus.PREMIUM.value
    # Should still calculate days remaining correctly
    assert result.subscription_days_remaining >= 29
    assert result.subscription_days_remaining <= 30


@pytest.mark.asyncio
async def test_get_user_with_context_data_usage(mock_db, mock_user_model):
    """Test data_usage field is populated correctly"""
    query_result = create_query_result(
        mock_user_model,
        data_usage=1_000_000_000,  # 1 GB
    )

    mock_result = Mock()
    mock_result.first.return_value = query_result
    mock_db.execute.return_value = mock_result

    result = await get_user_with_context(mock_db, 1)

    assert result.data_usage == 1_000_000_000
