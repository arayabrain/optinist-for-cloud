"""Which table the workflow counter writes to, and the leak-proof decrement.

Every case in ``test_workflow_tracking.py`` asserts ``session.execute.called``
and patches ``_get_user_tier`` out - which is precisely the free-versus-premium
decision these cases exist to cover. A bug writing to ``FreeUserAssignment``
for a premium user passes today, and the consequences are asymmetric:

- writing the premium user's count to the free table leaves their real premium
  count at zero, so the idle sweep releases the dedicated instance out from under
  a running workflow;
- writing the free user's count to the premium table leaves their free count at
  zero, so ``DataCleanupJob`` deletes their workspace mid-run.

So these tests inject each tier result and assert the compiled UPDATE's target
table, which is the branch below the decision. ``_get_user_tier``'s own
derivation - resolving the subscription and comparing ``plan.id`` - is stubbed
here and has no test of its own; a bug that misclassifies a premium user inside
that function is not caught by this file.

The leak-proofing half is the ``finally`` block in ``snakemake_execute``: the
decrement must run even when execution raises, or the count leaks and the user is
permanently treated as busy. No test reached that block.
"""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.dialects import mysql

from studio.app.common.core.workflow.workflow_tracking import (
    TIER_FREE,
    TIER_PREMIUM,
    decrement_workflow_count,
    increment_workflow_count,
)
from studio.app.common.models import FreeUserAssignment, PremiumUserAssignment

MODULE = "studio.app.common.core.workflow.workflow_tracking"

USER_ID = 4321


def _run_with_tier(operation, tier, has_free_record, has_premium_record, rowcount=1):
    """Run increment/decrement with an injected tier and return every UPDATE it
    executed, as (table_name, compiled_sql).

    ``_get_user_tier`` is patched so each branch can be addressed directly. That
    makes the *routing* below the decision observable, not the decision itself.
    """
    with patch(f"{MODULE}._get_user_tier") as get_tier, patch(
        f"{MODULE}.session_scope"
    ) as mock_session, patch(f"{MODULE}.MODE") as mode:
        mode.IS_STANDALONE = False
        get_tier.return_value = (tier, has_free_record, has_premium_record)

        session = MagicMock()
        session.execute.return_value.rowcount = rowcount
        mock_session.return_value.__enter__.return_value = session

        operation(USER_ID)

    statements = []
    for call in session.execute.call_args_list:
        statement = call.args[0]
        statements.append(
            (
                statement.table.name,
                " ".join(str(statement.compile(dialect=mysql.dialect())).split()),
            )
        )
    return statements, session


class TestIncrementWritesToTheTierTable:
    """The tier branch is observable in the compiled UPDATE."""

    def test_a_premium_user_increments_the_premium_table(self):
        statements, _ = _run_with_tier(
            increment_workflow_count,
            tier=TIER_PREMIUM,
            has_free_record=False,
            has_premium_record=True,
        )

        assert [table for table, _ in statements] == [
            PremiumUserAssignment.__tablename__
        ]

    def test_a_free_user_increments_the_free_table(self):
        statements, _ = _run_with_tier(
            increment_workflow_count,
            tier=TIER_FREE,
            has_free_record=True,
            has_premium_record=False,
        )

        assert [table for table, _ in statements] == [FreeUserAssignment.__tablename__]

    def test_a_premium_user_with_both_records_never_touches_the_free_table(self):
        """A premium user who was previously on free still has a
        ``FreeUserAssignment`` row. Writing there would leave the premium count at
        zero and let the idle sweep release the instance mid-workflow.
        """
        statements, _ = _run_with_tier(
            increment_workflow_count,
            tier=TIER_PREMIUM,
            has_free_record=True,
            has_premium_record=True,
        )

        tables = [table for table, _ in statements]
        assert FreeUserAssignment.__tablename__ not in tables
        assert tables == [PremiumUserAssignment.__tablename__]

    def test_a_free_user_with_both_records_never_touches_the_premium_table(self):
        """The mirror case: a lapsed premium user's stale assignment row must not
        capture the count that ``DataCleanupJob`` reads before deleting data."""
        statements, _ = _run_with_tier(
            increment_workflow_count,
            tier=TIER_FREE,
            has_free_record=True,
            has_premium_record=True,
        )

        tables = [table for table, _ in statements]
        assert PremiumUserAssignment.__tablename__ not in tables
        assert tables == [FreeUserAssignment.__tablename__]

    def test_an_unknown_tier_falls_back_to_the_record_that_exists(self):
        """``_get_user_tier`` returns ``None`` when its query fails. Doing nothing
        would under-count and let the sweep reclaim a busy instance, so the
        fallback exists - and must still pick the table the user actually has.
        """
        statements, _ = _run_with_tier(
            increment_workflow_count,
            tier=None,
            has_free_record=True,
            has_premium_record=False,
        )

        assert [table for table, _ in statements] == [FreeUserAssignment.__tablename__]

    def test_a_user_with_no_assignment_row_is_not_written_anywhere(self):
        statements, session = _run_with_tier(
            increment_workflow_count,
            tier=TIER_FREE,
            has_free_record=False,
            has_premium_record=False,
        )

        assert statements == []
        session.commit.assert_not_called()

    def test_the_increment_also_stamps_the_workflow_start(self):
        """``last_workflow_start`` is what the idle selector reads to avoid
        migrating a user mid-run; an increment that skips it makes the count
        correct and the migration guard blind."""
        statements, _ = _run_with_tier(
            increment_workflow_count,
            tier=TIER_PREMIUM,
            has_free_record=False,
            has_premium_record=True,
        )

        _, sql = statements[0]
        assert "last_workflow_start" in sql
        assert "active_workflow_count" in sql


