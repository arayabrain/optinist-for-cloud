"""
Storage usage tracking, calculation, and reconciliation.

Extracted from cloud_utils.py for module cohesion.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlmodel import select

from studio.app.common.core.logger import AppLogger
from studio.app.common.core.subscription.constants import (
    PlanName,
    StorageQuota,
    StorageReconciliation,
    StorageScanTriggers,
    StorageSize,
    SubscriptionPeriods,
    SubscriptionType,
)
from studio.app.common.core.subscription.subscription_service import SubscriptionService
from studio.app.common.db.database import session_scope
from studio.app.common.models import SubscriptionPlans
from studio.app.common.models import User as UserModel
from studio.app.common.models import UserStorageUsage, UserSubscription

logger = AppLogger.get_logger()


class StorageOwnerInactive(Exception):
    """No active user owns the storage row being reconciled.

    Raised rather than returned as 0 so a deleted account's row is left alone
    instead of being reconciled to "0 bytes", which reads as a real measurement.
    """

    def __init__(self, user_id: int):
        super().__init__(f"no active user row for user {user_id}")
        self.user_id = user_id


def _get_fallback_storage_quota(user_id: int) -> Dict[str, Any]:
    """
    Get fallback storage quota when storage usage table doesn't exist.
    Tries to determine quota based on user's subscription plan.
    """
    try:
        with session_scope() as db:
            statement = (
                select(SubscriptionPlans.name.label("plan_name"))
                .select_from(UserModel)
                .outerjoin(
                    UserSubscription,
                    (UserModel.id == UserSubscription.user_id)
                    & (
                        UserSubscription.expiration
                        > SubscriptionService.get_current_datetime()
                    ),
                )
                .outerjoin(
                    SubscriptionPlans,
                    UserSubscription.plan_id == SubscriptionPlans.id,
                )
                .where(
                    UserModel.id == user_id,
                    UserModel.active.is_(True),
                )
            )
            result = db.execute(statement).first()

        if result and result.plan_name:
            plan_name = result.plan_name
            subscription_type = (
                SubscriptionType.PREMIUM
                if plan_name == PlanName.PREMIUM
                else SubscriptionType.FREE
            )
        else:
            plan_name = PlanName.FREE
            subscription_type = SubscriptionType.FREE

        if subscription_type == SubscriptionType.PREMIUM:
            default_quota_bytes = StorageQuota.PREMIUM * StorageSize.GB
            logger.debug(
                f"Using paid plan quota for user {user_id} "
                f"({plan_name}): {StorageQuota.PREMIUM}GB"
            )
        else:
            default_quota_bytes = StorageQuota.FREE * StorageSize.GB
            logger.debug(
                f"Using free plan quota for user {user_id} "
                f"({plan_name}): {StorageQuota.FREE}GB"
            )

    except Exception as e:
        logger.warning(
            f"Error determining subscription quota for "
            f"user {user_id}: {e}, using free plan"
        )
        default_quota_bytes = StorageQuota.FREE * StorageSize.GB

    return {
        "user_id": user_id,
        "storage_usage_bytes": 0,
        "storage_quota_bytes": default_quota_bytes,
        "storage_usage_percent": 0.0,
        "last_updated": None,
    }


def get_user_storage_usage(
    user_id: int,
) -> Optional[Dict[str, Any]]:
    """
    Get storage usage information for a user.
    Falls back to default quota if storage table doesn't exist.
    """
    import os

    skip_checks_value = os.environ.get("SKIP_STORAGE_CHECKS", "")
    if skip_checks_value.lower() == "true":
        logger.debug(
            f"Skipping storage usage lookup for user " f"{user_id} (test mode)"
        )
        fallback = _get_fallback_storage_quota(user_id)
        fallback["last_updated"] = SubscriptionService.get_current_datetime()
        return fallback

    try:
        with session_scope() as db:
            try:
                query_result = db.execute(
                    select(UserStorageUsage).where(UserStorageUsage.user_id == user_id)
                )
                result_row = query_result.first()
                storage_usage = result_row[0] if result_row else None

                if storage_usage:
                    result_dict = {
                        "user_id": storage_usage.user_id,
                        "storage_usage_bytes": (storage_usage.storage_usage_bytes),
                        "storage_quota_bytes": (storage_usage.storage_quota_bytes),
                        "storage_usage_percent": (storage_usage.storage_usage_percent),
                        "last_updated": (storage_usage.last_updated),
                    }

                    logger.info(
                        f"Retrieved storage usage for user "
                        f"{user_id}: storage_usage="
                        f"{result_dict.get('storage_usage_bytes')}"
                        f", storage_quota="
                        f"{result_dict.get('storage_quota_bytes')}"
                        f", storage_usage_percent="
                        f"{result_dict.get('storage_usage_percent')}%"
                    )
                    return result_dict
                else:
                    logger.warning(
                        f"No storage usage data found for "
                        f"user {user_id}, using defaults"
                    )
                    return _get_fallback_storage_quota(user_id)

            except Exception as orm_error:
                logger.warning(
                    f"UserStorageUsage table not accessible:"
                    f" {orm_error}, using default quota"
                )
                return _get_fallback_storage_quota(user_id)

    except Exception as e:
        logger.warning(
            f"Failed to get storage usage for user " f"{user_id}: {e}, using defaults"
        )
        return _get_fallback_storage_quota(user_id)


def update_user_storage_usage(user_id: int, new_usage_bytes: int) -> bool:
    """
    Update storage usage for a user.
    Returns True if successful or if table doesn't exist
    (fallback scenario).

    Writes via a Core UPDATE keyed on user_id so concurrent writers (full
    scan, incremental updates from multiple processes) do not race on an ORM
    load-mutate-flush, which raises a stale-data error when a concurrent commit
    leaves the target row unchanged at flush time. The success is logged only
    after the commit lands.
    """
    try:
        with session_scope() as db:
            try:
                # Existence is checked with a SELECT rather than the UPDATE
                # rowcount: MySQL reports rows *changed*, so an update to an
                # identical value returns 0 even though the row exists.
                exists = (
                    db.execute(
                        select(UserStorageUsage.id).where(
                            UserStorageUsage.user_id == user_id
                        )
                    ).first()
                    is not None
                )

                if exists:
                    db.execute(
                        update(UserStorageUsage)
                        .where(UserStorageUsage.user_id == user_id)
                        .values(
                            storage_usage_bytes=new_usage_bytes,
                            last_updated=SubscriptionService.get_current_datetime(),
                        )
                    )
                else:
                    # No row for this user yet: create it with a plan-based quota.
                    statement = (
                        select(SubscriptionPlans.name.label("plan_name"))
                        .select_from(UserModel)
                        .outerjoin(
                            UserSubscription,
                            (UserModel.id == UserSubscription.user_id)
                            & (
                                UserSubscription.expiration
                                > SubscriptionService.get_current_datetime()
                            ),
                        )
                        .outerjoin(
                            SubscriptionPlans,
                            UserSubscription.plan_id == SubscriptionPlans.id,
                        )
                        .where(
                            UserModel.id == user_id,
                            UserModel.active.is_(True),
                        )
                    )
                    plan_result = db.execute(statement).first()

                    if plan_result and plan_result.plan_name == PlanName.PREMIUM:
                        default_quota = StorageQuota.PREMIUM * StorageSize.GB
                    else:
                        default_quota = StorageQuota.FREE * StorageSize.GB

                    new_storage_usage = UserStorageUsage(
                        user_id=user_id,
                        storage_usage_bytes=new_usage_bytes,
                        storage_quota_bytes=default_quota,
                    )
                    db.add(new_storage_usage)

            except (ProgrammingError, OperationalError) as orm_error:
                # Benign fallback for a missing/inaccessible table only; genuine
                # write errors (StaleDataError, etc.) propagate and return False.
                logger.warning(
                    f"UserStorageUsage table not accessible:"
                    f" {orm_error}, skipping storage update"
                )
                return True

        logger.info(
            f"Updated storage usage for user " f"{user_id}: {new_usage_bytes} bytes"
        )
        return True

    except Exception as e:
        logger.warning(f"Failed to update storage usage for " f"user {user_id}: {e}")
        return False


def increment_user_storage(user_id: int, bytes_added: int) -> bool:
    """
    Increment storage usage by a specific amount
    (e.g., after uploading files).

    This is much more efficient than recalculating total
    storage from S3. Use this when you know exactly how many
    bytes were added.

    Args:
        user_id: User ID to update
        bytes_added: Number of bytes to add to current usage

    Returns:
        True if successful, False otherwise
    """
    if bytes_added <= 0:
        logger.debug(
            f"Skipping storage increment for user " f"{user_id}: {bytes_added} bytes"
        )
        return True

    try:
        from sqlalchemy import update

        with session_scope() as db:
            try:
                query_result = db.execute(
                    select(UserStorageUsage).where(UserStorageUsage.user_id == user_id)
                )
                result_row = query_result.first()
                existing_usage = result_row[0] if result_row else None

                if existing_usage:
                    stmt = (
                        update(UserStorageUsage)
                        .where(UserStorageUsage.user_id == user_id)
                        .values(
                            storage_usage_bytes=(
                                UserStorageUsage.storage_usage_bytes + bytes_added
                            ),
                            delta_since_last_scan=(
                                UserStorageUsage.delta_since_last_scan + bytes_added
                            ),
                            last_updated=(SubscriptionService.get_current_datetime()),
                        )
                    )
                    db.execute(stmt)
                    new_total = existing_usage.storage_usage_bytes + bytes_added
                    new_delta = existing_usage.delta_since_last_scan + bytes_added
                    logger.info(
                        f"Incremented storage for user "
                        f"{user_id}: +{bytes_added:,} bytes "
                        f"(new total: {new_total:,} bytes, "
                        f"delta since scan: "
                        f"{new_delta:,} bytes)"
                    )
                else:
                    logger.warning(
                        f"No storage record found for user "
                        f"{user_id}, creating with initial "
                        f"value: {bytes_added} bytes"
                    )
                    update_user_storage_usage(user_id, bytes_added)

                return True

            except Exception as orm_error:
                logger.warning(
                    f"UserStorageUsage table not accessible:"
                    f" {orm_error}, skipping storage "
                    f"increment"
                )
                return True

    except Exception as e:
        logger.error(f"Failed to increment storage for " f"user {user_id}: {e}")
        return False


def decrement_user_storage(user_id: int, bytes_removed: int) -> bool:
    """
    Decrement storage usage by a specific amount
    (e.g., after deleting files).

    This is much more efficient than recalculating total
    storage from S3. Use this when you know exactly how many
    bytes were removed. Ensures storage never goes below 0.

    Args:
        user_id: User ID to update
        bytes_removed: Number of bytes to subtract

    Returns:
        True if successful, False otherwise
    """
    if bytes_removed <= 0:
        logger.debug(
            f"Skipping storage decrement for user " f"{user_id}: {bytes_removed} bytes"
        )
        return True

    try:
        from sqlalchemy import func, update

        with session_scope() as db:
            try:
                stmt = (
                    update(UserStorageUsage)
                    .where(UserStorageUsage.user_id == user_id)
                    .values(
                        storage_usage_bytes=func.greatest(
                            0,
                            UserStorageUsage.storage_usage_bytes - bytes_removed,
                        ),
                        delta_since_last_scan=(
                            UserStorageUsage.delta_since_last_scan + bytes_removed
                        ),
                        last_updated=(SubscriptionService.get_current_datetime()),
                    )
                )
                result = db.execute(stmt)

                if result.rowcount > 0:
                    query_result = db.execute(
                        select(UserStorageUsage).where(
                            UserStorageUsage.user_id == user_id
                        )
                    )
                    result_row = query_result.first()
                    if result_row:
                        new_total = result_row[0].storage_usage_bytes
                        logger.info(
                            f"Decremented storage for user "
                            f"{user_id}: -{bytes_removed:,} "
                            f"bytes (new total: "
                            f"{new_total:,} bytes)"
                        )
                else:
                    logger.warning(
                        f"No storage record found for user " f"{user_id} to decrement"
                    )

                return True

            except Exception as orm_error:
                logger.warning(
                    f"UserStorageUsage table not accessible:"
                    f" {orm_error}, skipping storage "
                    f"decrement"
                )
                return True

    except Exception as e:
        logger.error(f"Failed to decrement storage for " f"user {user_id}: {e}")
        return False


async def get_current_user_storage_usage(user_id: int, force_live: bool = False) -> int:
    """
    Get current storage usage with hybrid caching approach.

    Args:
        user_id: User ID to check storage for
        force_live: If True, always calculate live usage

    Returns:
        Current storage usage in bytes
    """
    try:
        if not force_live:
            storage_info = get_user_storage_usage(user_id)
            if storage_info and _is_storage_data_fresh(
                storage_info,
                SubscriptionPeriods.MAX_CACHE_AGE_MINUTES,
            ):
                logger.debug(f"Using cached storage data for " f"user {user_id}")
                return storage_info["storage_usage_bytes"]
            else:
                logger.info(
                    f"Storage data for user {user_id} is "
                    f"stale or missing, calculating live"
                )

        live_usage = await _calculate_live_storage_usage(user_id)
        update_user_storage_usage(user_id, live_usage)

        return live_usage

    except StorageOwnerInactive:
        # The storage row outlives the account, so this is not a failed read
        logger.info(f"Skipped live storage usage for inactive user {user_id}")
        storage_info = get_user_storage_usage(user_id)
        return storage_info.get("storage_usage_bytes", 0) if storage_info else 0
    except Exception as e:
        logger.error(f"Failed to get current storage usage for " f"user {user_id}: {e}")
        storage_info = get_user_storage_usage(user_id)
        return storage_info.get("storage_usage_bytes", 0) if storage_info else 0


def _is_storage_data_fresh(storage_info: Dict, max_cache_age_minutes: int) -> bool:
    """
    Check if storage data is fresh enough to use.

    Args:
        storage_info: Storage info from database
        max_cache_age_minutes: Maximum age in minutes

    Returns:
        True if data is fresh enough
    """
    try:
        last_updated = storage_info.get("last_updated")
        if not last_updated:
            return False

        if isinstance(last_updated, str):
            last_updated = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
        elif isinstance(last_updated, datetime):
            if last_updated.tzinfo is None:
                last_updated = last_updated.replace(tzinfo=timezone.utc)

        age_minutes = (
            SubscriptionService.get_current_datetime() - last_updated
        ).total_seconds() / 60
        return age_minutes <= max_cache_age_minutes

    except Exception as e:
        logger.warning(f"Failed to check storage data freshness: {e}")
        return False


async def _calculate_live_storage_usage(
    user_id: int,
) -> int:
    """
    Calculate live storage usage for a user.
    Detects S3 vs local environment and uses appropriate method.

    Args:
        user_id: User ID to check storage for

    Returns:
        Current storage usage in bytes
    """
    try:
        import os

        from studio.app.common.core.storage.remote_storage_controller import (
            RemoteStorageType,
        )

        remote_storage_type = RemoteStorageType.get_activated_type()

        if remote_storage_type == RemoteStorageType.S3:
            from studio.app.common.core.users.crud_users import get_user_with_context

            with session_scope() as db:
                try:
                    user = await get_user_with_context(db, user_id)
                except HTTPException as lookup_error:
                    if lookup_error.status_code != 404:
                        raise
                    # Deleting an account leaves its storage row behind, so the
                    # reconciliation job still selects it. Routine, not a fault.
                    raise StorageOwnerInactive(user_id) from None
                if (
                    user
                    and user.attributes
                    and user.attributes.get("remote_bucket_name")
                ):
                    user_bucket_name = user.attributes.get("remote_bucket_name")
                else:
                    user_bucket_name = os.environ.get("S3_DEFAULT_BUCKET_NAME")
                    logger.warning(
                        f"User {user_id} has no personal "
                        f"bucket, using shared bucket: "
                        f"{user_bucket_name}"
                    )

            from studio.app.common.core.cloud.s3_storage_monitor import S3StorageMonitor

            monitor = S3StorageMonitor(user_bucket_name)
            return await monitor.get_user_s3_storage_size_streaming(user_id)
        else:
            return await _calculate_local_user_storage(user_id)

    except StorageOwnerInactive:
        raise
    except Exception as e:
        # Use repr(e) so the exception type is visible even when str(e) is empty.
        logger.error(
            f"Failed to calculate live storage usage for user {user_id}: {e!r}",
            exc_info=True,
        )
        return 0


async def _calculate_local_user_storage(
    user_id: int,
) -> int:
    """
    Calculate total local storage usage for a user across
    all their workspaces.

    Args:
        user_id: User ID to check storage for

    Returns:
        Total storage size in bytes
    """
    import os

    skip_checks_value = os.environ.get("SKIP_STORAGE_CHECKS", "")
    if skip_checks_value.lower() == "true":
        logger.debug(f"Skipping storage calculation for user " f"{user_id} (test mode)")
        return 0

    try:
        from studio.app.common.core.workspace.workspace_services import WorkspaceService

        with session_scope() as db:
            workspace_ids = WorkspaceService.get_user_accessible_workspace_ids(
                db, user_id
            )

        total_usage = 0
        import os

        from studio.app.common.core.utils.file_reader import get_folder_size
        from studio.app.dir_path import DIRPATH

        for workspace_id in workspace_ids:
            input_path = os.path.join(DIRPATH.INPUT_DIR, str(workspace_id))
            if os.path.exists(input_path):
                input_size = get_folder_size(input_path)
                total_usage += input_size
                logger.info(
                    f"User {user_id} workspace "
                    f"{workspace_id} input: "
                    f"{input_size} bytes"
                )

            output_path = os.path.join(DIRPATH.OUTPUT_DIR, str(workspace_id))
            if os.path.exists(output_path):
                output_size = get_folder_size(output_path)
                total_usage += output_size

        logger.info(
            f"Calculated local storage size for user "
            f"{user_id}: {total_usage:,} bytes"
        )
        return total_usage

    except Exception as e:
        logger.error(
            f"Failed to calculate local storage size for " f"user {user_id}: {e}"
        )
        return 0


async def _should_trigger_full_scan(
    user_id: int,
) -> bool:
    """
    Determine if a full S3 scan is needed for a user.

    Triggers scan if:
    1. Delta since last scan > 5% of current storage
       OR > 200MB
    2. Last scan was > 60 minutes ago (and delta > 0)
    3. Never scanned before (last_full_scan is NULL)

    Args:
        user_id: User ID to check

    Returns:
        True if full scan needed, False otherwise
    """
    try:
        storage_info = get_user_storage_usage(user_id)
        if not storage_info:
            return False

        with session_scope() as db:
            query_result = db.execute(
                select(UserStorageUsage).where(UserStorageUsage.user_id == user_id)
            )
            result_row = query_result.first()
            if not result_row:
                return False

            storage_record = result_row[0]
            delta = storage_record.delta_since_last_scan
            last_scan = storage_record.last_full_scan
            current_storage = storage_record.storage_usage_bytes

            if last_scan is None:
                logger.info(
                    f"User {user_id} has never been "
                    f"scanned, triggering initial scan"
                )
                return True

            if delta > 0:
                delta_percent = (
                    (delta / current_storage * 100) if current_storage > 0 else 0
                )

                if (
                    delta_percent > StorageScanTriggers.DELTA_THRESHOLD_PERCENT
                    or delta > StorageScanTriggers.DELTA_THRESHOLD_BYTES
                ):
                    logger.info(
                        f"Delta threshold exceeded for "
                        f"user {user_id}: {delta:,} bytes "
                        f"({delta_percent:.1f}%)"
                    )
                    return True

                if last_scan.tzinfo is None:
                    last_scan = last_scan.replace(tzinfo=timezone.utc)

                time_since_scan = (
                    SubscriptionService.get_current_datetime() - last_scan
                ).total_seconds() / 60

                if time_since_scan > StorageScanTriggers.SCAN_INTERVAL_MINUTES:
                    logger.info(
                        f"Hourly reconciliation needed for "
                        f"user {user_id}: "
                        f"{time_since_scan:.0f} minutes "
                        f"since last scan"
                    )
                    return True

            return False

    except Exception as e:
        logger.error(f"Failed to check if scan needed for " f"user {user_id}: {e}")
        return False


async def _perform_full_scan_and_reset_delta(
    user_id: int,
) -> None:
    """
    Perform full S3 scan and reset delta counter with
    distributed lock protection.

    Uses MySQL GET_LOCK within a single session so the lock
    persists across the scan and DB update. If another
    process is already scanning this user, returns early.

    Args:
        user_id: User ID to scan
    """
    from sqlalchemy import text, update

    lock_name = (
        f"storage_scan_"
        f"{StorageReconciliation.ADVISORY_LOCK_NAMESPACE}"
        f"_{user_id}"
    )

    try:
        with session_scope() as db:
            lock_result = db.execute(
                text("SELECT GET_LOCK(:lock_name, :timeout)" " as lock_result"),
                {"lock_name": lock_name, "timeout": 0},
            )
            if lock_result.scalar() != 1:
                logger.info(
                    f"Skipping scan for user {user_id}: " f"another process is scanning"
                )
                return

            try:
                actual_storage = await _calculate_live_storage_usage(user_id)
                now = SubscriptionService.get_current_datetime()
                stmt = (
                    update(UserStorageUsage)
                    .where(UserStorageUsage.user_id == user_id)
                    .values(
                        storage_usage_bytes=actual_storage,
                        delta_since_last_scan=0,
                        last_full_scan=now,
                        last_updated=now,
                    )
                )
                db.execute(stmt)

                logger.info(
                    f"Full S3 scan completed for user "
                    f"{user_id}: "
                    f"{actual_storage:,} bytes (delta reset)"
                )
            finally:
                db.execute(
                    text("SELECT RELEASE_LOCK(:lock_name)"),
                    {"lock_name": lock_name},
                )

    except StorageOwnerInactive:
        raise
    except Exception as e:
        logger.error(f"Failed to perform full scan for " f"user {user_id}: {e}")


async def update_user_storage_after_workflow(
    workspace_id: str,
) -> None:
    """
    Update user storage usage after workflow completion.

    Storage is updated incrementally during upload/delete
    operations. This function checks if a full S3 scan is
    needed based on:
    1. Delta since last scan > 5% or > 200MB
    2. OR last scan was > 60 minutes ago

    Args:
        workspace_id: The workspace ID to update storage for
    """
    try:
        from sqlmodel import select

        from studio.app.common import models as common_model
        from studio.app.common.db.database import session_scope

        try:
            workspace_id_int = int(workspace_id)
        except ValueError:
            logger.debug(
                f"Skipping storage update for maintenance " f"workspace: {workspace_id}"
            )
            return

        with session_scope() as db:
            query_result = db.execute(
                select(common_model.Workspace.user_id).where(
                    common_model.Workspace.id == workspace_id_int
                )
            )
            result_row = query_result.first()
            user_id = result_row[0] if result_row else None

            if user_id:
                needs_scan = await _should_trigger_full_scan(user_id)

                if needs_scan:
                    logger.info(
                        f"Triggering full S3 scan for user "
                        f"{user_id} (delta threshold "
                        f"exceeded or hourly check)"
                    )
                    await _perform_full_scan_and_reset_delta(user_id)
                else:
                    logger.debug(
                        f"Skipping S3 scan for user "
                        f"{user_id} (incremental tracking "
                        f"within threshold)"
                    )

    except Exception as e:
        logger.warning(
            f"Failed to update user storage usage after " f"workflow completion: {e}"
        )
