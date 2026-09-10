from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional

from sqlalchemy import (
    BIGINT,
    DECIMAL,
    INTEGER,
    JSON,
    TIMESTAMP,
    Boolean,
    Date,
    DateTime,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import TINYINT
from sqlalchemy.sql import func
from sqlalchemy.sql.functions import current_timestamp
from sqlmodel import Column, Field, SQLModel

from studio.app.common.core.subscription.constants import CancellationReason, SyncStatus
from studio.app.common.core.utils.datetime_utils import get_current_datetime


class SubscriptionPlans(SQLModel, table=True):
    __tablename__ = "subscription_plans"
    __table_args__ = (Index("idx_subscription_plans_name", "name"),)

    id: Optional[int] = Field(
        sa_column=Column(BIGINT, primary_key=True, nullable=False, autoincrement=True),
        default=None,
    )
    name: str = Field(sa_column=Column(String(100), nullable=False))
    price: int = Field(
        sa_column=Column(BIGINT, nullable=False)
    )  # Changed from float to int (cents)
    billing_cycle: int = Field(
        sa_column=Column(BIGINT, nullable=False),
        description="Billing cycle in enum format (e.g., 1 for monthly, 2 for yearly)",
    )
    # Fixed: Use JSON column type and make it properly typed
    features: Optional[Dict[str, Any]] = Field(
        sa_column=Column(JSON, nullable=False),
        description="JSON object of features included in the plan",
    )
    status: bool = Field(
        sa_column=Column(Boolean, nullable=False, default=True),
        description="True=Active, False=Inactive",
    )
    currency: int = Field(
        # TINYINT UNSIGNED: currency is a small enum (1=USD, 2=JPY).
        sa_column=Column(TINYINT(unsigned=True), nullable=False, default=1),
        description="Currency code in enum format (e.g., 1 for USD, 2 for JPY)",
    )
    stripe_product_id: Optional[str] = Field(
        sa_column=Column(String(255), nullable=True),
        default=None,
        description="Stripe product ID for this subscription plan",
    )
    stripe_price_id: Optional[str] = Field(
        sa_column=Column(String(255), nullable=True),
        default=None,
        description="Stripe price ID for this subscription plan",
    )
    created_at: Optional[datetime] = Field(
        sa_column=Column(TIMESTAMP, nullable=False, server_default=current_timestamp()),
    )

    @property
    def formatted_price(self) -> str:
        return f"${self.price/100:.2f}" if self.price else "Free"


class UserSubscription(SQLModel, table=True):
    __tablename__ = "subscription_users"
    __table_args__ = (
        UniqueConstraint("user_id", name="idx_user_id_unique"),
        Index("idx_subscription_users_user_id", "user_id"),
        Index("idx_subscription_users_plan_id", "plan_id"),
        Index("idx_subscription_users_expiration", "expiration"),
        Index("idx_subscription_users_user_plan", "user_id", "plan_id"),
        Index("idx_subscription_users_user_expiration", "user_id", "expiration"),
    )

    id: Optional[int] = Field(
        sa_column=Column(BIGINT, primary_key=True, nullable=False, autoincrement=True),
        default=None,
    )
    plan_id: int = Field(
        sa_column=Column(
            BIGINT,
            ForeignKey("subscription_plans.id", name="fk_subscription_users_plan"),
            nullable=False,
        )
    )
    user_id: int = Field(
        sa_column=Column(
            BIGINT,
            ForeignKey("users.id", name="fk_subscription_users_user"),
            nullable=False,
        )
    )
    expiration: datetime = Field(sa_column=Column(DateTime, nullable=False))
    scheduled_downgrade: bool = Field(
        sa_column=Column(Boolean, nullable=False, default=False),
        description="True if a downgrade is scheduled after the current period",
        default=False,
    )
    # Memo: When I used SyncStatus Enum directly, I got an error of value mismatch
    sync_status: SyncStatus = Field(
        sa_column=Column(
            SQLEnum("pending", "synced", "failed", name="sync_status_enum"),
            nullable=False,
            default="pending",
        ),
        default=SyncStatus.PENDING,
    )
    last_synced: Optional[datetime] = Field(
        sa_column_kwargs={"server_default": current_timestamp()},
    )
    created_at: Optional[datetime] = Field(
        sa_column_kwargs={"server_default": current_timestamp()},
    )
    # DATETIME (not TIMESTAMP); changing the type needs a migration.
    # See DATABASE_DATETIME_TYPE_CONVENTIONS.md
    updated_at: Optional[datetime] = Field(
        sa_column=Column(
            DateTime,
            server_default=func.current_timestamp(),
            onupdate=func.current_timestamp(),
        ),
    )
    deletion_processed_at: Optional[datetime] = Field(
        sa_column=Column(DateTime, nullable=True),
        default=None,
    )


class SubscriptionProvider(SQLModel, table=True):
    __tablename__ = "subscription_providers"

    id: Optional[int] = Field(
        sa_column=Column(BIGINT, primary_key=True, nullable=False, autoincrement=True),
        default=None,
    )
    name: str = Field(
        sa_column=Column(String(100), nullable=False),
        description="Provider name (e.g., 'stripe', 'paypal')",
    )
    created_at: Optional[datetime] = Field(
        sa_column=Column(TIMESTAMP, nullable=False, server_default=current_timestamp()),
    )
    updated_at: Optional[datetime] = Field(
        sa_column=Column(
            TIMESTAMP,
            nullable=False,
            server_default=func.current_timestamp(),
            onupdate=func.current_timestamp(),
        ),
    )


class SubscriptionUserAccount(SQLModel, table=True):
    __tablename__ = "subscription_user_accounts"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "provider_id", name="uq_sub_user_account_user_provider"
        ),
        UniqueConstraint(
            "provider_customer_id", name="idx_unique_provider_customer_id"
        ),
        Index("idx_subscription_user_accounts_user", "user_id"),
        Index("idx_subscription_user_accounts_provider", "provider_id"),
    )

    id: Optional[int] = Field(
        sa_column=Column(BIGINT, primary_key=True, nullable=False, autoincrement=True),
        default=None,
    )
    user_id: int = Field(
        sa_column=Column(
            BIGINT,
            ForeignKey("users.id", name="fk_subscription_user_accounts_user"),
            nullable=False,
        )
    )
    provider_id: int = Field(
        sa_column=Column(
            BIGINT,
            ForeignKey(
                "subscription_providers.id",
                name="fk_subscription_user_accounts_provider",
            ),
            nullable=False,
        ),
        description="FK to subscription_providers.id",
    )
    provider_customer_id: str = Field(
        sa_column=Column(String(255), nullable=False),
        description="Provider's customer ID (e.g., Stripe customer ID)",
    )
    created_at: Optional[datetime] = Field(
        sa_column=Column(TIMESTAMP, nullable=False, server_default=current_timestamp()),
    )
    updated_at: Optional[datetime] = Field(
        sa_column=Column(
            TIMESTAMP,
            nullable=False,
            server_default=func.current_timestamp(),
            onupdate=func.current_timestamp(),
        ),
    )


