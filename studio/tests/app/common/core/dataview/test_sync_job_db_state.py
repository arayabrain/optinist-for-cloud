"""Dataview sync and publish, asserted against DB state rather than call counts.

Covers the pending-selection statuses, the sync-status transitions, and the
publish toggle.

All three previously claimed coverage from classes that assert
``session.execute.called`` against a ``MagicMock``, which makes the DB values the
rows name unobservable: the query could select the wrong statuses, the transition
could write the wrong one, and every assertion would still pass.

Here the selection query is inspected as a compiled statement and the writes are
inspected as the values actually assigned. The real S3 round trip stays manual.
"""

from unittest.mock import MagicMock, patch

from sqlalchemy.dialects import mysql

from studio.app.common.core.background.sync_job import PublishedExperimentSyncJob
from studio.app.common.core.dataview.dataview_services import DataviewService
from studio.app.common.schemas.dataview import (
    LocalSyncStatus,
    PublishFlags,
    PublishStatus,
)

SYNC_MODULE = "studio.app.common.core.background.sync_job"


def _compiled_pending_query(rows=(), limit=None):
    """Capture the statement ``_get_pending_experiments`` executes."""
    with patch(f"{SYNC_MODULE}.session_scope") as mock_session:
        db = MagicMock()
        mock_session.return_value.__enter__.return_value = db
        db.execute.return_value = list(rows)

        experiments = PublishedExperimentSyncJob._get_pending_experiments(limit=limit)

    statement = db.execute.call_args.args[0]
    sql = " ".join(str(statement.compile(dialect=mysql.dialect())).split())
    params = statement.compile(dialect=mysql.dialect()).params
    return sql, params, experiments


def _selected_sync_statuses(params):
    """The ``local_sync_status`` values bound into the IN clause.

    ``LocalSyncStatus`` is a ``str`` enum, so these arrive as strings; matching
    on the bind-parameter name rather than on the value's type keeps this
    working if the column ever becomes an integer. SQLAlchemy may bind an
    ``IN`` clause either as one list parameter or as one parameter per element,
    so both shapes are flattened.
    """
    selected = set()
    for key, value in params.items():
        if "local_sync_status" not in key:
            continue
        if isinstance(value, (list, tuple, set)):
            selected.update(value)
        else:
            selected.add(value)
    assert selected, "no local_sync_status values were bound into the query"
    return selected


def test_the_sync_statuses_are_the_stored_strings():
    """``local_sync_status`` is a VARCHAR column holding these values, so renaming
    a member strands every row already written."""
    assert LocalSyncStatus.pending.value == "pending"
    assert LocalSyncStatus.synced.value == "synced"
    assert LocalSyncStatus.error.value == "error"


