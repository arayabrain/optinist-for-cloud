"""
Storage Limit Alerts API Router.
Provides endpoints for checking and managing S3 storage alerts.
"""
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from studio.app.common.core.auth.auth_dependencies import (
    get_current_user,
    get_user_remote_bucket_name,
)
from studio.app.common.core.cloud.s3_storage_monitor import (
    S3StorageMonitor,
    monitor_storage_and_generate_alerts,
)
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.utils.datetime_utils import get_current_datetime
from studio.app.common.db.database import get_db
from studio.app.common.schemas.storage import (
    LimitWarning,
    LimitWarningStatus,
    StorageAlertResponse,
)
from studio.app.common.schemas.users import User

router = APIRouter(prefix="/storage-limit-alerts", tags=["storage-limit-alerts"])
logger = AppLogger.get_logger()


def _get_storage_utilities():
    """
    Helper function to get storage monitoring utilities for formatting and thresholds.
    Returns S3StorageMonitor instance (used only for utility functions).
    """
    from studio.app.common.core.cloud.s3_storage_monitor import S3StorageMonitor

    return S3StorageMonitor("dummy")  # Bucket name not used for utilities


@router.get(
    "/me",
    response_model=StorageAlertResponse,
    response_model_exclude_unset=True,
)
async def get_my_storage_alert(
    current_user: User = Depends(get_current_user),
):
    """
    Get storage alert information for the current user.

    Returns:
        Dict containing user's storage usage and alert information
    """
    try:
        # Use the new unified storage calculation function
        from studio.app.common.core.cloud.storage_tracking import (
            get_current_user_storage_usage,
        )

        # Get current usage (uses caching with 60min freshness)
        current_usage = await get_current_user_storage_usage(current_user.id)

        # Get user's quota and calculate alert
        from studio.app.common.core.cloud.storage_tracking import get_user_storage_usage

        storage_info = get_user_storage_usage(current_user.id)

        alert = None
        if storage_info and storage_info["storage_quota_bytes"] > 0:
            storage_quota = storage_info["storage_quota_bytes"]
            storage_usage_percent = (current_usage / storage_quota) * 100

            # Use thresholds from S3StorageMonitor for consistency
            monitor = _get_storage_utilities()
            alert_level = monitor.calculate_storage_alert_level(storage_usage_percent)

            if alert_level:
                alert = {
                    "alert_level": alert_level,
                    "storage_usage_bytes": current_usage,
                    "storage_quota_bytes": storage_quota,
                    "storage_usage_percent": round(storage_usage_percent, 2),
                    "timestamp": get_current_datetime().isoformat(),
                }

        if alert:
            # Add user information and format message (reuse monitor from above)
            alert.update(
                {
                    "user_name": current_user.name,
                    "user_email": current_user.email,
                    "message": monitor.get_alert_message(alert),
                }
            )
            return {"has_alert": True, "alert": alert}
        else:
            # No alert, but still return current usage info
            monitor = _get_storage_utilities()
            usage_formatted = monitor.format_bytes(current_usage)

            return {
                "has_alert": False,
                "storage_usage_bytes": current_usage,
                "storage_usage_formatted": usage_formatted,
                "alert": None,
            }

    except Exception as e:
        logger.error(f"Failed to get storage alert for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve storage alert information",
        )


@router.get("/usage", response_model=Dict)
async def get_my_storage_usage(
    current_user: User = Depends(get_current_user),
):
    """
    Get detailed storage usage information for the current user.

    Returns:
        Dict containing detailed storage usage statistics
    """
    try:
        # Use the new unified storage calculation function
        from studio.app.common.core.cloud.storage_tracking import (
            get_current_user_storage_usage,
            get_user_storage_usage,
        )

        # Get current usage (uses caching with 60min freshness)
        current_usage = await get_current_user_storage_usage(current_user.id)

        # Get quota information from database
        storage_info = get_user_storage_usage(current_user.id)

        # Use storage utilities for formatting and thresholds
        monitor = _get_storage_utilities()

        if not storage_info:
            return {
                "storage_usage_bytes": current_usage,
                "storage_usage_formatted": monitor.format_bytes(current_usage),
                "storage_quota_bytes": None,
                "storage_quota_formatted": None,
                "storage_usage_percent": None,
                "alert_level": None,
                "thresholds": {
                    "critical": monitor.CRITICAL_THRESHOLD,
                    "danger": monitor.DANGER_THRESHOLD,
                },
            }

        storage_quota = storage_info["storage_quota_bytes"]
        storage_usage_percent = (
            (current_usage / storage_quota * 100) if storage_quota > 0 else 0
        )
        alert_level = monitor.calculate_storage_alert_level(storage_usage_percent)

        return {
            "storage_usage_bytes": current_usage,
            "storage_usage_formatted": monitor.format_bytes(current_usage),
            "storage_quota_bytes": storage_quota,
            "storage_quota_formatted": monitor.format_bytes(storage_quota),
            "storage_usage_percent": round(storage_usage_percent, 2),
            "alert_level": alert_level,
            "thresholds": {
                "critical": monitor.CRITICAL_THRESHOLD,
                "danger": monitor.DANGER_THRESHOLD,
            },
        }

    except Exception as e:
        logger.error(f"Failed to get storage usage for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve storage usage information",
        )


