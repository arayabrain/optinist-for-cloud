"""Tests for BackgroundScheduler.add_job first-run scheduling.

add_job defaults next_run_time to now + a small delay so interval jobs run
shortly after startup (not now+interval), and lets callers override it.
"""

from datetime import timedelta
from unittest.mock import MagicMock

from studio.app.common.core.background.scheduler import BackgroundScheduler
from studio.app.common.core.subscription.constants import SyncStatusConstants
from studio.app.common.core.utils.datetime_utils import get_current_datetime

DELAY = timedelta(seconds=SyncStatusConstants.INITIAL_RUN_DELAY_SECONDS)


def _add_job_and_capture(**extra_kwargs):
    """Call add_job against a mocked scheduler; return (before, kwargs, after).

    `before`/`after` bracket the moment add_job computed next_run_time, so callers
    can assert the value falls in a precise window without a flaky bare
    wall-clock subtraction.
    """
    mock_scheduler = MagicMock()
    original = BackgroundScheduler._scheduler
    BackgroundScheduler._scheduler = mock_scheduler
    before = get_current_datetime()
    try:
        BackgroundScheduler.add_job(
            func=lambda: None,
            interval_minutes=60,
            job_id="test_job",
            **extra_kwargs,
        )
    finally:
        after = get_current_datetime()
        BackgroundScheduler._scheduler = original

    mock_scheduler.add_job.assert_called_once()
    return before, mock_scheduler.add_job.call_args.kwargs, after


def test_add_job_defaults_next_run_time_shortly_after_startup():
    """Default first run lands just after startup and within a literal ~minute."""
    before, kwargs, after = _add_job_and_capture()

    next_run_time = kwargs["next_run_time"]
    # Exact window: next_run_time == (now during add_job) + DELAY.
    assert before + DELAY <= next_run_time <= after + DELAY
    # Literal upper bound, independent of the constant: catches an over-large
    # INITIAL_RUN_DELAY_SECONDS (e.g. 3600) that would no longer be "shortly".
    assert (next_run_time - before).total_seconds() <= 60


def test_add_job_pins_misfire_grace_time():
    """misfire_grace_time stays 60s (the 3d3710d9b sweepjob fix)."""
    _, kwargs, _ = _add_job_and_capture()
    assert kwargs["misfire_grace_time"] == 60


def test_add_job_preserves_caller_next_run_time():
    """A caller-supplied (non-None) next_run_time is not overridden."""
    explicit = get_current_datetime() + timedelta(minutes=30)
    _, kwargs, _ = _add_job_and_capture(next_run_time=explicit)

    assert kwargs["next_run_time"] == explicit


def test_add_job_replaces_explicit_none_next_run_time():
    """An explicit next_run_time=None is replaced (None would pause the job)."""
    before, kwargs, after = _add_job_and_capture(next_run_time=None)

    next_run_time = kwargs["next_run_time"]
    assert next_run_time is not None
    assert before + DELAY <= next_run_time <= after + DELAY


def test_add_job_noop_when_not_initialized():
    """add_job is a safe no-op when the scheduler was never initialized."""
    original = BackgroundScheduler._scheduler
    BackgroundScheduler._scheduler = None
    try:
        result = BackgroundScheduler.add_job(
            func=lambda: None, interval_minutes=5, job_id="x"
        )
        assert result is None
        assert BackgroundScheduler._scheduler is None
    finally:
        BackgroundScheduler._scheduler = original
