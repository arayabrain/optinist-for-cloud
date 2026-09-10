import math
from datetime import datetime, timedelta, timezone
from typing import List, NamedTuple, Optional, Tuple

import stripe
from fastapi import HTTPException
from sqlalchemy import and_, exists
from sqlmodel import Session, select

from studio.app.common import models as common_model
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.subscription.constants import (
    DeletionPriority,
    PlanName,
    SubscriptionLifecycleStatus,
    SubscriptionPeriods,
    SubscriptionPlanIds,
    SubscriptionPlanType,
    SubscriptionStatus,
    SubscriptionStatusType,
    SubscriptionUserStatus,
    SyncStatus,
)
from studio.app.common.core.utils.config_handler import get_env_var
from studio.app.common.core.utils.datetime_utils import get_current_datetime
from studio.app.common.models.free_user import FreeUserAssignment
from studio.app.common.models.subscription import (
    SubscriptionCancellation,
    SubscriptionPlans,
    SubscriptionUserPurchase,
    UserSubscription,
)
from studio.app.common.models.user import User
from studio.app.common.models.user_preferences import UserPreferences

logger = AppLogger.get_logger()


class SubscriptionLifecycle(NamedTuple):
    """Resolved premium-subscription lifecycle for a user."""

    status: SubscriptionLifecycleStatus
    days_remaining: Optional[int]
    subscription_end: Optional[datetime]
    grace_end: Optional[datetime]
    deletion_date: Optional[datetime]


