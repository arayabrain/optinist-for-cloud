"""
Centralized constants for subscription management.

This module contains all constants, enums, and configuration values
used throughout the subscription system to ensure consistency and
ease of maintenance.
"""

from enum import IntEnum

from studio.app.common.core.compat import StrEnum


# ============================================================================
# Subscription User Status
# ============================================================================
class SubscriptionUserStatus(IntEnum):
    """User's subscription status"""

    FREE = 1
    SUBSCRIBED = 2
    EXPIRED = 3
    CANCELED = 4


# ============================================================================
# Subscription Plan Types
# ============================================================================
class SubscriptionPlanType(IntEnum):
    """Subscription billing plan types"""

    MONTHLY = 1
    YEARLY = 2


# ============================================================================
# Subscription Status
# ============================================================================
class SubscriptionStatusType(IntEnum):
    """Subscription active/inactive status"""

    INACTIVE = 0
    ACTIVE = 1


# ============================================================================
# Currency Types
# ============================================================================
class SubscriptionCurrencyType(IntEnum):
    """Supported currency types"""

    USD = 1
    JPY = 2

    def get_currency_string(self) -> str:
        """Get the string representation of the currency"""
        if self == self.__class__.USD:
            return "usd"
        elif self == self.__class__.JPY:
            return "jpy"
        return None

    @staticmethod
    def get_currency_enum(value: str):
        """Get the enum representation of the currency"""
        value = value.lower()
        if value == "usd":
            return SubscriptionCurrencyType.USD
        elif value == "jpy":
            return SubscriptionCurrencyType.JPY
        return None


# ============================================================================
# Sync Status
# ============================================================================
class SyncStatus(StrEnum):
    """Synchronization status for subscriptions"""

    PENDING = "pending"
    SYNCED = "synced"
    FAILED = "failed"


# ============================================================================
# Cancellation Reasons
# ============================================================================
class CancellationReason(StrEnum):
    """Reasons for subscription cancellation"""

    USER_REQUEST = "user_request"
    PAYMENT_FAILED = "payment_failed"
    ADMIN_ACTION = "admin_action"
    REFUND = "refund"


# ============================================================================
# Stripe Subscription Status
# ============================================================================
class StripeSubscriptionStatus(StrEnum):
    """Stripe subscription status values"""

    INCOMPLETE = "incomplete"
    INCOMPLETE_EXPIRED = "incomplete_expired"
    TRIAL = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    UNPAID = "unpaid"
    PAUSED = "paused"


# ============================================================================
# Stripe Webhook Events
# ============================================================================
class StripeWebhookEvent(StrEnum):
    """Stripe webhook event types"""

    CHECKOUT_SESSION_COMPLETED = "checkout.session.completed"
    INVOICE_PAYMENT_FAILED = "invoice.payment_failed"
    CUSTOMER_SUBSCRIPTION_CREATED = "customer.subscription.created"
    CUSTOMER_SUBSCRIPTION_UPDATED = "customer.subscription.updated"
    CUSTOMER_SUBSCRIPTION_DELETED = "customer.subscription.deleted"
    SUBSCRIPTION_SCHEDULE_RELEASED = "subscription_schedule.released"
    INVOICE_PAYMENT_SUCCEEDED = "invoice.payment_succeeded"
    INVOICE_CREATED = "invoice.created"
    INVOICE_FINALIZED = "invoice.finalized"


# ============================================================================
# Invoice Status
# ============================================================================
class InvoiceStatus(StrEnum):
    """Stripe invoice status values"""

    DRAFT = "draft"
    OPEN = "open"
    PAID = "paid"
    UNCOLLECTIBLE = "uncollectible"
    VOID = "void"


# ============================================================================
# Stripe Checkout Status
# ============================================================================
class StripeCheckoutSessionStatus(StrEnum):
    """Stripe checkout session status values"""

    COMPLETE = "complete"
    EXPIRED = "expired"
    OPEN = "open"


