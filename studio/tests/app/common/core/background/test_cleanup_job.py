"""
Unit tests for data cleanup job.

Tests cleanup logic with S3 verification and orphaned data handling.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.dialects import mysql

from studio.app.common.core.background.cleanup_job import CleanupOutcome, DataCleanupJob
from studio.app.common.core.subscription.constants import SyncStatusConstants

MODULE = "studio.app.common.core.background.cleanup_job"


@pytest.fixture(autouse=True)
def _reset_instance_id_cache():
    """Clear the process-lifetime instance-id cache between tests."""
    DataCleanupJob._instance_id_cache = None
    yield
    DataCleanupJob._instance_id_cache = None


def _capture_selection_query(rows=()):
    """Run ``_get_users_for_cleanup`` against a mock session and return
    ``(statement, compiled_sql, bound_params, returned_users)``.

    The query is the whole safety boundary, so it is inspected as a compiled
    statement rather than through ``execute.called``: the values that decide
    whether a user's data is deleted only exist as bind parameters.
    """
    with patch(f"{MODULE}.session_scope") as mock_session:
        db = MagicMock()
        mock_session.return_value.__enter__.return_value = db
        db.execute.return_value = list(rows)

        users = DataCleanupJob._get_users_for_cleanup()

    statement = db.execute.call_args.args[0]
    compiled = statement.compile(dialect=mysql.dialect())
    return statement, str(compiled), compiled.params, users


class TestGetUsersForCleanupGraceWindow:
    """The logout grace window before irreversible deletion.

    ``_cleanup_user_data`` deletes the input directory unconditionally and the
    verified experiment outputs with it, so this WHERE clause is the only thing
    between a user who just logged out and the loss of their local workspace.
    Before this class the interval had no test at all.
    """

    def test_cutoff_is_now_minus_the_logout_grace_period(self):
        _, _, params, _ = _capture_selection_query()

        cutoffs = [v for v in params.values() if hasattr(v, "tzinfo")]
        assert len(cutoffs) == 1, f"expected exactly one datetime bind, got {cutoffs}"

        from studio.app.common.core.utils.datetime_utils import get_current_datetime

        expected = get_current_datetime() - timedelta(
            minutes=SyncStatusConstants.LOGOUT_GRACE_PERIOD_MINUTES
        )
        # The job reads the clock itself, so allow for the elapsed test time but
        # not for a grace period of a different length.
        drift = abs((cutoffs[0] - expected).total_seconds())
        assert drift < 60, (
            f"cutoff {cutoffs[0]} is not now - "
            f"{SyncStatusConstants.LOGOUT_GRACE_PERIOD_MINUTES}min ({expected})"
        )

    def test_the_grace_window_is_at_least_an_hour(self):
        """The assertion above recomputes the cutoff from the same constant, so
        it pins that production reads it, not how long the window is. Shrinking
        the constant to a minute would satisfy it while deleting the grace
        period this row exists to protect."""
        assert SyncStatusConstants.LOGOUT_GRACE_PERIOD_MINUTES >= 60

    def test_query_excludes_inside_the_window_and_includes_past_it(self):
        """Column and direction. Together with the cutoff asserted above, these
        two facts *are* the row's "excludes inside / includes past" behaviour:
        ``logged_out_at < now - 60min`` is false for a recent logout and true
        for an old one.

        Asserted as the compiled comparison rather than by feeding rows through
        a real database, so a flipped operator - which would select exactly the
        users still inside the window - fails here. The deployed check against
        real MySQL needs a deployed environment.
        """
        _, sql, _, _ = _normalise(_capture_selection_query())

        assert "free_user_assignments.logged_out_at < " in sql
        assert "free_user_assignments.logged_out_at IS NOT NULL" in sql

    def test_query_keeps_the_other_three_safety_predicates(self):
        """The interval is one of four conjuncts; dropping any of the others is
        also a data-loss bug, and they are cheap to pin from the same parse."""
        _, sql, params, _ = _normalise(_capture_selection_query())

        assert "free_user_assignments.active_workflow_count = " in sql
        assert "users.active = " in sql
        assert "workspaces.deleted = " in sql

        # The compiled SQL renders values as %s, so the names above cannot tell
        # `active_workflow_count = 0` from `= 99`. Assert the bound values too.
        assert params == {
            **params,
            "active_workflow_count_1": 0,
            "active_1": 1,
            "deleted_1": 0,
        }, f"safety predicates bound unexpected values: {params}"

    def test_a_row_with_a_nonzero_workflow_count_is_dropped_after_the_query(self):
        """The post-query re-check, which exists because the query alone is not
        trusted. A row that arrives with work in flight must not be returned."""
        _, _, _, users = _capture_selection_query(rows=[("7", "1,2", 0), ("8", "3", 2)])

        assert users == [("7", ["1", "2"])]


def _normalise(captured):
    """Collapse whitespace in the compiled SQL so assertions are not sensitive
    to SQLAlchemy's line wrapping."""
    statement, sql, params, users = captured
    return statement, " ".join(sql.split()), params, users


