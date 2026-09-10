"""Standalone cleanup worker for free-tier instances.

Runs ``DataCleanupJob`` in its own process so that the cleanup scheduler
is decoupled from the multi-worker uvicorn (UVICORN_WORKERS=5) FastAPI
application.  This avoids the need for leader-election or distributed
locking among API workers.

Usage (from cloud-startup.sh)::

    python -m studio.cleanup_worker &

The process runs a single APScheduler ``AsyncIOScheduler`` that triggers
``DataCleanupJob.run`` every 60 minutes and shuts down gracefully on
SIGTERM / SIGINT (ECS sends SIGTERM on task stop).
"""

import asyncio
import signal
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from studio.app.common.core.background.cleanup_job import DataCleanupJob
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.subscription.constants import SyncStatusConstants
from studio.app.common.core.utils.datetime_utils import get_current_datetime
from studio.app.common.core.utils.instance_utils import resolve_instance_id

logger = AppLogger.get_logger()


def main() -> None:
    # Use the shared resolver (env → IMDS → "local") so the logged id matches
    # the one DataCleanupJob filters on; logging only the INSTANCE_ID env var
    # would read "local" whenever it is unset even though IMDS resolves a real id.
    instance_id = resolve_instance_id()
    logger.info(
        f"cleanup_worker starting (instance: {instance_id}, "
        f"interval: {SyncStatusConstants.CLEANUP_INTERVAL_MINUTES}min)"
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    scheduler = AsyncIOScheduler(event_loop=loop)
    # First cleanup shortly after startup, not now+interval.
    first_run = get_current_datetime() + timedelta(
        seconds=SyncStatusConstants.INITIAL_RUN_DELAY_SECONDS
    )
    scheduler.add_job(
        DataCleanupJob.run,
        trigger=IntervalTrigger(minutes=SyncStatusConstants.CLEANUP_INTERVAL_MINUTES),
        id="local_data_cleanup",
        replace_existing=True,
        next_run_time=first_run,
    )
    scheduler.start()

    # Graceful shutdown on SIGTERM / SIGINT
    def _shutdown(sig: int, _frame) -> None:  # noqa: ANN001
        logger.info(f"cleanup_worker received signal {sig}, shutting down")
        scheduler.shutdown(wait=False)
        loop.call_soon_threadsafe(loop.stop)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        loop.run_forever()
    finally:
        loop.close()
        logger.info("cleanup_worker stopped")


if __name__ == "__main__":
    main()
