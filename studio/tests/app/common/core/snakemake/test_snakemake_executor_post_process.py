"""
Unit tests for the post-process logic in _snakemake_execute_process.

These tests verify that experiment record registration and data usage
updates are skipped when observe_overall() fails, and that a warning
is logged.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "studio.app.common.core.snakemake.snakemake_executor"

WORKSPACE_ID = "test_workspace"
UNIQUE_ID = "test_unique_id"


@pytest.fixture()
def mock_sync_status():
    """Patch RemoteSyncStatusFileUtil; by default the concurrent observe path
    has NOT reported sync success (so lock conflicts exhaust their retries)."""
    with patch(f"{MODULE}.RemoteSyncStatusFileUtil") as mock_status:
        mock_status.check_sync_status_success.return_value = False
        yield mock_status


@pytest.fixture()
def _patch_snakemake_execution(mock_sync_status):
    """Patch everything except the post-process block under test."""
    with (
        patch(f"{MODULE}.SmkStatusLogger"),
        patch(f"{MODULE}.join_filepath", return_value="/tmp/fake"),
        patch(f"{MODULE}.SnakemakeApi") as mock_api_cls,
        patch(f"{MODULE}.RemoteStorageController") as mock_remote,
        patch(f"{MODULE}.RemoteSyncLockFileUtil") as mock_lock,
        patch(f"{MODULE}.get_pickle_file"),
        patch(f"{MODULE}.DIRPATH"),
        patch(f"{MODULE}.time.sleep"),  # avoid real backoff sleeps
    ):
        # Make snakemake execution itself succeed
        mock_ctx = MagicMock()
        mock_api_cls.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_api_cls.return_value.__exit__ = MagicMock(return_value=False)
        dag_mock = MagicMock()
        mock_ctx.dag.return_value.__enter__ = MagicMock(return_value=dag_mock)
        mock_ctx.dag.return_value.__exit__ = MagicMock(return_value=False)

        mock_remote.is_available.return_value = False

        # Provide real integer retry-policy constants (the class is mocked).
        mock_lock.LOCK_CONFLICT_RETRY_MAX = 3
        mock_lock.LOCK_CONFLICT_RETRY_BACKOFF_SECONDS = 0
        yield


@pytest.fixture()
def mock_observe():
    """Patch WorkflowResult and its observe_overall method."""
    with patch(f"{MODULE}.WorkflowResult") as mock_cls:
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance
        mock_instance.observe_overall = AsyncMock()
        yield mock_instance


@pytest.fixture()
def mock_experiment_record():
    with patch(f"{MODULE}.ExperimentRecordService") as mock_svc:
        mock_svc.is_available.return_value = True
        yield mock_svc


@pytest.fixture()
def mock_data_capacity():
    with patch(f"{MODULE}.WorkspaceDataCapacityService") as mock_svc:
        yield mock_svc


class TestPostProcessObserveSuccess:
    """When observe_overall() succeeds, downstream services should run."""

    @pytest.mark.usefixtures("_patch_snakemake_execution")
    def test_calls_record_and_data_usage(
        self, mock_observe, mock_experiment_record, mock_data_capacity
    ):
        from studio.app.common.core.snakemake.snakemake_executor import (
            _snakemake_execute_process,
        )

        _snakemake_execute_process(WORKSPACE_ID, UNIQUE_ID, MagicMock())

        record_fn = mock_experiment_record.regist_record_on_workflow_completed
        record_fn.assert_called_once_with(WORKSPACE_ID, UNIQUE_ID)
        mock_data_capacity.update_experiment_data_usage.assert_called_once_with(
            WORKSPACE_ID, UNIQUE_ID
        )


class TestPostProcessObserveFailure:
    """When observe_overall() fails, downstream services should be skipped."""

    @pytest.fixture(autouse=True)
    def _fail_observe(self, mock_observe):
        mock_observe.observe_overall = AsyncMock(
            side_effect=RuntimeError("observe failed")
        )

    @pytest.fixture(autouse=True)
    def _suppress_error_traceback(self):
        """Suppress ERROR+Traceback output from intentional observe failure."""
        with patch(f"{MODULE}.logger.error"):
            yield

    @pytest.mark.usefixtures("_patch_snakemake_execution")
    def test_skips_record_and_data_usage(
        self, mock_observe, mock_experiment_record, mock_data_capacity
    ):
        from studio.app.common.core.snakemake.snakemake_executor import (
            _snakemake_execute_process,
        )

        _snakemake_execute_process(WORKSPACE_ID, UNIQUE_ID, MagicMock())

        mock_experiment_record.regist_record_on_workflow_completed.assert_not_called()
        mock_data_capacity.update_experiment_data_usage.assert_not_called()

    @pytest.mark.usefixtures("_patch_snakemake_execution")
    def test_logs_warning(
        self, mock_observe, mock_experiment_record, mock_data_capacity, caplog
    ):
        from studio.app.common.core.snakemake.snakemake_executor import (
            _snakemake_execute_process,
        )

        with caplog.at_level(logging.WARNING):
            _snakemake_execute_process(WORKSPACE_ID, UNIQUE_ID, MagicMock())

        assert any(
            "Skipped experiment record registration and data usage update"
            in record.message
            and WORKSPACE_ID in record.message
            and UNIQUE_ID in record.message
            for record in caplog.records
        )


class TestPostProcessObserveLockConflict:
    """A remote upload-lock conflict must not skip DB finalization.

    The upload is idempotent and handled by the concurrent observe path, and
    the local ExptConfig is already finalized before the upload step, so
    registration and data-usage still run.
    """

    def _make_lock_error(self):
        from studio.app.common.core.storage.remote_storage_controller import (
            RemoteStorageLockError,
        )

        return RemoteStorageLockError(WORKSPACE_ID, UNIQUE_ID)

    @pytest.mark.usefixtures("_patch_snakemake_execution")
    def test_finalizes_when_lock_persists(
        self, mock_observe, mock_experiment_record, mock_data_capacity
    ):
        """Every attempt hits the lock: DB is still finalized."""
        mock_observe.observe_overall = AsyncMock(side_effect=self._make_lock_error())

        from studio.app.common.core.snakemake.snakemake_executor import (
            _snakemake_execute_process,
        )

        _snakemake_execute_process(WORKSPACE_ID, UNIQUE_ID, MagicMock())

        # observe_overall retried up to the configured maximum
        assert mock_observe.observe_overall.await_count == 3

        record_fn = mock_experiment_record.regist_record_on_workflow_completed
        record_fn.assert_called_once_with(WORKSPACE_ID, UNIQUE_ID)
        mock_data_capacity.update_experiment_data_usage.assert_called_once_with(
            WORKSPACE_ID, UNIQUE_ID
        )

    @pytest.mark.usefixtures("_patch_snakemake_execution")
    def test_retry_succeeds_then_finalizes(
        self, mock_observe, mock_experiment_record, mock_data_capacity
    ):
        """A transient lock clears on retry: observe succeeds, DB finalized."""
        mock_observe.observe_overall = AsyncMock(
            side_effect=[self._make_lock_error(), None]
        )

        from studio.app.common.core.snakemake.snakemake_executor import (
            _snakemake_execute_process,
        )

        _snakemake_execute_process(WORKSPACE_ID, UNIQUE_ID, MagicMock())

        assert mock_observe.observe_overall.await_count == 2
        record_fn = mock_experiment_record.regist_record_on_workflow_completed
        record_fn.assert_called_once_with(WORKSPACE_ID, UNIQUE_ID)
        mock_data_capacity.update_experiment_data_usage.assert_called_once_with(
            WORKSPACE_ID, UNIQUE_ID
        )

    @pytest.mark.usefixtures("_patch_snakemake_execution")
    def test_lock_conflict_then_genuine_failure_still_finalizes(
        self, mock_observe, mock_experiment_record, mock_data_capacity
    ):
        """A lock conflict proves the local ExptConfig was finalized, so a later
        genuine failure on the redundant upload does not un-prove it: the DB is
        still finalized."""
        mock_observe.observe_overall = AsyncMock(
            side_effect=[self._make_lock_error(), RuntimeError("boom")]
        )

        from studio.app.common.core.snakemake.snakemake_executor import (
            _snakemake_execute_process,
        )

        with patch(f"{MODULE}.logger.error"):  # suppress intentional traceback
            _snakemake_execute_process(WORKSPACE_ID, UNIQUE_ID, MagicMock())

        # Loop breaks on the genuine failure at the 2nd attempt.
        assert mock_observe.observe_overall.await_count == 2
        record_fn = mock_experiment_record.regist_record_on_workflow_completed
        record_fn.assert_called_once_with(WORKSPACE_ID, UNIQUE_ID)
        mock_data_capacity.update_experiment_data_usage.assert_called_once_with(
            WORKSPACE_ID, UNIQUE_ID
        )

    @pytest.mark.usefixtures("_patch_snakemake_execution")
    def test_logs_lock_conflict_warning(
        self, mock_observe, mock_experiment_record, mock_data_capacity, caplog
    ):
        mock_observe.observe_overall = AsyncMock(side_effect=self._make_lock_error())

        from studio.app.common.core.snakemake.snakemake_executor import (
            _snakemake_execute_process,
        )

        with caplog.at_level(logging.WARNING):
            _snakemake_execute_process(WORKSPACE_ID, UNIQUE_ID, MagicMock())

        assert any(
            "upload stayed locked" in record.message
            and WORKSPACE_ID in record.message
            and UNIQUE_ID in record.message
            for record in caplog.records
        )
        # The "skipped" warning must NOT be emitted on a lock conflict.
        assert not any(
            "Skipped experiment record registration" in record.message
            for record in caplog.records
        )

    @pytest.mark.usefixtures("_patch_snakemake_execution")
    def test_upload_confirmed_stops_retry_and_finalizes(
        self,
        mock_observe,
        mock_experiment_record,
        mock_data_capacity,
        mock_sync_status,
        caplog,
    ):
        """A SUCCESS remote sync status (from the path that won the lock) proves
        the redundant upload landed: stop retrying and finalize as confirmed."""
        mock_observe.observe_overall = AsyncMock(side_effect=self._make_lock_error())
        mock_sync_status.check_sync_status_success.return_value = True

        from studio.app.common.core.snakemake.snakemake_executor import (
            _snakemake_execute_process,
        )

        with caplog.at_level(logging.WARNING):
            _snakemake_execute_process(WORKSPACE_ID, UNIQUE_ID, MagicMock())

        # Confirmed on the 1st attempt: no further retries.
        assert mock_observe.observe_overall.await_count == 1
        record_fn = mock_experiment_record.regist_record_on_workflow_completed
        record_fn.assert_called_once_with(WORKSPACE_ID, UNIQUE_ID)
        mock_data_capacity.update_experiment_data_usage.assert_called_once_with(
            WORKSPACE_ID, UNIQUE_ID
        )
        assert any(
            "verified via remote sync status" in record.message
            for record in caplog.records
        )

    @pytest.mark.usefixtures("_patch_snakemake_execution")
    def test_unconfirmed_lock_logs_reconcile_warning(
        self, mock_observe, mock_experiment_record, mock_data_capacity, caplog
    ):
        """Persistent lock with no sync-success: DB is finalized from the local
        ExptConfig, and the unconfirmed remote upload is flagged for re-sync."""
        mock_observe.observe_overall = AsyncMock(side_effect=self._make_lock_error())

        from studio.app.common.core.snakemake.snakemake_executor import (
            _snakemake_execute_process,
        )

        with caplog.at_level(logging.WARNING):
            _snakemake_execute_process(WORKSPACE_ID, UNIQUE_ID, MagicMock())

        record_fn = mock_experiment_record.regist_record_on_workflow_completed
        record_fn.assert_called_once_with(WORKSPACE_ID, UNIQUE_ID)
        assert any(
            "remote upload is unconfirmed and will be reconciled" in record.message
            for record in caplog.records
        )
        assert not any(
            "verified via remote sync status" in record.message
            for record in caplog.records
        )