class TestVerifyS3Backup:
    """Test S3 backup verification"""

    def test_verify_s3_backup_exists(self):
        """Test verification when S3 backup exists"""
        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3
            mock_s3.head_object.return_value = {}

            result = DataCleanupJob._verify_s3_backup_exists(
                "test-bucket", "workspace1", "exp123"
            )

        assert result is True
        assert mock_s3.head_object.call_count == 2  # experiment.yaml, workflow.yaml
        # head_object must target the passed (per-user) bucket, not a default one
        for call in mock_s3.head_object.call_args_list:
            assert call.kwargs["Bucket"] == "test-bucket"

    def test_verify_s3_backup_missing(self):
        """Test verification when S3 backup is missing"""
        from botocore.exceptions import ClientError

        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3
            error_response = {"Error": {"Code": "404"}}
            mock_s3.head_object.side_effect = ClientError(error_response, "head_object")

            result = DataCleanupJob._verify_s3_backup_exists(
                "test-bucket", "workspace1", "exp123"
            )

        assert result is False

    def test_verify_s3_backup_no_bucket_resolved(self):
        """No bucket resolved → keep data (return False), never call S3"""
        with patch("boto3.client") as mock_boto:
            result = DataCleanupJob._verify_s3_backup_exists(
                None, "workspace1", "exp123"
            )

        assert result is False
        mock_boto.assert_not_called()


class TestResolveUserBucketName:
    """Test per-user S3 bucket resolution used before deletion"""

    def test_prefers_remote_bucket_name_from_db(self):
        """The bucket recorded on the user row wins (authoritative)."""
        user = MagicMock()
        user.remote_bucket_name = "development-optinist-user-7-abc123"

        mock_db = MagicMock()
        mock_db.execute.return_value.first.return_value = (user,)

        cm = MagicMock()
        cm.__enter__.return_value = mock_db
        cm.__exit__.return_value = False

        with patch(
            "studio.app.common.core.background.cleanup_job.session_scope",
            return_value=cm,
        ):
            result = DataCleanupJob._resolve_user_bucket_name("7")

        assert result == "development-optinist-user-7-abc123"

    def test_falls_back_to_deterministic_name(self):
        """No DB bucket → derive it with the writer's formula/prefix."""
        mock_db = MagicMock()
        mock_db.execute.return_value.first.return_value = None  # no user row

        cm = MagicMock()
        cm.__enter__.return_value = mock_db
        cm.__exit__.return_value = False

        with patch(
            "studio.app.common.core.background.cleanup_job.session_scope",
            return_value=cm,
        ):
            with patch.dict(
                "os.environ", {"S3_USER_BUCKET_PREFIX": "development-optinist-user"}
            ):
                with patch(
                    "studio.app.common.core.storage.remote_storage_controller."
                    "RemoteStorageController.create_user_bucket_name",
                    return_value="development-optinist-user-7-abc123",
                ) as mock_create:
                    result = DataCleanupJob._resolve_user_bucket_name("7")

        mock_create.assert_called_once_with(id=7, prefix="development-optinist-user")
        assert result == "development-optinist-user-7-abc123"


