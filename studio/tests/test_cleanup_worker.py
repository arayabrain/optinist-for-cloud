"""Smoke tests for the standalone cleanup worker.

The worker's run loop (loop.run_forever) is not exercised; these tests only
verify the scheduler is registered with the expected job id / interval and
that SIGTERM / SIGINT handlers are installed.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

from studio.app.common.core.background.cleanup_job import DataCleanupJob
from studio.app.common.core.subscription.constants import SyncStatusConstants
from studio.app.common.core.utils.datetime_utils import get_current_datetime


def test_main_registers_cleanup_job():
    """main() adds DataCleanupJob.run on the expected id / interval."""
    with patch("studio.cleanup_worker.AsyncIOScheduler") as mock_sched_cls, patch(
        "studio.cleanup_worker.asyncio"
    ) as mock_asyncio, patch("studio.cleanup_worker.signal") as mock_signal, patch(
        "studio.cleanup_worker.resolve_instance_id", return_value="i-worker"
    ):
        import studio.cleanup_worker as worker

        mock_sched = MagicMock()
        mock_sched_cls.return_value = mock_sched
        mock_loop = MagicMock()
        mock_asyncio.new_event_loop.return_value = mock_loop

        before = get_current_datetime()
        worker.main()
        after = get_current_datetime()

        # Job registered with correct target, id and interval
        mock_sched.add_job.assert_called_once()
        _, kwargs = mock_sched.add_job.call_args
        assert mock_sched.add_job.call_args[0][0] == DataCleanupJob.run
        assert kwargs["id"] == "local_data_cleanup"
        trigger = kwargs["trigger"]
        assert (
            trigger.interval.total_seconds()
            == SyncStatusConstants.CLEANUP_INTERVAL_MINUTES * 60
        )

        # First run shortly after startup: next_run_time falls in the window
        # [before, after] + delay, not a full interval later. Window bounds
        # (not a bare wall-clock subtraction) keep this non-flaky under load.
        delay = timedelta(seconds=SyncStatusConstants.INITIAL_RUN_DELAY_SECONDS)
        next_run_time = kwargs["next_run_time"]
        assert before + delay <= next_run_time <= after + delay

        mock_sched.start.assert_called_once()
        mock_loop.run_forever.assert_called_once()

        # Graceful-shutdown handlers installed for SIGTERM / SIGINT
        registered = {c.args[0] for c in mock_signal.signal.call_args_list}
        assert mock_signal.SIGTERM in registered
        assert mock_signal.SIGINT in registered
