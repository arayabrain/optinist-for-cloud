"""
Background job scheduler for OptiNiSt Cloud.

Uses APScheduler to run periodic background tasks:
- Sync published experiments from S3 to local storage
- Clean up data for logged-out free users
- Reconcile storage tracking with S3

This scheduler runs in a dedicated background ECS service with a single worker.
API services disable their schedulers via DISABLE_BACKGROUND_SCHEDULER=1.
"""

import os
from datetime import timedelta
from typing import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from studio.app.common.core.logger import AppLogger
from studio.app.common.core.mode import MODE
from studio.app.common.core.storage.remote_storage_controller import RemoteStorageType
from studio.app.common.core.subscription.constants import SyncStatusConstants
from studio.app.common.core.utils.datetime_utils import get_current_datetime

logger = AppLogger.get_logger()


class BackgroundScheduler:
    """
    Static class for managing background jobs scheduler.

    This scheduler runs in a dedicated background service. The single-instance
    architecture eliminates the need for multi-worker coordination.
    """

    _scheduler: AsyncIOScheduler = None

    @classmethod
    def initialize(cls):
        """
        Initialize the scheduler (call on app startup).

        Validates critical environment configuration (S3 bucket) and fails fast
        if misconfigured to prevent silent failures during job execution.
        """
        if cls._scheduler is not None:
            logger.warning("Scheduler already initialized")
            return

        # Skip in standalone mode
        if MODE.IS_STANDALONE:
            logger.info("Standalone mode - background scheduler disabled")
            return

        # Validate S3 configuration before initializing jobs
        cls._validate_s3_configuration()

        cls._scheduler = AsyncIOScheduler()
        logger.info("Background scheduler initialized")

    @classmethod
    def _validate_s3_configuration(cls):
        """
        Validate S3 bucket configuration at startup.

        Raises error if S3 is not properly configured, preventing silent failures
        during cleanup and sync jobs that depend on S3.
        """
        remote_storage_type = RemoteStorageType.get_activated_type()
        if remote_storage_type != RemoteStorageType.S3:
            logger.info(
                f"Remote storage type is {remote_storage_type.name}, "
                "skipping S3 configuration validation."
            )
            return

        s3_bucket = os.environ.get("S3_DEFAULT_BUCKET_NAME")

        if not s3_bucket:
            error_msg = (
                "S3_DEFAULT_BUCKET_NAME environment variable not set. "
                "Background jobs (cleanup, sync) require S3 configuration. "
                "Please configure S3_DEFAULT_BUCKET_NAME or disable background jobs."
            )
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        # Validate AWS credentials are available
        try:
            import boto3
            from botocore.exceptions import NoCredentialsError

            try:
                s3_client = boto3.client("s3")
                # Test access by listing buckets (lightweight operation)
                s3_client.head_bucket(Bucket=s3_bucket)
                logger.info(f"S3 configuration validated: bucket={s3_bucket}")
            except NoCredentialsError:
                error_msg = (
                    "AWS credentials not configured. Background jobs require "
                    "AWS credentials to access S3. Please configure AWS credentials."
                )
                logger.error(error_msg)
                raise RuntimeError(error_msg)
            except Exception as e:
                logger.warning(
                    f"S3 bucket validation warning: {e}. "
                    f"Bucket '{s3_bucket}' may not exist or be accessible. "
                    f"Background jobs may fail."
                )
        except ImportError:
            logger.error("boto3 not installed, cannot validate S3 configuration")
            raise RuntimeError("boto3 required for background jobs")

    @classmethod
    def start(cls):
        """Start the scheduler (call after adding all jobs)"""
        if cls._scheduler is None:
            logger.warning("Scheduler not initialized, cannot start")
            return

        if not cls._scheduler.running:
            cls._scheduler.start()
            logger.info("Background scheduler started")
        else:
            logger.warning("Scheduler already running")

    @classmethod
    def shutdown(cls):
        """Shutdown the scheduler gracefully."""
        if cls._scheduler is None:
            logger.warning("Scheduler not initialized, nothing to shutdown")
            return

        if cls._scheduler.running:
            cls._scheduler.shutdown(wait=True)
            logger.info("Background scheduler shut down")
        else:
            logger.warning("Scheduler not running, nothing to shutdown")

    @classmethod
    def add_job(cls, func: Callable, interval_minutes: int, job_id: str, **kwargs):
        """
        Add a job to run at a fixed interval.

        NOTE: AsyncIOScheduler automatically detects if func is an async function
        and handles it correctly. No special configuration needed for async jobs.

        First run defaults to now + INITIAL_RUN_DELAY_SECONDS (shortly after
        startup) instead of now + interval. Pass next_run_time to override.
        """
        if cls._scheduler is None:
            logger.warning("Scheduler not initialized, cannot add job")
            return

        # Apply the default when next_run_time is absent OR explicitly None:
        # APScheduler treats next_run_time=None as "paused" (never fires), so
        # setdefault alone would let a None slip through. get()-check covers both.
        if kwargs.get("next_run_time") is None:
            kwargs["next_run_time"] = get_current_datetime() + timedelta(
                seconds=SyncStatusConstants.INITIAL_RUN_DELAY_SECONDS
            )

        cls._scheduler.add_job(
            func,
            trigger=IntervalTrigger(minutes=interval_minutes),
            id=job_id,
            replace_existing=True,
            misfire_grace_time=60,
            **kwargs,
        )

        logger.info(
            f"Added background job: {job_id} "
            f"(runs every {interval_minutes} minutes)"
        )