class TestPendingSelectionStatuses:
    """The sync job picks up exactly ``pending`` and ``error``.

    The status set is the retry policy. Dropping ``error`` means a transient S3
    failure is never retried and the experiment stays invisible on the public
    page forever; adding ``synced`` means every published experiment is
    re-validated on every run, which is a per-run S3 cost proportional to the
    whole catalog.
    """

    def test_only_pending_and_error_are_selected(self):
        _, params, _ = _compiled_pending_query()

        assert _selected_sync_statuses(params) == {
            LocalSyncStatus.pending.value,
            LocalSyncStatus.error.value,
        }, f"selected sync statuses were {_selected_sync_statuses(params)}"

    def test_synced_is_not_selected(self):
        """Named separately from the set comparison above because this is the
        expensive direction: it would pass silently and only show up as S3
        spend."""
        _, params, _ = _compiled_pending_query()

        assert LocalSyncStatus.synced.value not in _selected_sync_statuses(params)

    def test_the_selection_is_scoped_to_published_successful_live_experiments(self):
        """The other three predicates, asserted by their bound values.

        The compiled SQL renders every value as ``%s``, so matching the column
        name alone cannot tell ``publish_status = on`` from ``= off``. Losing or
        inverting any of these makes the job validate experiments that were never
        published, failed, or belong to a deleted workspace - all of which then
        get marked ``error`` and alerted on.
        """
        _, params, _ = _compiled_pending_query()

        assert params == {
            **params,
            "publish_status_1": PublishStatus.on.value,
            "deleted_1": 0,
            "success_1": 1,
        }, f"selection predicates bound unexpected values: {params}"

    def test_the_selection_is_bounded_by_the_configured_limit(self):
        """An unbounded query, or one with a limit large enough not to matter,
        would let a backlogged run exhaust the validation concurrency budget.

        Pinned to literals, not to the constants production reads: comparing a
        bind against the same attribute the query was built from passes for any
        value. The previous version also exercised the ``limit is None`` fallback
        to ``MAX_SYNC_PER_RUN``, which no production caller takes.
        """
        assert PublishedExperimentSyncJob.VALIDATION_LIMIT == 50
        assert PublishedExperimentSyncJob.VALIDATION_CONCURRENCY == 10

        _, params, _ = _compiled_pending_query(
            limit=PublishedExperimentSyncJob.VALIDATION_LIMIT
        )

        assert params["param_1"] == 50

    def test_the_validation_run_asks_for_the_validation_limit(self):
        """``_run_validation_logic`` is the only caller, and it always passes a
        limit, so ``MAX_SYNC_PER_RUN`` is a dead default."""
        import asyncio

        with patch.object(
            PublishedExperimentSyncJob, "_get_pending_experiments", return_value=[]
        ) as pending, patch.object(PublishedExperimentSyncJob, "_publish_metrics"):
            asyncio.run(PublishedExperimentSyncJob._run_validation_logic())

        pending.assert_called_once_with(limit=50)

    def test_a_users_own_bucket_wins_over_the_default(self):
        """The row tuple carries the bucket each experiment is validated against.
        Falling back to the default for a user who has their own bucket would
        validate against someone else's storage and mark the experiment
        ``error``."""
        with patch.dict(
            "os.environ", {"S3_DEFAULT_BUCKET_NAME": "default-bucket"}, clear=False
        ):
            _, _, experiments = _compiled_pending_query(
                rows=[
                    (1, "uid-own", 11, {"remote_bucket_name": "user-bucket"}),
                    (2, "uid-default", 12, None),
                ]
            )

        assert experiments == [
            ("1", "uid-own", 11, "user-bucket"),
            ("2", "uid-default", 12, "default-bucket"),
        ]


class TestSyncStatusTransitions:
    """The ``error -> synced`` recovery, and the metric that reports it.

    An experiment that failed validation once must be able to reach ``synced`` on
    a later run without operator action. If ``_mark_sync_complete`` writes
    anything else, a recovered experiment stays in the retry set permanently.
    """

    @staticmethod
    def _apply(method, exp_id, starting_status):
        experiment = MagicMock()
        experiment.local_sync_status = starting_status

        with patch(f"{SYNC_MODULE}.session_scope") as mock_session:
            db = MagicMock()
            mock_session.return_value.__enter__.return_value = db
            db.get.return_value = experiment

            method(exp_id)

        return experiment, db

    def test_an_errored_experiment_transitions_to_synced(self):
        experiment, db = self._apply(
            PublishedExperimentSyncJob._mark_sync_complete,
            exp_id=11,
            starting_status=LocalSyncStatus.error.value,
        )

        assert experiment.local_sync_status == LocalSyncStatus.synced.value
        db.commit.assert_called_once()

    def test_a_failed_validation_transitions_to_error(self):
        experiment, db = self._apply(
            PublishedExperimentSyncJob._mark_sync_error,
            exp_id=11,
            starting_status=LocalSyncStatus.pending.value,
        )

        assert experiment.local_sync_status == LocalSyncStatus.error.value
        db.commit.assert_called_once()

    def test_a_missing_experiment_is_not_written(self):
        """A row deleted between selection and write must not resurrect."""
        with patch(f"{SYNC_MODULE}.session_scope") as mock_session:
            db = MagicMock()
            mock_session.return_value.__enter__.return_value = db
            db.get.return_value = None

            PublishedExperimentSyncJob._mark_sync_complete(11)

        db.commit.assert_not_called()

    def test_the_synced_count_is_published_as_experiments_synced(self):
        """The metric operators alarm on. Publishing the error count under this
        name, or omitting it, makes a stalled sync job invisible."""
        with patch("boto3.client") as boto:
            cloudwatch = MagicMock()
            boto.return_value = cloudwatch

            PublishedExperimentSyncJob._publish_metrics(synced_count=4, error_count=1)

        metrics = {
            datum["MetricName"]: datum["Value"]
            for call in cloudwatch.put_metric_data.call_args_list
            for datum in call.kwargs["MetricData"]
        }
        assert metrics["ExperimentsSynced"] == 4
        assert metrics["SyncErrors"] == 1

    def test_the_metric_namespace_is_environment_scoped(self):
        """A shared namespace would blend dev and production counts into one
        alarm."""
        with patch("boto3.client") as boto, patch.dict(
            "os.environ", {"ENV_PREFIX": "test-env"}, clear=False
        ):
            cloudwatch = MagicMock()
            boto.return_value = cloudwatch

            PublishedExperimentSyncJob._publish_metrics(synced_count=1, error_count=0)

        namespaces = {
            call.kwargs["Namespace"]
            for call in cloudwatch.put_metric_data.call_args_list
        }
        assert namespaces == {"OptiNiSt/BackgroundJobs/test-env"}