class TestCleanupUserData:
    """Test user data cleanup"""

    @patch.object(DataCleanupJob, "_verify_s3_input_backup_exists", return_value=True)
    @patch.object(
        DataCleanupJob, "_resolve_user_bucket_name", return_value="user-bucket"
    )
    @patch.object(DataCleanupJob, "_check_user_relogin", return_value=False)
    def test_cleanup_user_data_with_s3_verification(
        self, mock_relogin, mock_bucket, mock_input_verify
    ):
        """Test cleanup only deletes data with S3 backup"""
        with patch.object(
            DataCleanupJob, "_verify_s3_backup_exists", return_value=True
        ):
            with patch("os.path.exists", return_value=True):
                with patch("os.path.isdir", return_value=True):
                    with patch("os.listdir", return_value=["exp123"]):
                        with patch("shutil.rmtree") as mock_rmtree:
                            result = DataCleanupJob._cleanup_user_data(
                                "123", ["workspace1"]
                            )

        from studio.app.dir_path import DIRPATH

        assert result == CleanupOutcome.CLEANED
        # >= 1 was satisfied by the input directory alone, hiding the experiment
        assert mock_rmtree.call_count == 2
        deleted = [call[0][0] for call in mock_rmtree.call_args_list]
        assert deleted == [
            f"{DIRPATH.INPUT_DIR}/workspace1",
            f"{DIRPATH.OUTPUT_DIR}/workspace1/exp123",
        ]

    @patch.object(DataCleanupJob, "_verify_s3_input_backup_exists", return_value=True)
    @patch.object(
        DataCleanupJob, "_resolve_user_bucket_name", return_value="user-bucket"
    )
    @patch.object(DataCleanupJob, "_check_user_relogin", return_value=False)
    def test_cleanup_user_data_keeps_unverified(
        self, mock_relogin, mock_bucket, mock_input_verify
    ):
        """Unverified experiment outputs are kept locally → ERROR (data could
        not be safely deleted; an S3-backup failure must stay visible)."""
        with patch.object(
            DataCleanupJob, "_verify_s3_backup_exists", return_value=False
        ):
            with patch("os.path.exists", return_value=True):
                with patch("os.path.isdir", return_value=True):
                    with patch("os.listdir", return_value=["exp123"]):
                        with patch("shutil.rmtree") as mock_rmtree:
                            result = DataCleanupJob._cleanup_user_data(
                                "123", ["workspace1"]
                            )

        assert result == CleanupOutcome.ERROR
        # Input directory is deleted (backup verified, 1 call); experiments kept
        assert mock_rmtree.call_count == 1
        # Verify it was the input directory that was deleted
        assert "input" in str(mock_rmtree.call_args_list[0])

    @patch.object(
        DataCleanupJob, "_resolve_user_bucket_name", return_value="user-bucket"
    )
    @patch.object(DataCleanupJob, "_check_user_relogin", return_value=False)
    def test_cleanup_keeps_input_when_backup_unverified(
        self, mock_relogin, mock_bucket
    ):
        """Input with no verified S3 backup is kept, not deleted (data safety);
        the unsafe-to-delete retention is classified ERROR, not KEPT."""
        with patch.object(
            DataCleanupJob, "_verify_s3_input_backup_exists", return_value=False
        ):
            # Only the input dir exists (no output dir)
            def _exists(path):
                return "input" in str(path)

            with patch("os.path.exists", side_effect=_exists):
                with patch("shutil.rmtree") as mock_rmtree:
                    result = DataCleanupJob._cleanup_user_data("123", ["workspace1"])

        assert result == CleanupOutcome.ERROR
        mock_rmtree.assert_not_called()


class TestVerifyNoActiveWorkflows:
    """Test workflow verification before cleanup"""

    def test_verify_no_active_workflows_safe(self):
        """Test verification when no workflows active"""
        mock_assignment = MagicMock()
        mock_assignment.active_workflow_count = 0

        with patch(
            "studio.app.common.core.background.cleanup_job.session_scope"
        ) as mock:
            mock_session = MagicMock()
            mock.return_value.__enter__.return_value = mock_session
            # Mock execute() to return a row-like tuple
            mock_session.execute.return_value.first.return_value = (mock_assignment,)

            result = DataCleanupJob._verify_no_active_workflows("123")

        assert result is True

    def test_verify_no_active_workflows_unsafe(self):
        """Test verification when workflows are active"""
        mock_assignment = MagicMock()
        mock_assignment.active_workflow_count = 2

        with patch(
            "studio.app.common.core.background.cleanup_job.session_scope"
        ) as mock:
            mock_session = MagicMock()
            mock.return_value.__enter__.return_value = mock_session
            # Mock execute() to return a row-like tuple
            mock_session.execute.return_value.first.return_value = (mock_assignment,)

            result = DataCleanupJob._verify_no_active_workflows("123")

        assert result is False