class SubscriptionUserPurchase(SQLModel, table=True):
    __tablename__ = "subscription_user_purchases"
    __table_args__ = (
        Index("idx_subscription_purchase_history_user_id", "user_id"),
        Index("idx_subscription_purchase_history_plan", "plan_id"),
        Index("idx_subscription_purchase_history_created", "created_at"),
        Index("idx_subscription_user_purchases_user_id", "user_id"),
        Index(
            "idx_subscription_user_purchases_user_plan_created",
            "user_id",
            "plan_id",
            "created_at",
        ),
    )

    id: Optional[int] = Field(
        sa_column=Column(BIGINT, primary_key=True, nullable=False, autoincrement=True),
        default=None,
    )
    plan_id: int = Field(
        sa_column=Column(
            BIGINT,
            ForeignKey(
                "subscription_plans.id", name="fk_subscription_user_purchases_plan"
            ),
            nullable=False,
        ),
        description="1=FREE, 2=Premium",
    )
    user_id: int = Field(
        sa_column=Column(
            BIGINT,
            ForeignKey("users.id", name="fk_subscription_purchase_history_user"),
            nullable=False,
        )
    )
    created_at: Optional[datetime] = Field(
        default_factory=get_current_datetime,
        sa_column=Column(
            TIMESTAMP, nullable=False, server_default=func.current_timestamp()
        ),
    )
    updated_at: Optional[datetime] = Field(
        sa_column=Column(
            TIMESTAMP,
            nullable=False,
            server_default=func.current_timestamp(),
            onupdate=func.current_timestamp(),
        ),
    )


