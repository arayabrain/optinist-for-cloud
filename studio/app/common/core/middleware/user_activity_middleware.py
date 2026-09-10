"""
User Activity Tracking Middleware

This middleware tracks activity for both free and premium tier users to enable:

For Free Users:
1. Intelligent load balancing and autoscaling
2. Identify idle users (no activity for 10+ minutes)
3. Safely migrate idle users to newly launched instances
4. Count active users to trigger autoscaling

For Premium Users:
1. Prevent stale assignment cleanup for active users
2. Enable proper scale-down of premium instance pool
3. Track actual usage for billing/analytics

IMPORTANT: Database updates run in background tasks to avoid blocking requests.

Note: This uses pure ASGI middleware instead of BaseHTTPMiddleware to avoid
known performance issues and connection handling problems with BaseHTTPMiddleware.
See: https://github.com/encode/starlette/issues/1012
"""

import asyncio
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from studio.app.common.core.auth.auth_helper import extract_uid_from_firebase_jwt
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.middleware.constants import (
    SKIP_ACTIVITY_PATHS,
    SKIP_AUTH_PATHS,
)
from studio.app.common.core.mode import MODE
from studio.app.common.core.subscription.constants import SubscriptionPlanIds
from studio.app.common.core.utils.datetime_utils import get_current_datetime
from studio.app.common.core.utils.instance_utils import resolve_instance_id
from studio.app.common.db.database import session_scope
from studio.app.common.models import FreeUserAssignment
from studio.app.common.models.instance_usage import InstanceUsageLog

# Constants
BEARER_PREFIX = "Bearer "
BEARER_PREFIX_LENGTH = len(BEARER_PREFIX)
TIER_FREE = "free"
TIER_PREMIUM = "premium"

# In-memory cache to reduce database load (tracks last update time per user)
# Separate caches for free and premium to avoid key collisions
_free_activity_cache = {}
_premium_activity_cache = {}
_cache_lock = threading.Lock()
_CACHE_TTL_SECONDS = 60  # Only update DB once per minute per user

# Track recently logged out users to prevent orphaned background activity updates
# Key: user_id, Value: logout timestamp
_logged_out_users = {}
_logged_out_lock = threading.Lock()
_LOGGED_OUT_TTL_SECONDS = 10

# Cache instance ID (fetch once at startup, reuse for all requests)
_instance_id_cache = None
_instance_id_lock = threading.Lock()

# User tier cache to avoid repeated subscription lookups
# Key: uid (Firebase UID), Value: (user_id, tier, timestamp)
_user_tier_cache = {}
_user_tier_cache_lock = threading.Lock()
_USER_TIER_CACHE_TTL_SECONDS = 300  # Cache tier for 5 minutes

logger = AppLogger.get_logger()


