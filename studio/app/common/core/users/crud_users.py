from datetime import datetime, timezone
from typing import Type, TypeVar

from fastapi import HTTPException
from fastapi_pagination.ext.sqlmodel import paginate
from firebase_admin import auth as firebase_auth
from firebase_admin.auth import EmailAlreadyExistsError, UserNotFoundError, UserRecord
from firebase_admin.exceptions import FirebaseError
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from studio.app.common.core.auth.auth import authenticate_user
from studio.app.common.core.auth.auth_email_service import AuthEmailService
from studio.app.common.core.cloud.cloud_utils import ensure_user_bucket_exists
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.storage.remote_storage_controller import (
    RemoteStorageController,
    RemoteStorageSimpleWriter,
)
from studio.app.common.core.subscription.constants import (
    PlanName,
    StorageQuota,
    StorageSize,
    SubscriptionPlanIds,
)
from studio.app.common.core.subscription.stripe_service import StripeService
from studio.app.common.core.subscription.subscription_service import (
    SubscriptionService,
    SubscriptionUserStatus,
    derive_subscription_status,
)
from studio.app.common.core.utils.datetime_utils import get_current_datetime
from studio.app.common.core.workspace.workspace_services import WorkspaceService
from studio.app.common.models import Role as RoleModel
from studio.app.common.models import User as UserModel
from studio.app.common.models import UserRole as UserRoleModel
from studio.app.common.models.subscription import (
    DeletionStatus,
    DeletionStep,
    SubscriptionAuditLog,
    SubscriptionPlans,
    UserDeletionRecord,
    UserStorageUsage,
    UserSubscription,
)
from studio.app.common.models.user_preferences import UserPreferences
from studio.app.common.models.workspace import Workspace
from studio.app.common.schemas.auth import UserAuth
from studio.app.common.schemas.base import SortOptions
from studio.app.common.schemas.users import (
    SubscriptionAuditSnapshot,
    User,
    UserCreate,
    UserPasswordUpdate,
    UserRole,
    UserSearchOptions,
    UserSubscriptionUpdate,
    UserUpdate,
)

logger = AppLogger.get_logger()


# =============================================================================
# Shared Query Helpers (DRY principle)
# =============================================================================
# These helpers are used by both list_user() and get_user_with_context() to
# avoid code duplication and ensure consistent behavior.
#
# NOTE: Capacity subqueries have been removed. We now use
# UserStorageUsage.storage_usage_bytes directly as the data_usage value. This is
# possible because storage_usage_bytes tracks the same total (input + output storage)
# via incremental delta updates, eliminating need for expensive SUM() aggregations
# across Workspace and ExperimentRecord tables.


def _transform_user_row(item) -> UserModel:
    """
    Transform a raw query result row into an enriched UserModel.

    Unpacks the query result tuple and adds computed attributes like
    subscription status, days remaining, storage usage percentage, etc.

    Args:
        item: Query result tuple containing:
            (user, role_id, data_usage, plan_name, storage_bytes, storage_quota,
             expiration, plan_id)

    Returns:
        UserModel with additional attributes set via __dict__
    """
    (
        user,
        role_id,
        data_usage,
        subscription_plan_name,
        storage_usage_bytes,
        storage_quota_bytes,
        subscription_expiration,
        subscription_plan_id,
    ) = item

    # Basic attributes
    user.__dict__["role_id"] = role_id
    user.__dict__["data_usage"] = data_usage
    user.__dict__["subscription_plan_name"] = (
        subscription_plan_name or PlanName.FREE.value
    )
    user.__dict__["storage_usage_bytes"] = storage_usage_bytes or 0
    user.__dict__["storage_quota_bytes"] = storage_quota_bytes or 0
    user.__dict__["storage_usage_percent"] = round(
        (storage_usage_bytes or 0) / (storage_quota_bytes or 1) * 100, 2
    )

    user.__dict__["subscription_expiration"] = subscription_expiration

    # One derivation, shared with crud_users: this block existed twice, so the
    # day-truncation bug it used to carry existed twice too.
    status, days_remaining = derive_subscription_status(
        subscription_expiration,
        subscription_plan_id,
        subscription_plan_name,
        get_current_datetime(),
    )
    user.__dict__["subscription_status"] = status
    user.__dict__["subscription_days_remaining"] = days_remaining

    return user