class StripeCheckoutPaymentStatus(StrEnum):
    """Stripe checkout payment status values"""

    PAID = "paid"
    UNPAID = "unpaid"
    NO_PAYMENT_REQUIRED = "no_payment_required"


# ============================================================================
# Billing Cycles
# ============================================================================
class BillingCycle(StrEnum):
    """Billing cycle identifiers"""

    MONTHLY = "1"
    YEARLY = "2"


# ============================================================================
# Payment Status
# ============================================================================
class PaymentStatus(StrEnum):
    """Payment status values"""

    PAID = "paid"


# ============================================================================
# Active Status (for backward compatibility with Enum)
# ============================================================================
class SubscriptionActiveStatus(IntEnum):
    """Subscription active status (legacy enum format)"""

    ACTIVE = 1
    INACTIVE = 0


# Storage size constants (in bytes)
class StorageSize:
    """Constants for storage size calculations"""

    KB = 1024  # 1 Kilobyte
    MB = 1024 * 1024  # 1 Megabyte
    GB = 1024 * 1024 * 1024  # 1 Gigabyte
    TB = 1024 * 1024 * 1024 * 1024  # 1 Terabyte


class StorageQuota:
    """Constants for storage quota limits"""

    FREE = 5  # 5 GB for free plan
    PREMIUM = 200  # 200 GB for premium plan
    CRITICAL_THRESHOLD_PERCENT = 90  # 90% usage threshold for critical warning
    DANGER_THRESHOLD_PERCENT = 100  # 100% usage threshold for danger warning

    @classmethod
    def bytes_for_plan(cls, plan_id: int) -> int:
        """Return storage quota in bytes for a given SubscriptionPlanIds value."""
        _PLAN_QUOTA_GB = {
            SubscriptionPlanIds.PREMIUM: cls.PREMIUM,
            SubscriptionPlanIds.FREE: cls.FREE,
        }
        return _PLAN_QUOTA_GB.get(plan_id, cls.FREE) * StorageSize.GB


class SubscriptionPeriods:
    """
    Constants for subscription period calculations
    Note: any updates should also be reflected in
    frontend/src/const/Subscription.ts SubscriptionPeriods
    """

    TRIAL_PERIOD_DAYS = 30
    GRACE_PERIOD_DAYS = 30
    WARNING_PERIOD_DAYS = 30
    # Days before grace period end to warn about quota drop
    QUOTA_DROP_WARNING_DAYS = 3
    STORAGE_WARNING_DAYS = 30  # Days to remove excess storage for free users

    # Cache age for storage usage (in minutes)
    MAX_CACHE_AGE_MINUTES = 20

    # Progress calculation constants
    MAX_PROGRESS_PERCENT = 100
    MIN_PROGRESS_PERCENT = 0
    PROGRESS_REFERENCE_DAYS = 30  # Reference period for progress bar (30 days)

    # Warning color thresholds (days remaining)
    CRITICAL_THRESHOLD_DAYS = 0  # Red/error
    URGENT_THRESHOLD_DAYS = 7  # Red/error
    WARNING_THRESHOLD_DAYS = 14  # Yellow/warning


class SubscriptionPlanIds:
    """
    Constants for subscription plan database IDs.

    Usage: Database queries comparing plan_id field
    Example: if subscription_plan_id == SubscriptionPlanIds.FREE

    Note: Different from SubscriptionType (string identifiers) and
    PlanName (display names)
    """

    FREE = 1
    PREMIUM = 2


class SubscriptionType(StrEnum):
    """
    String identifiers for subscription types.

    Usage: Type discriminator in API responses and business logic
    Example: if subscription_type == SubscriptionType.FREE.value

    Note: Different from SubscriptionPlanIds (database IDs) and
    PlanName (display strings)
    """

    PREMIUM = "premium"
    FREE = "free"


class PlanName(StrEnum):
    """
    Display names for subscription plans.

    Usage: UI labels and user-facing text
    Example: user.__dict__["subscription_plan_name"] = PlanName.FREE.value

    Note: Different from SubscriptionPlanIds (database IDs) and
    SubscriptionType (type identifiers)
    """

    PREMIUM = "Premium"
    FREE = "Free"
    UNKNOWN = "Unknown"  # Fallback for when plan cannot be determined