@router.get("/all", response_model=List[Dict])
async def get_all_storage_alerts(
    current_user: User = Depends(get_current_user),
    remote_bucket_name: str = Depends(get_user_remote_bucket_name),
    db: Session = Depends(get_db),
):
    """
    Get storage alerts for all users (admin only).

    Returns:
        List of storage alert dictionaries
    """
    try:
        # Check if user is admin
        if not hasattr(current_user, "is_admin") or not current_user.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin access required",
            )

        if not remote_bucket_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No S3 bucket configured",
            )

        alerts = await monitor_storage_and_generate_alerts(remote_bucket_name)

        # Add formatted messages to alerts
        if alerts:
            monitor = S3StorageMonitor(remote_bucket_name)
            for alert in alerts:
                alert["message"] = monitor.get_alert_message(alert)

        return alerts

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get all storage alerts: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve storage alerts",
        )


@router.post("/refresh", response_model=Dict)
async def refresh_storage_usage(
    current_user: User = Depends(get_current_user),
    remote_bucket_name: str = Depends(get_user_remote_bucket_name),
):
    """
    Refresh storage usage calculation for the current user.

    Returns:
        Dict with updated storage information
    """
    try:
        if not remote_bucket_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No S3 bucket configured for user",
            )

        monitor = S3StorageMonitor(remote_bucket_name)

        # Recalculate current usage
        current_usage = await monitor.get_user_s3_storage_size(current_user.id)

        # Update database
        from studio.app.common.core.cloud.storage_tracking import (
            update_user_storage_usage,
        )

        success = update_user_storage_usage(current_user.id, current_usage)

        if not success:
            logger.warning(
                f"Failed to update storage usage in database for user {current_user.id}"
            )

        return {
            "success": True,
            "updated_usage_bytes": current_usage,
            "updated_usage_formatted": monitor.format_bytes(current_usage),
            "database_updated": success,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to refresh storage usage for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to refresh storage usage",
        )


@router.get("/limit-warning", response_model=Optional[LimitWarning])
async def get_my_limit_warning(
    current_user: User = Depends(get_current_user),
):
    """
    Get limit warning details for the current user.

    Returns warning information if the user has exceeded free plan limits
    after subscription expiration, None otherwise.
    """
    try:
        from studio.app.common.core.cloud.cloud_utils import calculate_limit_warning

        logger.info(f"Checking limit warning for user {current_user.id}")
        warning = await calculate_limit_warning(current_user.id)

        if warning:
            logger.info(
                f"Limit warning for user {current_user.id}: "
                f"{warning.alert_type}-{warning.days_remaining} days remaining"
            )
        else:
            logger.debug(f"No limit warning for user {current_user.id}")

        return warning

    except Exception as e:
        logger.error(f"Failed to get limit warning for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve limit warning",
        )


@router.get("/limit-warning/check", response_model=LimitWarningStatus)
async def check_limit_warning_status(
    current_user: User = Depends(get_current_user),
):
    """
    Quick check if user has any limit warnings.

    Returns a simple status indicating if warnings exist.
    """
    try:
        from studio.app.common.core.cloud.cloud_utils import calculate_limit_warning

        warning = await calculate_limit_warning(current_user.id)

        return LimitWarningStatus(
            has_alert=warning is not None,
            alert_type=warning.alert_type if warning else None,
            days_remaining=warning.days_remaining if warning else None,
        )

    except Exception as e:
        logger.error(
            f"Failed to check limit warning status for user " f"{current_user.id}: {e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to check limit warning status",
        )
