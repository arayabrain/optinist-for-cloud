"""
Pydantic schemas for storage-related API responses.

These schemas define the contract between backend and frontend for:
- Storage usage information
- Limit warnings (subscription expiration, storage exceeded)
"""

from typing import Optional

from pydantic import BaseModel, Field


class LimitWarning(BaseModel):
    """
    Limit warning response for subscription/storage alerts.

    This schema is used by:
    - GET /storage/limit-warning
    - GET /storage/limit-warning/check
    - Login response (embedded in user context)

    Frontend expects these exact field names - see:
    frontend/src/api/storage/StorageAlerts.ts
    """

    has_alert: bool = Field(..., description="Whether an alert is active")
    alert_type: str = Field(
        ...,
        description="Type of alert: 'storage', 'grace', or 'overdue'",
        example="grace",
    )
    days_remaining: int = Field(
        ...,
        ge=0,
        description="Days remaining before action required",
    )
    excess_data_bytes: int = Field(
        ...,
        ge=0,
        description="Bytes over quota limit",
    )
    excess_data_gb: float = Field(
        ...,
        ge=0,
        description="GB over quota limit (rounded to 2 decimal places)",
    )
    storage_usage_bytes: int = Field(
        ...,
        ge=0,
        description="Current storage usage in bytes",
    )
    storage_usage_gb: float = Field(
        ...,
        ge=0,
        description="Current storage usage in GB",
    )
    storage_quota_bytes: int = Field(
        ...,
        ge=0,
        description="Storage quota limit in bytes",
    )
    storage_quota_gb: float = Field(
        ...,
        ge=0,
        description="Storage quota limit in GB",
    )
    message: str = Field(
        ...,
        description="Human-readable warning message",
    )

    # Optional fields for subscription-related warnings
    subscription_end_date: Optional[str] = Field(
        None,
        description="ISO date when subscription ended",
    )
    grace_end_date: Optional[str] = Field(
        None,
        description="ISO date when grace period ends",
    )
    deletion_date: Optional[str] = Field(
        None,
        description="ISO date when data deletion is scheduled",
    )

    class Config:
        use_enum_values = True
        schema_extra = {
            "example": {
                "has_alert": True,
                "alert_type": "grace",
                "days_remaining": 15,
                "excess_data_bytes": 2147483648,
                "excess_data_gb": 2.0,
                "storage_usage_bytes": 7516192768,
                "storage_usage_gb": 7.0,
                "storage_quota_bytes": 5368709120,
                "storage_quota_gb": 5.0,
                "message": "Your premium subscription expired. "
                "You have 15 days remaining.",
                "subscription_end_date": "2024-01-15T00:00:00+00:00",
                "grace_end_date": "2024-02-14T00:00:00+00:00",
                "deletion_date": "2024-03-15T00:00:00+00:00",
            }
        }


class StorageAlertDetail(BaseModel):
    """Nested alert object for GET /storage-limit-alerts/me.

    Typed so undeclared keys are dropped on serialization. user_name /
    user_email are the owner's own PII (frontend-unused; removal deferred).
    """

    alert_level: Optional[str] = None
    storage_usage_bytes: Optional[int] = None
    storage_quota_bytes: Optional[int] = None
    storage_usage_percent: Optional[float] = None
    timestamp: Optional[str] = None
    user_name: Optional[str] = None
    user_email: Optional[str] = None
    message: Optional[str] = None


class StorageAlertResponse(BaseModel):
    """Response for GET /storage-limit-alerts/me. Covers both branches:
    alert present (has_alert=True, alert set) and none (has_alert=False,
    storage_usage_bytes / _formatted set). Served with exclude_unset so only
    each branch's keys are emitted.
    """

    has_alert: bool
    alert: Optional[StorageAlertDetail] = None
    storage_usage_bytes: Optional[int] = None
    storage_usage_formatted: Optional[str] = None


class LimitWarningStatus(BaseModel):
    """
    Quick status check response for limit warnings.

    Used by: GET /storage/limit-warning/check
    """

    has_alert: bool = Field(..., description="Whether any alert is active")
    alert_type: Optional[str] = Field(
        None,
        description="Type of alert if present: 'storage', 'grace', or 'overdue'",
    )
    days_remaining: Optional[int] = Field(
        None,
        description="Days remaining if alert is present",
    )

    class Config:
        schema_extra = {
            "example": {
                "has_alert": True,
                "alert_type": "grace",
                "days_remaining": 15,
            }
        }