class SubscriptionStatus(StrEnum):
    """
    User subscription status labels.

    Usage: Represents current state of user's subscription for display
    Example: user.__dict__["subscription_status"] = SubscriptionStatus.FREE.value
    """

    FREE = "Free"  # User on free plan
    PREMIUM = "Premium"  # Active premium subscription
    LIMIT_GRACE = "Limit Grace"  # Premium expired, in grace period
    EXPIRED = "Expired"  # Grace period ended


class SubscriptionLifecycleStatus(StrEnum):
    """
    Lifecycle status for subscription expiration checking in limit warnings.

    Usage: Determines warning state based on subscription expiration timeline
    Example: Used in calculate_limit_warning() to decide warning type
    """

    ACTIVE = "active"  # Subscription has not expired yet
    GRACE = "grace"  # In grace period after expiration
    WARNING = "warning"  # In warning period (after grace, before deletion)
    OVERDUE = "overdue"  # Past warning period
    FREE = "free"  # Never had premium subscription


class AlertType(StrEnum):
    """
    Frontend-facing alert types for limit warnings.

    Usage: API response field 'alert_type' in limit warning payloads
    Note: Frontend expects exactly these values: "storage", "grace", "overdue"

    Mapping from SubscriptionLifecycleStatus:
    - GRACE, WARNING -> AlertType.GRACE (both are grace period warnings)
    - OVERDUE -> AlertType.OVERDUE
    - Storage exceeded -> AlertType.STORAGE
    """

    STORAGE = "storage"  # Storage quota exceeded
    GRACE = "grace"  # In grace or warning period (premium features expiring)
    OVERDUE = "overdue"  # Past all grace periods, data deletion imminent


class SyncStatusConstants:
    """
    Configuration constants for background sync and cleanup jobs.

    Controls the behavior of:
    - Published experiment sync job (downloads experiments from S3 to local storage)
    - User data cleanup job (removes data for logged-out free tier users)
    """

    import os
    import tempfile

    # Sync job configuration
    SYNC_INTERVAL_MINUTES = 5  # How often to run sync job
    MAX_SYNC_PER_RUN = 10  # Max experiments to sync per run (avoid overload)
    LOCK_FILE = os.path.join(
        tempfile.gettempdir(), "optinist_sync_job.lock"
    )  # Lock file to prevent concurrent runs (cross-platform)

    # Interval jobs' first run: next_run_time = now + this delay.
    # Without it, IntervalTrigger first fires at now+interval (delaying the
    # first run a full interval after each restart).
    # Non-zero to avoid racing container/DB startup; kept under a minute.
    INITIAL_RUN_DELAY_SECONDS = 10

    # Cleanup job configuration
    CLEANUP_INTERVAL_MINUTES = 60  # How often to run cleanup job
    LOGOUT_GRACE_PERIOD_MINUTES = 60  # Wait time after logout before cleanup
    MAX_USERS_PER_RUN = 50  # Max users to clean per run (avoid overload)


# ============================================================================
# Configuration Constants
# ============================================================================


# Authentication routes
class AuthPaths:
    """Authentication-related URL paths"""

    LOGIN = "/login"
    REGISTER = "/register"
    LOGOUT = "/logout"


# Time constants (in seconds)
class TimeConstants:
    """Time-related constants in seconds"""

    ONE_HOUR = 3600  # 1 hour
    ONE_DAY = 86400  # 24 hours
    ONE_WEEK = 604800  # 7 days


# Trial subscription configuration
TRIAL_PERIOD_DAYS = 30  # Number of days for trial subscription period

# Provider names
STRIPE_PROVIDER_NAME = "stripe"

# Timeouts and limits
DUPLICATE_PURCHASE_WINDOW_MINUTES = 30  # Window for detecting duplicate purchases
# Extended from 7 to 30 days to handle trial-to-paid with 8+ day gap
RECENT_SUBSCRIPTION_WINDOW_DAYS = 30
INVOICE_LIST_LIMIT = 100  # Maximum number of invoices to retrieve

