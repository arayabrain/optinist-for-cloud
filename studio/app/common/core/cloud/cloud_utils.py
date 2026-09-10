"""
Cloud utilities for user context and subscription management.
"""
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlmodel import select

from studio.app.common.core.cloud.storage_tracking import (
    _is_storage_data_fresh,
    get_current_user_storage_usage,
    get_user_storage_usage,
)
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.subscription.constants import (
    AlertType,
    PlanName,
    StorageQuota,
    StorageSize,
    SubscriptionLifecycleStatus,
    SubscriptionPeriods,
    SubscriptionStatus,
    SubscriptionType,
)
from studio.app.common.core.subscription.subscription_service import SubscriptionService
from studio.app.common.db.database import session_scope
from studio.app.common.models import SubscriptionPlans
from studio.app.common.models import User as UserModel
from studio.app.common.models import UserStorageUsage, UserSubscription
from studio.app.common.schemas.storage import LimitWarning

logger = AppLogger.get_logger()


async def ensure_user_bucket_exists(
    user_id: int, db=None, auto_commit: bool = True
) -> Optional[str]:
    """
    Ensure a user has a valid S3 bucket. Creates one if missing.

    This function handles:
    1. User has no bucket name in DB -> generate name, create bucket, save to DB
    2. User has bucket name but bucket doesn't exist -> create the bucket

    Args:
        user_id: The user's database ID
        db: Optional database session. If None, creates a new session.
        auto_commit: If True, commits DB changes. Set False when caller manages
            the transaction (e.g., during user creation).

    Returns:
        The bucket name if successful, None if failed or storage not available.
    """
    from studio.app.common.core.storage.remote_storage_controller import (
        RemoteStorageController,
    )

    if not RemoteStorageController.is_available():
        logger.debug("Remote storage not available, skipping bucket creation")
        return None

    try:
        # Use provided session or create new one
        if db is None:
            with session_scope() as db:
                return await _ensure_user_bucket_exists_impl(
                    user_id, db, auto_commit=True
                )
        else:
            return await _ensure_user_bucket_exists_impl(user_id, db, auto_commit)

    except Exception as e:
        logger.error(f"Failed to ensure bucket exists for user {user_id}: {e}")
        return None


async def _ensure_user_bucket_exists_impl(
    user_id: int, db, auto_commit: bool = True
) -> Optional[str]:
    """Implementation of ensure_user_bucket_exists with db session."""
    from studio.app.common.core.storage.remote_storage_controller import (
        RemoteStorageController,
        RemoteStorageSimpleWriter,
    )

    # Get user from DB
    user = db.query(UserModel).filter(UserModel.id == user_id).first()
    if not user:
        logger.error(f"User {user_id} not found in database")
        return None

    # Check if user already has a bucket name
    bucket_name = None
    if user.attributes and isinstance(user.attributes, dict):
        bucket_name = user.attributes.get("remote_bucket_name")

    # Short-circuit: if the DB already has a bucket name, trust it and skip
    # the S3 CreateBucket call. This is the hot path on every login.
    if bucket_name:
        return bucket_name

    # Generate new bucket name if not exists
    if not bucket_name:
        prefix = os.environ.get("S3_USER_BUCKET_PREFIX", "optinist-user")
        bucket_name = RemoteStorageController.create_user_bucket_name(
            id=user_id, prefix=prefix
        )
        logger.info(f"Generated new bucket name for user {user_id}: {bucket_name}")

    # Create bucket (idempotent - will succeed if already exists)
    try:
        async with RemoteStorageSimpleWriter(bucket_name) as storage:
            await storage.create_bucket()
        logger.info(f"Bucket created/verified for user {user_id}: {bucket_name}")
    except Exception as e:
        # Check if bucket already exists (not an error)
        if "BucketAlreadyOwnedByYou" in str(e) or "BucketAlreadyExists" in str(e):
            logger.debug(f"Bucket already exists for user {user_id}: {bucket_name}")
        else:
            logger.error(f"Failed to create bucket for user {user_id}: {e}")
            raise

    # Update user attributes if bucket name was newly generated
    if not user.attributes or not user.attributes.get("remote_bucket_name"):
        new_attributes = dict(user.attributes) if user.attributes else {}
        new_attributes["remote_bucket_name"] = bucket_name
        user.attributes = new_attributes
        if auto_commit:
            db.commit()
        logger.info(f"Updated user {user_id} attributes with bucket name")

    return bucket_name