def _transform_user_rows(items) -> list:
    """
    Transform multiple query result rows into enriched UserModels.

    Args:
        items: List of query result tuples

    Returns:
        List of UserModels with additional attributes
    """
    return [_transform_user_row(item) for item in items]


async def set_role(db: Session, user_id: int, role_id: int, auto_commit=True):
    db.query(UserRoleModel).filter_by(user_id=user_id).delete(synchronize_session=False)
    role_user = UserRoleModel(user_id=user_id, role_id=role_id)
    db.add(role_user)
    db.flush()
    if auto_commit:
        db.commit()


async def get_user(db: Session, user_id: int, organization_id: int) -> User:
    try:
        data = (
            db.query(UserModel, UserRoleModel.role_id)
            .outerjoin(UserRoleModel, UserModel.id == UserRoleModel.user_id)
            .filter(
                UserModel.id == user_id,
                UserModel.active.is_(True),
                UserModel.organization_id == organization_id,
            )
            .first()
        )
        assert data is not None, "User not found"
        user, role_id = data
        user.__dict__["role_id"] = role_id
        return User.from_orm(user)
    except AssertionError as e:
        logger.error(e, exc_info=True)
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


async def get_user_with_context(db: Session, user_id: int) -> User:
    """
    Get user with full context including subscription and storage information.

    Optimized query: Uses UserStorageUsage.storage_usage_bytes as data_usage instead
    of calculating SUM(Workspace.input_data_usage) + SUM(ExperimentRecord.data_usage)
    via expensive subqueries. storage_usage_bytes is already tracked incrementally.
    """
    try:
        query_result = db.execute(
            select(
                UserModel,
                func.min(UserRoleModel.role_id),
                # Use pre-tracked storage_usage_bytes as data_usage
                func.coalesce(UserStorageUsage.storage_usage_bytes, 0).label(
                    "data_usage"
                ),
                func.max(SubscriptionPlans.name).label("subscription_plan_name"),
                UserStorageUsage.storage_usage_bytes,
                UserStorageUsage.storage_quota_bytes,
                func.max(UserSubscription.expiration).label("subscription_expiration"),
                func.max(UserSubscription.plan_id).label("subscription_plan_id"),
            )
            .join(UserRoleModel, UserRoleModel.user_id == UserModel.id, isouter=True)
            .join(RoleModel, RoleModel.id == UserRoleModel.role_id, isouter=True)
            .outerjoin(UserSubscription, UserSubscription.user_id == UserModel.id)
            .outerjoin(
                SubscriptionPlans, SubscriptionPlans.id == UserSubscription.plan_id
            )
            .outerjoin(UserStorageUsage, UserStorageUsage.user_id == UserModel.id)
            .filter(
                UserModel.active.is_(True),
                UserModel.id == user_id,
            )
            .group_by(UserModel.id)
        )

        result = query_result.first()
        if not result:
            raise HTTPException(status_code=404, detail="User not found")

        # Transform using shared helper
        transformed_user = _transform_user_row(result)
        return User.from_orm(transformed_user)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


# Both role columns live on joined tables while the list query groups by
# users.id, so they have to be ordered by an aggregate: MySQL's
# only_full_group_by rejects a bare joined column here (1055) and the request
# fails outright. min() matches the role_id the query already selects.
USER_LIST_SORT_MAPPING = {
    "role_id": func.min(UserRoleModel.role_id),
    "role": func.min(RoleModel.role),
}


