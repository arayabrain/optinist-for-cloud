from typing import Any, Dict, Optional

from pydantic import BaseModel

# Response models for the premium instance-assignment / routing endpoints
# (studio/app/common/routers/users_me.py).
# Branch-divergent fields are Optional so every return path
# (success / retry / error) stays valid.


class PremiumAssignmentDetail(BaseModel):
    """Whitelisted `/premium/status` assignment body (mirrors the frontend
    `PremiumAssignment`). Typed so undeclared keys from the passthrough Lambda
    payload are dropped on serialization. All Optional: any branch stays valid.
    """

    instance_id: Optional[str] = None
    instance_id_hash: Optional[str] = None
    assigned_at: Optional[str] = None
    status: Optional[str] = None
    is_shared: Optional[bool] = None
    assignment_source: Optional[str] = None


class RoutingInfoResponse(BaseModel):
    user_tier: str
    requires_premium_routing: bool
    routing_headers: Dict[str, str]


class PremiumAssignResponse(BaseModel):
    message: str
    assigned: bool
    instance_id: Optional[str] = None
    instance_id_hash: Optional[str] = None
    is_shared: Optional[bool] = None
    assignment_source: Optional[str] = None
    retry_after: Optional[int] = None
    scaling_in_progress: Optional[bool] = None


class PremiumReleaseResponse(BaseModel):
    message: str
    released: bool
    released_instance: Optional[str] = None


class PremiumStatusResponse(BaseModel):
    subscription_type: str
    is_premium: bool
    assignment: Optional[PremiumAssignmentDetail] = None
    migration_ready: Optional[bool] = None
    health_status: Optional[str] = None
    error: Optional[str] = None


class PremiumHeartbeatResponse(BaseModel):
    message: str
    updated: bool
    user_tier: str
    assignment_active: bool
    activity_update: Optional[Any] = None
    heartbeat_failures: Optional[int] = None
    error: Optional[str] = None


class FreeLogoutResponse(BaseModel):
    message: str
    logged_out: bool
    user_tier: Optional[str] = None
    cleanup_after_minutes: Optional[int] = None
    error: Optional[str] = None
