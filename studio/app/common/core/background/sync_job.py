"""
Background job to validate published experiments in S3.

Runs every 5 minutes, validates experiments with
local_sync_status='pending' exist in S3, and updates DB status.
The background instance has no shared filesystem -- actual file
downloads happen on API instances via startup sync and on-demand.
"""

import asyncio
import os
from typing import TYPE_CHECKING, Dict, List, Tuple

from studio.app.common.core.utils.datetime_utils import get_current_datetime

if TYPE_CHECKING:
    from mypy_boto3_cloudwatch import CloudWatchClient

from sqlmodel import select

from studio.app.common.core.logger import AppLogger
from studio.app.common.core.storage.remote_storage_controller import (
    RemoteExperimentSyncMode,
)
from studio.app.common.core.storage.s3_storage_controller import S3StorageController
from studio.app.common.core.subscription.constants import SyncStatusConstants
from studio.app.common.db.database import session_scope
from studio.app.common.models import ExperimentRecord, User, Workspace
from studio.app.common.schemas.dataview import LocalSyncStatus, PublishStatus
from studio.app.dir_path import DIRPATH

logger = AppLogger.get_logger()

# Tracks retry counts across job runs (in-memory, resets on restart)
_retry_counts: Dict[int, int] = {}
MAX_PERSISTENT_RETRIES = 9  # 3 attempts/run * 3 runs