class TestDecrementWritesToTheTierTable:
    """The same, for the release half."""

    def test_a_premium_user_decrements_the_premium_table(self):
        statements, _ = _run_with_tier(
            decrement_workflow_count,
            tier=TIER_PREMIUM,
            has_free_record=True,
            has_premium_record=True,
        )

        assert [table for table, _ in statements] == [
            PremiumUserAssignment.__tablename__
        ]

    def test_a_free_user_decrements_the_free_table(self):
        statements, _ = _run_with_tier(
            decrement_workflow_count,
            tier=TIER_FREE,
            has_free_record=True,
            has_premium_record=True,
        )

        assert [table for table, _ in statements] == [FreeUserAssignment.__tablename__]

    def test_the_decrement_is_floored_at_zero(self):
        """A count driven negative never returns to zero, so the user is treated
        as permanently idle and their data becomes eligible for cleanup while
        they are still working."""
        statements, _ = _run_with_tier(
            decrement_workflow_count,
            tier=TIER_FREE,
            has_free_record=True,
            has_premium_record=False,
        )

        _, sql = statements[0]
        assert (
            "CASE" in sql.upper() or "GREATEST" in sql.upper()
        ), f"decrement has no floor against negative counts: {sql}"


class TestConcurrentCountsCannotBeLost:
    """Two workflows starting or finishing at once.

    A real two-connection race needs a real database and stays in the opt-in L3
    lane. What is assertable per-PR is the property that makes the race safe:
    both counters are computed by the *database*, as ``column +/- 1`` inside the
    UPDATE, never read into Python and written back.

    That distinction is the whole row. Read-modify-write loses one of two
    concurrent increments, and the lost increment is the dangerous direction: the
    count reaches zero while a workflow is still running, and the sweep reclaims
    the instance or the cleanup job deletes the workspace.
    """

    @staticmethod
    def _values_clause(sql):
        upper = sql.upper()
        start = upper.index(" SET ")
        end = upper.index(" WHERE ") if " WHERE " in upper else len(sql)
        return sql[start:end]

    def test_the_increment_is_computed_in_sql(self):
        statements, _ = _run_with_tier(
            increment_workflow_count,
            tier=TIER_PREMIUM,
            has_free_record=False,
            has_premium_record=True,
        )

        _, sql = statements[0]
        values = self._values_clause(sql)
        assert "active_workflow_count + " in values, (
            f"increment is not a SQL-side expression, so two concurrent starts "
            f"can lose one: {values}"
        )

    def test_the_decrement_is_computed_in_sql(self):
        statements, _ = _run_with_tier(
            decrement_workflow_count,
            tier=TIER_PREMIUM,
            has_free_record=False,
            has_premium_record=True,
        )

        _, sql = statements[0]
        values = self._values_clause(sql)
        assert "active_workflow_count - " in values, (
            f"decrement does not subtract from the stored count, so it "
            f"overwrites rather than adjusts: {values}"
        )

    def test_the_decrement_floor_does_not_replace_the_stored_count(self):
        """``GREATEST(0, count - 1)`` is safe; ``GREATEST(0, :python_value)`` is
        not. The floor must wrap an expression over the column, not a value
        computed before the statement ran.
        """
        statements, _ = _run_with_tier(
            decrement_workflow_count,
            tier=TIER_FREE,
            has_free_record=True,
            has_premium_record=False,
        )

        _, sql = statements[0]
        values = self._values_clause(sql).lower()

        assert "greatest(" in values
        floor_argument = values.split("greatest(", 1)[1]
        assert "active_workflow_count - " in floor_argument