# Payment method types
PAYMENT_METHOD_TYPE_CARD = "card"
PAYMENT_METHOD_TYPE_LINK = "link"

# Setup intent usage
SETUP_INTENT_USAGE_OFF_SESSION = "off_session"


# ============================================================================
# Storage Reconciliation Constants
# ============================================================================
class StorageReconciliation:
    """Constants for storage reconciliation background job"""

    # Job scheduling
    INTERVAL_MINUTES = 60  # Run every 60 minutes

    # Drift detection thresholds (for logging warnings)
    DRIFT_THRESH_PERCENT = 5.0  # 5% drift
    DRIFT_THRESH_BYTES = 100 * 1024 * 1024  # 100 MB

    # Batch processing configuration
    BATCH_SIZE = 10  # Process 10 users at a time to prevent OOM
    RATE_LIMIT_DELAY_SECONDS = 0.5  # 0.5s delay between users to avoid S3 throttling

    # PostgreSQL advisory lock namespace for storage scans
    # This namespace ID is multiplied by 1000000 and added to user_id
    # to create unique lock keys: lock_key = ADVISORY_LOCK_NAMESPACE * 1000000 + user_id
    # Range: 12345000000 - 12345999999 (supports up to 1M users)
    ADVISORY_LOCK_NAMESPACE = 12345


class StorageScanTriggers:
    """Constants for triggering full S3 storage scans"""

    # Delta thresholds for triggering scans
    DELTA_THRESHOLD_PERCENT = 5.0  # 5% of current storage
    DELTA_THRESHOLD_BYTES = 200 * 1024 * 1024  # 200 MB

    # Time-based scan interval
    SCAN_INTERVAL_MINUTES = 60  # Hourly reconciliation


class DeletionPriority(StrEnum):
    """User preference for which data to preserve during expiration deletion."""

    PRESERVE_OUTPUTS = "preserve_outputs"
    PRESERVE_INPUTS = "preserve_inputs"


class ExpirationDeletion:
    """Constants for expiration deletion background job"""

    JOB_ID = "expiration_lifecycle"
    JOB_INTERVAL_MINUTES = 1440  # 24 hours
    BATCH_SIZE = 10  # Max users per job run
    RECHECK_SUBSCRIPTION_INTERVAL = 5  # Re-check subscription every N units
    FREE_QUOTA_BYTES = StorageQuota.FREE * StorageSize.GB  # 5 GB
    REPROCESS_COOLDOWN_DAYS = 7  # Days before re-processing a user
    # CloudWatch metrics
    METRIC_NAMESPACE_BASE = "OptiNiSt/BackgroundJobs"
    METRIC_PROCESSED = "ExpirationDeletionProcessed"
    METRIC_ERRORS = "ExpirationDeletionErrors"


class PremiumExpirationSweep:
    """Constants for the premium expiration -> release backstop sweep job.

    Safety net for the event-driven path: releases dangling premium
    assignments for users whose subscription expired past the grace period
    when no Stripe ``customer.subscription.deleted`` event released them
    (e.g. a missed webhook, or a local expiration applied via direct DB
    UPDATE such as test 600-17b).
    """

    JOB_ID = "premium_expiration_sweep"
    JOB_INTERVAL_MINUTES = 60  # Hourly backstop
    MAX_RELEASES_PER_RUN = 50  # Bound work per run
    # Short per-release timeout so one slow/hung Lambda can't stall the whole
    # sweep. This is a backstop: a skipped release is retried next run.
    # Worst case per run ~= MAX_RELEASES_PER_RUN * RELEASE_TIMEOUT_SECONDS,
    # which stays well under JOB_INTERVAL_MINUTES.
    RELEASE_TIMEOUT_SECONDS = 15


class S3Pagination:
    """Constants for S3 pagination and streaming"""

    PAGE_SIZE = 1000  # Process 1000 objects at a time
