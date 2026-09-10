"""
Contract Tests for Storage Alerts API

These tests verify that API responses match the frontend TypeScript interfaces.
This ensures the backend and frontend stay in sync and prevents contract mismatches.

Frontend interfaces are defined in:
  frontend/src/api/storage/StorageAlerts.ts

Tested endpoints:
  - GET  /storage-limit-alerts/me      -> StorageAlertResponse
  - GET  /storage-limit-alerts/usage   -> StorageUsage
  - POST /storage-limit-alerts/refresh -> RefreshStorageResponse
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

# ============================================================================
# Frontend Contract Definitions
# ============================================================================
# These mirror the TypeScript interfaces in StorageAlerts.ts

# StorageAlert interface (nested in StorageAlertResponse)
STORAGE_ALERT_REQUIRED_FIELDS = {
    "user_name": str,
    "user_email": str,
    "alert_level": str,  # "critical" | "danger"
    "storage_usage_bytes": int,
    "storage_quota_bytes": int,
    "storage_usage_percent": (int, float),
    "timestamp": str,
    "message": str,
}

STORAGE_ALERT_OPTIONAL_FIELDS = {
    "subscription_plan": str,
}

# Valid alert levels
VALID_ALERT_LEVELS = {"critical", "danger"}

# StorageUsage interface
STORAGE_USAGE_REQUIRED_FIELDS = {
    "storage_usage_bytes": int,
    "storage_usage_formatted": str,
    "thresholds": dict,
}

STORAGE_USAGE_NULLABLE_FIELDS = {
    "storage_quota_bytes": (int, type(None)),
    "storage_quota_formatted": (str, type(None)),
    "storage_usage_percent": (int, float, type(None)),
    "alert_level": (str, type(None)),
}

# Thresholds nested structure
THRESHOLDS_REQUIRED_FIELDS = {
    "critical": (int, float),
    "danger": (int, float),
}

# StorageAlertResponse interface
STORAGE_ALERT_RESPONSE_REQUIRED_FIELDS = {
    "has_alert": bool,
}

STORAGE_ALERT_RESPONSE_OPTIONAL_FIELDS = {
    "storage_usage_bytes": int,
    "storage_usage_formatted": str,
    "alert": (dict, type(None)),
}

# RefreshStorageResponse interface
REFRESH_STORAGE_RESPONSE_REQUIRED_FIELDS = {
    "success": bool,
    "updated_usage_bytes": int,
    "updated_usage_formatted": str,
    "database_updated": bool,
}


# ============================================================================
# Contract Validation Helpers
# ============================================================================


def validate_contract(
    result: dict,
    required_fields: dict,
    optional_fields: dict = None,
    nullable_fields: dict = None,
    context: str = "",
) -> None:
    """
    Validate that a response matches the frontend contract.
    """
    # Check required fields
    for field, expected_type in required_fields.items():
        assert field in result, (
            f"Contract violation ({context}): Missing required field '{field}'. "
            f"Response has: {list(result.keys())}"
        )
        if isinstance(expected_type, tuple):
            assert isinstance(result[field], expected_type), (
                f"Contract violation ({context}): Field '{field}' has wrong type. "
                f"Expected one of {expected_type}, got {type(result[field])}"
            )
        else:
            assert isinstance(result[field], expected_type), (
                f"Contract violation ({context}): Field '{field}' has wrong type. "
                f"Expected {expected_type}, got {type(result[field])}"
            )

    # Check optional fields (if present, must have correct type)
    if optional_fields:
        for field, expected_type in optional_fields.items():
            if field in result and result[field] is not None:
                if isinstance(expected_type, tuple):
                    assert isinstance(result[field], expected_type), (
                        f"Contract violation ({context}): "
                        f"Optional field '{field}' has wrong type."
                    )
                else:
                    assert isinstance(result[field], expected_type), (
                        f"Contract violation ({context}): "
                        f"Optional field '{field}' has wrong type."
                    )

    # Check nullable fields (must be present, can be null)
    if nullable_fields:
        for field, expected_type in nullable_fields.items():
            assert (
                field in result
            ), f"Contract violation ({context}): Missing nullable field '{field}'."
            if isinstance(expected_type, tuple):
                assert isinstance(result[field], expected_type), (
                    f"Contract violation ({context}): "
                    f"Nullable field '{field}' has wrong type."
                )


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_current_user():
    """Mock current user"""
    user = Mock()
    user.id = 1
    user.uid = "test-user-123"
    user.name = "Test User"
    user.email = "test@example.com"
    user.is_admin = False
    return user


@pytest.fixture
def mock_storage_monitor():
    """Mock S3StorageMonitor"""
    from studio.app.common.core.subscription.constants import StorageQuota

    monitor = Mock()
    monitor.CRITICAL_THRESHOLD = StorageQuota.CRITICAL_THRESHOLD_PERCENT
    monitor.DANGER_THRESHOLD = StorageQuota.DANGER_THRESHOLD_PERCENT
    monitor.calculate_storage_alert_level = Mock(return_value="danger")
    monitor.format_bytes = Mock(return_value="4.5 GB")
    monitor.get_alert_message = Mock(return_value="Storage at 90%")
    return monitor


# ============================================================================
# Contract Tests: GET /storage-limit-alerts/me
# ============================================================================


@pytest.mark.asyncio
async def test_contract_storage_alert_response_with_alert(
    mock_current_user, mock_storage_monitor
):
    """
    Contract test: StorageAlertResponse with active alert matches frontend interface.
    """
    with patch(
        "studio.app.common.core.cloud.storage_tracking.get_current_user_storage_usage"
    ) as mock_usage:
        with patch(
            "studio.app.common.core.cloud.storage_tracking.get_user_storage_usage"
        ) as mock_storage_info:
            with patch(
                "studio.app.common.routers.storage_limit_alerts._get_storage_utilities"
            ) as mock_utils:
                mock_usage.return_value = 4500000000  # 4.5 GB
                mock_storage_info.return_value = {
                    "storage_quota_bytes": 5000000000,  # 5 GB
                }
                mock_utils.return_value = mock_storage_monitor

                from studio.app.common.routers.storage_limit_alerts import (
                    get_my_storage_alert,
                )

                result = await get_my_storage_alert(current_user=mock_current_user)

                validate_contract(
                    result,
                    STORAGE_ALERT_RESPONSE_REQUIRED_FIELDS,
                    STORAGE_ALERT_RESPONSE_OPTIONAL_FIELDS,
                    context="StorageAlertResponse (with alert)",
                )

                # When has_alert is True, alert should be present
                if result["has_alert"]:
                    assert result["alert"] is not None
                    validate_contract(
                        result["alert"],
                        STORAGE_ALERT_REQUIRED_FIELDS,
                        STORAGE_ALERT_OPTIONAL_FIELDS,
                        context="StorageAlert (nested)",
                    )
                    # Validate alert_level is valid enum
                    assert result["alert"]["alert_level"] in VALID_ALERT_LEVELS
                    # The internal user id must not be echoed back to the client.
                    assert "user_id" not in result["alert"]


@pytest.mark.asyncio
async def test_contract_storage_alert_response_no_alert(
    mock_current_user, mock_storage_monitor
):
    """
    Contract test: StorageAlertResponse without alert matches frontend interface.
    """
    mock_storage_monitor.calculate_storage_alert_level.return_value = None

    with patch(
        "studio.app.common.core.cloud.storage_tracking.get_current_user_storage_usage"
    ) as mock_usage:
        with patch(
            "studio.app.common.core.cloud.storage_tracking.get_user_storage_usage"
        ) as mock_storage_info:
            with patch(
                "studio.app.common.routers.storage_limit_alerts._get_storage_utilities"
            ) as mock_utils:
                mock_usage.return_value = 1000000000  # 1 GB (under quota)
                mock_storage_info.return_value = {
                    "storage_quota_bytes": 5000000000,  # 5 GB
                }
                mock_utils.return_value = mock_storage_monitor

                from studio.app.common.routers.storage_limit_alerts import (
                    get_my_storage_alert,
                )

                result = await get_my_storage_alert(current_user=mock_current_user)

                validate_contract(
                    result,
                    STORAGE_ALERT_RESPONSE_REQUIRED_FIELDS,
                    STORAGE_ALERT_RESPONSE_OPTIONAL_FIELDS,
                    context="StorageAlertResponse (no alert)",
                )

                # When has_alert is False, alert should be None
                assert result["has_alert"] is False
                assert result.get("alert") is None


# ============================================================================
# Contract Tests: GET /storage-limit-alerts/usage
# ============================================================================


@pytest.mark.asyncio
async def test_contract_storage_usage_response(mock_current_user, mock_storage_monitor):
    """
    Contract test: StorageUsage response matches frontend interface.
    """
    with patch(
        "studio.app.common.core.cloud.storage_tracking.get_current_user_storage_usage"
    ) as mock_usage:
        with patch(
            "studio.app.common.core.cloud.storage_tracking.get_user_storage_usage"
        ) as mock_storage_info:
            with patch(
                "studio.app.common.routers.storage_limit_alerts._get_storage_utilities"
            ) as mock_utils:
                mock_usage.return_value = 4500000000
                mock_storage_info.return_value = {
                    "storage_quota_bytes": 5000000000,
                }
                mock_utils.return_value = mock_storage_monitor

                from studio.app.common.routers.storage_limit_alerts import (
                    get_my_storage_usage,
                )

                result = await get_my_storage_usage(current_user=mock_current_user)

                validate_contract(
                    result,
                    STORAGE_USAGE_REQUIRED_FIELDS,
                    nullable_fields=STORAGE_USAGE_NULLABLE_FIELDS,
                    context="StorageUsage",
                )

                # Validate thresholds structure
                assert "thresholds" in result
                validate_contract(
                    result["thresholds"],
                    THRESHOLDS_REQUIRED_FIELDS,
                    context="StorageUsage.thresholds",
                )

                # Validate alert_level if present
                if result.get("alert_level") is not None:
                    assert result["alert_level"] in VALID_ALERT_LEVELS


@pytest.mark.asyncio
async def test_contract_storage_usage_no_quota(mock_current_user, mock_storage_monitor):
    """
    Contract test: StorageUsage with no quota info matches frontend interface.
    """
    with patch(
        "studio.app.common.core.cloud.storage_tracking.get_current_user_storage_usage"
    ) as mock_usage:
        with patch(
            "studio.app.common.core.cloud.storage_tracking.get_user_storage_usage"
        ) as mock_storage_info:
            with patch(
                "studio.app.common.routers.storage_limit_alerts._get_storage_utilities"
            ) as mock_utils:
                mock_usage.return_value = 1000000000
                mock_storage_info.return_value = None  # No storage info
                mock_utils.return_value = mock_storage_monitor

                from studio.app.common.routers.storage_limit_alerts import (
                    get_my_storage_usage,
                )

                result = await get_my_storage_usage(current_user=mock_current_user)

                validate_contract(
                    result,
                    STORAGE_USAGE_REQUIRED_FIELDS,
                    nullable_fields=STORAGE_USAGE_NULLABLE_FIELDS,
                    context="StorageUsage (no quota)",
                )

                # Nullable fields should be null when no quota info
                assert result["storage_quota_bytes"] is None
                assert result["storage_quota_formatted"] is None
                assert result["storage_usage_percent"] is None


# ============================================================================
# Contract Tests: POST /storage-limit-alerts/refresh
# ============================================================================


@pytest.mark.asyncio
async def test_contract_refresh_storage_response(mock_current_user):
    """
    Contract test: RefreshStorageResponse matches frontend interface.
    """
    with patch(
        "studio.app.common.routers.storage_limit_alerts.S3StorageMonitor"
    ) as MockMonitor:
        with patch(
            "studio.app.common.core.cloud.storage_tracking.update_user_storage_usage"
        ) as mock_update:
            with patch(
                "studio.app.common.routers.storage_limit_alerts"
                ".get_user_remote_bucket_name"
            ) as mock_bucket:
                mock_monitor_instance = Mock()
                mock_monitor_instance.get_user_s3_storage_size = AsyncMock(
                    return_value=5000000000
                )
                mock_monitor_instance.format_bytes = Mock(return_value="5.0 GB")
                MockMonitor.return_value = mock_monitor_instance

                mock_update.return_value = True
                mock_bucket.return_value = "test-bucket"

                from studio.app.common.routers.storage_limit_alerts import (
                    refresh_storage_usage,
                )

                result = await refresh_storage_usage(
                    current_user=mock_current_user,
                    remote_bucket_name="test-bucket",
                )

                validate_contract(
                    result,
                    REFRESH_STORAGE_RESPONSE_REQUIRED_FIELDS,
                    context="RefreshStorageResponse",
                )

                # Semantic validation
                assert result["success"] is True
                assert result["updated_usage_bytes"] > 0
                assert isinstance(result["updated_usage_formatted"], str)


# ============================================================================
# Contract Tests: Field Naming Consistency
# ============================================================================


@pytest.mark.asyncio
async def test_contract_no_legacy_alert_fields(mock_current_user, mock_storage_monitor):
    """
    Ensure no legacy field names are used in storage alert responses.
    """
    with patch(
        "studio.app.common.core.cloud.storage_tracking.get_current_user_storage_usage"
    ) as mock_usage:
        with patch(
            "studio.app.common.core.cloud.storage_tracking.get_user_storage_usage"
        ) as mock_storage_info:
            with patch(
                "studio.app.common.routers.storage_limit_alerts._get_storage_utilities"
            ) as mock_utils:
                mock_usage.return_value = 4500000000
                mock_storage_info.return_value = {"storage_quota_bytes": 5000000000}
                mock_utils.return_value = mock_storage_monitor

                from studio.app.common.routers.storage_limit_alerts import (
                    get_my_storage_alert,
                )

                result = await get_my_storage_alert(current_user=mock_current_user)

                # Check for legacy field names
                legacy_fields = [
                    "hasAlert",  # camelCase
                    "has_warning",  # Wrong name
                    "storageUsageBytes",  # camelCase
                    "storageUsageFormatted",  # camelCase
                ]

                for legacy in legacy_fields:
                    assert legacy not in result, (
                        f"Legacy field '{legacy}' found. "
                        f"Frontend expects snake_case field names."
                    )


@pytest.mark.asyncio
async def test_contract_thresholds_field_names(mock_current_user, mock_storage_monitor):
    """
    Contract test: Thresholds object uses correct field names.
    """
    with patch(
        "studio.app.common.core.cloud.storage_tracking.get_current_user_storage_usage"
    ) as mock_usage:
        with patch(
            "studio.app.common.core.cloud.storage_tracking.get_user_storage_usage"
        ) as mock_storage_info:
            with patch(
                "studio.app.common.routers.storage_limit_alerts._get_storage_utilities"
            ) as mock_utils:
                mock_usage.return_value = 1000000000
                mock_storage_info.return_value = {"storage_quota_bytes": 5000000000}
                mock_utils.return_value = mock_storage_monitor

                from studio.app.common.routers.storage_limit_alerts import (
                    get_my_storage_usage,
                )

                result = await get_my_storage_usage(current_user=mock_current_user)

                thresholds = result["thresholds"]

                # Frontend expects these exact field names
                assert "critical" in thresholds, "Frontend expects 'critical' threshold"
                assert "danger" in thresholds, "Frontend expects 'danger' threshold"

                # No camelCase variants
                assert "criticalThreshold" not in thresholds
                assert "dangerThreshold" not in thresholds


# ============================================================================
# Contract Tests: Alert Level Enum
# ============================================================================


def test_contract_alert_level_values():
    """
    Contract test: Alert level values match frontend expectations.

    Frontend TypeScript: alert_level: "critical" | "danger"
    """
    # These are the only valid values frontend expects
    valid_levels = {"critical", "danger"}

    # Verify our test constants match
    assert VALID_ALERT_LEVELS == valid_levels

    # Alert levels should be lowercase
    for level in valid_levels:
        assert level == level.lower(), f"Alert level '{level}' should be lowercase"
