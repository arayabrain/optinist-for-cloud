"""
Storage Reconciliation Background Job

Periodically reconciles user storage usage by comparing incremental tracking
with actual S3 storage. This ensures accuracy and catches any drift from
failed incremental updates.

Runs every 60 minutes to balance accuracy vs. cost/performance.
"""

import asyncio

from sqlalchemy import func, or_
from sqlmodel import select

from studio.app.common.core.cloud.storage_tracking import StorageOwnerInactive
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.mode import MODE
from studio.app.common.core.subscription.constants import StorageReconciliation

logger = AppLogger.get_logger()


class StorageReconciliationJob:
    """
    Background job to reconcile storage usage for all users.

    This job:
    1. Gets all active users with storage records in batches
    2. For each user, does a full S3 scan to get actual storage
    3. Always updates database with fresh S3 value
    4. Logs warning if drift was significant (for monitoring)

    The incremental tracking (upload/delete) should keep storage accurate,
    but this job catches edge cases like:
    - Failed increment/decrement operations
    - Manual S3 changes outside the app
    - Race conditions during concurrent operations

    Note: Since the S3 scan is the expensive operation, we always update
    the database after scanning. The threshold is only used for logging.

    Uses batch processing to prevent OOM when many users need reconciliation.
    """

    @classmethod
    async def run(cls):
        """
        Run storage reconciliation for all users.

        This is an async function that will be scheduled by BackgroundScheduler.
        """
        if MODE.IS_STANDALONE:
            logger.debug("Standalone mode - skipping storage reconciliation")
            return

        logger.info("Starting storage reconciliation job")

        try:
            # Get all users with storage records in batches
            from studio.app.common.db.database import session_scope
            from studio.app.common.models import User as UserModel
            from studio.app.common.models import UserStorageUsage

            # Deleting an account, or registering one that is never verified,
            # leaves a storage row behind whose owner no longer satisfies
            # "active". Those rows can never be scanned - the S3 lookup needs the
            # user's bucket name - and because nothing stamps last_full_scan on
            # them they would stay in the candidate set and be retried every
            # hour, for good. Exclude them here and count them instead.
            needs_scan = or_(
                UserStorageUsage.delta_since_last_scan > 0,
                UserStorageUsage.last_full_scan.is_(None),
            )
            has_active_owner = (
                select(UserModel.id)
                .where(
                    UserModel.id == UserStorageUsage.user_id,
                    UserModel.active.is_(True),
                )
                .exists()
            )

            reconciled_count = 0
            drift_detected_count = 0
            error_count = 0
            offset = 0
            total_users = 0

            # First, get total count for logging
            with session_scope() as db:
                count_result = db.execute(
                    select(func.count())
                    .select_from(UserStorageUsage)
                    .where(needs_scan, has_active_owner)
                )
                scannable_users = count_result.scalar() or 0
                orphan_result = db.execute(
                    select(func.count())
                    .select_from(UserStorageUsage)
                    .where(needs_scan, ~has_active_owner)
                )
                skipped_count = orphan_result.scalar() or 0
                total_users = scannable_users + skipped_count

            logger.info(
                f"Starting reconciliation for {scannable_users} users with "
                f"activity since last scan (processing in batches of "
                f"{StorageReconciliation.BATCH_SIZE})"
                + (
                    f"; skipping {skipped_count} row(s) whose owner is no longer "
                    f"active"
                    if skipped_count
                    else ""
                )
            )

            # Process users in batches to prevent OOM
            while True:
                # Fetch next batch of users
                with session_scope() as db:
                    batch_records = db.execute(
                        select(
                            UserStorageUsage.user_id,
                            UserStorageUsage.storage_usage_bytes,
                            UserStorageUsage.delta_since_last_scan,
                            UserStorageUsage.last_full_scan,
                        )
                        .where(needs_scan, has_active_owner)
                        .order_by(UserStorageUsage.user_id)
                        .limit(StorageReconciliation.BATCH_SIZE)
                        .offset(offset)
                    ).fetchall()

                # Exit if no more users to process
                if not batch_records:
                    break

                logger.info(
                    f"Processing batch "
                    f"{offset // StorageReconciliation.BATCH_SIZE + 1}: "
                    f"{len(batch_records)} users (offset: {offset})"
                )

                # Process each user in the batch
                for row in batch_records:
                    user_id, db_storage, delta, last_scan = row

                    try:
                        # Use the shared scan and reset function
                        from studio.app.common.core.cloud.storage_tracking import (
                            _perform_full_scan_and_reset_delta,
                        )

                        # Get storage before scan for drift logging
                        logger.debug(
                            f"Reconciling user {user_id}: "
                            f"current={db_storage:,} bytes, "
                            f"delta={delta:,} bytes, "
                            f"last_scan={last_scan or 'never'}"
                        )

                        # Perform scan and reset delta
                        await _perform_full_scan_and_reset_delta(user_id)

                        # Get updated storage to log drift
                        with session_scope() as update_db:
                            query_result = update_db.execute(
                                select(UserStorageUsage.storage_usage_bytes).where(
                                    UserStorageUsage.user_id == user_id
                                )
                            )
                            result_row = query_result.first()
                            actual_storage = result_row[0] if result_row else db_storage

                        drift_bytes = abs(actual_storage - db_storage)
                        drift_percent = (
                            (drift_bytes / db_storage * 100) if db_storage > 0 else 0
                        )

                        # Log drift for monitoring
                        if (
                            drift_percent > StorageReconciliation.DRIFT_THRESH_PERCENT
                            or drift_bytes > StorageReconciliation.DRIFT_THRESH_BYTES
                        ):
                            logger.warning(
                                f"Significant storage drift corrected for user "
                                f"{user_id}: "
                                f"DB={db_storage:,} bytes → "
                                f"S3={actual_storage:,} bytes "
                                f"(drift: {drift_bytes:,} bytes, "
                                f"{drift_percent:.1f}%)"
                            )
                            drift_detected_count += 1
                        else:
                            logger.debug(
                                f"Storage reconciled for user {user_id}: "
                                f"{db_storage:,} → {actual_storage:,} bytes "
                                f"(drift: {drift_bytes:,} bytes, {drift_percent:.1f}%)"
                            )

                        reconciled_count += 1

                        # Rate limiting to avoid S3 API throttling and spread load
                        await asyncio.sleep(
                            StorageReconciliation.RATE_LIMIT_DELAY_SECONDS
                        )

                    except StorageOwnerInactive:
                        # Deleting an account leaves its storage row behind. That
                        # is bookkeeping to clean up, not a failure to report,
                        # and the row must keep its last real value rather than
                        # being written down to zero.
                        logger.info(
                            f"Skipped user {user_id}: no active user owns this "
                            f"storage row"
                        )
                        skipped_count += 1
                        continue
                    except Exception as user_error:
                        logger.error(
                            f"Failed to reconcile storage for user {user_id}: "
                            f"{user_error}"
                        )
                        error_count += 1
                        continue

                # Move to next batch
                offset += StorageReconciliation.BATCH_SIZE

                logger.info(
                    f"Batch completed. Progress: "
                    f"{reconciled_count + error_count + skipped_count}/"
                    f"{total_users} users processed"
                )

            logger.info(
                f"Storage reconciliation completed: "
                f"{reconciled_count}/{total_users} users reconciled, "
                f"{drift_detected_count} drifts corrected, "
                f"{error_count} errors, "
                f"{skipped_count} skipped (no active user)"
            )

        except Exception as e:
            logger.error(f"Storage reconciliation job failed: {e}", exc_info=True)

    @classmethod
    async def reconcile_user_storage(cls, user_id: int) -> bool:
        """
        Reconcile storage for a specific user (useful for manual triggers).

        Args:
            user_id: User ID to reconcile

        Returns:
            True if reconciliation successful, False otherwise
        """
        try:
            from studio.app.common.core.cloud.storage_tracking import (
                _calculate_live_storage_usage,
                get_user_storage_usage,
                update_user_storage_usage,
            )

            # Get current database value
            storage_info = get_user_storage_usage(user_id)
            if not storage_info:
                logger.warning(
                    f"No storage record found for user {user_id}, "
                    f"skipping reconciliation"
                )
                return False

            db_storage = storage_info["storage_usage_bytes"]

            # Calculate actual S3 storage
            actual_storage = await _calculate_live_storage_usage(user_id)

            # Update database
            update_user_storage_usage(user_id, actual_storage)

            drift_bytes = abs(actual_storage - db_storage)
            drift_percent = (drift_bytes / db_storage * 100) if db_storage > 0 else 0

            logger.info(
                f"Reconciled storage for user {user_id}: "
                f"{db_storage:,} → {actual_storage:,} bytes "
                f"(drift: {drift_bytes:,} bytes, {drift_percent:.1f}%)"
            )

            return True

        except Exception as e:
            logger.error(f"Failed to reconcile storage for user {user_id}: {e}")
            return False