async def get_user_context_with_warnings(user_id: int) -> Optional[Dict[str, Any]]:
    """
    Get user context including subscription plan & limit warnings from database by ID.
    Returns user info with subscription details and limit warnings or None if not found.
    Note: This is an async function that includes live storage warning calculations.
    """
    try:
        # Get basic user context using crud_users
        from studio.app.common.core.users.crud_users import get_user_with_context

        with session_scope() as db:
            user = await get_user_with_context(db, user_id)

        if user:
            # Convert User object to dict format for backward compatibility
            user_context = {
                "id": user.id,
                "uid": user.uid,
                "name": user.name,
                "email": str(user.email),
                "active": True,  # get_user_with_context only returns active users
                "attributes": user.attributes,
                "subscription_plan_name": user.subscription_plan_name,
                "subscription_price": 0,  # Not available in User schema
                "subscription_status": user.subscription_status,
                "subscription_plan": user.subscription_type,
            }

            # Add limit warning information (async)
            limit_warning = await calculate_limit_warning(user_id)
            user_context["limit_warning"] = limit_warning

            logger.info(
                f"Complete user context with warnings for "
                f"user {user_id}: {user_context}"
            )
            return user_context
        else:
            return None

    except Exception as e:
        logger.error(
            f"Failed to get user context with warnings for user {user_id}: {e}"
        )
        return None


def _effective_quota_bytes(
    status: Optional[SubscriptionLifecycleStatus], raw_quota_bytes: int
) -> int:
    """Storage quota actually applied, given lifecycle status.

    Premium users past expiry (grace/warning/overdue) are held to the free-tier
    limit even while their raw quota record still reads the premium quota;
    everyone else (active, free, or an undeterminable status) uses the raw quota.
    This is the single mapping shared by the warning banner and the enforcement
    gates so they cannot disagree.
    """
    downgraded = {
        SubscriptionLifecycleStatus.GRACE,
        SubscriptionLifecycleStatus.WARNING,
        SubscriptionLifecycleStatus.OVERDUE,
    }
    if status in downgraded:
        return StorageQuota.FREE * StorageSize.GB
    return raw_quota_bytes


def get_effective_quota_bytes(
    user_id: int, storage_info: Optional[Dict[str, Any]] = None
) -> int:
    """Return the storage quota to enforce for a user, in bytes.

    Mirrors the effective quota that ``calculate_limit_warning`` uses so the
    run/upload gates and the warning banner stay in agreement. Returns 0 when
    the raw quota is unknown/disabled, letting callers skip enforcement exactly
    as the pre-existing ``quota > 0`` guard did.
    """
    if storage_info is None:
        storage_info = get_user_storage_usage(user_id)
    raw_quota_bytes = storage_info.get("storage_quota_bytes", 0) if storage_info else 0
    if raw_quota_bytes <= 0:
        return 0

    # Fail open to the raw quota on any lookup error: a transient DB issue must
    # not turn a run/upload into a 500 (this runs on the request hot path).
    try:
        with session_scope() as db:
            lifecycle = SubscriptionService.determine_lifecycle(db, user_id)
    except Exception as e:
        logger.warning(
            f"Failed to determine subscription lifecycle for user {user_id}: {e}; "
            f"enforcing raw quota {raw_quota_bytes}"
        )
        return raw_quota_bytes

    if lifecycle is None:
        logger.warning(
            f"Could not determine subscription lifecycle for user {user_id}; "
            f"enforcing raw quota {raw_quota_bytes}"
        )
        return raw_quota_bytes

    return _effective_quota_bytes(lifecycle.status, raw_quota_bytes)