class TestDecrementSurvivesAnExecutionFailure:
    """The ``finally`` block no test reached.

    ``snakemake_execute`` decrements in a ``finally`` precisely so a crashing
    workflow does not leak the count. Without it, one failed run leaves the user
    permanently at count 1: the idle sweep never reclaims their instance and
    ``DataCleanupJob`` never cleans their workspace.
    """

    @staticmethod
    def _execute(raises=None):
        from studio.app.common.core.snakemake import snakemake_executor

        executor = MagicMock()
        future = MagicMock()
        if raises is not None:
            future.result.side_effect = raises
        else:
            future.result.return_value = "done"
        executor.__enter__.return_value.submit.return_value = future

        with patch.object(
            snakemake_executor, "ProcessPoolExecutor", return_value=executor
        ), patch.object(
            snakemake_executor, "update_user_storage_after_workflow"
        ), patch.object(
            snakemake_executor, "get_client_id_for_subprocess", return_value="client"
        ), patch(
            f"{MODULE}.decrement_workflow_count"
        ) as decrement:
            outcome = None
            error = None
            try:
                outcome = snakemake_executor.snakemake_execute(
                    "1", "exp1", MagicMock(), user_id=USER_ID
                )
            except Exception as raised:  # noqa: BLE001 - re-inspected below
                error = raised

        return decrement, outcome, error

    def test_decrement_runs_after_a_successful_execution(self):
        decrement, outcome, error = self._execute()

        assert error is None
        assert outcome == "done"
        decrement.assert_called_once_with(USER_ID)

    def test_decrement_still_runs_when_execution_raises(self):
        decrement, _, error = self._execute(raises=RuntimeError("snakemake died"))

        assert isinstance(error, RuntimeError), "the failure must still propagate"
        decrement.assert_called_once_with(USER_ID)

    def test_the_original_failure_is_not_swallowed_by_the_decrement(self):
        """A ``finally`` that raises would replace the real error with a
        bookkeeping one and hide why the workflow failed."""
        from studio.app.common.core.snakemake import snakemake_executor

        executor = MagicMock()
        future = MagicMock()
        future.result.side_effect = RuntimeError("snakemake died")
        executor.__enter__.return_value.submit.return_value = future

        with patch.object(
            snakemake_executor, "ProcessPoolExecutor", return_value=executor
        ), patch.object(
            snakemake_executor, "update_user_storage_after_workflow"
        ), patch.object(
            snakemake_executor, "get_client_id_for_subprocess", return_value="client"
        ), patch(
            f"{MODULE}.decrement_workflow_count",
            side_effect=RuntimeError("db unreachable"),
        ):
            with pytest.raises(RuntimeError) as excinfo:
                snakemake_executor.snakemake_execute(
                    "1", "exp1", MagicMock(), user_id=USER_ID
                )

        assert "snakemake died" in str(excinfo.value)

    def test_no_decrement_is_attempted_without_a_user_id(self):
        """Standalone runs have no user to bill the count to."""
        from studio.app.common.core.snakemake import snakemake_executor

        executor = MagicMock()
        executor.__enter__.return_value.submit.return_value.result.return_value = "done"

        with patch.object(
            snakemake_executor, "ProcessPoolExecutor", return_value=executor
        ), patch.object(
            snakemake_executor, "update_user_storage_after_workflow"
        ), patch.object(
            snakemake_executor, "get_client_id_for_subprocess", return_value="client"
        ), patch(
            f"{MODULE}.decrement_workflow_count"
        ) as decrement:
            snakemake_executor.snakemake_execute("1", "exp1", MagicMock(), user_id=None)

        decrement.assert_not_called()


class TestIncrementIsWiredToWorkflowStart:
    """The other half of the pair.

    The decrement wiring is covered above via ``snakemake_execute``'s
    ``finally``; nothing pinned that anything ever increments. Without the
    increment the count stays at 0 for the whole run, so the idle sweep reclaims
    a premium instance and ``DataCleanupJob`` deletes a free user's workspace
    while their workflow is still running.
    """

    @staticmethod
    def _construct(user_id):
        from studio.app.common.core.workflow import workflow_runner as module

        run_item = MagicMock()
        run_item.nwbParam = None

        with patch.object(module, "WorkflowConfigWriter"), patch.object(
            module, "ExptConfigWriter"
        ), patch.object(module, "get_typecheck_params", return_value={}), patch.object(
            module, "Runner"
        ), patch(
            f"{MODULE}.increment_workflow_count"
        ) as increment:
            module.WorkflowRunner("bucket", "1", "exp1", run_item, user_id)

        return increment

    def test_constructing_a_runner_increments_the_count(self):
        self._construct(USER_ID).assert_called_once_with(USER_ID)

    def test_a_standalone_run_still_reaches_the_guard(self):
        """``increment_workflow_count`` owns the ``user_id is None`` decision, so
        the runner must hand it through rather than filtering first."""
        self._construct(None).assert_called_once_with(None)