class TestHandleOrphanedData:
    """Test orphaned data handling from terminated instances"""

    def test_handle_orphaned_data_terminated_instance(self):
        """Test DB-only cleanup of orphaned assignment from terminated instance.

        When an instance is terminated its EBS is destroyed, so
        _cleanup_orphaned_assignment should only remove the DB record
        (no _cleanup_user_data call).
        """
        mock_assignment = MagicMock()
        mock_assignment.user_id = "123"
        mock_assignment.instance_id = "i-12345"
        mock_assignment.active_workflow_count = 0

        with patch("boto3.client") as mock_boto:
            mock_ec2 = MagicMock()
            mock_boto.return_value = mock_ec2
            mock_ec2.describe_instances.return_value = {
                "Reservations": [{"Instances": [{"State": {"Name": "terminated"}}]}]
            }

            with patch(
                "studio.app.common.core.background.cleanup_job.session_scope"
            ) as mock:
                mock_session = MagicMock()
                mock.return_value.__enter__.return_value = mock_session
                mock_session.execute.return_value.all.return_value = [
                    (mock_assignment,)
                ]

                with patch.dict("os.environ", {"INSTANCE_ID": "i-current"}):
                    DataCleanupJob._handle_orphaned_data()

                mock_session.delete.assert_called_once_with(mock_assignment)

    def test_handle_orphaned_data_running_instance(self):
        """Test no cleanup for running instance"""
        mock_assignment = MagicMock()
        mock_assignment.instance_id = "i-12345"

        with patch("boto3.client") as mock_boto:
            mock_ec2 = MagicMock()
            mock_boto.return_value = mock_ec2
            mock_ec2.describe_instances.return_value = {
                "Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]
            }

            with patch(
                "studio.app.common.core.background.cleanup_job.session_scope"
            ) as mock:
                mock_session = MagicMock()
                mock.return_value.__enter__.return_value = mock_session
                # Mock execute() to return row-like tuples
                mock_session.execute.return_value.all.return_value = [
                    (mock_assignment,)
                ]

                with patch.dict("os.environ", {"INSTANCE_ID": "i-current"}):
                    DataCleanupJob._handle_orphaned_data()

                mock_session.delete.assert_not_called()

    def test_handle_orphaned_data_empty_reservations(self):
        """Test DB-only cleanup when EC2 returns empty Reservations"""
        mock_assignment = MagicMock()
        mock_assignment.user_id = "123"
        mock_assignment.instance_id = "i-12345"
        mock_assignment.active_workflow_count = 0

        with patch("boto3.client") as mock_boto:
            mock_ec2 = MagicMock()
            mock_boto.return_value = mock_ec2
            mock_ec2.describe_instances.return_value = {"Reservations": []}

            with patch(
                "studio.app.common.core.background.cleanup_job.session_scope"
            ) as mock:
                mock_session = MagicMock()
                mock.return_value.__enter__.return_value = mock_session
                mock_session.execute.return_value.all.return_value = [
                    (mock_assignment,)
                ]

                with patch.dict("os.environ", {"INSTANCE_ID": "i-current"}):
                    DataCleanupJob._handle_orphaned_data()

                mock_session.delete.assert_called_once_with(mock_assignment)

    def test_handle_orphaned_data_skipped_on_free_tier(self):
        """Test _handle_orphaned_data is skipped when ENABLE_LOCAL_CLEANUP=1"""
        import asyncio

        with patch("boto3.client"):
            with patch.dict(
                "os.environ",
                {"INSTANCE_ID": "i-current", "ENABLE_LOCAL_CLEANUP": "1"},
            ):
                # run() should NOT call _handle_orphaned_data
                with patch.object(
                    DataCleanupJob, "_handle_orphaned_data"
                ) as mock_orphan:
                    with patch.object(
                        DataCleanupJob, "_get_users_for_cleanup", return_value=[]
                    ):
                        asyncio.run(DataCleanupJob.run())
                    mock_orphan.assert_not_called()

    def test_handle_orphaned_data_runs_on_background_service(self):
        """Test _handle_orphaned_data runs when ENABLE_LOCAL_CLEANUP is not set

        (after the first, startup run — see the warm-up skip test below).
        """
        import asyncio

        with patch.dict("os.environ", {"INSTANCE_ID": "i-bg"}, clear=False):
            # Ensure ENABLE_LOCAL_CLEANUP is not set
            with patch.dict("os.environ", {}, clear=False):
                import os as _os

                _os.environ.pop("ENABLE_LOCAL_CLEANUP", None)

                # Simulate a process past its startup warm-up run.
                with patch.object(DataCleanupJob, "_orphan_sweep_warmup_done", True):
                    with patch.object(
                        DataCleanupJob, "_handle_orphaned_data"
                    ) as mock_orphan:
                        with patch.object(
                            DataCleanupJob, "_get_users_for_cleanup", return_value=[]
                        ):
                            asyncio.run(DataCleanupJob.run())
                        mock_orphan.assert_called_once()

    def test_handle_orphaned_data_skipped_on_first_run(self):
        """First run after startup skips the grace-less orphan sweep.

        Deferring it past the deploy window avoids deleting assignments for an
        instance that is still handing off during a rolling deploy.
        """
        import asyncio

        with patch.dict("os.environ", {"INSTANCE_ID": "i-bg"}, clear=False):
            with patch.dict("os.environ", {}, clear=False):
                import os as _os

                _os.environ.pop("ENABLE_LOCAL_CLEANUP", None)

                # Fresh process: warm-up run not yet done.
                with patch.object(DataCleanupJob, "_orphan_sweep_warmup_done", False):
                    with patch.object(
                        DataCleanupJob, "_handle_orphaned_data"
                    ) as mock_orphan:
                        with patch.object(
                            DataCleanupJob, "_get_users_for_cleanup", return_value=[]
                        ):
                            asyncio.run(DataCleanupJob.run())
                        mock_orphan.assert_not_called()