async def calculate_limit_warning(user_id: int) -> Optional[LimitWarning]:
    """
    Calculate limit warning based on subscription and storage status.

    Returns a LimitWarning Pydantic model for type-safe API responses,
    or None if no warning is needed.

    Cases:
    1. Free user, no storage limit exceeded → No warning
    2. Free user, storage limit exceeded → Storage warning
    3. Premium user, storage limit exceeded → Storage warning
    4. Premium user, subscription expiring only → Subscription warning
    5. Premium user, storage exceeded + subscription expiring → Combined warning
    """
    try:
        FREE_PLAN_LIMIT_BYTES = StorageQuota.FREE * StorageSize.GB

        logger.info(f"Calculating limit warning for user {user_id}")

        with session_scope() as db:
            # Get user's current storage usage - prioritize fresh data at startup

            # First check if we have cached data (within 20 minutes)
            storage_info = get_user_storage_usage(user_id)
            if storage_info and _is_storage_data_fresh(
                storage_info, SubscriptionPeriods.MAX_CACHE_AGE_MINUTES
            ):
                current_usage_bytes = storage_info.get("storage_usage_bytes", 0)
                logger.info(
                    f"LimitWarning: Using fresh cached storage data "
                    f"for user {user_id}: {current_usage_bytes}"
                )
            else:
                # Get fresh storage data using async call
                current_usage_bytes = await get_current_user_storage_usage(
                    user_id, force_live=True
                )
                logger.info(
                    f"LimitWarning: Calculated fresh storage data "
                    f"for user {user_id}: {current_usage_bytes}"
                )

            # Use quota from already retrieved storage_info to avoid redundant DB call
            storage_quota_bytes = (
                storage_info.get("storage_quota_bytes", FREE_PLAN_LIMIT_BYTES)
                if storage_info
                else FREE_PLAN_LIMIT_BYTES
            )
            storage_quota_gb = storage_quota_bytes / StorageSize.GB

            # Step 1: Determine subscription status.
            # A malformed premium row (None returned) means no warning.
            lifecycle = SubscriptionService.determine_lifecycle(db, user_id)
            if lifecycle is None:
                return None
            subscription_status = lifecycle.status
            subscription_end = lifecycle.subscription_end
            grace_end = lifecycle.grace_end
            deletion_date = lifecycle.deletion_date
            days_remaining = lifecycle.days_remaining

            # Step 2: Determine storage status against the effective quota
            # (grace/warning/overdue are held to the free-tier limit), using the
            # same mapping the enforcement gates apply.
            effective_quota_bytes = _effective_quota_bytes(
                subscription_status, storage_quota_bytes
            )
            effective_quota_gb = effective_quota_bytes / StorageSize.GB

            storage_exceeded = current_usage_bytes > effective_quota_bytes
            excess_bytes = max(0, current_usage_bytes - effective_quota_bytes)
            excess_gb = excess_bytes / StorageSize.GB
            current_usage_gb = current_usage_bytes / StorageSize.GB

            # Step 3: Apply the 5 cases
            logger.debug(f"User {user_id} warning analysis:")
            logger.debug(f"Subscription status: {subscription_status}")
            logger.debug(f"Storage exceeded: {storage_exceeded}")
            logger.debug(
                f"Current usage: {current_usage_gb:.2f}GB / {effective_quota_gb:.1f}GB "
                f"(effective quota for {subscription_status})"
            )

            # Case 1: Free user, no storage limit exceeded → No warning
            if (
                subscription_status == SubscriptionLifecycleStatus.FREE
                and not storage_exceeded
            ):
                logger.debug(
                    f"User {user_id}: No warning needed (free plan, within limits)"
                )
                return None

            # Case 2: Free user, storage limit exceeded → Storage warning
            if (
                subscription_status == SubscriptionLifecycleStatus.FREE
                and storage_exceeded
            ):
                return LimitWarning(
                    has_alert=True,
                    alert_type=AlertType.STORAGE.value,
                    days_remaining=SubscriptionPeriods.STORAGE_WARNING_DAYS,
                    excess_data_bytes=excess_bytes,
                    excess_data_gb=round(excess_gb, 2),
                    storage_usage_bytes=current_usage_bytes,
                    storage_usage_gb=round(current_usage_gb, 2),
                    storage_quota_bytes=storage_quota_bytes,
                    storage_quota_gb=storage_quota_gb,
                    deletion_date=(
                        SubscriptionService.get_current_datetime()
                        + timedelta(days=SubscriptionPeriods.STORAGE_WARNING_DAYS)
                    ).isoformat(),
                    message=(
                        f"Your data usage ({round(current_usage_gb, 1)} GB) "
                        f"exceeds the free plan limit ({storage_quota_gb:.1f} GB). "
                        f"Please upgrade or remove {round(excess_gb, 1)} GB of data "
                        f"within {SubscriptionPeriods.STORAGE_WARNING_DAYS} days."
                    ),
                )

            # Case 3: Premium user active, storage limit exceeded → Storage warning only
            if (
                subscription_status == SubscriptionLifecycleStatus.ACTIVE
                and storage_exceeded
            ):
                return LimitWarning(
                    has_alert=True,
                    alert_type=AlertType.STORAGE.value,
                    days_remaining=SubscriptionPeriods.STORAGE_WARNING_DAYS,
                    excess_data_bytes=excess_bytes,
                    excess_data_gb=round(excess_gb, 2),
                    storage_usage_bytes=current_usage_bytes,
                    storage_usage_gb=round(current_usage_gb, 2),
                    storage_quota_bytes=storage_quota_bytes,
                    storage_quota_gb=storage_quota_gb,
                    message=(
                        f"Your storage usage ({round(current_usage_gb, 1)} GB) is over "
                        f"the limit for your plan. You will be unable to run workflows."
                        f" Consider cleaning up unused data."
                    ),
                )

            # Cases 4 & 5: Premium user with subscription issues (grace/warning/overdue)
            # Always show warning for expired premium users
            match subscription_status:
                case (
                    SubscriptionLifecycleStatus.GRACE
                    | SubscriptionLifecycleStatus.WARNING
                    | SubscriptionLifecycleStatus.OVERDUE
                ):
                    logger.debug(
                        f"User {user_id}: Creating limit warning "
                        f"(status: {subscription_status}, "
                        f"storage_exceeded: {storage_exceeded})"
                    )

                    # Determine alert type using AlertType enum
                    match subscription_status:
                        case (
                            SubscriptionLifecycleStatus.GRACE
                            | SubscriptionLifecycleStatus.WARNING
                        ):
                            alert_type = AlertType.GRACE
                        case SubscriptionLifecycleStatus.OVERDUE:
                            alert_type = AlertType.OVERDUE
                        case _:
                            alert_type = AlertType.GRACE  # Fallback

                    # Generate message based on status and storage
                    message = _generate_subscription_warning_message(
                        subscription_status=subscription_status,
                        subscription_end=subscription_end,
                        storage_exceeded=storage_exceeded,
                        current_usage_gb=current_usage_gb,
                        effective_quota_gb=effective_quota_gb,
                        excess_gb=excess_gb,
                        days_remaining=days_remaining,
                    )

                    return LimitWarning(
                        has_alert=True,
                        alert_type=alert_type.value,
                        days_remaining=days_remaining or 0,
                        excess_data_bytes=excess_bytes,
                        excess_data_gb=round(excess_gb, 2),
                        storage_usage_bytes=current_usage_bytes,
                        storage_usage_gb=round(current_usage_gb, 2),
                        storage_quota_bytes=effective_quota_bytes,
                        storage_quota_gb=effective_quota_gb,
                        subscription_end_date=(
                            subscription_end.isoformat() if subscription_end else None
                        ),
                        grace_end_date=(grace_end.isoformat() if grace_end else None),
                        deletion_date=(
                            deletion_date.isoformat() if deletion_date else None
                        ),
                        message=message,
                    )
                case _:
                    pass

            # All other cases: No warning needed
            logger.info(f"User {user_id}: No warning needed")
            return None

    except Exception as e:
        logger.error(f"Failed to calculate limit warning for user {user_id}: {e}")
        return None