class UserActivityMiddleware:
    """
    ASGI Middleware to track user activity for both free and premium tiers.

    This middleware:
    - Extracts user ID from authentication tokens
    - Determines user tier (free or premium)
    - Updates last_activity timestamp in appropriate table
    - Runs asynchronously without blocking request processing
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Skip non-HTTP requests (e.g., websocket, lifespan)
            await self.app(scope, receive, send)
            return

        # Skip in standalone mode
        if MODE.IS_STANDALONE:
            await self.app(scope, receive, send)
            return

        # Get request path
        path = scope.get("path", "")

        # Skip health check, auth endpoints, and system-internal API endpoints
        if path in SKIP_AUTH_PATHS or path.startswith("/system-internal/"):
            await self.app(scope, receive, send)
            return

        # Skip activity tracking for automated endpoints (auth still applies)
        if path in SKIP_ACTIVITY_PATHS:
            await self.app(scope, receive, send)
            return

        # Extract Firebase JWT from Authorization header
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode()

        if not auth_header.startswith(BEARER_PREFIX):
            # No auth header, skip tracking
            await self.app(scope, receive, send)
            return

        firebase_token = auth_header[BEARER_PREFIX_LENGTH:]  # Remove "Bearer "

        # Extract UID from JWT
        uid, error = extract_uid_from_firebase_jwt(firebase_token)
        if error or not uid:
            # Invalid token, skip tracking
            await self.app(scope, receive, send)
            return

        # Flag to track when response body starts (so we only track once)
        has_tracked = False

        async def send_wrapper(message: Message) -> None:
            """
            Wrap the send function to intercept response transmission.
            When the response body starts being sent, we schedule the activity tracking.
            """
            nonlocal has_tracked

            # When response starts, schedule activity tracking
            if message["type"] == "http.response.start" and not has_tracked:
                has_tracked = True

                try:
                    # Get user ID and tier from database
                    user_id, tier = _get_user_id_and_tier(uid)

                    if user_id:
                        if tier == TIER_FREE:
                            # Check cache to avoid excessive DB writes
                            if _should_update_activity(user_id, TIER_FREE):
                                # Schedule background update (doesn't block response)
                                asyncio.create_task(
                                    _update_free_user_activity_async(user_id)
                                )
                        elif tier == TIER_PREMIUM:
                            # Check cache to avoid excessive DB writes
                            if _should_update_activity(user_id, TIER_PREMIUM):
                                # Schedule background update (doesn't block response)
                                asyncio.create_task(
                                    _update_premium_user_activity_async(user_id)
                                )

                except Exception as e:
                    # Log error but don't fail the request
                    logger.warning(f"Failed to track user activity: {e}")

            # Always send the original message
            await send(message)

        # Process the request with our wrapped send function
        await self.app(scope, receive, send_wrapper)


# Keep FreeUserActivityMiddleware as alias for backwards compatibility
FreeUserActivityMiddleware = UserActivityMiddleware


def _get_user_id_and_tier(uid: str) -> Tuple[Optional[int], Optional[str]]:
    """
    Get user ID and subscription tier from UID with caching.

    Uses a 5-minute TTL cache to avoid repeated database lookups for the same user.
    This reduces database load by ~90% for active users making multiple requests.

    Args:
        uid: Firebase user ID

    Returns:
        Tuple of (user_id, tier) where tier is TIER_FREE or TIER_PREMIUM
        Returns (None, None) if user not found or error occurs
    """
    import time

    # Check cache first
    with _user_tier_cache_lock:
        cached = _user_tier_cache.get(uid)
        if cached:
            user_id, tier, cached_at = cached
            if time.time() - cached_at < _USER_TIER_CACHE_TTL_SECONDS:
                return user_id, tier
            # Cache expired, will refresh below

    # Cache miss or expired - query database
    try:
        from studio.app.common.core.subscription.subscription_service import (
            SubscriptionService,
        )
        from studio.app.common.db.database import get_db
        from studio.app.common.models.user import User

        db = next(get_db())
        try:
            user = db.query(User).filter(User.uid == uid).first()
            if not user:
                return None, None

            # Check if user has active subscription
            subscription_data = SubscriptionService.get_user_subscription(db, user.id)
            if subscription_data:
                _, plan = subscription_data  # Only need plan for tier check
                tier = (
                    TIER_PREMIUM
                    if plan.id == SubscriptionPlanIds.PREMIUM
                    else TIER_FREE
                )
            else:
                tier = TIER_FREE

            # Update cache
            with _user_tier_cache_lock:
                _user_tier_cache[uid] = (user.id, tier, time.time())

            return user.id, tier
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Failed to get user tier for {uid}: {e}")
        return None, None


def invalidate_user_tier_cache(uid: str) -> None:
    """
    Invalidate the tier cache for a specific user.

    Call this when a user's subscription status changes (upgrade, downgrade, etc.)
    to ensure the middleware uses fresh data.

    Args:
        uid: Firebase user ID to invalidate
    """
    with _user_tier_cache_lock:
        _user_tier_cache.pop(uid, None)


def invalidate_activity_cache(user_id: int) -> None:
    """
    Invalidate the activity cache for a specific user.

    Call this when a user logs out to prevent stale cache entries
    on rapid re-login.
    """
    with _cache_lock:
        _free_activity_cache.pop(user_id, None)
        _premium_activity_cache.pop(user_id, None)


def mark_user_logged_out(user_id: int) -> None:
    """
    Mark a user as logged out to prevent orphaned background
    activity updates.
    """
    with _logged_out_lock:
        _logged_out_users[user_id] = time.time()


def is_user_logged_out(user_id: int) -> bool:
    """
    Check if a user has recently logged out.

    Used by background activity tasks to skip updates for logged
    out users. Entries auto-clean after TTL expires.
    """
    with _logged_out_lock:
        logout_time = _logged_out_users.get(user_id)
        if logout_time is None:
            return False
        if time.time() - logout_time > _LOGGED_OUT_TTL_SECONDS:
            del _logged_out_users[user_id]
            return False
        return True


def clear_logged_out_status(user_id: int) -> None:
    """Clear logged out status for a user on re-login."""
    with _logged_out_lock:
        _logged_out_users.pop(user_id, None)


def clear_free_user_logged_out_at(user_id: int) -> bool:
    """
    Clear logged_out_at timestamp for a free user on re-login.

    Prevents the cleanup job from deleting a user's data after
    they log back in.
    """
    try:
        from sqlalchemy import update

        with session_scope() as session:
            stmt = (
                update(FreeUserAssignment)
                .where(FreeUserAssignment.user_id == user_id)
                .where(FreeUserAssignment.logged_out_at.isnot(None))
                .values(
                    logged_out_at=None,
                    last_activity=get_current_datetime(),
                )
            )
            result = session.execute(stmt)
            session.commit()
            if result.rowcount > 0:
                logger.debug(f"Cleared logged_out_at for user {user_id} on re-login")

        return True
    except Exception as e:
        logger.warning(f"Failed to clear logged_out_at for user {user_id}: {e}")
        return False


def _should_update_activity(user_id: int, tier: str) -> bool:
    """
    Check if we should update activity for this user (throttling).
    Only update DB once per minute per user to reduce load.

    Uses separate caches for free and premium users.
    """
    cache = _free_activity_cache if tier == TIER_FREE else _premium_activity_cache

    with _cache_lock:
        last_update = cache.get(user_id, 0)
        now = time.time()

        if now - last_update >= _CACHE_TTL_SECONDS:
            return True
        return False


def _update_cache_after_commit(user_id: int, tier: str):
    """
    Update cache timestamp after successful database commit.
    This ensures cache consistency with database state.
    """
    cache = _free_activity_cache if tier == TIER_FREE else _premium_activity_cache

    with _cache_lock:
        cache[user_id] = time.time()


# ============================================================================
# FREE USER ACTIVITY TRACKING
# ============================================================================


async def _update_free_user_activity_async(user_id: int):
    """
    Update last_activity timestamp for free tier user (async wrapper).
    Runs in background to avoid blocking request.
    """
    if is_user_logged_out(user_id):
        logger.debug(f"Skipping activity update for logged out user {user_id}")
        return

    # Update cache immediately (optimistic) to reduce perceived latency
    _update_cache_after_commit(user_id, TIER_FREE)

    # Run blocking database call in thread pool (fire-and-forget)
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _update_free_user_activity_sync, user_id)
    except Exception as e:
        # Log error but don't propagate (fire-and-forget pattern)
        logger.warning(
            f"Background activity update failed for free user {user_id}: {e}"
        )


def _update_free_user_activity_sync(user_id: int) -> bool:
    """
    Update last_activity timestamp for free tier user (sync implementation).

    This method:
    1. Gets current instance ID from EC2 metadata
    2. Upserts record in free_user_assignments table using merge()
    3. Updates last_activity timestamp

    Returns:
        True if update successful, False otherwise
    """
    try:
        if is_user_logged_out(user_id):
            logger.debug(
                f"Skipping free user activity DB update "
                f"for logged out user {user_id}"
            )
            return False

        # Get current instance ID from EC2 metadata or environment
        instance_id = _get_instance_id()

        # Don't track if we can't determine instance ID
        # This prevents polluting database with fake "local" entries
        if not instance_id or instance_id == "local":
            return False

        # Update database with direct SQL to avoid StaleDataError
        # across concurrent workers
        from sqlalchemy import update

        with session_scope() as session:
            now = get_current_datetime()

            # UPDATE first (common path); instance_id is refreshed to the
            # serving instance. The ``logged_out_at IS NULL`` guard stops a
            # stray still-authenticated request (in-flight call after logout, a
            # second tab) from resurrecting a logged-out assignment — a
            # cross-process guard, unlike the short-lived in-memory logout map.
            # (instance_id write-ownership rework: separate follow-up.)
            from sqlalchemy import select

            stmt = (
                update(FreeUserAssignment)
                .where(FreeUserAssignment.user_id == user_id)
                .where(FreeUserAssignment.logged_out_at.is_(None))
                .values(
                    last_activity=now,
                    instance_id=instance_id,
                )
            )
            result = session.execute(stmt)
            if result.rowcount == 0:
                # UPDATE matched nothing: either no assignment exists yet (new
                # user → INSERT below) or one exists but is logged out (excluded
                # by the guard). Never resurrect a logged-out assignment.
                existing = session.execute(
                    select(FreeUserAssignment.user_id).where(
                        FreeUserAssignment.user_id == user_id
                    )
                ).first()
                if existing is not None:
                    return False

                # No existing row — insert new record
                assignment = FreeUserAssignment(
                    user_id=user_id,
                    instance_id=instance_id,
                    assigned_at=now,
                    last_activity=now,
                )
                session.add(assignment)

                # Log usage session for cost tracking
                usage_entry = InstanceUsageLog(
                    user_id=user_id,
                    instance_id=instance_id,
                    tier=TIER_FREE,
                    started_at=now,
                )
                session.add(usage_entry)
            else:
                # Existing assignment updated — ensure an open usage
                # session exists.  After a logout/re-login cycle the
                # previous InstanceUsageLog has ended_at set, so we
                # need to start a new one for the current session.
                open_session = session.execute(
                    select(InstanceUsageLog.id)
                    .where(
                        InstanceUsageLog.user_id == user_id,
                        InstanceUsageLog.tier == TIER_FREE,
                        InstanceUsageLog.ended_at.is_(None),
                    )
                    .limit(1)
                ).scalar()

                if open_session is None:
                    usage_entry = InstanceUsageLog(
                        user_id=user_id,
                        instance_id=instance_id,
                        tier=TIER_FREE,
                        started_at=now,
                    )
                    session.add(usage_entry)

            session.commit()
            return True

    except IntegrityError:
        # Race condition: another worker inserted the row between our
        # UPDATE (rowcount==0) and INSERT. Row exists — treat as success.
        logger.debug(
            f"Concurrent insert for free user {user_id}, "
            f"row already exists (benign race)"
        )
        return True

    except Exception as e:
        logger.error(f"Error updating free user activity for user {user_id}: {e}")
        return False


# ============================================================================
# PREMIUM USER ACTIVITY TRACKING
# ============================================================================


async def _update_premium_user_activity_async(user_id: int):
    """
    Update last_activity timestamp for premium tier user (async wrapper).
    Runs in background to avoid blocking request.
    """
    if is_user_logged_out(user_id):
        logger.debug(f"Skipping activity update for logged out user {user_id}")
        return

    # Update cache immediately (optimistic) to reduce perceived latency
    _update_cache_after_commit(user_id, TIER_PREMIUM)

    # Run blocking database call in thread pool (fire-and-forget)
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _update_premium_user_activity_sync, user_id)
    except Exception as e:
        # Log error but don't propagate (fire-and-forget pattern)
        logger.warning(
            f"Background activity update failed for premium user {user_id}: {e}"
        )


def increment_heartbeat_failures(user_id: int) -> int:
    """
    Increment heartbeat_failures counter for a premium user.

    Called when heartbeat fails to track consecutive failures.

    Returns:
        New failure count, or -1 on error
    """
    try:
        with session_scope() as session:
            result = session.execute(
                text(
                    """
                UPDATE premium_user_assignments
                SET heartbeat_failures = heartbeat_failures + 1
                WHERE user_id = :user_id
                AND status = 'active'
                AND is_standby = 0
                """
                ),
                {"user_id": user_id},
            )
            session.commit()

            if result.rowcount > 0:
                row = session.execute(
                    text(
                        """
                    SELECT heartbeat_failures
                    FROM premium_user_assignments
                    WHERE user_id = :user_id AND status = 'active'
                    """
                    ),
                    {"user_id": user_id},
                ).fetchone()
                count = row[0] if row else 0
                logger.debug(
                    f"Incremented heartbeat failures for user {user_id} to {count}"
                )
                return count
            return 0
    except Exception as e:
        logger.error(f"Error incrementing heartbeat failures for user {user_id}: {e}")
        return -1


def _update_premium_user_activity_sync(user_id: int) -> bool:
    """
    Update last_activity timestamp for premium tier user (sync implementation).

    This method updates the last_activity column in the premium_user_assignments
    table, which is used by the Premium Cleanup Lambda to determine if an
    assignment is stale.

    Returns:
        True if update successful, False otherwise
    """
    try:
        if is_user_logged_out(user_id):
            logger.debug(
                f"Skipping premium activity DB update " f"for logged out user {user_id}"
            )
            return False

        with session_scope() as session:
            now = datetime.now(timezone.utc)

            # Update last_activity and reset heartbeat_failures.
            # Also restores a row from `terminating` (= PENDING_RELEASE) back to
            # `active` so multi-tab close on Tab A doesn't silently finalize
            # Tab B's still-active session during the 120s grace window.
            result = session.execute(
                text(
                    """
                UPDATE premium_user_assignments
                SET last_activity = :now,
                    status = 'active',
                    heartbeat_failures = 0
                WHERE user_id = :user_id
                AND status IN ('active', 'terminating')
                AND is_standby = 0
                AND (
                    status = 'active'
                    OR instance_state IS NULL
                    OR instance_state NOT IN
                        ('terminated','shutting-down','stopped','stopping')
                )
                """
                ),
                {"now": now, "user_id": user_id},
            )

            session.commit()

            if result.rowcount > 0:
                logger.debug(
                    f"Updated premium activity for user {user_id} at {now.isoformat()}"
                )
                return True
            else:
                # No assignment found (user may not have active premium assignment)
                return False

    except Exception as e:
        logger.error(f"Error updating premium user activity for user {user_id}: {e}")
        return False


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================


def _get_instance_id() -> Optional[str]:
    """
    Get current EC2 instance ID (cached).

    Delegates resolution to the shared ``resolve_instance_id`` (env →
    IMDSv2 → IMDSv1 → "local") and caches the result to avoid repeated
    metadata-service calls on every request.
    """
    global _instance_id_cache

    with _instance_id_lock:
        if _instance_id_cache is not None:
            return _instance_id_cache

        _instance_id_cache = resolve_instance_id()
        return _instance_id_cache