async def list_user(
    db: Session,
    organization_id: int,
    options: UserSearchOptions,
    sortOptions: SortOptions,
):
    """
    List users with pagination and full context including subscription/storage info.

    Optimized query: Uses UserStorageUsage.storage_usage_bytes as data_usage instead
    of calculating SUM(Workspace.input_data_usage) + SUM(ExperimentRecord.data_usage)
    via expensive subqueries. storage_usage_bytes is already tracked incrementally.
    """
    try:
        sa_sort_list = sortOptions.get_sa_sort_list(
            sa_table=UserModel,
            mapping=USER_LIST_SORT_MAPPING,
        )
        users = paginate(
            db,
            query=select(
                UserModel,
                func.min(UserRoleModel.role_id),
                # Use pre-tracked storage_usage_bytes as data_usage
                func.coalesce(UserStorageUsage.storage_usage_bytes, 0).label(
                    "data_usage"
                ),
                func.max(SubscriptionPlans.name).label("subscription_plan_name"),
                UserStorageUsage.storage_usage_bytes,
                UserStorageUsage.storage_quota_bytes,
                func.max(UserSubscription.expiration).label("subscription_expiration"),
                func.max(UserSubscription.plan_id).label("subscription_plan_id"),
            )
            .join(UserRoleModel, UserRoleModel.user_id == UserModel.id, isouter=True)
            .join(RoleModel, RoleModel.id == UserRoleModel.role_id, isouter=True)
            .outerjoin(UserSubscription, UserSubscription.user_id == UserModel.id)
            .outerjoin(
                SubscriptionPlans, SubscriptionPlans.id == UserSubscription.plan_id
            )
            .outerjoin(UserStorageUsage, UserStorageUsage.user_id == UserModel.id)
            .filter(
                UserModel.active.is_(True),
                UserModel.organization_id == organization_id,
            )
            .filter(
                UserModel.name.contains(options.name, autoescape=True),
                UserModel.email.contains(options.email, autoescape=True),
            )
            .group_by(UserModel.id)
            .order_by(*sa_sort_list),
            transformer=_transform_user_rows,  # Use shared transformer
            unique=False,
        )
        return users
    except Exception as e:
        logger.error(e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


async def create_user(
    db: Session, data: UserCreate, organization_id: int, verified=False
):
    firebase_user = None

    try:
        if not verified:
            data.role_id = UserRole.operator.value

        # Create Firebase user with email NOT verified
        try:
            firebase_user: UserRecord = firebase_auth.create_user(
                email=data.email,
                password=data.password,
                email_verified=verified,
            )
        except FirebaseError as firebase_error:
            # Handle specific Firebase authentication errors
            error_code = (
                firebase_error.code if hasattr(firebase_error, "code") else None
            )
            error_message = str(firebase_error)

            logger.error(
                f"Firebase error during user creation: {error_code} - {error_message}"
            )

            # Map Firebase error codes to user-friendly messages. The SDK
            # raises the typed error with code ALREADY_EXISTS, so the string
            # checks alone never match a real duplicate.
            if (
                isinstance(firebase_error, EmailAlreadyExistsError)
                or error_code == "EMAIL_ALREADY_EXISTS"
                or "email-already-exists" in error_message.lower()
            ):
                raise HTTPException(
                    status_code=400,
                    detail="This email address is already registered. "
                    "Please use a different email or try logging in.",
                )
            elif (
                error_code == "INVALID_EMAIL"
                or "invalid-email" in error_message.lower()
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid email address format. "
                    "Please provide a valid email.",
                )
            elif (
                error_code == "WEAK_PASSWORD"
                or "weak-password" in error_message.lower()
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Password is too weak. "
                    "It must be at least 6 characters long.",
                )
            elif (
                error_code == "OPERATION_NOT_ALLOWED"
                or "operation-not-allowed" in error_message.lower()
            ):
                raise HTTPException(
                    status_code=403,
                    detail="User registration is currently disabled. "
                    "Please contact support.",
                )
            else:
                # Generic Firebase error
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to create user account: {error_message}",
                )

        # Create application DB user
        user_db = UserModel(
            uid=firebase_user.uid,
            email=firebase_user.email,
            name=data.name,
            organization_id=organization_id,
            active=True,
        )
        db.add(user_db)
        db.flush()  # Get user_db.id

        await set_role(db, user_id=user_db.id, role_id=data.role_id, auto_commit=False)

        # Create remote storage bucket
        if RemoteStorageController.is_available():
            bucket_name = await ensure_user_bucket_exists(
                user_db.id, db, auto_commit=False
            )
            if not bucket_name:
                raise HTTPException(
                    status_code=500,
                    detail="Failed to create storage bucket for user.",
                )

        # Create subscription user record
        # expiration is set to current time for free plan.
        # Since its non nullable and must have a value
        subscription = UserSubscription(
            plan_id=SubscriptionUserStatus.FREE,
            user_id=user_db.id,
            expiration=SubscriptionService.get_current_datetime(),
        )
        db.add(subscription)

        # Create storage usage record with free plan quota
        storage_usage = UserStorageUsage(
            user_id=user_db.id,
            storage_usage_bytes=0,
            storage_quota_bytes=StorageQuota.FREE * StorageSize.GB,
        )
        db.add(storage_usage)

        # Commit all changes
        db.commit()

        # Refresh user_db to load relationships (especially organization)
        db.refresh(user_db)

        # Add role_id for response (if needed)
        user_db.__dict__["role_id"] = data.role_id

        # Send verification email if user is not verified
        if not verified:
            try:
                AuthEmailService.send_verification_email(data.email)
                logger.info(f"Verification email sent to {data.email}")
            except Exception as email_error:
                logger.error(
                    f"Failed to send verification email: {email_error}", exc_info=True
                )
                # Don't fail user creation if email fails
                # The user can request a resend later

        return {
            "user": User.from_orm(user_db),
        }

    except HTTPException:
        # Re-raise HTTPException as-is (from Firebase error handling)
        db.rollback()
        if firebase_user:
            try:
                firebase_auth.delete_user(firebase_user.uid)
                logger.info(f"Cleaned up Firebase user: {firebase_user.uid}")
            except Exception as cleanup_error:
                logger.error(f"Failed to cleanup Firebase user: {cleanup_error}")
        raise
    except Exception as e:
        logger.error(f"Failed to create user: {e}", exc_info=True)

        # Rollback database
        db.rollback()

        # Cleanup Firebase user if created
        if firebase_user:
            try:
                firebase_auth.delete_user(firebase_user.uid)
                logger.info(f"Cleaned up Firebase user: {firebase_user.uid}")
            except Exception as cleanup_error:
                logger.error(f"Failed to cleanup Firebase user: {cleanup_error}")

        # Return appropriate error
        if isinstance(e, ValueError):
            raise HTTPException(status_code=400, detail=str(e))
        raise HTTPException(status_code=500, detail="Failed to create user")