class SyncRetryError(Exception):
    """Exception indicating validation should be retried"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class PublishedExperimentSyncJob:
    """Background job to validate published experiments in S3"""

    # Validation is cheap (ListObjectsV2 only), so higher limits
    VALIDATION_LIMIT = 50
    VALIDATION_CONCURRENCY = 10

    # Startup sync downloads files on API instances
    THUMBNAIL_CONCURRENCY = 10
    METADATA_CONCURRENCY = 3

    @classmethod
    async def run(cls):
        """
        Main sync job: validate experiments exist in S3.

        Checks S3 for required files (experiment.yaml,
        workflow.yaml) without downloading, then updates
        local_sync_status in the DB.
        """
        try:
            logger.info("Starting published experiment validation job")
            await cls._run_validation_logic()
        except Exception as e:
            logger.error(
                f"Fatal error in sync job: {e}",
                exc_info=True,
            )

    @classmethod
    async def run_startup_sync(cls):
        """
        One-time sync at container startup.

        Unlike the periodic sync job, this:
        - Queries ALL published experiments (ignores local_sync_status)
        - Checks local file existence instead of DB status
        - Downloads only experiments missing locally
        - Does NOT modify DB sync status
        - Does NOT use file locking
        """
        logger.info("Starting one-time startup sync")

        try:
            missing = cls._get_missing_experiments()
            if not missing:
                return

            s3_controllers: dict[str, S3StorageController] = {}
            await cls._sync_startup_thumbnails(missing, s3_controllers)
            await cls._sync_startup_metadata(missing, s3_controllers)

            logger.info(f"Startup sync completed for" f" {len(missing)} experiments")
        except Exception as e:
            logger.error(f"Startup sync error: {e}", exc_info=True)

    @classmethod
    def _get_s3_controller(
        cls,
        bucket_name: str,
        controllers: dict[str, S3StorageController],
    ) -> S3StorageController:
        """Cache S3 controllers per bucket."""
        if bucket_name not in controllers:
            controllers[bucket_name] = S3StorageController(bucket_name)
        return controllers[bucket_name]

    @classmethod
    async def _sync_startup_thumbnails(
        cls,
        experiments: List[Tuple[str, str, int, str]],
        s3_controllers: dict[str, S3StorageController],
    ):
        """Phase 1: Download thumbnails (high concurrency)."""
        sem = asyncio.Semaphore(cls.THUMBNAIL_CONCURRENCY)

        async def dl_thumb(ws_id, uid, bucket):
            async with sem:
                try:
                    s3 = cls._get_s3_controller(bucket, s3_controllers)
                    await s3.download_experiment(
                        ws_id,
                        uid,
                        sync_mode=RemoteExperimentSyncMode.THUMBNAILS_ONLY,
                    )
                except Exception as e:
                    logger.warning(f"Startup thumb sync failed" f" {ws_id}/{uid}: {e}")

        await asyncio.gather(
            *[dl_thumb(w, u, b) for w, u, _, b in experiments],
            return_exceptions=True,
        )

    @classmethod
    async def _sync_startup_metadata(
        cls,
        experiments: List[Tuple[str, str, int, str]],
        s3_controllers: dict[str, S3StorageController],
    ):
        """Phase 2: Download essential metadata."""
        sem = asyncio.Semaphore(cls.METADATA_CONCURRENCY)

        async def dl_meta(ws_id, uid, bucket):
            async with sem:
                try:
                    s3 = cls._get_s3_controller(bucket, s3_controllers)
                    await s3.download_experiment(
                        ws_id,
                        uid,
                        sync_mode=RemoteExperimentSyncMode.ESSENTIAL_ONLY,
                    )
                except Exception as e:
                    logger.warning(f"Startup meta sync failed" f" {ws_id}/{uid}: {e}")

        await asyncio.gather(
            *[dl_meta(w, u, b) for w, u, _, b in experiments],
            return_exceptions=True,
        )

    @classmethod
    async def _run_validation_logic(cls):
        """Validate pending experiments exist in S3."""
        try:
            pending = cls._get_pending_experiments(limit=cls.VALIDATION_LIMIT)

            if not pending:
                logger.debug("No pending experiments to validate")
                cls._publish_metrics(0, 0)
                return

            logger.info(f"Found {len(pending)} experiments to validate")

            s3_controllers: dict[str, S3StorageController] = {}
            sem = asyncio.Semaphore(cls.VALIDATION_CONCURRENCY)

            async def validate_with_semaphore(
                workspace_id, unique_id, exp_id, bucket_name
            ):
                async with sem:
                    try:
                        s3 = cls._get_s3_controller(bucket_name, s3_controllers)
                        success = await cls._validate_experiment(
                            s3,
                            workspace_id,
                            unique_id,
                            exp_id,
                            bucket_name,
                        )
                        return (
                            workspace_id,
                            unique_id,
                            exp_id,
                            success,
                            None,
                        )
                    except Exception as e:
                        logger.error(
                            f"Error validating"
                            f" {workspace_id}/{unique_id}"
                            f" from bucket"
                            f" {bucket_name}: {e}",
                            exc_info=True,
                        )
                        cls._mark_sync_error(exp_id)
                        return (
                            workspace_id,
                            unique_id,
                            exp_id,
                            False,
                            e,
                        )

            tasks = [validate_with_semaphore(w, u, e, b) for w, u, e, b in pending]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            synced_count = 0
            error_count = 0

            for result in results:
                if isinstance(result, Exception):
                    logger.error("Unexpected error in" f" validation: {result}")
                    error_count += 1
                else:
                    _, _, _, success, _ = result
                    if success:
                        synced_count += 1
                    else:
                        error_count += 1

            logger.info(
                f"Validation job completed: "
                f"{synced_count} synced, {error_count} errors "
                f"(max {cls.VALIDATION_CONCURRENCY} concurrent)"
            )

            cls._publish_metrics(synced_count, error_count)

        except Exception as e:
            logger.error(
                f"Error in validation logic: {e}",
                exc_info=True,
            )

    @classmethod
    def _get_pending_experiments(
        cls, limit: int = None
    ) -> List[Tuple[str, str, int, str]]:
        """
        Query DB for published experiments with pending/error
        sync status.

        Returns:
            List of (workspace_id, unique_id, exp_id, bucket_name)
        """
        if limit is None:
            limit = SyncStatusConstants.MAX_SYNC_PER_RUN

        default_bucket = os.environ.get("S3_DEFAULT_BUCKET_NAME")

        with session_scope() as db:
            statement = (
                select(
                    ExperimentRecord.workspace_id,
                    ExperimentRecord.uid,
                    ExperimentRecord.id,
                    User.attributes,
                )
                .join(
                    Workspace,
                    Workspace.id == ExperimentRecord.workspace_id,
                )
                .join(User, User.id == Workspace.user_id)
                .where(ExperimentRecord.publish_status == PublishStatus.on.value)
                .where(
                    ExperimentRecord.local_sync_status.in_(
                        [
                            LocalSyncStatus.pending.value,
                            LocalSyncStatus.error.value,
                        ]
                    )
                )
                .where(Workspace.deleted == 0)
                .where(ExperimentRecord.success == 1)
                .order_by(ExperimentRecord.analyzed_at.desc())
                .limit(limit)
            )

            result = db.execute(statement)

            experiments = []
            for row in result:
                workspace_id = str(row[0])
                unique_id = row[1]
                exp_id = row[2]
                user_attributes = row[3]
                bucket_name = (
                    user_attributes.get("remote_bucket_name")
                    if user_attributes
                    else None
                ) or default_bucket
                experiments.append((workspace_id, unique_id, exp_id, bucket_name))

            return experiments

    @classmethod
    def _get_missing_experiments(
        cls,
    ) -> List[Tuple[str, str, int, str]]:
        """
        Return published experiments missing locally.

        Queries all published experiments and filters to those
        lacking required YAML files on the local filesystem.
        """
        from studio.app.common.core.utils.filepath_creater import join_filepath

        all_experiments = cls._get_all_published_experiments()
        if not all_experiments:
            logger.info("No published experiments to sync")
            return []

        missing = []
        for ws_id, uid, exp_id, bucket in all_experiments:
            local_path = join_filepath([DIRPATH.OUTPUT_DIR, ws_id, uid])
            exp_yaml = os.path.join(local_path, DIRPATH.EXPERIMENT_YML)
            wf_yaml = os.path.join(local_path, DIRPATH.WORKFLOW_YML)
            if not os.path.exists(exp_yaml) or not os.path.exists(wf_yaml):
                missing.append((ws_id, uid, exp_id, bucket))

        if not missing:
            logger.info("All published experiments already present locally")
            return []

        logger.info(
            f"Startup sync: {len(missing)}/{len(all_experiments)}"
            f" experiments missing locally"
        )
        return missing

    @classmethod
    def _get_all_published_experiments(
        cls,
    ) -> List[Tuple[str, str, int, str]]:
        """
        Query ALL published experiments regardless of sync status.

        Used by startup sync to check local file presence.

        Returns:
            List of (workspace_id, unique_id, exp_id, bucket_name)
        """
        default_bucket = os.environ.get("S3_DEFAULT_BUCKET_NAME")

        with session_scope() as db:
            statement = (
                select(
                    ExperimentRecord.workspace_id,
                    ExperimentRecord.uid,
                    ExperimentRecord.id,
                    User.attributes,
                )
                .join(
                    Workspace,
                    Workspace.id == ExperimentRecord.workspace_id,
                )
                .join(User, User.id == Workspace.user_id)
                .where(ExperimentRecord.publish_status == PublishStatus.on.value)
                .where(Workspace.deleted == 0)
                .where(ExperimentRecord.success == 1)
                .order_by(ExperimentRecord.analyzed_at.desc())
            )

            result = db.execute(statement)

            experiments = []
            for row in result:
                workspace_id = str(row[0])
                unique_id = row[1]
                exp_id = row[2]
                user_attributes = row[3]
                bucket_name = (
                    user_attributes.get("remote_bucket_name")
                    if user_attributes
                    else None
                ) or default_bucket
                experiments.append((workspace_id, unique_id, exp_id, bucket_name))

            return experiments

    @classmethod
    async def _validate_experiment(
        cls,
        s3_controller: S3StorageController,
        workspace_id: str,
        unique_id: str,
        exp_id: int,
        bucket_name: str = "",
    ) -> bool:
        """
        Validate experiment exists in S3 with exponential backoff.

        Checks that required files exist in S3 via
        validate_experiment_in_s3() (no downloads). Updates DB
        sync status based on the result.

        Returns:
            True if validation successful, False otherwise
        """
        logger.info(
            f"Validating experiment" f" {workspace_id}/{unique_id}" f" (id={exp_id})"
        )

        try:
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    logger.info(
                        f"Validating in S3"
                        f" (attempt"
                        f" {attempt + 1}/{max_retries})"
                        f": {workspace_id}/{unique_id}"
                    )
                    result = await s3_controller.validate_experiment_in_s3(
                        workspace_id, unique_id
                    )

                    if not result["valid"]:
                        raise SyncRetryError(result["error"] or "Validation failed")

                    logger.info("Successfully validated" f" {workspace_id}/{unique_id}")
                    has_thumbnails = result.get("has_thumbnails", True)
                    cls._mark_sync_complete(exp_id)
                    cls._clear_retry_count(exp_id)
                    asyncio.create_task(
                        cls._trigger_proactive_download(
                            workspace_id,
                            unique_id,
                            bucket_name,
                            has_thumbnails=has_thumbnails,
                        )
                    )
                    return True

                except Exception as e:
                    is_expected = isinstance(e, SyncRetryError)
                    error_msg = str(e)

                    if attempt < max_retries - 1:
                        wait = 2**attempt
                        logger.warning(f"{error_msg}," f" retrying in {wait}s...")
                        await asyncio.sleep(wait)
                    else:
                        logger.error(
                            f"Failed to validate"
                            f" {workspace_id}/{unique_id}"
                            f" after {max_retries}"
                            f" attempts: {error_msg}",
                            exc_info=not is_expected,
                        )

            # All retries failed
            cls._mark_sync_error(exp_id)
            cls._increment_retry_count(exp_id)
            cls._check_persistent_failure(exp_id, workspace_id, unique_id)
            return False

        except Exception as e:
            logger.error(
                f"Error validating" f" {workspace_id}/{unique_id}: {e}",
                exc_info=True,
            )
            cls._mark_sync_error(exp_id)
            cls._increment_retry_count(exp_id)
            return False

    @classmethod
    def _do_proactive_download_sync(
        cls,
        workspace_id: str,
        unique_id: str,
        bucket_name: str,
        has_thumbnails: bool = True,
    ) -> bool:
        """Sync HTTP call to ALB (runs in executor)."""
        import requests

        alb_dns = os.environ.get("ALB_DNS_NAME")
        internal_secret = os.environ.get("INTERNAL_API_SECRET")

        if not alb_dns or not internal_secret:
            return False

        base = os.environ.get("INTERNAL_API_BASE_URL") or f"https://{alb_dns}"
        url = (
            f"{base}" f"/system-internal/sync-experiment" f"/{workspace_id}/{unique_id}"
        )
        headers = {
            "X-Internal-Secret": internal_secret,
            "Content-Type": "application/json",
        }
        params = {
            "bucket_name": bucket_name,
            "has_thumbnails": str(has_thumbnails).lower(),
        }

        try:
            # Skip SSL verification for internal VPC traffic on the prod
            # HTTPS path; the ALB cert doesn't match the AWS-generated
            # hostname. On dev the base URL is plain HTTP (:8080), where
            # verify=False is a no-op.
            response = requests.post(
                url,
                headers=headers,
                params=params,
                timeout=10.0,
                verify=False,
            )
            if response.status_code == 200:
                logger.info(
                    "Proactive download triggered" f" for {workspace_id}/{unique_id}"
                )
                return True
            else:
                logger.warning(
                    "Proactive download request"
                    f" failed for"
                    f" {workspace_id}/{unique_id}:"
                    f" status {response.status_code}"
                )
                return False
        except Exception as e:
            logger.warning(
                "Proactive download trigger"
                f" error for"
                f" {workspace_id}/{unique_id}: {e}"
            )
            return False

    @classmethod
    async def _trigger_proactive_download(
        cls,
        workspace_id: str,
        unique_id: str,
        bucket_name: str,
        has_thumbnails: bool = True,
    ) -> bool:
        """
        Call ALB to trigger thumbnail/metadata download
        on an API instance. Fire-and-forget.

        Returns False silently if ALB config is missing
        (non-cloud environments).
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            cls._do_proactive_download_sync,
            workspace_id,
            unique_id,
            bucket_name,
            has_thumbnails,
        )

    @classmethod
    def _mark_sync_complete(cls, exp_id: int):
        """Mark experiment as successfully synced"""
        with session_scope() as db:
            experiment = db.get(ExperimentRecord, exp_id)
            if experiment:
                experiment.local_sync_status = LocalSyncStatus.synced.value
                db.add(experiment)
                db.commit()
                logger.debug(f"Marked experiment {exp_id} as synced")
            else:
                logger.warning(f"Experiment {exp_id} not found for sync status update")

    @classmethod
    def _mark_sync_error(cls, exp_id: int):
        """Mark experiment sync as failed (retries next run)"""
        with session_scope() as db:
            experiment = db.get(ExperimentRecord, exp_id)
            if experiment:
                experiment.local_sync_status = LocalSyncStatus.error.value
                db.add(experiment)
                db.commit()
                logger.debug(f"Marked experiment {exp_id} as sync error")
            else:
                logger.warning(f"Experiment {exp_id} not found for sync error update")

    @classmethod
    def _increment_retry_count(cls, exp_id: int):
        """Increment retry count for failed validation."""
        _retry_counts[exp_id] = _retry_counts.get(exp_id, 0) + 1
        logger.info(f"Retry count for experiment {exp_id}:" f" {_retry_counts[exp_id]}")

    @classmethod
    def _clear_retry_count(cls, exp_id: int):
        """Clear retry count after successful validation."""
        _retry_counts.pop(exp_id, None)
        logger.debug(f"Cleared retry count for experiment {exp_id}")

    @classmethod
    def _check_persistent_failure(
        cls,
        exp_id: int,
        workspace_id: str,
        unique_id: str,
    ):
        """Alert operators only after MAX_PERSISTENT_RETRIES."""
        count = _retry_counts.get(exp_id, 0)
        if count < MAX_PERSISTENT_RETRIES:
            return

        try:
            import boto3

            env_prefix = os.environ.get("ENV_PREFIX", "default")
            cloudwatch: "CloudWatchClient" = boto3.client("cloudwatch")
            cloudwatch.put_metric_data(
                Namespace=f"OptiNiSt/BackgroundJobs/{env_prefix}",
                MetricData=[
                    {
                        "MetricName": "PersistentSyncFailure",
                        "Value": 1,
                        "Unit": "Count",
                        "Timestamp": get_current_datetime(),
                        "Dimensions": [
                            {
                                "Name": "ExperimentId",
                                "Value": str(exp_id),
                            },
                            {
                                "Name": "WorkspaceId",
                                "Value": workspace_id,
                            },
                        ],
                    }
                ],
            )
            logger.error(
                "PERSISTENT SYNC FAILURE:"
                f" {workspace_id}/{unique_id}"
                f" (id={exp_id}) has failed"
                f" {count} validation attempts."
                " Manual intervention may be"
                " required."
            )
        except Exception as e:
            logger.warning("Failed to publish persistent" f" failure metric: {e}")

        # Reset so alert can re-fire if failure persists
        _retry_counts.pop(exp_id, None)

    @classmethod
    def _publish_metrics(cls, synced_count: int, error_count: int):
        """Publish sync job metrics to CloudWatch."""
        try:
            import boto3

            cloudwatch: "CloudWatchClient" = boto3.client("cloudwatch")

            total = synced_count + error_count
            error_rate = float(error_count / total * 100) if total > 0 else 0.0
            timestamp = get_current_datetime()
            env_prefix = os.environ.get("ENV_PREFIX", "default")
            namespace = f"OptiNiSt/BackgroundJobs/{env_prefix}"

            cloudwatch.put_metric_data(
                Namespace=namespace,
                MetricData=[
                    {
                        "MetricName": "ExperimentsSynced",
                        "Value": synced_count,
                        "Unit": "Count",
                        "Timestamp": timestamp,
                    },
                    {
                        "MetricName": "SyncErrors",
                        "Value": error_count,
                        "Unit": "Count",
                        "Timestamp": timestamp,
                    },
                ],
            )

            cloudwatch.put_metric_data(
                Namespace=namespace,
                MetricData=[
                    {
                        "MetricName": "SyncErrorRate",
                        "Value": error_rate,
                        "Unit": "Percent",
                        "Timestamp": timestamp,
                    },
                ],
            )
            logger.debug(
                "Published CloudWatch metrics:"
                f" {synced_count} synced,"
                f" {error_count} errors"
            )

            if total > 0 and error_rate > 50:
                logger.error(
                    "HIGH SYNC ERROR RATE:"
                    f" {error_rate:.1f}%"
                    f" ({error_count}/{total} failed)."
                    " Check S3 connectivity and"
                    " permissions."
                )
        except Exception as e:
            logger.error(
                f"Failed to publish metrics: {e}",
                exc_info=True,
            )