class TestGetCurrentInstanceId:
    """Test _get_current_instance_id helper"""

    def test_returns_env_var_when_set(self):
        with patch.dict("os.environ", {"INSTANCE_ID": "i-abc12345"}):
            assert DataCleanupJob._get_current_instance_id() == "i-abc12345"

    def test_returns_local_when_unset_and_no_metadata(self):
        # env unset → IMDS attempted → both fail → "local"
        with patch.dict("os.environ", {}, clear=True):
            with patch("urllib.request.urlopen", side_effect=Exception("no imds")):
                assert DataCleanupJob._get_current_instance_id() == "local"

    def test_returns_local_for_empty_string_and_no_metadata(self):
        with patch.dict("os.environ", {"INSTANCE_ID": ""}):
            with patch("urllib.request.urlopen", side_effect=Exception("no imds")):
                assert DataCleanupJob._get_current_instance_id() == "local"

    def test_falls_back_to_imds_when_env_unset(self):
        """When env is unset, the worker resolves via IMDS (matches middleware),
        so the per-instance filter is not silently dropped."""
        from unittest.mock import MagicMock

        token_resp = MagicMock()
        token_resp.read.return_value = b"token123"
        token_resp.__enter__.return_value = token_resp
        id_resp = MagicMock()
        id_resp.read.return_value = b"i-fromimds"
        id_resp.__enter__.return_value = id_resp

        with patch.dict("os.environ", {}, clear=True):
            with patch("urllib.request.urlopen", side_effect=[token_resp, id_resp]):
                assert DataCleanupJob._get_current_instance_id() == "i-fromimds"


class TestGetUsersForCleanupInstanceFilter:
    """Test instance_id filtering in _get_users_for_cleanup"""

    def test_filters_by_instance_id_when_set(self):
        """When INSTANCE_ID is set, query should include instance_id filter"""
        with patch.dict("os.environ", {"INSTANCE_ID": "i-abc12345"}):
            with patch(
                "studio.app.common.core.background.cleanup_job.session_scope"
            ) as mock_scope:
                mock_session = MagicMock()
                mock_scope.return_value.__enter__.return_value = mock_session
                mock_session.execute.return_value = iter([])

                result = DataCleanupJob._get_users_for_cleanup()

                assert result == []
                # Verify the SQL was executed (with instance_id filter)
                mock_session.execute.assert_called_once()
                # Check the compiled SQL contains instance_id
                call_args = mock_session.execute.call_args
                stmt = call_args[0][0]
                compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
                assert "instance_id" in compiled

    @patch.object(DataCleanupJob, "_get_current_instance_id", return_value="local")
    def test_no_filter_in_dev_mode(self, mock_instance):
        """When resolver returns 'local' (dev / no metadata), no instance filter"""
        with patch.dict("os.environ", {}, clear=True):
            with patch(
                "studio.app.common.core.background.cleanup_job.session_scope"
            ) as mock_scope:
                mock_session = MagicMock()
                mock_scope.return_value.__enter__.return_value = mock_session
                mock_session.execute.return_value = iter([])

                result = DataCleanupJob._get_users_for_cleanup()

                assert result == []
                mock_session.execute.assert_called_once()
                call_args = mock_session.execute.call_args
                stmt = call_args[0][0]
                compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
                assert "instance_id" not in compiled