def derive_subscription_status(
    expiration: Optional[datetime],
    plan_id: Optional[int],
    plan_name: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Tuple[str, Optional[int]]:
    """Return the (status label, days remaining) a user's subscription is in.

    Compares the expiration instant, not a truncated day count: a premium
    subscription is active until the moment it expires. `timedelta.days`
    truncates toward zero, so anything under 24 hours away reported 0, failed a
    `> 0` test and dropped a paying user into the grace branch - which on a
    daily billing cycle is most of the time, and on a monthly one is the last
    day of every period.

    Days remaining is a display value and rounds up, so "expires later today"
    reads as 1 day rather than 0.
    """
    if not expiration or not plan_id:
        return SubscriptionStatus.FREE.value, None

    if expiration.tzinfo is None:
        expiration = expiration.replace(tzinfo=timezone.utc)
    # Taken from the caller so the moment being compared against is the
    # caller's, which is also what its tests already pin.
    now = now or get_current_datetime()

    def days_until(moment: datetime) -> int:
        return math.ceil((moment - now).total_seconds() / 86400)

    if plan_id == SubscriptionPlanIds.FREE:
        return SubscriptionStatus.FREE.value, None

    if plan_id == SubscriptionPlanIds.PREMIUM:
        if expiration > now:
            return SubscriptionStatus.PREMIUM.value, days_until(expiration)
        grace_end = expiration + timedelta(days=SubscriptionPeriods.GRACE_PERIOD_DAYS)
        if now <= grace_end:
            return SubscriptionStatus.LIMIT_GRACE.value, days_until(grace_end)
        return SubscriptionStatus.EXPIRED.value, None

    remaining = days_until(expiration)
    return (
        plan_name or PlanName.UNKNOWN.value,
        remaining if remaining > 0 else None,
    )


class SubscriptionService:
    """Service class for managing internal subscription logic and database operations.

    This service acts as the primary business logic layer for subscription management,
    handling all subscription-related data in the application's database and
    orchestrating subscription workflows. It serves as the single source of truth
    for subscription state within the application.

    Primary Responsibilities:
    - Manage subscription data persistence in the application database
    - Implement business rules and validation for subscription operations
    - Track subscription lifecycle states (active, expired, cancelled)
    - Handle subscription plan retrieval and user subscription status
    - Coordinate with StripeService for payment provider operations
    - Process subscription updates, downgrades, and cancellation tracking
    - Maintain user subscription metadata and expiration dates

    Key Features:
    - Retrieve active subscription plans from database
    - Check subscription cancellation status and history
    - Determine subscription status based on plan type and cancellation state
    - Update subscription plans and manage scheduled downgrades
    - Track subscription purchases and cancellation events

    Integration Points:
    - Database session management for subscription data persistence
    - User authentication and authorization
    - Subscription plan types (monthly, yearly)
    - StripeService for payment processing and Stripe API interactions

    Note: This service focuses on internal business logic. For Stripe-specific
    operations (payment methods, Stripe customer management, Stripe API calls),
    use StripeService."""

    _stripe_initialized = False

    @classmethod
    def _ensure_stripe_initialized(cls):
        """Lazy initialization of Stripe API key"""
        if not cls._stripe_initialized:
            try:
                stripe.api_key = cls.get_stripe_key()
                cls._stripe_initialized = True
            except ValueError as e:
                logger.warning(f"Stripe not initialized: {e}")
                # Don't raise here - allow module to load for tests

    @staticmethod
    def get_active_plans(db: Session) -> List[SubscriptionPlans]:
        return (
            db.query(SubscriptionPlans)
            .filter(SubscriptionPlans.status == SubscriptionStatusType.ACTIVE)
            .all()
        )

    @staticmethod
    def is_subscription_cancelled(db: Session, user_id: int) -> bool:
        """
        Check if a user's current active subscription is cancelled.

        This method uses a single optimized query with JOINs instead of
        3 sequential queries, reducing database round trips by 66%.

        Args:
            db: Database session
            user_id: The user's ID

        Returns:
            bool: True if the user has an active cancelled subscription, False otherwise
        """
        current_time = __class__.get_current_datetime()

        # Single query with LEFT JOINs to check subscription cancellation status
        # This replaces 3 separate queries:
        # 1. Get active subscription
        # 2. Get matching purchase
        # 3. Check for cancellation record
        result = (
            db.query(
                UserSubscription.id.label("subscription_id"),
                SubscriptionCancellation.id.label("cancellation_id"),
            )
            .outerjoin(
                SubscriptionUserPurchase,
                and_(
                    SubscriptionUserPurchase.user_id == UserSubscription.user_id,
                    SubscriptionUserPurchase.plan_id == UserSubscription.plan_id,
                    SubscriptionUserPurchase.created_at <= UserSubscription.created_at,
                ),
            )
            .outerjoin(
                SubscriptionCancellation,
                SubscriptionCancellation.purchases_id == SubscriptionUserPurchase.id,
            )
            .filter(
                UserSubscription.user_id == user_id,
                UserSubscription.expiration > current_time,
            )
            .order_by(
                UserSubscription.expiration.desc(),
                SubscriptionUserPurchase.created_at.desc(),
            )
            .first()
        )

        if not result:
            # No active subscription found
            logger.debug(f"No active subscription for user {user_id}")
            return False

        subscription_id, cancellation_id = result

        logger.debug(
            f"Subscription check for user {user_id}: "
            f"subscription_id={subscription_id}, cancellation_id={cancellation_id}"
        )

        return cancellation_id is not None

    @staticmethod
    def get_subscription_status(plan_data_id: int, is_cancelled: bool) -> int:
        # Determine status based on plan ID and cancellation state
        if is_cancelled:
            subscription_status = SubscriptionUserStatus.CANCELED
        elif plan_data_id == SubscriptionPlanType.MONTHLY:
            subscription_status = SubscriptionUserStatus.FREE
        elif plan_data_id == SubscriptionPlanType.YEARLY:
            subscription_status = SubscriptionUserStatus.SUBSCRIBED
        else:
            subscription_status = SubscriptionUserStatus.FREE
        return subscription_status

    @staticmethod
    def get_plan_by_id(db: Session, plan_id: int) -> SubscriptionPlans:
        return (
            db.query(SubscriptionPlans)
            .filter(SubscriptionPlans.id == plan_id, SubscriptionPlans.status.is_(True))
            .first()
        )

    @staticmethod
    def get_user_subscription(
        db: Session, user_id: int
    ) -> Optional[Tuple[UserSubscription, SubscriptionPlans]]:
        return (
            db.query(common_model.UserSubscription, common_model.SubscriptionPlans)
            .join(
                common_model.SubscriptionPlans,
                common_model.UserSubscription.plan_id
                == common_model.SubscriptionPlans.id,
            )
            .join(
                common_model.User,
                common_model.UserSubscription.user_id == common_model.User.id,
            )
            .filter(
                and_(
                    common_model.UserSubscription.user_id == user_id,
                    common_model.UserSubscription.expiration
                    > __class__.get_current_datetime(),
                    common_model.User.active.is_(True),
                )
            )
            .order_by(common_model.UserSubscription.expiration.desc())
            .first()
        )

    @staticmethod
    def get_user_subscription_purchase(
        db: Session, user_id: int
    ) -> Optional[SubscriptionUserPurchase]:
        return (
            db.query(SubscriptionUserPurchase)
            .filter(SubscriptionUserPurchase.user_id == user_id)
            .order_by(SubscriptionUserPurchase.created_at.desc())
            .first()
        )

    @staticmethod
    def get_user_expired_subscription(
        db: Session, user_id: int
    ) -> common_model.UserSubscription:
        return (
            db.query(
                common_model.UserSubscription,
                common_model.SubscriptionPlans,
                common_model.User,
            )
            .join(
                common_model.SubscriptionPlans,
                common_model.UserSubscription.plan_id
                == common_model.SubscriptionPlans.id,
            )
            .join(
                common_model.User,
                common_model.UserSubscription.user_id == common_model.User.id,
            )
            .filter(
                common_model.UserSubscription.user_id == user_id,
                common_model.UserSubscription.expiration
                <= __class__.get_current_datetime(),
                common_model.User.active.is_(True),
            )
            .order_by(common_model.UserSubscription.expiration.desc())
            .first()
        )

    @staticmethod
    def determine_lifecycle(
        db: Session, user_id: int
    ) -> Optional[SubscriptionLifecycle]:
        """Resolve a user's premium-subscription lifecycle status.

        Returns FREE when the user never had premium. Returns None when a premium
        row exists but is malformed (missing/None expiration) so callers can decide
        how to fail: the warning banner shows nothing, enforcement falls open to
        the raw quota.
        """
        GRACE_PERIOD_DAYS = SubscriptionPeriods.GRACE_PERIOD_DAYS
        WARNING_PERIOD_DAYS = SubscriptionPeriods.WARNING_PERIOD_DAYS

        query_result = db.execute(
            select(UserSubscription)
            .where(UserSubscription.user_id == user_id)
            .where(UserSubscription.plan_id == SubscriptionPlanIds.PREMIUM)
            .order_by(UserSubscription.expiration.desc())
            .limit(1)
        )
        result_rows = query_result.all()

        logger.debug(
            "Found %d premium subscription records for user %s",
            len(result_rows),
            user_id,
        )

        if not result_rows:
            return SubscriptionLifecycle(
                status=SubscriptionLifecycleStatus.FREE,
                days_remaining=None,
                subscription_end=None,
                grace_end=None,
                deletion_date=None,
            )

        last_subscription_row = result_rows[0]
        if hasattr(last_subscription_row, "__getitem__"):
            last_subscription = last_subscription_row[0]
        else:
            last_subscription = last_subscription_row

        if not hasattr(last_subscription, "expiration"):
            logger.error(
                f"User {user_id} subscription object missing "
                f"expiration attribute: {dir(last_subscription)}"
            )
            return None

        subscription_end = last_subscription.expiration
        if subscription_end is None:
            logger.error(f"User {user_id} subscription has None expiration date")
            return None
        if subscription_end.tzinfo is None:
            subscription_end = subscription_end.replace(tzinfo=timezone.utc)

        grace_end = subscription_end + timedelta(days=GRACE_PERIOD_DAYS)
        deletion_date = grace_end + timedelta(days=WARNING_PERIOD_DAYS)
        now = get_current_datetime()

        days_remaining = None
        if subscription_end > now:
            status = SubscriptionLifecycleStatus.ACTIVE
        elif now <= grace_end:
            status = SubscriptionLifecycleStatus.GRACE
            days_remaining = (grace_end - now).days
        elif now <= deletion_date:
            status = SubscriptionLifecycleStatus.WARNING
            days_remaining = (deletion_date - now).days
        else:
            status = SubscriptionLifecycleStatus.OVERDUE
            days_remaining = 0

        logger.debug("Final status: %s, days_remaining: %s", status, days_remaining)

        return SubscriptionLifecycle(
            status=status,
            days_remaining=days_remaining,
            subscription_end=subscription_end,
            grace_end=grace_end,
            deletion_date=deletion_date,
        )

    @staticmethod
    def get_stripe_key() -> str:
        return get_env_var("STRIPE_SECRET_KEY", required=True)

    @staticmethod
    def get_base_url() -> str:
        return get_env_var("STRIPE_CALLBACK_URL", required=True)

    @staticmethod
    def update_scheduled_downgrade(db: Session, user_id: int, scheduled: bool) -> None:
        """
        Update the scheduled downgrade status for a user's subscription
        """
        try:
            subscription = (
                db.query(UserSubscription)
                .filter(UserSubscription.user_id == user_id)
                .first()
            )

            if subscription:
                subscription.scheduled_downgrade = scheduled
                db.commit()
        except Exception as e:
            db.rollback()
            logger.error(
                f"Error updating scheduled downgrade for user {user_id}: {str(e)}"
            )
            raise HTTPException(
                status_code=500, detail="Failed to update scheduled downgrade"
            )

    @staticmethod
    def update_user_subscription(
        db: Session, user_id: int, new_plan_id: int
    ) -> Optional[UserSubscription]:
        """
        Update user's subscription to a new plan
        """
        try:
            # Get existing subscription
            subscription = (
                db.query(UserSubscription)
                .filter(
                    UserSubscription.user_id == user_id,
                    UserSubscription.expiration > __class__.get_current_datetime(),
                )
                .first()
            )

            if not subscription:
                return None

            # Update the subscription
            subscription.plan_id = new_plan_id
            subscription.updated_at = __class__.get_current_datetime()

            db.commit()
            db.refresh(subscription)

            return subscription

        except Exception as e:
            db.rollback()
            logger.error(f"Error updating subscription for user {user_id}: {str(e)}")
            raise HTTPException(
                status_code=500, detail="Failed to update user subscription"
            )

    @staticmethod
    def get_current_datetime() -> datetime:
        """
        Get the current UTC date and time.

        Note: This method delegates to the centralized datetime utility.
        New code should import directly from datetime_utils instead.
        """
        return get_current_datetime()

    @staticmethod
    def get_user_subscription_by_user_id(
        db: Session, user_id: int
    ) -> Optional[Tuple[UserSubscription, User]]:
        """
        Get user subscription by user ID with user details
        """
        return (
            db.query(UserSubscription, User)
            .join(
                User,
                UserSubscription.user_id == User.id,
            )
            .filter(UserSubscription.user_id == user_id)
            .first()
        )

    @staticmethod
    def get_user_for_invoice_lookup(
        db: Session, user_id: int
    ) -> Optional[Tuple[Optional[UserSubscription], User]]:
        """
        Get user and their subscription (if exists) for invoice lookup purposes.

        This method handles cases where a user may not have a subscription record
        but still needs to be looked up for invoice retrieval.

        Args:
            db: Database session
            user_id: The user's ID

        Returns:
            Tuple of (UserSubscription or None, User) if user exists
            None if user not found
        """
        # Try to get user with subscription first
        result = __class__.get_user_subscription_by_user_id(db, user_id)

        if result:
            # User has a subscription record
            subscription_user, user = result
            return (subscription_user, user)

        # User has no subscription record, get user directly
        user = db.query(User).filter(User.id == user_id).first()

        if not user:
            return None

        logger.info(
            f"No subscription record for user {user_id}, "
            f"returning user without subscription"
        )
        return (None, user)

    @staticmethod
    def get_current_excess_bytes(db: Session, user_id: int) -> Optional[int]:
        """Return bytes over free-tier quota, or None if no storage record."""
        from studio.app.common.core.subscription.constants import ExpirationDeletion
        from studio.app.common.models.subscription import UserStorageUsage

        storage = (
            db.query(UserStorageUsage)
            .filter(UserStorageUsage.user_id == user_id)
            .first()
        )
        if not storage:
            return None
        return max(0, storage.storage_usage_bytes - ExpirationDeletion.FREE_QUOTA_BYTES)

    @staticmethod
    def has_active_workflows(db: Session, user_id: int) -> bool:
        """Check if user has active workflows via FreeUserAssignment."""
        assignment = (
            db.query(FreeUserAssignment)
            .filter(FreeUserAssignment.user_id == user_id)
            .first()
        )
        return assignment is not None and assignment.active_workflow_count > 0

    @staticmethod
    def get_deletion_priority(db: Session, user_id: int) -> DeletionPriority:
        """Get user's deletion priority preference, defaulting to preserve_outputs."""
        prefs = (
            db.query(UserPreferences).filter(UserPreferences.user_id == user_id).first()
        )
        if prefs and prefs.deletion_priority:
            return DeletionPriority(prefs.deletion_priority)
        return DeletionPriority.PRESERVE_OUTPUTS

    @staticmethod
    def update_deletion_priority(
        db: Session, user_id: int, priority: DeletionPriority
    ) -> None:
        """Upsert user's deletion priority preference into UserPreferences."""
        prefs = (
            db.query(UserPreferences).filter(UserPreferences.user_id == user_id).first()
        )
        if prefs:
            prefs.deletion_priority = priority
        else:
            prefs = UserPreferences(user_id=user_id, deletion_priority=priority)
            db.add(prefs)
        db.commit()

    @staticmethod
    def mark_deletion_processed(db: Session, user_id: int) -> None:
        """Set deletion_processed_at on expired subscription records for the user.

        Only stamps records whose expiration is past the grace cutoff (the same
        records the eligibility query selected). New subscription records created
        after re-subscribing start with deletion_processed_at = NULL.
        """
        from studio.app.common.core.subscription.constants import SubscriptionPeriods

        now = get_current_datetime()
        grace_cutoff = now - timedelta(days=SubscriptionPeriods.GRACE_PERIOD_DAYS)
        subscriptions = (
            db.query(UserSubscription)
            .filter(
                UserSubscription.user_id == user_id,
                UserSubscription.deletion_processed_at.is_(None),
                UserSubscription.expiration <= grace_cutoff,
            )
            .all()
        )
        for subscription in subscriptions:
            subscription.deletion_processed_at = now
        if subscriptions:
            db.commit()

    @staticmethod
    def get_users_for_expiration_deletion(db: Session) -> List[dict]:
        """
        Get users past grace period who need expiration deletion.

        Finds users where:
        - Premium subscription expired > GRACE_PERIOD_DAYS ago
        - deletion_processed_at is NULL on those records (not yet processed)
        - Storage usage exceeds free tier quota

        Also re-processes users whose storage has grown back above quota
        after a cooldown period (self-healing).
        """
        from studio.app.common.core.subscription.constants import (
            ExpirationDeletion,
            SubscriptionPeriods,
            SubscriptionPlanIds,
        )
        from studio.app.common.models.subscription import UserStorageUsage

        current_time = get_current_datetime()
        grace_cutoff = current_time - timedelta(
            days=SubscriptionPeriods.GRACE_PERIOD_DAYS
        )
        reprocess_cutoff = current_time - timedelta(
            days=ExpirationDeletion.REPROCESS_COOLDOWN_DAYS
        )

        # Subquery: exclude users who have a current active subscription
        from sqlalchemy.orm import aliased

        ActiveSub = aliased(UserSubscription)
        has_active_sub = exists().where(
            and_(
                ActiveSub.user_id == UserSubscription.user_id,
                ActiveSub.expiration > current_time,
            )
        )

        # Query 1: Users with unprocessed expired subscriptions
        unprocessed = (
            db.query(UserSubscription, User, UserStorageUsage)
            .join(User, UserSubscription.user_id == User.id)
            .join(UserStorageUsage, UserStorageUsage.user_id == User.id)
            .filter(
                UserSubscription.plan_id == SubscriptionPlanIds.PREMIUM,
                UserSubscription.expiration <= grace_cutoff,
                UserSubscription.deletion_processed_at.is_(None),
                UserStorageUsage.storage_usage_bytes
                > ExpirationDeletion.FREE_QUOTA_BYTES,
                ~has_active_sub,
            )
            .order_by(UserSubscription.expiration.desc())
            .all()
        )

        # Query 2: Self-healing — users already processed but storage
        # grew back above quota, after cooldown period
        reprocess = (
            db.query(UserSubscription, User, UserStorageUsage)
            .join(User, UserSubscription.user_id == User.id)
            .join(UserStorageUsage, UserStorageUsage.user_id == User.id)
            .filter(
                UserSubscription.plan_id == SubscriptionPlanIds.PREMIUM,
                UserSubscription.expiration <= grace_cutoff,
                UserSubscription.deletion_processed_at.isnot(None),
                UserSubscription.deletion_processed_at <= reprocess_cutoff,
                UserStorageUsage.storage_usage_bytes
                > ExpirationDeletion.FREE_QUOTA_BYTES,
                ~has_active_sub,
            )
            .order_by(UserSubscription.expiration.desc())
            .all()
        )

        # Deduplicate by user_id, then limit to BATCH_SIZE
        seen_user_ids = set()
        users = []
        for sub, user, storage in list(unprocessed) + list(reprocess):
            if user.id in seen_user_ids:
                continue
            seen_user_ids.add(user.id)
            users.append(
                {
                    "user_id": user.id,
                    "user_uid": user.uid,
                    "storage_usage_bytes": storage.storage_usage_bytes,
                    "excess_bytes": (
                        storage.storage_usage_bytes
                        - ExpirationDeletion.FREE_QUOTA_BYTES
                    ),
                }
            )
            if len(users) >= ExpirationDeletion.BATCH_SIZE:
                break

        logger.info(f"Found {len(users)} users for expiration deletion processing")
        return users


class SyncService:
    """Service class for handling subscription synchronization"""

    @staticmethod
    def sync_subscription_status(db: Session, subscription_user_id: int) -> bool:
        """
        Sync subscription status with external systems

        Args:
            db: Database session
            subscription_user_id: Subscription user ID

        Returns:
            True if sync successful, False otherwise
        """
        try:
            subscription = (
                db.query(UserSubscription)
                .filter(UserSubscription.id == subscription_user_id)
                .first()
            )

            if not subscription:
                logger.error(f"Subscription {subscription_user_id} not found")
                return False

            # Mark as synced
            subscription.sync_status = SyncStatus.SYNCED
            subscription.last_synced = SubscriptionService.get_current_datetime()
            subscription.updated_at = SubscriptionService.get_current_datetime()
            db.commit()

            logger.info(f"Successfully synced subscription {subscription_user_id}")
            return True

        except Exception as e:
            logger.error(f"Error syncing subscription {subscription_user_id}: {str(e)}")

            # Mark as failed
            if subscription:
                subscription.sync_status = SyncStatus.FAILED
                subscription.updated_at = SubscriptionService.get_current_datetime()
                db.commit()

            return False