async def update_user(
    db: Session, user_id: int, data: UserUpdate, organization_id: int
):
    try:
        # update application db user
        user_db = (
            db.query(UserModel)
            .filter(
                UserModel.active.is_(True),
                UserModel.id == user_id,
                UserModel.organization_id == organization_id,
            )
            .first()
        )
        assert user_db is not None, "User not found"
        user_data = data.dict(exclude_unset=True)
        role_id = user_data.pop("role_id", None)
        for key, value in user_data.items():
            setattr(user_db, key, value)
        if role_id is not None:
            await set_role(db, user_id=user_db.id, role_id=role_id, auto_commit=False)
            user_db.__dict__["role_id"] = role_id

        # create firebase user
        firebase_auth.update_user(user_db.uid, email=data.email)

        # Sync email to Stripe customer if user has a subscription account
        if data.email:
            import stripe

            from studio.app.common.core.subscription.checkout_service import (
                CheckoutService,
            )

            try:
                stripe_account = CheckoutService.get_subscription_account(
                    db, user_db.id
                )
                if stripe_account:
                    stripe.Customer.modify(
                        stripe_account.provider_customer_id,
                        email=data.email,
                    )
                    logger.info(
                        f"Synced email to Stripe customer "
                        f"{stripe_account.provider_customer_id} "
                        f"for user {user_db.id}"
                    )
            except stripe.error.StripeError as e:
                logger.warning(
                    f"Failed to sync email to Stripe for user {user_db.id}: {e}"
                )

        db.commit()

        # Refresh user_db to ensure relationships are loaded
        db.refresh(user_db)

        return User.from_orm(user_db)
    except AssertionError as e:
        logger.error(e, exc_info=True)
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