class TestCleanupUserDataNotFound:
    """Test _cleanup_user_data no-data behavior (finding #1)"""

    @patch.object(DataCleanupJob, "_check_user_relogin", return_value=False)
    def test_returns_kept_when_no_local_data(self, mock_relogin):
        """No local data on this instance → KEPT (keep the DB record).

        instance_id is refreshed to the serving instance, so "no local data"
        must never close a record: a stray cross-instance request could point
        instance_id here while the real data lives on another instance. KEPT is
        a routine outcome, not an error.
        """
        with patch("os.path.exists", return_value=False):
            result = DataCleanupJob._cleanup_user_data("123", ["workspace1"])

        assert result == CleanupOutcome.KEPT

    @patch.object(DataCleanupJob, "_verify_s3_input_backup_exists", return_value=True)
    @patch.object(
        DataCleanupJob, "_resolve_user_bucket_name", return_value="user-bucket"
    )
    @patch.object(DataCleanupJob, "_check_user_relogin", return_value=False)
    def test_returns_cleaned_when_data_exists_and_cleaned(
        self, mock_relogin, mock_bucket, mock_input_verify
    ):
        """When data exists and is fully cleaned, returns CLEANED"""
        with patch.object(
            DataCleanupJob, "_verify_s3_backup_exists", return_value=True
        ):
            with patch("os.path.exists", return_value=True):
                with patch("os.path.isdir", return_value=True):
                    with patch("os.listdir", return_value=["exp123"]):
                        with patch("shutil.rmtree"):
                            result = DataCleanupJob._cleanup_user_data(
                                "123", ["workspace1"]
                            )

        assert result == CleanupOutcome.CLEANED


class TestRunOutcomeAccounting:
    """run() buckets each _cleanup_user_data outcome correctly (finding: metrics)."""

    def _run_with_outcome(self, outcome, relogin=False):
        import asyncio
        from contextlib import ExitStack

        with ExitStack() as stack:
            enter = stack.enter_context
            enter(
                patch.dict(
                    "os.environ",
                    {"INSTANCE_ID": "i-current", "ENABLE_LOCAL_CLEANUP": "1"},
                )
            )
            enter(patch.object(DataCleanupJob, "_handle_orphaned_data"))
            enter(
                patch.object(
                    DataCleanupJob,
                    "_get_users_for_cleanup",
                    return_value=[("123", ["ws1"])],
                )
            )
            if isinstance(relogin, (list, tuple)):
                enter(
                    patch.object(
                        DataCleanupJob,
                        "_check_user_relogin",
                        side_effect=list(relogin),
                    )
                )
            else:
                enter(
                    patch.object(
                        DataCleanupJob, "_check_user_relogin", return_value=relogin
                    )
                )
            enter(
                patch.object(
                    DataCleanupJob, "_verify_no_active_workflows", return_value=True
                )
            )
            enter(
                patch.object(DataCleanupJob, "_cleanup_user_data", return_value=outcome)
            )
            mock_mark = enter(patch.object(DataCleanupJob, "_mark_cleaned"))
            mock_metrics = enter(patch.object(DataCleanupJob, "_publish_metrics"))
            asyncio.run(DataCleanupJob.run())
        return mock_mark, mock_metrics

    def test_kept_is_not_cleaned_nor_error(self):
        """A KEPT user must not be closed, and must count as kept — not error."""
        mock_mark, mock_metrics = self._run_with_outcome(CleanupOutcome.KEPT)

        mock_mark.assert_not_called()
        mock_metrics.assert_called_once()
        cleaned, errors, kept = mock_metrics.call_args.args
        assert (cleaned, errors, kept) == (0, 0, 1)

    def test_cleaned_marks_and_counts_cleaned(self):
        """A CLEANED user is marked cleaned and counted as cleaned."""
        mock_mark, mock_metrics = self._run_with_outcome(CleanupOutcome.CLEANED)

        mock_mark.assert_called_once()
        cleaned, errors, kept = mock_metrics.call_args.args
        assert (cleaned, errors, kept) == (1, 0, 0)

    def test_error_counts_error_only(self):
        """An ERROR user is not closed and counts only as an error."""
        mock_mark, mock_metrics = self._run_with_outcome(CleanupOutcome.ERROR)

        mock_mark.assert_not_called()
        cleaned, errors, kept = mock_metrics.call_args.args
        assert (cleaned, errors, kept) == (0, 1, 0)

    def test_relogin_during_cleanup_counts_as_kept(self):
        """A user who returns *during* cleanup is kept, not errored — same
        bucket as the relogin abort inside _cleanup_user_data."""
        # 1st relogin check (pre-cleanup) False → proceed; 2nd (post-CLEANED)
        # True → the user came back mid-cleanup.
        mock_mark, mock_metrics = self._run_with_outcome(
            CleanupOutcome.CLEANED, relogin=[False, True]
        )

        mock_mark.assert_not_called()
        cleaned, errors, kept = mock_metrics.call_args.args
        assert (cleaned, errors, kept) == (0, 0, 1)