class SubscriptionCancellation(SQLModel, table=True):
    __tablename__ = "subscription_cancellations"
    __table_args__ = (
        Index("idx_subscription_cancellations_user", "cancelled_by_user_id"),
        Index("idx_subscription_cancellations_purchase", "purchases_id"),
        Index("idx_subscription_cancellations_cancelled_at", "cancelled_at"),
    )

    id: Optional[int] = Field(
        sa_column=Column(BIGINT, primary_key=True, nullable=False, autoincrement=True),
        default=None,
    )
    cancelled_by_user_id: int = Field(
        sa_column=Column(
            BIGINT,
            ForeignKey(
                "users.id", name="fk_subscription_cancellations_cancelled_by_user"
            ),
            nullable=False,
        )
    )
    purchases_id: int = Field(
        sa_column=Column(
            BIGINT,
            ForeignKey(
                "subscription_user_purchases.id",
                name="fk_subscription_cancellations_purchase",
            ),
            nullable=False,
        ),
        description="FK to subscription_user_purchases.id",
    )
    cancelled_at: Optional[datetime] = Field(
        default_factory=get_current_datetime,
        sa_column=Column(
            TIMESTAMP, nullable=False, server_default=func.current_timestamp()
        ),
    )
    reason: Optional[CancellationReason] = Field(
        sa_column=Column(
            SQLEnum(
                CancellationReason,
                name="cancellation_reason_enum",
                create_type=False,
                values_callable=lambda x: [e.value for e in x],
            ),
            nullable=True,
        ),
        default=None,
        description="Reason for cancellation",
    )
    notes: Optional[str] = Field(
        sa_column=Column(Text, nullable=True),
        default=None,
        description="Additional notes or comments",
    )


class UserStorageUsage(SQLModel, table=True):
    __tablename__ = "user_storage_usage"
    __table_args__ = (
        UniqueConstraint("id", name="idx_id"),
        Index("idx_user_storage_usage_user_id", "user_id"),
    )

    id: Optional[int] = Field(
        sa_column=Column(BIGINT, primary_key=True, nullable=False, autoincrement=True),
        default=None,
    )
    user_id: int = Field(sa_column=Column(BIGINT, nullable=False, unique=True))
    storage_usage_bytes: int = Field(
        sa_column=Column(BIGINT, nullable=False, default=0)
    )
    storage_quota_bytes: int = Field(sa_column=Column(BIGINT, nullable=False))
    last_updated: Optional[datetime] = Field(
        default_factory=get_current_datetime,
        sa_column=Column(
            DateTime, nullable=False, server_default=func.current_timestamp()
        ),
    )
    created_at: Optional[datetime] = Field(
        default_factory=get_current_datetime,
        sa_column=Column(
            DateTime, nullable=False, server_default=func.current_timestamp()
        ),
    )
    delta_since_last_scan: int = Field(
        sa_column=Column(
            BIGINT,
            nullable=False,
            default=0,
            server_default="0",
            comment="Cumulative bytes changed since last full S3 scan",
        )
    )
    last_full_scan: Optional[datetime] = Field(
        default=None,
        sa_column=Column(
            DateTime,
            nullable=True,
            comment="Timestamp of last full S3 storage scan",
        ),
    )

    @property
    def storage_usage_percent(self) -> float:
        """Calculate usage percentage."""
        if self.storage_quota_bytes == 0:
            return 0.0
        return round((self.storage_usage_bytes / self.storage_quota_bytes) * 100, 2)


class StorageOperationStatus(str, Enum):
    """Status of storage operation for idempotent tracking."""

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class StorageOperationType(str, Enum):
    """Type of storage operation."""

    INCREMENT = "increment"
    DECREMENT = "decrement"