_UniqueRow = TypeVar("_UniqueRow")


def _insert_or_reselect(
    db: Session, row: _UniqueRow, model: Type[_UniqueRow], user_id: int
) -> _UniqueRow:
    """Insert ``row`` (a UserSubscription or UserStorageUsage instance) inside a
    SAVEPOINT. Both tables are unique on user_id, so a concurrent writer can win
    the race; on conflict, re-select and return the existing row instead of
    surfacing a 500."""
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
        return row
    except IntegrityError:
        existing = (
            db.query(model).filter(model.user_id == user_id).with_for_update().first()
        )
        if existing is None:
            raise
        return existing


async def update_user_subscription_admin(
    db: Session,
    user_id: int,
    data: UserSubscriptionUpdate,
    admin_user: User,
) -> User:
    """Admin-only: directly update a user's subscription plan,
    expiration, and storage quota.
    This bypasses Stripe and modifies the database directly.
    All changes are recorded in the subscription_audit_log."""
    try:
        user_db = (
            db.query(UserModel)
            .filter(
                UserModel.active.is_(True),
                UserModel.id == user_id,
                UserModel.organization_id == admin_user.organization.id,
            )
            .first()
        )
        if user_db is None:
            raise HTTPException(status_code=404, detail="User not found")

        if data.plan_id not in (SubscriptionPlanIds.FREE, SubscriptionPlanIds.PREMIUM):
            raise HTTPException(
                status_code=400, detail=f"Invalid plan_id: {data.plan_id}"
            )
        if data.plan_id == SubscriptionPlanIds.PREMIUM and data.expiration is None:
            raise HTTPException(
                status_code=400, detail="expiration is required for the premium plan"
            )

        subscription = (
            db.query(UserSubscription)
            .filter(UserSubscription.user_id == user_id)
            .first()
        )
        subscription_existed = subscription is not None

        storage = (
            db.query(UserStorageUsage)
            .filter(UserStorageUsage.user_id == user_id)
            .first()
        )
        storage_existed = storage is not None

        # These rows should already exist from signup provisioning; the admin
        # path materializing them repairs a state that "shouldn't happen", so
        # surface it in case rows are going missing from a systemic cause.
        if not subscription_existed or not storage_existed:
            missing = [
                name
                for name, existed in (
                    ("subscription_users", subscription_existed),
                    ("user_storage_usage", storage_existed),
                )
                if not existed
            ]
            logger.warning(
                "Admin subscription update creating missing %s row(s) for user %s",
                ", ".join(missing),
                user_id,
            )

        # Capture old values before applying changes.
        # A missing row is recorded as None so the audit reflects that the
        # record was created rather than edited.
        old_plan_id = None
        old_expiration_str = None
        if subscription_existed:
            old_plan_id = subscription.plan_id
            # Normalize expiration to UTC ISO string for consistent audit format
            if subscription.expiration:
                old_exp = subscription.expiration
                if old_exp.tzinfo is None:
                    old_exp = old_exp.replace(tzinfo=timezone.utc)
                old_expiration_str = old_exp.isoformat()

        old_value = SubscriptionAuditSnapshot(
            plan_id=old_plan_id,
            expiration=old_expiration_str,
            storage_quota_bytes=(
                storage.storage_quota_bytes if storage_existed else None
            ),
        )

        # Apply changes
        # For Free plan, expiration is not meaningful — default to now
        expiration = data.expiration or datetime.now(timezone.utc)
        # This admin repair path does not provision an S3 bucket; it only fixes
        # subscription/quota rows. A user missing these rows in practice already
        # has a bucket from signup. Add ensure_user_bucket_exists here if that
        # assumption ever stops holding.
        if not subscription_existed:
            subscription = _insert_or_reselect(
                db,
                UserSubscription(
                    user_id=user_id, plan_id=data.plan_id, expiration=expiration
                ),
                UserSubscription,
                user_id,
            )
        subscription.plan_id = data.plan_id
        subscription.expiration = expiration
        subscription.scheduled_downgrade = False

        if not storage_existed:
            storage = _insert_or_reselect(
                db,
                UserStorageUsage(
                    user_id=user_id,
                    storage_usage_bytes=0,
                    storage_quota_bytes=data.storage_quota_bytes,
                ),
                UserStorageUsage,
                user_id,
            )
        storage.storage_quota_bytes = data.storage_quota_bytes

        # Write audit log
        new_value = SubscriptionAuditSnapshot(
            plan_id=data.plan_id,
            expiration=expiration.isoformat(),
            storage_quota_bytes=data.storage_quota_bytes,
        )
        audit_log = SubscriptionAuditLog(
            user_id=user_id,
            changed_by=admin_user.id,
            old_value=old_value.dict(),
            new_value=new_value.dict(),
            reason=data.reason,
        )
        db.add(audit_log)

        db.commit()
        return await get_user_with_context(db, user_id)

    except HTTPException as e:
        db.rollback()
        logger.warning(
            "Subscription update rejected for user %s: [%s] %s",
            user_id,
            e.status_code,
            e.detail,
        )
        raise
    except Exception as e:
        db.rollback()
        logger.error(
            "Unexpected error updating subscription for user %s: %s",
            user_id,
            e,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(e))