class TestVerifyS3InputBackup:
    """Test _verify_s3_input_backup_exists (finding: MEDIUM input deletion)."""

    def test_no_bucket_resolved_keeps_data(self):
        with patch("boto3.client") as mock_boto:
            result = DataCleanupJob._verify_s3_input_backup_exists(None, "workspace1")
        assert result is False
        mock_boto.assert_not_called()

    def test_all_files_present_returns_true(self):
        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3
            mock_s3.head_object.return_value = {}
            with patch("os.path.isdir", return_value=True):
                with patch(
                    "os.walk",
                    return_value=[("/in/workspace1", [".locks"], ["a.h5", "b.mat"])],
                ):
                    result = DataCleanupJob._verify_s3_input_backup_exists(
                        "user-bucket", "workspace1"
                    )
        assert result is True
        assert mock_s3.head_object.call_count == 2
        for call in mock_s3.head_object.call_args_list:
            assert call.kwargs["Bucket"] == "user-bucket"

    def test_missing_file_returns_false(self):
        from botocore.exceptions import ClientError

        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3
            mock_s3.head_object.side_effect = ClientError(
                {"Error": {"Code": "404"}}, "head_object"
            )
            with patch("os.path.isdir", return_value=True):
                with patch(
                    "os.walk",
                    return_value=[("/in/workspace1", [], ["a.h5"])],
                ):
                    result = DataCleanupJob._verify_s3_input_backup_exists(
                        "user-bucket", "workspace1"
                    )
        assert result is False

    def test_empty_input_dir_returns_true(self):
        """Nothing to verify (no files) → safe to delete the empty dir."""
        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3
            with patch("os.path.isdir", return_value=True):
                with patch("os.walk", return_value=[("/in/workspace1", [], [])]):
                    result = DataCleanupJob._verify_s3_input_backup_exists(
                        "user-bucket", "workspace1"
                    )
        assert result is True
        mock_s3.head_object.assert_not_called()


class TestCleanupOrphanedAssignmentNoFileCleanup:
    """Test _cleanup_orphaned_assignment does NOT call _cleanup_user_data"""

    def test_does_not_call_cleanup_user_data(self):
        """Terminated instance EBS is destroyed — only DB record removal needed"""
        mock_assignment = MagicMock()
        mock_assignment.user_id = "123"
        mock_assignment.instance_id = "i-12345"

        mock_db = MagicMock()

        with patch.object(DataCleanupJob, "_cleanup_user_data") as mock_cleanup:
            result = DataCleanupJob._cleanup_orphaned_assignment(
                mock_db, mock_assignment
            )

        assert result is True
        mock_cleanup.assert_not_called()
        mock_db.delete.assert_called_once_with(mock_assignment)
        mock_db.commit.assert_called_once()