def _generate_subscription_warning_message(
    subscription_status: SubscriptionLifecycleStatus,
    subscription_end: datetime,
    storage_exceeded: bool,
    current_usage_gb: float,
    effective_quota_gb: float,
    excess_gb: float,
    days_remaining: Optional[int],
) -> str:
    """
    Generate warning message based on subscription status and storage state.

    Uses match/case for cleaner status handling.
    """
    date_str = subscription_end.strftime("%B %d, %Y")

    if storage_exceeded:
        # Case 4: Both storage and subscription issues
        match subscription_status:
            case SubscriptionLifecycleStatus.GRACE:
                return (
                    f"Your premium subscription expired on {date_str}. "
                    f"Your storage ({round(current_usage_gb, 1)} GB) exceeds "
                    f"the free plan limit ({effective_quota_gb:.0f} GB). "
                    f"You have {days_remaining or 0} days to upgrade or remove "
                    f"{round(excess_gb, 1)} GB of data."
                )
            case SubscriptionLifecycleStatus.WARNING:
                return (
                    f"Your premium subscription expired on {date_str}. "
                    f"Your storage ({round(current_usage_gb, 1)} GB) exceeds "
                    f"the free plan limit ({effective_quota_gb:.0f} GB). "
                    f"Remove {round(excess_gb, 1)} GB of data within "
                    f"{days_remaining or 0} days or your data will be deleted."
                )
            case SubscriptionLifecycleStatus.OVERDUE:
                return (
                    f"Your premium subscription expired on {date_str}. "
                    f"Your storage ({round(current_usage_gb, 1)} GB) exceeds "
                    f"the free plan limit ({effective_quota_gb:.0f} GB). "
                    f"Your data is scheduled for deletion. "
                    f"Please upgrade or remove {round(excess_gb, 1)} GB."
                )
            case _:
                return f"Your premium subscription expired on {date_str}."
    else:
        # Case 5: Subscription issue only (user within storage limits)
        match subscription_status:
            case SubscriptionLifecycleStatus.GRACE:
                return (
                    f"Your premium subscription expired on {date_str}. "
                    f"You have {days_remaining or 0} days of premium features "
                    f"remaining. Please upgrade to maintain access."
                )
            case SubscriptionLifecycleStatus.WARNING:
                return (
                    f"Your premium subscription expired on {date_str}. "
                    f"Your data will be deleted in {days_remaining or 0} days. "
                    f"Please upgrade to prevent data loss."
                )
            case SubscriptionLifecycleStatus.OVERDUE:
                return (
                    f"Your premium subscription expired on {date_str}. "
                    f"Your data is scheduled for deletion. "
                    f"Please upgrade immediately to prevent data loss."
                )
            case _:
                return f"Your premium subscription expired on {date_str}."


