"""
Tests for startup-sync leader election and INSTANCE_MODE gating in lifespan.

Covers:
- `startup_sync_leader_lock` runs the sync only when GET_LOCK is acquired.
- `_should_run_startup_sync` gates the lifespan branch to INSTANCE_MODE=public.
"""

import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI

from studio import __main_unit__ as main_unit
from studio.app.common.core.background.sync_job import PublishedExperimentSyncJob
from studio.app.common.core.instance_mode import INSTANCE_MODE_ENV


@contextmanager
def _fake_leader_lock(acquired: bool):
    """Stand-in for startup_sync_leader_lock used by the lifespan branch."""
    yield acquired


class TestStartupSyncLeaderElection:
    """Leader election as the lifespan actually wires it.

    These three cases used to re-implement the leader branch in the test body,
    so ``_startup_sync``'s own ``if not acquired: return`` was unpinned: deleting
    it would have let every task in the ASG sync against S3 with the suite green.
    They now call the production coroutine.
    """

    @staticmethod
    def _patches(acquired, lock=None, sync_error=None):
        return (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch(
                "studio.app.common.core.storage.startup_leader."
                "startup_sync_leader_lock",
                lock if lock else (lambda: _fake_leader_lock(acquired)),
            ),
            patch.object(
                PublishedExperimentSyncJob,
                "run_startup_sync",
                new_callable=AsyncMock,
                side_effect=sync_error,
            ),
            patch.object(main_unit, "logger"),
        )

    @pytest.mark.asyncio
    async def test_leader_runs_startup_sync(self):
        """When the lock is acquired, startup sync runs once."""
        sleep, lock, sync, logger = self._patches(True)
        with sleep, lock, sync as mock_sync, logger as mock_logger:
            await main_unit._startup_sync()

        mock_sync.assert_awaited_once()
        assert "Startup sync deferred to leader task" not in [
            c.args[0] for c in mock_logger.info.call_args_list
        ]
        mock_logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_leader_skips_startup_sync_and_says_so(self):
        """The loser syncs nothing and logs the deferral line the sheet reads."""
        sleep, lock, sync, logger = self._patches(False)
        with sleep, lock, sync as mock_sync, logger as mock_logger:
            await main_unit._startup_sync()

        mock_sync.assert_not_awaited()
        mock_logger.info.assert_called_once_with("Startup sync deferred to leader task")

    @pytest.mark.asyncio
    async def test_leader_lock_released_on_sync_error(self):
        """The lock context exits and the failure is logged, not raised."""
        release_calls = []

        @contextmanager
        def fake_lock():
            try:
                yield True
            finally:
                release_calls.append(True)

        sleep, lock, sync, logger = self._patches(
            True, lock=fake_lock, sync_error=RuntimeError("S3 unavailable")
        )
        with sleep, lock, sync, logger as mock_logger:
            await main_unit._startup_sync()

        assert release_calls == [True], "lock context should exit even on error"
        assert "Startup sync error: S3 unavailable" in mock_logger.error.call_args[0][0]


class TestLifespanSchedulesTheStartupSync:
    """The wiring between the gate and the coroutine.

    ``_should_run_startup_sync`` and ``_startup_sync`` were both covered while
    nothing pinned that the lifespan connects them.
    """

    @pytest.mark.asyncio
    async def test_public_tier_creates_the_task_and_logs_it(self):
        # lifespan rebinds ``logger`` from AppLogger, so the module attribute
        # cannot be patched here.
        mock_logger = Mock()
        with patch.dict(
            os.environ,
            {INSTANCE_MODE_ENV: "public", "DISABLE_BACKGROUND_SCHEDULER": "1"},
        ), patch.object(
            main_unit, "_startup_sync", new_callable=AsyncMock
        ) as mock_startup_sync, patch.object(
            main_unit.MODE, "IS_STANDALONE", False
        ), patch.object(
            main_unit.AppLogger, "get_logger", return_value=mock_logger
        ):
            app = FastAPI()
            async with main_unit.lifespan(app):
                await app.state.startup_sync_task

        mock_startup_sync.assert_awaited_once()
        assert "Startup sync task scheduled (runs in background)" in [
            c.args[0] for c in mock_logger.info.call_args_list
        ]

    @pytest.mark.asyncio
    async def test_free_tier_creates_no_task(self):
        with patch.dict(
            os.environ,
            {INSTANCE_MODE_ENV: "free", "DISABLE_BACKGROUND_SCHEDULER": "1"},
        ), patch.object(
            main_unit, "_startup_sync", new_callable=AsyncMock
        ) as mock_startup_sync, patch.object(
            main_unit.MODE, "IS_STANDALONE", False
        ):
            app = FastAPI()
            async with main_unit.lifespan(app):
                pass

        mock_startup_sync.assert_not_awaited()
        assert not hasattr(app.state, "startup_sync_task")


class TestShouldRunStartupSync:
    """Gating predicate for the lifespan startup-sync branch."""

    def test_public_mode_runs_sync(self):
        from studio.__main_unit__ import _should_run_startup_sync

        assert _should_run_startup_sync("public", is_standalone=False) is True

    def test_default_mode_skips_sync(self):
        from studio.__main_unit__ import _should_run_startup_sync

        assert _should_run_startup_sync("default", is_standalone=False) is False

    def test_unknown_mode_skips_sync(self):
        from studio.__main_unit__ import _should_run_startup_sync

        assert _should_run_startup_sync("free", is_standalone=False) is False

    def test_standalone_always_skips(self):
        from studio.__main_unit__ import _should_run_startup_sync

        # Even with instance_mode=public, standalone never runs the sync.
        assert _should_run_startup_sync("public", is_standalone=True) is False
