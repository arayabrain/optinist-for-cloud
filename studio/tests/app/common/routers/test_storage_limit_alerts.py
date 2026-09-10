"""
Unit tests for storage_limit_alerts.py API router

Tests cover:
- GET /me - Current user storage alert
- GET /usage - Detailed storage usage
- GET /all - All users storage alerts (admin)
- POST /refresh - Refresh storage calculation
- GET /limit-warning - Limit warning details
- GET /limit-warning/check - Quick warning status check
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException

from studio.app.common.core.cloud.s3_storage_monitor import S3StorageMonitor
from studio.app.common.core.subscription.constants import AlertType, StorageQuota

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_current_user():
    """Mock current user (non-admin)"""
    user = Mock()
    user.id = 1
    user.uid = "test-user-123"
    user.name = "Test User"
    user.email = "test@example.com"
    user.is_admin = False
    return user


@pytest.fixture
def mock_admin_user():
    """Mock admin user"""
    user = Mock()
    user.id = 2
    user.uid = "admin-user-123"
    user.name = "Admin User"
    user.email = "admin@example.com"
    user.is_admin = True
    return user


@pytest.fixture
def mock_storage_monitor():
    """Mock S3StorageMonitor instance"""
    monitor = Mock(spec=S3StorageMonitor)
    monitor.CRITICAL_THRESHOLD = StorageQuota.CRITICAL_THRESHOLD_PERCENT
    monitor.DANGER_THRESHOLD = StorageQuota.DANGER_THRESHOLD_PERCENT
    monitor.calculate_storage_alert_level = Mock()
    monitor.format_bytes = Mock(return_value="4.5 GB")
    monitor.get_alert_message = Mock(return_value="Storage at 90%")
    return monitor


# ============================================================================
# Tests for GET /me endpoint
# ============================================================================


@pytest.mark.asyncio
async def test_get_me_no_alert(mock_current_user):
    """Test /me endpoint when user has no storage alert"""
    with patch(
        "studio.app.common.core.cloud.storage_tracking.get_current_user_storage_usage"
    ) as mock_get_usage:
        with patch(
            "studio.app.common.core.cloud.storage_tracking.get_user_storage_usage"
        ) as mock_storage_info:
            with patch(
                "studio.app.common.routers.storage_limit_alerts._get_storage_utilities"
            ) as mock_get_utils:
                # Mock usage at 50% (below threshold)
                mock_get_usage.return_value = 2_500_000_000  # 2.5 GB
                mock_storage_info.return_value = {
                    "storage_usage_bytes": 2_500_000_000,
                    "storage_quota_bytes": 5_000_000_000,
                }

                mock_monitor = Mock()
                mock_monitor.calculate_storage_alert_level.return_value = None
                mock_monitor.format_bytes.return_value = "2.5 GB"
                mock_get_utils.return_value = mock_monitor

                # Import and call the endpoint function
                from studio.app.common.routers.storage_limit_alerts import (
                    get_my_storage_alert,
                )

                result = await get_my_storage_alert(current_user=mock_current_user)

                assert result["has_alert"] is False
                assert result["storage_usage_bytes"] == 2_500_000_000
                assert result["storage_usage_formatted"] == "2.5 GB"
                assert result["alert"] is None


@pytest.mark.asyncio
async def test_get_me_critical_alert(mock_current_user):
    """Test /me endpoint when user has critical storage alert (90%)"""
    with patch(
        "studio.app.common.core.cloud.storage_tracking.get_current_user_storage_usage"
    ) as mock_get_usage:
        with patch(
            "studio.app.common.core.cloud.storage_tracking.get_user_storage_usage"
        ) as mock_storage_info:
            with patch(
                "studio.app.common.routers.storage_limit_alerts._get_storage_utilities"
            ) as mock_get_utils:
                # Mock usage at 90% (critical threshold)
                mock_get_usage.return_value = 4_500_000_000  # 4.5 GB
                mock_storage_info.return_value = {
                    "storage_usage_bytes": 4_500_000_000,
                    "storage_quota_bytes": 5_000_000_000,
                }

                mock_monitor = Mock()
                mock_monitor.calculate_storage_alert_level.return_value = "critical"
                mock_monitor.get_alert_message.return_value = (
                    "Storage usage at 90% - approaching limit"
                )
                mock_get_utils.return_value = mock_monitor

                from studio.app.common.routers.storage_limit_alerts import (
                    get_my_storage_alert,
                )

                result = await get_my_storage_alert(current_user=mock_current_user)

                assert result["has_alert"] is True
                assert result["alert"]["alert_level"] == "critical"
                assert result["alert"]["storage_usage_bytes"] == 4_500_000_000
                assert result["alert"]["storage_quota_bytes"] == 5_000_000_000
                assert result["alert"]["storage_usage_percent"] == 90.0
                assert "Storage usage at 90%" in result["alert"]["message"]


@pytest.mark.asyncio
async def test_get_me_danger_alert(mock_current_user):
    """Test /me endpoint when user has danger alert (100%+)"""
    with patch(
        "studio.app.common.core.cloud.storage_tracking.get_current_user_storage_usage"
    ) as mock_get_usage:
        with patch(
            "studio.app.common.core.cloud.storage_tracking.get_user_storage_usage"
        ) as mock_storage_info:
            with patch(
                "studio.app.common.routers.storage_limit_alerts._get_storage_utilities"
            ) as mock_get_utils:
                # Mock usage at 105% (danger threshold)
                mock_get_usage.return_value = 5_250_000_000  # 5.25 GB
                mock_storage_info.return_value = {
                    "storage_usage_bytes": 5_250_000_000,
                    "storage_quota_bytes": 5_000_000_000,
                }

                mock_monitor = Mock()
                mock_monitor.calculate_storage_alert_level.return_value = "danger"
                mock_monitor.get_alert_message.return_value = (
                    "Storage quota exceeded - immediate action required"
                )
                mock_get_utils.return_value = mock_monitor

                from studio.app.common.routers.storage_limit_alerts import (
                    get_my_storage_alert,
                )

                result = await get_my_storage_alert(current_user=mock_current_user)

                assert result["has_alert"] is True
                assert result["alert"]["alert_level"] == "danger"
                assert result["alert"]["storage_usage_percent"] == 105.0


@pytest.mark.asyncio
async def test_get_me_no_storage_info(mock_current_user):
    """Test /me endpoint when user has no storage info in database"""
    with patch(
        "studio.app.common.core.cloud.storage_tracking.get_current_user_storage_usage"
    ) as mock_get_usage:
        with patch(
            "studio.app.common.core.cloud.storage_tracking.get_user_storage_usage"
        ) as mock_storage_info:
            with patch(
                "studio.app.common.routers.storage_limit_alerts._get_storage_utilities"
            ) as mock_get_utils:
                mock_get_usage.return_value = 1_000_000_000
                mock_storage_info.return_value = None

                mock_monitor = Mock()
                mock_monitor.format_bytes.return_value = "1.0 GB"
                mock_get_utils.return_value = mock_monitor

                from studio.app.common.routers.storage_limit_alerts import (
                    get_my_storage_alert,
                )

                result = await get_my_storage_alert(current_user=mock_current_user)

                assert result["has_alert"] is False
                assert result["storage_usage_bytes"] == 1_000_000_000


# ============================================================================
# Tests for GET /usage endpoint
# ============================================================================


@pytest.mark.asyncio
async def test_get_usage_with_quota(mock_current_user):
    """Test /usage endpoint with valid storage quota"""
    with patch(
        "studio.app.common.core.cloud.storage_tracking.get_current_user_storage_usage"
    ) as mock_get_usage:
        with patch(
            "studio.app.common.core.cloud.storage_tracking.get_user_storage_usage"
        ) as mock_storage_info:
            with patch(
                "studio.app.common.routers.storage_limit_alerts._get_storage_utilities"
            ) as mock_get_utils:
                mock_get_usage.return_value = 3_000_000_000  # 3 GB
                mock_storage_info.return_value = {
                    "storage_usage_bytes": 3_000_000_000,
                    "storage_quota_bytes": 5_000_000_000,
                }

                mock_monitor = Mock()
                mock_monitor.CRITICAL_THRESHOLD = 90
                mock_monitor.DANGER_THRESHOLD = 100
                mock_monitor.calculate_storage_alert_level.return_value = None
                mock_monitor.format_bytes.side_effect = ["3.0 GB", "5.0 GB"]
                mock_get_utils.return_value = mock_monitor

                from studio.app.common.routers.storage_limit_alerts import (
                    get_my_storage_usage,
                )

                result = await get_my_storage_usage(current_user=mock_current_user)

                assert result["storage_usage_bytes"] == 3_000_000_000
                assert result["storage_quota_bytes"] == 5_000_000_000
                assert result["storage_usage_percent"] == 60.0
                assert result["alert_level"] is None
                assert result["thresholds"]["critical"] == 90
                assert result["thresholds"]["danger"] == 100


@pytest.mark.asyncio
async def test_get_usage_no_storage_info(mock_current_user):
    """Test /usage endpoint when user has no storage info"""
    with patch(
        "studio.app.common.core.cloud.storage_tracking.get_current_user_storage_usage"
    ) as mock_get_usage:
        with patch(
            "studio.app.common.core.cloud.storage_tracking.get_user_storage_usage"
        ) as mock_storage_info:
            with patch(
                "studio.app.common.routers.storage_limit_alerts._get_storage_utilities"
            ) as mock_get_utils:
                mock_get_usage.return_value = 1_000_000_000
                mock_storage_info.return_value = None

                mock_monitor = Mock()
                mock_monitor.CRITICAL_THRESHOLD = 90
                mock_monitor.DANGER_THRESHOLD = 100
                mock_monitor.format_bytes.return_value = "1.0 GB"
                mock_get_utils.return_value = mock_monitor

                from studio.app.common.routers.storage_limit_alerts import (
                    get_my_storage_usage,
                )

                result = await get_my_storage_usage(current_user=mock_current_user)

                assert result["storage_usage_bytes"] == 1_000_000_000
                assert result["storage_quota_bytes"] is None
                assert result["storage_usage_percent"] is None
                assert result["alert_level"] is None


@pytest.mark.asyncio
async def test_get_usage_zero_quota(mock_current_user):
    """Test /usage endpoint when quota is zero (edge case)"""
    with patch(
        "studio.app.common.core.cloud.storage_tracking.get_current_user_storage_usage"
    ) as mock_get_usage:
        with patch(
            "studio.app.common.core.cloud.storage_tracking.get_user_storage_usage"
        ) as mock_storage_info:
            with patch(
                "studio.app.common.routers.storage_limit_alerts._get_storage_utilities"
            ) as mock_get_utils:
                mock_get_usage.return_value = 1_000_000_000
                mock_storage_info.return_value = {
                    "storage_usage_bytes": 1_000_000_000,
                    "storage_quota_bytes": 0,
                }

                mock_monitor = Mock()
                mock_monitor.CRITICAL_THRESHOLD = 90
                mock_monitor.DANGER_THRESHOLD = 100
                mock_monitor.calculate_storage_alert_level.return_value = None
                mock_monitor.format_bytes.side_effect = ["1.0 GB", "0 B"]
                mock_get_utils.return_value = mock_monitor

                from studio.app.common.routers.storage_limit_alerts import (
                    get_my_storage_usage,
                )

                result = await get_my_storage_usage(current_user=mock_current_user)

                assert result["storage_usage_percent"] == 0  # Should handle div by 0


# ============================================================================
# Tests for GET /all endpoint (admin)
# ============================================================================


@pytest.mark.asyncio
async def test_get_all_admin_access(mock_admin_user):
    """Test /all endpoint with admin user"""
    with patch(
        "studio.app.common.routers.storage_limit_alerts."
        "monitor_storage_and_generate_alerts"
    ) as mock_monitor_alerts:
        mock_alerts = [
            {
                "user_id": 1,
                "alert_level": "critical",
                "storage_usage_bytes": 4_500_000_000,
                "storage_quota_bytes": 5_000_000_000,
                "storage_usage_percent": 90.0,
            }
        ]
        mock_monitor_alerts.return_value = mock_alerts

        with patch(
            "studio.app.common.routers.storage_limit_alerts.S3StorageMonitor"
        ) as mock_monitor_class:
            mock_monitor = Mock()
            mock_monitor.get_alert_message.return_value = "Storage at 90%"
            mock_monitor_class.return_value = mock_monitor

            from studio.app.common.routers.storage_limit_alerts import (
                get_all_storage_alerts,
            )

            result = await get_all_storage_alerts(
                current_user=mock_admin_user,
                remote_bucket_name="test-bucket",
                db=Mock(),
            )

            assert len(result) == 1
            assert result[0]["alert_level"] == "critical"
            assert "message" in result[0]


@pytest.mark.asyncio
async def test_get_all_non_admin_forbidden(mock_current_user):
    """Test /all endpoint rejects non-admin users"""
    from studio.app.common.routers.storage_limit_alerts import get_all_storage_alerts

    with pytest.raises(HTTPException) as exc_info:
        await get_all_storage_alerts(
            current_user=mock_current_user,
            remote_bucket_name="test-bucket",
            db=Mock(),
        )

    assert exc_info.value.status_code == 403
    assert "Admin access required" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_get_all_no_bucket(mock_admin_user):
    """Test /all endpoint when no S3 bucket configured"""
    from studio.app.common.routers.storage_limit_alerts import get_all_storage_alerts

    with pytest.raises(HTTPException) as exc_info:
        await get_all_storage_alerts(
            current_user=mock_admin_user,
            remote_bucket_name=None,
            db=Mock(),
        )

    assert exc_info.value.status_code == 400
    assert "No S3 bucket configured" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_get_all_empty_alerts(mock_admin_user):
    """Test /all endpoint when no alerts exist"""
    with patch(
        "studio.app.common.routers.storage_limit_alerts."
        "monitor_storage_and_generate_alerts"
    ) as mock_monitor_alerts:
        mock_monitor_alerts.return_value = []

        from studio.app.common.routers.storage_limit_alerts import (
            get_all_storage_alerts,
        )

        result = await get_all_storage_alerts(
            current_user=mock_admin_user,
            remote_bucket_name="test-bucket",
            db=Mock(),
        )

        assert result == []


# ============================================================================
# Tests for POST /refresh endpoint
# ============================================================================


@pytest.mark.asyncio
async def test_refresh_storage_success(mock_current_user):
    """Test /refresh endpoint successfully recalculates storage"""
    with patch(
        "studio.app.common.routers.storage_limit_alerts.S3StorageMonitor"
    ) as mock_monitor_class:
        with patch(
            "studio.app.common.core.cloud.storage_tracking.update_user_storage_usage"
        ) as mock_update:
            mock_monitor = Mock()
            mock_monitor.get_user_s3_storage_size = AsyncMock(
                return_value=3_500_000_000
            )
            mock_monitor.format_bytes.return_value = "3.5 GB"
            mock_monitor_class.return_value = mock_monitor

            mock_update.return_value = True

            from studio.app.common.routers.storage_limit_alerts import (
                refresh_storage_usage,
            )

            result = await refresh_storage_usage(
                current_user=mock_current_user,
                remote_bucket_name="test-bucket",
            )

            assert result["success"] is True
            assert result["updated_usage_bytes"] == 3_500_000_000
            assert result["database_updated"] is True


@pytest.mark.asyncio
async def test_refresh_storage_no_bucket(mock_current_user):
    """Test /refresh endpoint when no bucket configured"""
    from studio.app.common.routers.storage_limit_alerts import refresh_storage_usage

    with pytest.raises(HTTPException) as exc_info:
        await refresh_storage_usage(
            current_user=mock_current_user,
            remote_bucket_name=None,
        )

    assert exc_info.value.status_code == 400
    assert "No S3 bucket configured" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_refresh_storage_database_update_fails(mock_current_user):
    """Test /refresh endpoint when database update fails"""
    with patch(
        "studio.app.common.routers.storage_limit_alerts.S3StorageMonitor"
    ) as mock_monitor_class:
        with patch(
            "studio.app.common.core.cloud.storage_tracking.update_user_storage_usage"
        ) as mock_update:
            mock_monitor = Mock()
            mock_monitor.get_user_s3_storage_size = AsyncMock(
                return_value=3_500_000_000
            )
            mock_monitor.format_bytes.return_value = "3.5 GB"
            mock_monitor_class.return_value = mock_monitor

            mock_update.return_value = False  # Database update failed

            from studio.app.common.routers.storage_limit_alerts import (
                refresh_storage_usage,
            )

            result = await refresh_storage_usage(
                current_user=mock_current_user,
                remote_bucket_name="test-bucket",
            )

            assert result["success"] is True
            assert result["database_updated"] is False


# ============================================================================
# Tests for GET /limit-warning endpoint
# ============================================================================


@pytest.mark.asyncio
async def test_get_limit_warning_has_alert(mock_current_user):
    """Test /limit-warning endpoint when user has a warning"""
    with patch(
        "studio.app.common.core.cloud.cloud_utils.calculate_limit_warning"
    ) as mock_calc:
        from studio.app.common.schemas.storage import LimitWarning

        mock_warning = LimitWarning(
            has_alert=True,
            alert_type=AlertType.STORAGE.value,
            days_remaining=7,
            excess_data_bytes=500_000_000,
            excess_data_gb=0.5,
            storage_usage_bytes=4_750_000_000,
            storage_usage_gb=4.75,
            storage_quota_bytes=5_000_000_000,
            storage_quota_gb=5.0,
            message="Storage exceeded",
        )
        mock_calc.return_value = mock_warning

        from studio.app.common.routers.storage_limit_alerts import get_my_limit_warning

        result = await get_my_limit_warning(current_user=mock_current_user)

        assert result == mock_warning
        assert result.has_alert is True
        assert result.alert_type == AlertType.STORAGE.value


@pytest.mark.asyncio
async def test_get_limit_warning_no_warning(mock_current_user):
    """Test /limit-warning endpoint when user has no warning"""
    with patch(
        "studio.app.common.core.cloud.cloud_utils.calculate_limit_warning"
    ) as mock_calc:
        mock_calc.return_value = None

        from studio.app.common.routers.storage_limit_alerts import get_my_limit_warning

        result = await get_my_limit_warning(current_user=mock_current_user)

        assert result is None


# ============================================================================
# Tests for GET /limit-warning/check endpoint
# ============================================================================


@pytest.mark.asyncio
async def test_check_limit_warning_has_alert(mock_current_user):
    """Test /limit-warning/check endpoint when user has warning"""
    with patch(
        "studio.app.common.core.cloud.cloud_utils.calculate_limit_warning"
    ) as mock_calc:
        from studio.app.common.schemas.storage import LimitWarning

        mock_warning = LimitWarning(
            has_alert=True,
            alert_type="grace",
            days_remaining=5,
            excess_data_bytes=0,
            excess_data_gb=0.0,
            storage_usage_bytes=3_000_000_000,
            storage_usage_gb=3.0,
            storage_quota_bytes=5_000_000_000,
            storage_quota_gb=5.0,
            message="Subscription expiring",
        )
        mock_calc.return_value = mock_warning

        from studio.app.common.routers.storage_limit_alerts import (
            check_limit_warning_status,
        )

        result = await check_limit_warning_status(current_user=mock_current_user)

        assert result.has_alert is True
        assert result.alert_type == "grace"
        assert result.days_remaining == 5


@pytest.mark.asyncio
async def test_check_limit_warning_no_warning(mock_current_user):
    """Test /limit-warning/check endpoint when user has no warning"""
    with patch(
        "studio.app.common.core.cloud.cloud_utils.calculate_limit_warning"
    ) as mock_calc:
        mock_calc.return_value = None

        from studio.app.common.routers.storage_limit_alerts import (
            check_limit_warning_status,
        )

        result = await check_limit_warning_status(current_user=mock_current_user)

        assert result.has_alert is False
        assert result.alert_type is None
        assert result.days_remaining is None


# ============================================================================
# Contract Tests - Verify response format matches frontend TypeScript interface
# ============================================================================
# These tests ensure the API response structure matches what the frontend expects.
# Frontend interface (from frontend/src/api/storage/StorageAlerts.ts):
#
# export interface LimitAlert {
#   has_alert: boolean
#   alert_type: LimitAlertType  // "storage" | "grace" | "overdue"
#   days_remaining: number
#   excess_data_bytes: number
#   excess_data_gb: number
#   storage_usage_bytes: number
#   storage_usage_gb: number
#   storage_quota_bytes: number
#   storage_quota_gb: number
#   subscription_end_date?: string
#   grace_end_date?: string
#   deletion_date?: string
#   message: string
# }

# Required fields that must be present in all LimitAlert responses
LIMIT_ALERT_REQUIRED_FIELDS = {
    "has_alert": bool,
    "alert_type": str,  # Must be AlertType value: STORAGE, GRACE, or OVERDUE
    "days_remaining": int,
    "excess_data_bytes": (int, float),
    "excess_data_gb": (int, float),
    "storage_usage_bytes": (int, float),
    "storage_usage_gb": (int, float),
    "storage_quota_bytes": (int, float),
    "storage_quota_gb": (int, float),
    "message": str,
}

# Optional fields (only present in certain alert types)
LIMIT_ALERT_OPTIONAL_FIELDS = {
    "subscription_end_date": str,
    "grace_end_date": str,
    "deletion_date": str,
}

# Valid alert types that frontend can handle
VALID_ALERT_TYPES = {
    AlertType.STORAGE.value,
    AlertType.GRACE.value,
    AlertType.OVERDUE.value,
}


def validate_limit_alert_contract(result: dict) -> None:
    """
    Validate that a LimitAlert response matches the frontend contract.
    Raises AssertionError with details if contract is violated.
    """
    # Check all required fields are present with correct types
    for field, expected_type in LIMIT_ALERT_REQUIRED_FIELDS.items():
        assert field in result, (
            f"Contract violation: Missing required field '{field}'. "
            f"Frontend expects: {list(LIMIT_ALERT_REQUIRED_FIELDS.keys())}"
        )
        assert isinstance(result[field], expected_type), (
            f"Contract violation: Field '{field}' has wrong type. "
            f"Expected {expected_type}, got {type(result[field])}"
        )

    # Validate alert_type is one of the expected values
    assert result["alert_type"] in VALID_ALERT_TYPES, (
        f"Contract violation: Invalid alert_type '{result['alert_type']}'. "
        f"Frontend expects one of: {VALID_ALERT_TYPES}"
    )

    # Check optional fields have correct types if present
    for field, expected_type in LIMIT_ALERT_OPTIONAL_FIELDS.items():
        if field in result and result[field] is not None:
            assert isinstance(result[field], expected_type), (
                f"Contract violation: Optional field '{field}' has wrong type. "
                f"Expected {expected_type}, got {type(result[field])}"
            )

    # Verify no legacy field names are present (common migration issues)
    legacy_fields = ["has_warning", "warning_type"]
    for legacy_field in legacy_fields:
        assert legacy_field not in result, (
            f"Contract violation: Legacy field '{legacy_field}' found. "
            f"Frontend expects 'has_alert'/'alert_type' instead."
        )


@pytest.mark.asyncio
async def test_contract_storage_alert_response_format():
    """
    Contract test: Verify storage alert response matches frontend LimitAlert interface.

    This test calls the real calculate_limit_warning function and validates
    the response structure matches what the frontend TypeScript code expects.
    """
    from unittest.mock import Mock

    from studio.app.common.core.cloud.cloud_utils import calculate_limit_warning

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

                # Setup: Free user with storage exceeded (triggers storage alert)
                mock_get_storage.return_value = {
                    "storage_usage_bytes": 6_000_000_000,  # 6GB
                    "storage_quota_bytes": 5_000_000_000,  # 5GB limit
                }
                mock_fresh.return_value = True
                mock_db.execute.return_value.all.return_value = []  # No subscription

                result = await calculate_limit_warning(user_id)

                # Validate contract
                assert result is not None, "Expected storage alert for user over quota"
                # Convert Pydantic model to dict for contract validation
                result_dict = result.dict()
                validate_limit_alert_contract(result_dict)

                # Additional semantic validation for storage alerts
                assert result.alert_type == AlertType.STORAGE.value
                assert result.has_alert is True


@pytest.mark.asyncio
async def test_contract_grace_period_alert_response_format():
    """
    Contract test: Verify grace period alert response matches frontend interface.
    """
    from datetime import timedelta
    from unittest.mock import Mock

    from studio.app.common.core.cloud.cloud_utils import calculate_limit_warning
    from studio.app.common.core.utils.datetime_utils import get_current_datetime

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

                # Setup: Premium user in grace period with storage over free limit
                mock_get_storage.return_value = {
                    "storage_usage_bytes": 10_000_000_000,  # 10GB (over free 5GB)
                    "storage_quota_bytes": 200_000_000_000,  # 200GB premium quota
                }
                mock_fresh.return_value = True

                # Subscription expired 5 days ago (in grace period)
                mock_subscription = Mock()
                mock_subscription.expiration = get_current_datetime() - timedelta(
                    days=5
                )
                mock_db.execute.return_value.all.return_value = [[mock_subscription]]

                result = await calculate_limit_warning(user_id)

                # Validate contract
                assert result is not None, "Expected grace period alert"
                # Convert Pydantic model to dict for contract validation
                result_dict = result.dict()
                validate_limit_alert_contract(result_dict)

                # Additional semantic validation for grace alerts
                assert result.alert_type == AlertType.GRACE.value
                assert result.has_alert is True
                # Grace alerts should include subscription dates
                assert result.subscription_end_date is not None
                assert result.grace_end_date is not None


@pytest.mark.asyncio
async def test_contract_overdue_alert_response_format():
    """
    Contract test: Verify overdue alert response matches frontend interface.
    """
    from datetime import timedelta
    from unittest.mock import Mock

    from studio.app.common.core.cloud.cloud_utils import calculate_limit_warning
    from studio.app.common.core.subscription.constants import SubscriptionPeriods
    from studio.app.common.core.utils.datetime_utils import get_current_datetime

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

                # Setup: User past grace and warning periods (overdue)
                mock_get_storage.return_value = {
                    "storage_usage_bytes": 10_000_000_000,  # 10GB
                    "storage_quota_bytes": 200_000_000_000,  # 200GB
                }
                mock_fresh.return_value = True

                # Subscription expired long ago (past grace + warning periods)
                days_expired = grace_period + warning_period + 5
                mock_subscription = Mock()
                mock_subscription.expiration = get_current_datetime() - timedelta(
                    days=days_expired
                )
                mock_db.execute.return_value.all.return_value = [[mock_subscription]]

                result = await calculate_limit_warning(user_id)

                # Validate contract
                assert result is not None, "Expected overdue alert"
                # Convert Pydantic model to dict for contract validation
                result_dict = result.dict()
                validate_limit_alert_contract(result_dict)

                # Additional semantic validation for overdue alerts
                assert result.alert_type == AlertType.OVERDUE.value
                assert result.has_alert is True
                assert result.days_remaining == 0


@pytest.mark.asyncio
async def test_contract_no_alert_returns_none():
    """
    Contract test: Verify no alert condition returns None (not empty dict).

    Frontend checks: if (alertResponse && alertResponse.has_alert)
    So we must return None, not an empty dict or dict with has_alert=False.
    """
    from unittest.mock import Mock

    from studio.app.common.core.cloud.cloud_utils import calculate_limit_warning

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

                # Setup: Free user within storage limits (no alert)
                mock_get_storage.return_value = {
                    "storage_usage_bytes": 2_000_000_000,  # 2GB
                    "storage_quota_bytes": 5_000_000_000,  # 5GB limit
                }
                mock_fresh.return_value = True
                mock_db.execute.return_value.all.return_value = []  # No subscription

                result = await calculate_limit_warning(user_id)

                # Must return None, not a dict
                assert result is None, (
                    "Contract violation: Expected None when no alert, "
                    f"but got {type(result)}: {result}"
                )


# ============================================================================
# Regression Tests - Bug Fixes
# ============================================================================


@pytest.mark.asyncio
async def test_regression_free_user_no_premium_expired_warning():
    """
    REGRESSION TEST: Free user (never had premium) should NOT see
    "Premium Subscription Expired" warning.

    Bug: The subscription query wasn't filtering by plan_id, so users with
    FREE plan subscription records (plan_id=1) were incorrectly shown
    premium expired warnings.

    This test verifies the fix by ensuring the warning calculation correctly
    identifies users as FREE when they only have FREE plan subscriptions.
    """
    from unittest.mock import Mock

    from studio.app.common.core.cloud.cloud_utils import calculate_limit_warning

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

                # Setup: User within storage limits
                mock_get_storage.return_value = {
                    "storage_usage_bytes": 2_000_000_000,  # 2GB
                    "storage_quota_bytes": 5_000_000_000,  # 5GB limit
                }
                mock_fresh.return_value = True

                # The query now filters for PREMIUM plans only, so FREE plan
                # subscriptions won't be returned. Mock empty result.
                mock_db.execute.return_value.all.return_value = []

                result = await calculate_limit_warning(user_id)

                # Free user within limits should get NO warning
                assert result is None, (
                    "Bug regression: Free user should not see any warning when "
                    "within storage limits. Got premium expired warning instead."
                )


@pytest.mark.asyncio
async def test_regression_free_user_storage_exceeded_gets_correct_alert_type():
    """
    REGRESSION TEST: Free user who exceeds storage should get "storage" alert,
    NOT "grace" or "overdue" (which are for premium subscriptions).

    This ensures the alert_type is correct for free users.
    """
    from unittest.mock import Mock

    from studio.app.common.core.cloud.cloud_utils import calculate_limit_warning

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

                # Setup: User over storage limits
                mock_get_storage.return_value = {
                    "storage_usage_bytes": 6_000_000_000,  # 6GB
                    "storage_quota_bytes": 5_000_000_000,  # 5GB limit
                }
                mock_fresh.return_value = True

                # Free user (no premium subscriptions)
                mock_db.execute.return_value.all.return_value = []

                result = await calculate_limit_warning(user_id)

                assert result is not None, "Expected storage alert for user over quota"
                assert result.alert_type == AlertType.STORAGE.value, (
                    f"Bug regression: Free user should get 'storage' alert type, "
                    f"not '{result.alert_type}' (which is for premium users)"
                )
                # Message should NOT mention "premium" or "subscription expired"
                message_lower = result.message.lower()
                assert (
                    "premium" not in message_lower
                    or "upgrade to premium" in message_lower
                ), (
                    "Bug regression: Free user storage alert should not mention "
                    f"premium subscription expiration. Message: {result.message}"
                )


@pytest.mark.asyncio
async def test_contract_alert_message_is_user_friendly():
    """
    Contract test: Alert messages should be user-friendly and actionable.

    This test verifies that alert messages:
    1. Are not empty
    2. Don't contain technical jargon or internal field names
    3. Suggest an action the user can take
    """
    from datetime import timedelta
    from unittest.mock import Mock

    from studio.app.common.core.cloud.cloud_utils import calculate_limit_warning
    from studio.app.common.core.utils.datetime_utils import get_current_datetime

    user_id = 1

    # Test storage alert message
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
                    "storage_usage_bytes": 6_000_000_000,
                    "storage_quota_bytes": 5_000_000_000,
                }
                mock_fresh.return_value = True
                mock_db.execute.return_value.all.return_value = []

                result = await calculate_limit_warning(user_id)

                assert result is not None
                assert len(result.message) > 10, "Message should be substantial"
                # Should not contain internal field names
                internal_terms = ["storage_usage_bytes", "plan_id", "user_id", "None"]
                for term in internal_terms:
                    assert (
                        term not in result.message
                    ), f"Message contains internal term '{term}': {result.message}"

    # Test grace period alert message (requires storage over free limit)
    with patch("studio.app.common.core.cloud.cloud_utils.session_scope") as mock_scope:
        with patch(
            "studio.app.common.core.cloud.cloud_utils.get_user_storage_usage"
        ) as mock_get_storage:
            with patch(
                "studio.app.common.core.cloud.cloud_utils._is_storage_data_fresh"
            ) as mock_fresh:
                mock_db = Mock()
                mock_scope.return_value.__enter__.return_value = mock_db

                # Storage over free tier limit to trigger grace period alert
                mock_get_storage.return_value = {
                    "storage_usage_bytes": 8_000_000_000,  # 8GB over 5GB limit
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
                assert len(result.message) > 10, "Message should be substantial"
                # Grace message should mention upgrade or action
                message_lower = result.message.lower()
                assert any(
                    word in message_lower
                    for word in ["upgrade", "renew", "remove", "delete"]
                ), f"Grace message should suggest an action: {result.message}"