class CloudDebug:
    """Debug utilities for cloud functionality."""

    @staticmethod
    def test_database_connection() -> bool:
        """
        Test database connectivity and test ORM models.
        """
        try:
            logger.info("Testing database connection with ORM...")

            with session_scope() as db:
                # Test basic connection
                query_result = db.execute(select(UserModel).where(UserModel.id == 1))
                fallback_user_row = query_result.first()

                if fallback_user_row:
                    logger.info("Database connection successful!")
                    # fallback_user_row is a SQLAlchemy Row containing UserModel
                    user_obj = fallback_user_row[0]
                    logger.info(
                        f"Found fallback user: {user_obj.name} ({user_obj.email})"
                    )
                else:
                    logger.info(
                        "Database connection successful, but no fallback user found"
                    )

                # Test subscription models
                try:
                    query_result = db.execute(select(SubscriptionPlans))
                    subscription_count = len(query_result.all())
                    logger.info(f"Subscription plans available: {subscription_count}")
                except Exception as plan_error:
                    logger.warning(
                        f"Subscription plans table not accessible: {plan_error}"
                    )

                try:
                    query_result = db.execute(
                        select(UserSubscription).where(
                            UserSubscription.expiration
                            > SubscriptionService.get_current_datetime()
                        )
                    )
                    active_subscriptions = len(query_result.all())
                    logger.info(f"Active subscriptions: {active_subscriptions}")
                except Exception as sub_error:
                    logger.warning(
                        f"User subscriptions table not accessible: {sub_error}"
                    )

                try:
                    query_result = db.execute(select(UserStorageUsage))
                    storage_records = len(query_result.all())
                    logger.info(f"Storage usage records: {storage_records}")
                except Exception as storage_error:
                    logger.warning(
                        f"Storage usage table not accessible: {storage_error}"
                    )

                return True

        except Exception as e:
            logger.error(f"Database connection test failed: {e}")
            return False

    @staticmethod
    def initialize_cloud_utils():
        """
        Initialize cloud utils and test connectivity.
        """
        logger.info("Initializing cloud utilities...")

        # Test database connection
        if CloudDebug.test_database_connection():
            logger.info("Database connection test passed")
            logger.info("Cloud utilities initialized successfully")
            return True
        else:
            logger.error("Cloud utilities initialization failed")
            return False

    @staticmethod
    async def print_user_details(user_id: int = 1) -> None:
        """
        Print details of the admin user for debugging.
        """
        try:
            logger.debug("=== ADMIN USER DETAILS ===")

            # Get user context using crud_users
            from studio.app.common.core.users import crud_users
            from studio.app.common.db.database import session_scope

            with session_scope() as db:
                user_with_details = await crud_users.get_user_with_context(db, user_id)
                if user_with_details:
                    logger.debug(f"User ID: {user_with_details.id}")
                    logger.debug(f"Name: {user_with_details.name}")
                    logger.debug(f"Email: {user_with_details.email}")
                    logger.debug(f"UID: {user_with_details.uid}")
                    logger.debug(
                        f"Subscription Type: {user_with_details.subscription_type}"
                    )
                    logger.debug(
                        f"Has Active Subscription: "
                        f"{user_with_details.has_active_subscription}"
                    )
                    subscription_status = (
                        user_with_details.subscription_status or SubscriptionStatus.FREE
                    )
                    logger.debug(f"Subscription Status: {subscription_status}")
                    logger.debug(
                        f"Storage Usage: "
                        f"{user_with_details.storage_usage_bytes or 0} bytes"
                    )
                    logger.debug(
                        f"Storage Quota: "
                        f"{user_with_details.storage_quota_bytes or 0} bytes"
                    )
                else:
                    logger.error(
                        f"Failed to retrieve user details for user_id {user_id}"
                    )

                # Get count of all active subscriptions
                try:
                    active_subscriptions = await crud_users.list_user(
                        db,
                        limit=1000,  # Large limit to get all users
                        skip=0,
                        user_id=None,  # Get all users
                        search=None,
                        email=None,
                    )
                    # Count users with active subscriptions
                    active_count = sum(
                        1
                        for user in active_subscriptions
                        if user.subscription_status
                        and user.subscription_status != SubscriptionStatus.FREE
                    )
                    logger.debug(f"Total active subscriptions: {active_count}")
                except Exception as e:
                    logger.warning(f"Failed to count active subscriptions: {e}")

            logger.debug("=== END ADMIN USER DETAILS ===")

        except Exception as e:
            logger.error(f"Failed to print admin user details: {e}")