async def update_password(
    db: Session,
    user_id: int,
    data: UserPasswordUpdate,
    organization_id: int,
):
    user = await get_user(db, user_id, organization_id)
    await authenticate_user(
        db, data=UserAuth(email=user.email, password=data.old_password)
    )
    try:
        user = firebase_auth.update_user(user.uid, password=data.new_password)
        return True
    except Exception as e:
        logger.error(e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


async def delete_user(db: Session, user_id: int, organization_id: int) -> bool:
    """
    Delete user with proper ordering and recovery support.

    Deletion order (Firebase FIRST to prevent orphaned accounts):
    1. Firebase account (hardest to reverse, must be first)
    2. Stripe subscription (reversible, can fail gracefully)
    3. S3 bucket (with cleanup queue fallback)
    4. Workspaces (soft-delete)
    5. Mark user inactive
    """
    deletion_record = None

    try:
        user_db: User = (
            db.query(UserModel)
            .filter(
                UserModel.active.is_(True),
                UserModel.id == user_id,
                UserModel.organization_id == organization_id,
            )
            .first()
        )
        if user_db is None:
            raise HTTPException(status_code=404, detail="User not found")

        # Create deletion record for recovery tracking
        deletion_record = UserDeletionRecord(
            user_id=user_id,
            user_uid=user_db.uid,
            step=DeletionStep.STARTED.value,
            status=DeletionStatus.IN_PROGRESS.value,
            started_at=get_current_datetime(),
        )
        db.add(deletion_record)
        db.commit()

        # ----------------------------------------
        # Step 1: Delete Firebase FIRST (two-phase commit)
        # ----------------------------------------
        try:
            # Phase 1: Mark intent before calling Firebase
            deletion_record.step = DeletionStep.FIREBASE_PENDING.value
            db.commit()

            # Phase 2: Actually delete Firebase account
            firebase_auth.delete_user(user_db.uid)

            # Phase 3: Mark Firebase as deleted
            deletion_record.step = DeletionStep.FIREBASE_DELETED.value
            db.commit()

        except UserNotFoundError:
            logger.info(
                f"Firebase user {user_db.uid} already deleted, "
                f"continuing cleanup for user {user_id}"
            )
            deletion_record.step = DeletionStep.FIREBASE_DELETED.value
            db.commit()

        except FirebaseError as e:
            deletion_record.error = str(e)
            deletion_record.status = DeletionStatus.FAILED.value
            db.commit()
            logger.error(f"Firebase deletion failed for user " f"{user_id}: {e}")
            raise HTTPException(
                status_code=400,
                detail=f"Firebase deletion failed: {e}",
            )
        except Exception as e:
            # DB commit failed after Firebase deletion - critical state
            logger.critical(
                f"CRITICAL: Firebase may be deleted for user {user_id} "
                f"but DB commit failed. Manual recovery required. Error: {e}"
            )
            raise

        # ----------------------------------------
        # Step 2: Cancel Stripe subscription (reversible)
        # ----------------------------------------
        try:
            SubscriptionService._ensure_stripe_initialized()
            await StripeService.handle_cancel_user_subscription(
                db, user_db, immediate=True
            )
            deletion_record.step = DeletionStep.STRIPE_CANCELLED.value
            db.commit()
        except Exception as e:
            # Log but continue - Stripe will auto-cancel eventually
            logger.warning(f"Stripe cancellation failed for user {user_id}: {e}")

        # ----------------------------------------
        # Step 3: Delete S3 bucket
        # ----------------------------------------
        try:
            if RemoteStorageController.is_available():
                async with RemoteStorageSimpleWriter(
                    user_db.remote_bucket_name
                ) as remote_storage_controller:
                    await remote_storage_controller.delete_bucket(force_delete=True)
            deletion_record.step = DeletionStep.S3_DELETED.value
            db.commit()
        except Exception as e:
            # S3 failed after Firebase deleted - log for cleanup
            logger.error(
                f"S3 deletion failed for user {user_id}, "
                f"bucket: {user_db.remote_bucket_name}. Error: {e}"
            )

        # ----------------------------------------
        # Step 4: Soft-delete workspaces
        # ----------------------------------------
        workspaces = (
            db.query(Workspace)
            .filter(
                Workspace.user_id == user_id,
                Workspace.deleted.is_(False),
            )
            .all()
        )
        for ws in workspaces:
            try:
                await WorkspaceService.initiate_workspace_deletion(
                    db, user_db.remote_bucket_name, ws.id, user_id
                )
            except Exception as e:
                logger.warning(f"Workspace {ws.id} deletion failed: {e}")

        deletion_record.step = DeletionStep.WORKSPACES_DELETED.value
        db.commit()

        # Clean up user preferences
        db.query(UserPreferences).filter(UserPreferences.user_id == user_id).delete(
            synchronize_session=False
        )

        # ----------------------------------------
        # Step 5: Mark user inactive
        # ----------------------------------------
        user_db.active = False
        deletion_record.step = DeletionStep.COMPLETED.value
        deletion_record.status = DeletionStatus.COMPLETED.value
        deletion_record.completed_at = get_current_datetime()
        db.commit()

        return True

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"User deletion failed for user {user_id}: {e}", exc_info=True)
        if deletion_record:
            deletion_record.error = str(e)
            deletion_record.status = DeletionStatus.FAILED.value
            try:
                db.commit()
            except Exception:
                pass
        raise HTTPException(status_code=400, detail=str(e))


