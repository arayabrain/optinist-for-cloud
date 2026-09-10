"""
Tests for storage reconciliation background job.
Tests batch processing, drift detection, and error handling.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from studio.app.common.core.background.storage_reconciliation_job import (
    StorageReconciliationJob,
)
from studio.app.common.core.cloud.storage_tracking import StorageOwnerInactive


@pytest.mark.asyncio
async def test_reconciliation_job_batch_processing():
    """Test that reconciliation job processes users in batches."""
    with patch("studio.app.common.db.database.session_scope") as mock_session:
        with patch(
            "studio.app.common.core.cloud.storage_tracking."
            "_perform_full_scan_and_reset_delta",
            new_callable=AsyncMock,
        ) as mock_scan:
            with patch(
                "studio.app.common.core.background.storage_reconciliation_job.MODE"
            ) as mock_mode:
                mock_mode.IS_STANDALONE = False

                mock_db = Mock()
                mock_session.return_value.__enter__.return_value = mock_db

                # Mock total count query
                mock_count_result = Mock()
                mock_count_result.scalar.return_value = 25  # 25 users total

                # Mock batch queries (3 batches of 10 users each)
                batch_1 = [(i, 1000000, 50000, None) for i in range(1, 11)]
                batch_2 = [(i, 1000000, 50000, None) for i in range(11, 21)]
                batch_3 = [(i, 1000000, 50000, None) for i in range(21, 26)]

                mock_batch_result_1 = Mock()
                mock_batch_result_1.fetchall.return_value = batch_1

                mock_batch_result_2 = Mock()
                mock_batch_result_2.fetchall.return_value = batch_2

                mock_batch_result_3 = Mock()
                mock_batch_result_3.fetchall.return_value = batch_3

                mock_batch_result_empty = Mock()
                mock_batch_result_empty.fetchall.return_value = []

                # Mock storage update query
                mock_storage_result = Mock()
                mock_storage_result.first.return_value = (1000000,)

                def execute_side_effect(*args, **kwargs):
                    query = str(args[0]).lower() if args else ""
                    if "count(" in query:
                        return mock_count_result
                    elif "user_id" in query and "limit" in query:
                        if not hasattr(execute_side_effect, "batch_count"):
                            execute_side_effect.batch_count = 0
                        execute_side_effect.batch_count += 1
                        if execute_side_effect.batch_count == 1:
                            return mock_batch_result_1
                        elif execute_side_effect.batch_count == 2:
                            return mock_batch_result_2
                        elif execute_side_effect.batch_count == 3:
                            return mock_batch_result_3
                        else:
                            return mock_batch_result_empty
                    else:
                        return mock_storage_result

                mock_db.execute.side_effect = execute_side_effect

                # Run reconciliation job
                await StorageReconciliationJob.run()

                # Verify scan was called for all 25 users
                assert mock_scan.call_count == 25


@pytest.mark.asyncio
async def test_reconciliation_detects_drift():
    """Test that reconciliation job runs successfully and processes users with drift."""
    user_id = 123
    db_storage = 1000000000  # 1 GB in DB
    actual_storage = 1050000000  # 1.05 GB in S3 (5% drift)

    with patch("studio.app.common.db.database.session_scope") as mock_session:
        with patch(
            "studio.app.common.core.cloud.storage_tracking."
            "_perform_full_scan_and_reset_delta",
            new_callable=AsyncMock,
        ) as mock_scan:
            with patch(
                "studio.app.common.core.background.storage_reconciliation_job.MODE"
            ) as mock_mode:
                with patch(
                    "studio.app.common.core.background."
                    "storage_reconciliation_job.asyncio.sleep",
                    new_callable=AsyncMock,
                ):
                    mock_mode.IS_STANDALONE = False
                    db = _counting_db(
                        scannable=1,
                        orphans=0,
                        batch=[(user_id, db_storage, 50000000, None)],
                        after_scan=actual_storage,
                    )
                    mock_session.return_value.__enter__.return_value = db

                    await StorageReconciliationJob.run()

                    # The user was scanned, and the post-scan read that the drift
                    # calculation needs was issued
                    mock_scan.assert_called_once_with(user_id)
                    assert db.execute.call_count >= 4  # 2 counts + batch + storage


def _raise(exc):
    raise exc


def _counting_db(scannable, orphans, batch, after_scan=1000):
    """A mocked session whose two COUNT queries answer separately.

    The orphan count is the one carrying NOT EXISTS, which is how the job asks
    "how many candidate rows have no active owner".
    """
    db = Mock()
    scannable_count, orphan_count = Mock(), Mock()
    scannable_count.scalar.return_value = scannable
    orphan_count.scalar.return_value = orphans
    first_batch, empty = Mock(), Mock()
    first_batch.fetchall.return_value = batch
    empty.fetchall.return_value = []
    storage = Mock()
    storage.first.return_value = [after_scan]
    seen = {"batches": 0}
    db.queries = []

    def execute(*args, **kwargs):
        query = str(args[0]).lower()
        db.queries.append(query)
        if "count(" in query:
            return orphan_count if "not (exists" in query else scannable_count
        if "limit" in query:
            seen["batches"] += 1
            return first_batch if seen["batches"] == 1 else empty
        return storage

    db.execute.side_effect = execute
    return db


def _summary(mock_logger):
    lines = [
        str(call.args[0])
        for call in mock_logger.info.call_args_list
        if "reconciliation completed" in str(call.args[0])
    ]
    assert len(lines) == 1, mock_logger.info.call_args_list
    return lines[0]


@pytest.mark.asyncio
async def test_a_row_whose_owner_went_inactive_mid_run_is_skipped_not_zeroed():
    """The race: the owner is deactivated between selection and the scan.

    The bug this pins: the user lookup inside the scan raised 404, the broad
    handler swallowed it and returned 0, and the job wrote that 0 down and
    reported "0 errors" - a phantom user silently zeroed on a clean-looking run.
    """
    gone, present = 100748, 123

    with patch("studio.app.common.db.database.session_scope") as mock_session:
        with patch(
            "studio.app.common.core.cloud.storage_tracking."
            "_perform_full_scan_and_reset_delta",
            new_callable=AsyncMock,
        ) as mock_scan:
            with patch(
                "studio.app.common.core.background.storage_reconciliation_job.MODE"
            ) as mock_mode:
                with patch(
                    "studio.app.common.core.background."
                    "storage_reconciliation_job.asyncio.sleep",
                    new_callable=AsyncMock,
                ):
                    mock_mode.IS_STANDALONE = False
                    mock_session.return_value.__enter__.return_value = _counting_db(
                        scannable=2,
                        orphans=0,
                        batch=[(gone, 0, 0, None), (present, 1000, 0, None)],
                    )
                    mock_scan.side_effect = lambda user_id: (
                        _raise(StorageOwnerInactive(user_id))
                        if user_id == gone
                        else None
                    )

                    with patch(
                        "studio.app.common.core.background."
                        "storage_reconciliation_job.logger"
                    ) as mock_logger:
                        await StorageReconciliationJob.run()

                    assert mock_scan.call_count == 2
                    summary = _summary(mock_logger)
                    # The live user still reconciled; the other is named as
                    # skipped rather than counted as an error or as a success
                    assert "1/2 users reconciled" in summary
                    assert "0 errors" in summary
                    assert "1 skipped (no active user)" in summary
                    assert not mock_logger.error.called


@pytest.mark.asyncio
async def test_orphan_rows_are_filtered_out_of_the_batch_and_still_counted():
    """Rows with no active owner are excluded up front, not retried hourly.

    Nothing stamps last_full_scan on a row the scan cannot touch, so a row left
    in the candidate set would be attempted again every run, for good. They are
    filtered out - and reported, so the leftover data stays visible.

    The exclusion is asserted against the SQL the job emits, because a mocked
    session returns its canned batch whatever the WHERE clause says: asserting
    only on the rows handed back would pass with the filter deleted.
    """
    with patch("studio.app.common.db.database.session_scope") as mock_session:
        with patch(
            "studio.app.common.core.cloud.storage_tracking."
            "_perform_full_scan_and_reset_delta",
            new_callable=AsyncMock,
        ) as mock_scan:
            with patch(
                "studio.app.common.core.background.storage_reconciliation_job.MODE"
            ) as mock_mode:
                with patch(
                    "studio.app.common.core.background."
                    "storage_reconciliation_job.asyncio.sleep",
                    new_callable=AsyncMock,
                ):
                    mock_mode.IS_STANDALONE = False
                    db = _counting_db(
                        scannable=1, orphans=2, batch=[(123, 1000, 0, None)]
                    )
                    mock_session.return_value.__enter__.return_value = db

                    with patch(
                        "studio.app.common.core.background."
                        "storage_reconciliation_job.logger"
                    ) as mock_logger:
                        await StorageReconciliationJob.run()

                    # The batch select really asks for an active owner
                    batch_queries = [q for q in db.queries if "limit" in q]
                    assert batch_queries, db.queries
                    for query in batch_queries:
                        assert "exists" in query, query
                        assert "active" in query, query
                    # ...and the two counts are asked separately, one of them
                    # for the rows that have no active owner
                    counts = [q for q in db.queries if "count(" in q]
                    assert len(counts) == 2, counts
                    assert sum("not (exists" in q for q in counts) == 1, counts

                    mock_scan.assert_called_once_with(123)
                    summary = _summary(mock_logger)
                    assert "1/3 users reconciled" in summary
                    assert "0 errors" in summary
                    assert "2 skipped (no active user)" in summary
