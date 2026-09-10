"""Behavior tests for the run_result record-existence gate (issue #740 item #1).

The executor writes the experiment_records row asynchronously after a run
finishes, so a poll can observe all nodes finished before the row lands. The
poll must keep reporting "processing" until the record exists, so the dataview
is never opened against a missing record -- but only within a grace window, so a
dead executor post-process can't strand the run in a perpetual spinner.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

from studio.app.common.core.workflow.workflow import NodeItem
from studio.app.common.routers.run import RECORD_WRITE_GRACE_SEC, run_result
from studio.app.common.schemas.workflow import CompleteStatus
from studio.app.const import DATE_FORMAT


def _finished_at(seconds_ago: float) -> str:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return (now - timedelta(seconds=seconds_ago)).strftime(DATE_FORMAT)


def _poll(
    *,
    record_present=False,
    all_finished=True,
    is_available=True,
    finished_seconds_ago=0,
    record_exists_raises=False,
    complete_status=None,
):
    """Drive run_result with the gate inputs stubbed."""
    expt_config = Mock(finished_at=_finished_at(finished_seconds_ago), timezone=None)

    record_exists = Mock(
        side_effect=RuntimeError("db down") if record_exists_raises else None,
        return_value=record_present,
    )

    with patch(
        "studio.app.common.routers.run.ExptConfigReader.ensure_synced_async",
        new=AsyncMock(),
    ), patch("studio.app.common.routers.run.WorkflowResult") as mock_wfr, patch(
        "studio.app.common.routers.run.RemoteStorageController.is_available",
        return_value=complete_status is not None,
    ), patch(
        "studio.app.common.routers.run.RemoteSyncStatusFileUtil.check_sync_status_file",
        return_value=Mock(value=complete_status.value) if complete_status else None,
    ), patch(
        "studio.app.common.routers.run.ExperimentRecordService.is_available",
        return_value=is_available,
    ), patch(
        "studio.app.common.routers.run.NodeResult.is_all_nodes_already_finished",
        return_value=all_finished,
    ), patch(
        "studio.app.common.routers.run.ExptConfigReader.read",
        return_value=expt_config,
    ), patch(
        "studio.app.common.routers.run.ExperimentRecordService.record_exists",
        record_exists,
    ):
        mock_wfr.return_value.observe = AsyncMock(return_value={})
        return asyncio.run(
            run_result(
                workspace_id="1",
                uid="exp123",
                nodeDict=NodeItem(pendingNodeIdList=[]),
                background_tasks=Mock(),
                remote_bucket_name="",
            )
        )


def test_holds_processing_when_record_absent_within_grace():
    """All nodes finished but the record has not landed yet -> keep polling."""
    resp = _poll(record_present=False)
    assert resp.completeStatus == CompleteStatus.PROCESSING.value


def test_completes_when_record_present():
    """Record present -> completion is not forced back to processing."""
    resp = _poll(record_present=True)
    assert resp.completeStatus is None


def test_completes_when_record_absent_past_grace():
    """Record never landed after the grace window -> complete, don't strand."""
    resp = _poll(record_present=False, finished_seconds_ago=RECORD_WRITE_GRACE_SEC + 60)
    assert resp.completeStatus is None


def test_does_not_gate_while_still_running():
    """Nodes still running -> gate must not fire mid-run."""
    resp = _poll(record_present=False, all_finished=False)
    assert resp.completeStatus is None


def test_gate_off_in_standalone_mode():
    """ExperimentRecordService unavailable -> no behavior change."""
    resp = _poll(record_present=False, is_available=False)
    assert resp.completeStatus is None


def test_fails_open_when_record_check_raises():
    """A read failure must not force processing nor 500 the poll."""
    resp = _poll(record_exists_raises=True)
    assert resp.completeStatus is None


def test_terminal_error_not_masked_into_processing():
    """A terminal ERROR completeStatus is preserved, not rewritten."""
    resp = _poll(record_present=False, complete_status=CompleteStatus.ERROR)
    assert resp.completeStatus == CompleteStatus.ERROR.value