async def check_firebase_account_exists(uid: str) -> bool:
    """Check if a Firebase account exists for the given UID."""
    try:
        firebase_auth.get_user(uid)
        return True
    except UserNotFoundError:
        return False
    except Exception as e:
        logger.error(f"Error checking Firebase account {uid}: {e}")
        raise


async def recover_incomplete_deletions(db: Session) -> int:
    """
    Resume incomplete user deletions (older than 1 hour).
    Returns the number of recovered deletions.
    """
    from datetime import timedelta

    cutoff_time = get_current_datetime() - timedelta(hours=1)

    incomplete = (
        db.query(UserDeletionRecord)
        .filter(
            UserDeletionRecord.status == DeletionStatus.IN_PROGRESS.value,
            UserDeletionRecord.started_at < cutoff_time,
        )
        .all()
    )

    recovered_count = 0
    for record in incomplete:
        try:
            # Handle firebase_pending: check if Firebase account still exists
            if record.step == DeletionStep.FIREBASE_PENDING.value:
                firebase_exists = await check_firebase_account_exists(record.user_uid)
                if not firebase_exists:
                    record.step = DeletionStep.FIREBASE_DELETED.value
                    db.commit()
                else:
                    # Firebase still exists but deletion was attempted
                    # Mark as failed for manual review
                    record.status = DeletionStatus.FAILED.value
                    record.error = "Firebase account still exists after pending state"
                    db.commit()
                    continue

            # Resume deletion from current step
            await resume_deletion_from_step(record, db)
            recovered_count += 1

        except Exception as e:
            logger.error(
                f"Error recovering deletion for user {record.user_id}: {e}",
                exc_info=True,
            )
            record.error = str(e)
            record.status = DeletionStatus.FAILED.value
            db.commit()

    return recovered_count