async def get_user_subscription_plan(user_id: int) -> Dict[str, Any]:
    """
    Get user subscription tier information.
    Returns subscription tier details including plan name and active status.
    """
    try:
        from studio.app.common.core.users import crud_users

        with session_scope() as db:
            user = await crud_users.get_user_with_context(db, user_id)

            if not user:
                logger.warning(f"User {user_id} not found")
                return {
                    "tier": SubscriptionType.FREE,
                    "plan_name": PlanName.FREE,
                    "is_premium": False,
                    "has_active_subscription": False,
                }

            # Extract subscription information from user context
            plan_name = getattr(user, "subscription_plan_name", PlanName.FREE)
            # getattr (not user.has_active_subscription) because `user` may be a
            # raw row rather than a full User schema; the property is evaluated
            # when present, else we fall back to non-premium.
            has_active = getattr(user, "has_active_subscription", False)

            # Determine tier from plan name and active status
            is_premium = bool(
                has_active
                and plan_name
                and plan_name.lower() == SubscriptionType.PREMIUM
            )
            tier = SubscriptionType.PREMIUM if is_premium else SubscriptionType.FREE

            logger.info(f"User {user_id} subscription tier: {tier} (plan: {plan_name})")

            return {
                "tier": tier,
                "plan_name": plan_name or PlanName.FREE,
                "is_premium": is_premium,
                "has_active_subscription": has_active,
            }

    except Exception as e:
        logger.warning(f"Failed to get subscription tier for user {user_id}: {e}")
        # Return free tier as fallback
        return {
            "tier": SubscriptionType.FREE,
            "plan_name": PlanName.FREE,
            "is_premium": False,
            "has_active_subscription": False,
        }