class TestPublishToggleIsLastWriteWins:
    """Rapid publish / unpublish / publish toggles.

    Each toggle is one bulk UPDATE with no read-modify-write, so the final state
    is whatever the last statement set. What the row is really asking is that a
    toggle sets *both* columns coherently: publishing must re-queue the
    experiment for validation, and unpublishing must take it out of the retry set
    rather than leaving it pending forever.
    """

    @staticmethod
    def _update_dict(flag):
        db = MagicMock()
        DataviewService.multiple_publish_dataview_records(
            db, user_id=7, ids=[11, 12], flag=flag
        )
        update = db.query.return_value.filter.return_value.update
        update.assert_called_once()
        return {
            column.name: value for column, value in update.call_args.args[0].items()
        }, db

    def test_publishing_turns_the_flag_on_and_requeues_for_validation(self):
        values, _ = self._update_dict(PublishFlags.on)

        assert values["publish_status"] == PublishStatus.on.value
        assert values["local_sync_status"] == LocalSyncStatus.pending.value

    def test_unpublishing_turns_the_flag_off_and_leaves_the_retry_set(self):
        values, _ = self._update_dict(PublishFlags.off)

        assert values["publish_status"] == PublishStatus.off.value
        assert values["local_sync_status"] == LocalSyncStatus.synced.value

    def test_three_toggles_on_one_session_end_in_the_last_state(self):
        """publish -> unpublish -> publish against a single session, so the
        ordering is real rather than three independent invocations."""
        db = MagicMock()
        for flag in (PublishFlags.on, PublishFlags.off, PublishFlags.on):
            DataviewService.multiple_publish_dataview_records(
                db, user_id=7, ids=[11], flag=flag
            )

        update = db.query.return_value.filter.return_value.update
        assert update.call_count == 3, "each toggle must issue its own UPDATE"

        writes = [
            {column.name: value for column, value in call.args[0].items()}
            for call in update.call_args_list
        ]
        assert [w["publish_status"] for w in writes] == [
            PublishStatus.on.value,
            PublishStatus.off.value,
            PublishStatus.on.value,
        ]
        assert writes[-1]["local_sync_status"] == LocalSyncStatus.pending.value

    def test_the_update_is_scoped_to_the_calling_active_user(self):
        """Without the ownership predicates, an id list lets one user publish
        another user's experiment.

        Asserted against the predicates actually handed to ``filter`` rather than
        against the function's source text: a refactor that hoists them into an
        unused local leaves every source string in place while dropping the
        ownership check from the query.
        """
        db = MagicMock()
        DataviewService.multiple_publish_dataview_records(
            db, user_id=7, ids=[11, 12], flag=PublishFlags.on
        )

        predicates = db.query.return_value.filter.call_args.args
        rendered = " ".join(
            str(
                p.compile(
                    dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}
                )
            )
            for p in predicates
        )

        assert "users.id = 7" in rendered, f"no owner predicate in {rendered}"
        assert "users.active IS true" in rendered, f"no active check in {rendered}"
        assert "experiment_records.id IN (11, 12)" in rendered
        assert (
            "users.id = workspaces.user_id" in rendered
            and "workspaces.id = experiment_records.workspace_id" in rendered
        ), f"the join tying an experiment to its owner is missing: {rendered}"

    def test_the_write_is_committed(self):
        _, db = self._update_dict(PublishFlags.on)

        db.commit.assert_called_once()