class StorageOperation(SQLModel, table=True):
    """
    Tracks storage operations for idempotent increment/decrement.
    Prevents double-counting when operations fail or retry.
    """

    __tablename__ = "storage_operations"

    __table_args__ = (
        Index("idx_storage_ops_status_created", "status", "created_at"),
        Index("idx_storage_ops_status_retry", "status", "retry_count"),
    )

    id: Optional[int] = Field(
        sa_column=Column(BIGINT, primary_key=True, nullable=False, autoincrement=True),
        default=None,
    )
    user_id: int = Field(
        sa_column=Column(BIGINT, nullable=False, index=True),
        description="User ID for this storage operation",
    )
    idempotency_key: str = Field(
        sa_column=Column(
            String(255),
            nullable=False,
            unique=True,
            index=True,
            comment="Unique key to prevent duplicate operations",
        ),
        description="Unique key to prevent duplicate operations",
    )
    operation_type: str = Field(
        sa_column=Column(
            SQLEnum(
                "increment",
                "decrement",
                name="storage_operation_type_enum",
            ),
            nullable=False,
        ),
    )
    bytes_delta: int = Field(
        sa_column=Column(
            BIGINT,
            nullable=False,
            comment="Number of bytes to add or remove",
        ),
        description="Number of bytes to add (positive) or remove (negative)",
    )
    status: str = Field(
        sa_column=Column(
            SQLEnum(
                "pending",
                "completed",
                "failed",
                name="storage_operation_status_enum",
            ),
            nullable=False,
            default="pending",
        ),
        default=StorageOperationStatus.PENDING.value,
    )
    error_message: Optional[str] = Field(
        sa_column=Column(
            String(500),
            nullable=True,
            comment="Error message if operation failed",
        ),
        default=None,
        description="Error message if operation failed",
    )
    # Retry tracking for failed storage operations
    retry_count: int = Field(
        sa_column=Column(
            INTEGER,
            nullable=False,
            default=0,
            comment="Number of retry attempts for failed operations",
        ),
        default=0,
        description="Number of retry attempts for failed operations",
    )
    created_at: Optional[datetime] = Field(
        default_factory=get_current_datetime,
        sa_column=Column(
            TIMESTAMP, nullable=False, server_default=func.current_timestamp()
        ),
    )
    completed_at: Optional[datetime] = Field(
        sa_column=Column(DateTime, nullable=True),
        default=None,
    )


# Constants for storage operation retry
STORAGE_OPERATION_MAX_RETRIES = 5


class DeletionStep(str, Enum):
    """
    User deletion step tracking for safe deletion ordering.
    Firebase is deleted FIRST to prevent orphaned accounts.
    """

    STARTED = "started"
    FIREBASE_PENDING = "firebase_pending"
    FIREBASE_DELETED = "firebase_deleted"
    STRIPE_CANCELLED = "stripe_cancelled"
    S3_DELETED = "s3_deleted"
    WORKSPACES_DELETED = "workspaces_deleted"
    COMPLETED = "completed"