def _get_step_order(step: DeletionStep) -> int:
    """Get the numeric order of a deletion step for comparison."""
    step_order = {
        DeletionStep.STARTED: 0,
        DeletionStep.FIREBASE_PENDING: 1,
        DeletionStep.FIREBASE_DELETED: 2,
        DeletionStep.STRIPE_CANCELLED: 3,
        DeletionStep.S3_DELETED: 4,
        DeletionStep.WORKSPACES_DELETED: 5,
        DeletionStep.COMPLETED: 6,
    }
    return step_order.get(step, 0)


async def resume_deletion_from_step(record: UserDeletionRecord, db: Session) -> bool:
    """Resume user deletion from the last completed step."""
    user_db = db.query(UserModel).filter(UserModel.id == record.user_id).first()

    if user_db is None:
        record.status = DeletionStatus.COMPLETED.value
        record.completed_at = get_current_datetime()
        db.commit()
        return True

    step = DeletionStep(record.step)
    current_order = _get_step_order(step)

    # Skip steps that are already completed
    if step in (DeletionStep.STARTED, DeletionStep.FIREBASE_PENDING):
        # Should have been handled by caller
        pass

    if current_order < _get_step_order(DeletionStep.STRIPE_CANCELLED):
        try:
            SubscriptionService._ensure_stripe_initialized()
            await StripeService.handle_cancel_user_subscription(
                db, user_db, immediate=True
            )
            record.step = DeletionStep.STRIPE_CANCELLED.value
            db.commit()
        except Exception as e:
            logger.warning(f"Stripe cancellation in recovery failed: {e}")

    if current_order < _get_step_order(DeletionStep.S3_DELETED):
        try:
            if RemoteStorageController.is_available():
                async with RemoteStorageSimpleWriter(
                    user_db.remote_bucket_name
                ) as remote_storage_controller:
                    await remote_storage_controller.delete_bucket(force_delete=True)
            record.step = DeletionStep.S3_DELETED.value
            db.commit()
        except Exception as e:
            logger.warning(f"S3 deletion in recovery failed: {e}")

    if current_order < _get_step_order(DeletionStep.WORKSPACES_DELETED):
        workspaces = (
            db.query(Workspace)
            .filter(Workspace.user_id == record.user_id, Workspace.deleted.is_(False))
            .all()
        )
        for ws in workspaces:
            try:
                await WorkspaceService.initiate_workspace_deletion(
                    db, user_db.remote_bucket_name, ws.id, record.user_id
                )
            except Exception as e:
                logger.warning(f"Workspace deletion in recovery failed: {e}")
        record.step = DeletionStep.WORKSPACES_DELETED.value
        db.commit()

    # Clean up user preferences
    db.query(UserPreferences).filter(UserPreferences.user_id == record.user_id).delete(
        synchronize_session=False
    )

    # Final step: mark user inactive
    user_db.active = False
    record.step = DeletionStep.COMPLETED.value
    record.status = DeletionStatus.COMPLETED.value
    record.completed_at = get_current_datetime()
    db.commit()

    return True