class DeletionStatus(str, Enum):
    """Status of user deletion process."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class UserDeletionRecord(SQLModel, table=True):
    """
    Tracks user deletion progress for recovery from failures.
    Uses two-phase commit for Firebase deletion to prevent orphaned accounts.
    """

    __tablename__ = "user_deletion_records"
    __table_args__ = (
        Index("idx_user_deletion_records_user_id", "user_id"),
        Index("idx_user_deletion_records_status_started", "status", "started_at"),
    )

    id: Optional[int] = Field(
        sa_column=Column(BIGINT, primary_key=True, nullable=False, autoincrement=True),
        default=None,
    )
    user_id: int = Field(
        sa_column=Column(BIGINT, nullable=False),
        description="FK to users.id being deleted",
    )
    user_uid: str = Field(
        sa_column=Column(
            String(128),
            nullable=False,
            comment="Firebase UID for recovery checks",
        ),
        description="Firebase UID for recovery checks",
    )
    step: str = Field(
        sa_column=Column(
            SQLEnum(
                "started",
                "firebase_pending",
                "firebase_deleted",
                "stripe_cancelled",
                "s3_deleted",
                "workspaces_deleted",
                "completed",
                name="deletion_step_enum",
            ),
            nullable=False,
            default="started",
        ),
        default=DeletionStep.STARTED.value,
    )
    status: str = Field(
        sa_column=Column(
            SQLEnum(
                "in_progress",
                "completed",
                "failed",
                name="deletion_status_enum",
            ),
            nullable=False,
            default="in_progress",
        ),
        default=DeletionStatus.IN_PROGRESS.value,
    )
    error: Optional[str] = Field(
        sa_column=Column(
            Text,
            nullable=True,
            comment="Error message if deletion failed",
        ),
        default=None,
        description="Error message if deletion failed",
    )
    started_at: Optional[datetime] = Field(
        default_factory=get_current_datetime,
        sa_column=Column(
            TIMESTAMP, nullable=False, server_default=func.current_timestamp()
        ),
    )
    completed_at: Optional[datetime] = Field(
        sa_column=Column(DateTime, nullable=True),
        default=None,
    )
    updated_at: Optional[datetime] = Field(
        sa_column=Column(
            TIMESTAMP,
            nullable=False,
            server_default=func.current_timestamp(),
            onupdate=func.current_timestamp(),
        ),
    )


class SubscriptionAuditLog(SQLModel, table=True):
    """
    Audit log for admin-initiated subscription changes.
    Records old/new values and the reason for each manual edit.
    """

    __tablename__ = "subscription_audit_log"
    __table_args__ = (Index("idx_subscription_audit_log_user_id", "user_id"),)

    id: Optional[int] = Field(
        sa_column=Column(BIGINT, primary_key=True, nullable=False, autoincrement=True),
        default=None,
    )
    user_id: int = Field(
        sa_column=Column(
            BIGINT,
            nullable=False,
            comment="The user whose subscription was changed",
        ),
        description="The user whose subscription was changed. "
        "No FK constraint — audit records must survive user deletion.",
    )
    changed_by: int = Field(
        sa_column=Column(
            BIGINT,
            nullable=False,
            comment="Admin user ID who made the change",
        ),
        description="Admin user ID who made the change. "
        "No FK constraint — audit records must survive user deletion.",
    )
    old_value: Dict[str, Any] = Field(
        sa_column=Column(
            JSON,
            nullable=False,
            comment="Subscription state before the change",
        ),
        description="Subscription state before the change",
    )
    new_value: Dict[str, Any] = Field(
        sa_column=Column(
            JSON,
            nullable=False,
            comment="Subscription state after the change",
        ),
        description="Subscription state after the change",
    )
    reason: str = Field(
        sa_column=Column(
            Text,
            nullable=False,
            comment="Admin-provided reason for the manual edit",
        ),
        description="Admin-provided reason for the manual edit",
    )
    created_at: Optional[datetime] = Field(
        default_factory=get_current_datetime,
        sa_column=Column(TIMESTAMP, server_default=func.current_timestamp()),
    )


class Taxes(SQLModel, table=True):
    """
    UNUSED / dead schema. Created by migration
    `af8c4144cd54_add_stripe_integration_tables` but never wired to any code:
    tax calculation is handled by Stripe (session.total_details.breakdown.taxes),
    not this table. Mapped here only so `alembic check` matches the DB without
    a destructive migration. Candidate for an explicit drop in a future PR.
    """

    __tablename__ = "taxes"
    __table_args__ = (
        Index("idx_taxes_type", "tax_type"),
        Index("idx_taxes_active", "is_active"),
    )

    id: Optional[int] = Field(
        sa_column=Column(BIGINT, primary_key=True, nullable=False, autoincrement=True),
        default=None,
    )
    tax_type: str = Field(sa_column=Column(String(50), nullable=False))
    tax_name: str = Field(sa_column=Column(String(100), nullable=False))
    tax_rate: Decimal = Field(
        sa_column=Column(DECIMAL(precision=5, scale=4), nullable=False)
    )
    is_active: bool = Field(
        sa_column=Column(Boolean, nullable=False, server_default="1"), default=True
    )
    effective_date: date = Field(sa_column=Column(Date, nullable=False))
    created_at: Optional[datetime] = Field(
        sa_column=Column(
            TIMESTAMP, nullable=False, server_default=func.current_timestamp()
        ),
    )
    updated_at: Optional[datetime] = Field(
        sa_column=Column(
            TIMESTAMP,
            nullable=False,
            server_default=func.current_timestamp(),
            onupdate=func.current_timestamp(),
        ),
    )
