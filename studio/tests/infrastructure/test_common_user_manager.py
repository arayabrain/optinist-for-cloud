"""Tests for common_user_manager Lambda function."""

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock, patch

import pytest


def split_boto3_clients(mock_boto3):
    """Wire boto3.client to return a distinct mock per service name.

    The Lambda creates separate elbv2 and cloudwatch clients; sharing one mock
    would let an alarm delete on the wrong client pass undetected.
    """
    clients = {"elbv2": MagicMock(), "cloudwatch": MagicMock()}
    mock_boto3.client.side_effect = lambda service, *a, **k: clients[service]
    return clients["elbv2"], clients["cloudwatch"]


class TestRecoverStaleWorkflowCounts:
    """SQLAlchemy-based workflow recovery tests."""

    def test_no_stale_workflows(self, mock_env_vars_common):
        """No stale workflows returns all zeros."""
        with patch.dict("os.environ", mock_env_vars_common):
            import common_user_manager

            with patch.object(
                common_user_manager,
                "get_sqlalchemy_session",
            ) as mock_ctx:
                mock_session = MagicMock()
                mock_ctx.return_value.__enter__ = Mock(return_value=mock_session)
                mock_ctx.return_value.__exit__ = Mock(return_value=False)

                mock_result = MagicMock()
                mock_result.rowcount = 0
                mock_session.execute.return_value = mock_result

                result = common_user_manager.recover_stale_workflow_counts()

                assert result["recovered"] == 0
                assert result["free"] == 0
                assert result["premium"] == 0
                assert "error" not in result
                assert mock_session.execute.call_count == 2

    def test_recovers_stale_workflows(self, mock_env_vars_common):
        """Stale workflows found and reset."""
        with patch.dict("os.environ", mock_env_vars_common):
            import common_user_manager

            with patch.object(
                common_user_manager,
                "get_sqlalchemy_session",
            ) as mock_ctx:
                mock_session = MagicMock()
                mock_ctx.return_value.__enter__ = Mock(return_value=mock_session)
                mock_ctx.return_value.__exit__ = Mock(return_value=False)

                mock_free = MagicMock()
                mock_free.rowcount = 2
                mock_premium = MagicMock()
                mock_premium.rowcount = 1
                mock_session.execute.side_effect = [
                    mock_free,
                    mock_premium,
                ]

                result = common_user_manager.recover_stale_workflow_counts()

                assert result["recovered"] == 3
                assert result["free"] == 2
                assert result["premium"] == 1
                assert "error" not in result

    def test_database_error(self, mock_env_vars_common):
        """DB error returns recovered=0 with error key."""
        with patch.dict("os.environ", mock_env_vars_common):
            import common_user_manager

            with patch.object(
                common_user_manager,
                "get_sqlalchemy_session",
            ) as mock_ctx:
                mock_session = MagicMock()
                mock_ctx.return_value.__enter__ = Mock(return_value=mock_session)
                mock_ctx.return_value.__exit__ = Mock(return_value=False)

                mock_session.execute.side_effect = Exception("Connection refused")

                result = common_user_manager.recover_stale_workflow_counts()

                assert result["recovered"] == 0
                assert "Connection refused" in result["error"]


class TestCheckFreeUserInactivity:
    """Pymysql-based free user inactivity tests."""

    def _setup_db_mock(self, mock_env_vars_common):
        """Return patched env + mock connection/cursor."""
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = Mock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = Mock(return_value=False)
        return mock_conn, mock_cursor

    def test_no_inactive_users(self, mock_env_vars_common):
        """Empty fetchall returns logged_out=0."""
        mock_conn, mock_cursor = self._setup_db_mock(mock_env_vars_common)
        mock_cursor.fetchall.return_value = []

        with patch.dict("os.environ", mock_env_vars_common), patch(
            "common_user_manager.get_db_connection"
        ) as mock_db:
            mock_db.return_value = mock_conn

            from common_user_manager import check_free_user_inactivity

            result = check_free_user_inactivity()

            assert result["logged_out"] == 0
            assert "error" not in result

    def test_logout_inactive_users(self, mock_env_vars_common):
        """Inactive users deleted and committed."""
        mock_conn, mock_cursor = self._setup_db_mock(mock_env_vars_common)
        mock_cursor.fetchall.return_value = [
            {"user_id": "user1", "instance_id": "i-123"},
            {"user_id": "user2", "instance_id": "i-456"},
        ]

        with patch.dict("os.environ", mock_env_vars_common), patch(
            "common_user_manager.get_db_connection"
        ) as mock_db:
            mock_db.return_value = mock_conn

            from common_user_manager import check_free_user_inactivity

            result = check_free_user_inactivity()

            assert result["logged_out"] == 2
            assert "error" not in result
            # SELECT + DELETE
            assert mock_cursor.execute.call_count == 2
            mock_conn.commit.assert_called_once()

    def test_database_error(self, mock_env_vars_common):
        """DB error returns logged_out=0 with error."""
        mock_conn, mock_cursor = self._setup_db_mock(mock_env_vars_common)
        mock_cursor.fetchall.side_effect = Exception("Query failed")

        with patch.dict("os.environ", mock_env_vars_common), patch(
            "common_user_manager.get_db_connection"
        ) as mock_db, patch("common_user_manager.traceback.print_exc"):
            mock_db.return_value = mock_conn

            from common_user_manager import check_free_user_inactivity

            result = check_free_user_inactivity()

            assert result["logged_out"] == 0
            assert "error" in result


class TestCheckPremiumUserInactivity:
    """Pymysql + ALB cleanup tests for premium inactivity."""

    def _setup_db_mock(self):
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = Mock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = Mock(return_value=False)
        return mock_conn, mock_cursor

    def test_no_inactive_users(self, mock_env_vars_common):
        """Empty fetchall returns logged_out=0."""
        mock_conn, mock_cursor = self._setup_db_mock()
        mock_cursor.fetchall.return_value = []

        with patch.dict("os.environ", mock_env_vars_common), patch(
            "common_user_manager.get_db_connection"
        ) as mock_db, patch("common_user_manager.boto3"):
            mock_db.return_value = mock_conn

            from common_user_manager import check_premium_user_inactivity

            result = check_premium_user_inactivity()

            assert result["logged_out"] == 0
            assert "error" not in result

    def test_logout_with_alb_cleanup(self, mock_env_vars_common):
        """Inactive premium user triggers ALB rule + TG delete, and the TG's
        unhealthy-host alarm goes with it. The alarm delete is best-effort in
        the Lambda (exceptions are swallowed), so without this assertion a
        leaked alarm would page on a target group that no longer exists."""
        mock_conn, mock_cursor = self._setup_db_mock()
        mock_cursor.fetchall.return_value = [
            {
                "user_id": 123,
                "target_group_arn": (
                    "arn:aws:elasticloadbalancing:region:account:"
                    "targetgroup/premium-123-tg/xyz"
                ),
                "alb_rule_arn": "arn:aws:rule/user-rule",
            }
        ]

        with patch.dict("os.environ", mock_env_vars_common), patch(
            "common_user_manager.get_db_connection"
        ) as mock_db, patch("common_user_manager.boto3") as mock_boto3:
            mock_db.return_value = mock_conn
            mock_elbv2, mock_cloudwatch = split_boto3_clients(mock_boto3)

            from common_user_manager import check_premium_user_inactivity

            result = check_premium_user_inactivity()

            assert result["logged_out"] == 1
            assert result.get("failed", 0) == 0
            mock_elbv2.delete_rule.assert_called_once()
            mock_elbv2.delete_target_group.assert_called_once()
            mock_cloudwatch.delete_alarms.assert_called_once_with(
                AlarmNames=["test-premium-123-tg-unhealthy-hosts"]
            )
            mock_conn.commit.assert_called_once()

    def test_skips_standby_markers(self, mock_env_vars_common):
        """Standby/reserving markers skip ALB cleanup."""
        mock_conn, mock_cursor = self._setup_db_mock()
        mock_cursor.fetchall.return_value = [
            {
                "user_id": "standby_user",
                "target_group_arn": "standby",
                "alb_rule_arn": "standby",
            }
        ]

        with patch.dict("os.environ", mock_env_vars_common), patch(
            "common_user_manager.get_db_connection"
        ) as mock_db, patch("common_user_manager.boto3") as mock_boto3:
            mock_db.return_value = mock_conn
            mock_elbv2, mock_cloudwatch = split_boto3_clients(mock_boto3)

            from common_user_manager import check_premium_user_inactivity

            result = check_premium_user_inactivity()

            assert result["logged_out"] == 1
            mock_elbv2.delete_rule.assert_not_called()
            mock_elbv2.delete_target_group.assert_not_called()
            mock_cloudwatch.delete_alarms.assert_not_called()

    def test_skips_autoscaling_tg(self, mock_env_vars_common):
        """Autoscaling TG is never deleted during cleanup."""
        mock_conn, mock_cursor = self._setup_db_mock()
        asg_tg = mock_env_vars_common["AUTOSCALING_TARGET_GROUP_ARN"]
        mock_cursor.fetchall.return_value = [
            {
                "user_id": "asg_user",
                "target_group_arn": asg_tg,
                "alb_rule_arn": "arn:aws:rule/asg-rule",
            }
        ]

        with patch.dict("os.environ", mock_env_vars_common), patch(
            "common_user_manager.get_db_connection"
        ) as mock_db, patch("common_user_manager.boto3") as mock_boto3:
            mock_db.return_value = mock_conn
            mock_elbv2, mock_cloudwatch = split_boto3_clients(mock_boto3)

            from common_user_manager import check_premium_user_inactivity

            result = check_premium_user_inactivity()

            assert result["logged_out"] == 1
            mock_elbv2.delete_rule.assert_called_once()
            mock_elbv2.delete_target_group.assert_not_called()
            mock_cloudwatch.delete_alarms.assert_not_called()

    def test_partial_failure(self, mock_env_vars_common):
        """One user fails, other succeeds."""
        mock_conn, mock_cursor = self._setup_db_mock()
        mock_cursor.fetchall.return_value = [
            {
                "user_id": "user1",
                "target_group_arn": "arn:aws:tg/u1",
                "alb_rule_arn": "arn:aws:rule/u1",
            },
            {
                "user_id": "user2",
                "target_group_arn": "arn:aws:tg/u2",
                "alb_rule_arn": "arn:aws:rule/u2",
            },
        ]

        with patch.dict("os.environ", mock_env_vars_common), patch(
            "common_user_manager.get_db_connection"
        ) as mock_db, patch("common_user_manager.boto3") as mock_boto3:
            mock_db.return_value = mock_conn
            mock_elbv2 = MagicMock()
            mock_elbv2.delete_rule.side_effect = [
                None,
                Exception("ALB error"),
            ]
            mock_boto3.client.return_value = mock_elbv2

            from common_user_manager import check_premium_user_inactivity

            result = check_premium_user_inactivity()

            assert result["logged_out"] == 1
            assert result["failed"] == 1


class TestHandler:
    """Lambda handler orchestration tests."""

    def test_successful_execution(self, mock_env_vars_common):
        """Handler returns 200 with results from all steps."""
        with patch.dict("os.environ", mock_env_vars_common), patch(
            "common_user_manager.recover_stale_workflow_counts"
        ) as mock_recover, patch(
            "common_user_manager.check_free_user_inactivity"
        ) as mock_free, patch(
            "common_user_manager.check_premium_user_inactivity"
        ) as mock_premium, patch(
            "common_user_manager.reap_terminated_ecs_registrations"
        ) as mock_reap:
            mock_recover.return_value = {"recovered": 2}
            mock_free.return_value = {"logged_out": 1}
            mock_premium.return_value = {"logged_out": 0}
            mock_reap.return_value = {"deregistered": 0}

            from common_user_manager import handler

            event = {"source": "aws.events"}
            context = MagicMock()
            context.aws_request_id = "test-123"

            result = handler(event, context)

            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert "results" in body
            mock_recover.assert_called_once()
            mock_free.assert_called_once()
            mock_premium.assert_called_once()
            mock_reap.assert_called_once()

    def test_handler_error_returns_500(self, mock_env_vars_common):
        """Unhandled exception returns 500."""
        with patch.dict("os.environ", mock_env_vars_common), patch(
            "common_user_manager.recover_stale_workflow_counts"
        ) as mock_recover, patch("common_user_manager.traceback.print_exc"):
            mock_recover.side_effect = RuntimeError("Unexpected crash")

            from common_user_manager import handler

            event = {"source": "aws.events"}
            context = MagicMock()
            context.aws_request_id = "test-456"

            result = handler(event, context)

            assert result["statusCode"] == 500
            body = json.loads(result["body"])
            assert "Unexpected crash" in body["error"]

    def test_step_failure_does_not_fail_the_sweep(self, mock_env_vars_common):
        """A step that fails internally reports its error in the body while the
        handler still returns 200, so one broken step never stops the sweep
        (and the 10-minute schedule keeps retrying). Drives the real step
        functions against a failing DB rather than stubbing their results."""
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = Exception("Critical database error")
        mock_conn = MagicMock()
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = Mock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = Mock(return_value=False)

        with patch.dict("os.environ", mock_env_vars_common), patch(
            "common_user_manager.get_db_connection", return_value=mock_conn
        ), patch(
            "common_user_manager.get_sqlalchemy_session",
            side_effect=Exception("Critical database error"),
        ), patch(
            "common_user_manager.reap_terminated_ecs_registrations",
            return_value={"deregistered": 0},
        ), patch(
            "common_user_manager.boto3"
        ), patch(
            "common_user_manager.traceback.print_exc"
        ):
            from common_user_manager import handler

            result = handler({"source": "aws.events"}, MagicMock())

            assert result["statusCode"] == 200
            results = json.loads(result["body"])["results"]
            for step in ("workflow_recovery", "free_inactivity", "premium_inactivity"):
                assert "error" in results[step]
            assert results["free_inactivity"]["logged_out"] == 0
            assert results["premium_inactivity"]["logged_out"] == 0


class TestHandlerGhostReap:
    """Handler gates the ghost reaper on CLUSTER_NAME being set."""

    def _patches(self):
        return (
            patch(
                "common_user_manager.recover_stale_workflow_counts",
                return_value={"recovered": 0},
            ),
            patch(
                "common_user_manager.check_free_user_inactivity",
                return_value={"logged_out": 0},
            ),
            patch(
                "common_user_manager.check_premium_user_inactivity",
                return_value={"logged_out": 0},
            ),
            patch(
                "common_user_manager.reap_terminated_ecs_registrations",
                return_value={"deregistered": 1},
            ),
        )

    def test_reap_runs_when_cluster_set(self, mock_env_vars_common):
        env = {**mock_env_vars_common, "CLUSTER_NAME": "test-cluster"}
        p_recover, p_free, p_premium, p_reap = self._patches()
        with patch.dict(
            "os.environ", env
        ), p_recover, p_free, p_premium, p_reap as mock_reap:
            from common_user_manager import handler

            result = handler({"source": "aws.events"}, MagicMock())

            assert result["statusCode"] == 200
            mock_reap.assert_called_once()

    def test_reap_skipped_when_cluster_unset(self, mock_env_vars_common):
        p_recover, p_free, p_premium, p_reap = self._patches()
        with patch.dict(
            "os.environ", mock_env_vars_common
        ), p_recover, p_free, p_premium, p_reap as mock_reap:
            os.environ.pop("CLUSTER_NAME", None)

            from common_user_manager import handler

            result = handler({"source": "aws.events"}, MagicMock())

            assert result["statusCode"] == 200
            mock_reap.assert_not_called()


class TestReapTerminatedECSRegistrations:
    """Tier-independent terminated-EC2 ghost reaper."""

    def _run(self, container_instances, ec2_states, mock_env_vars_common):
        """Invoke the reaper with mocked ECS/EC2; return (ecs_mock, result)."""
        mock_ecs = MagicMock()
        mock_ec2 = MagicMock()

        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "containerInstanceArns": [
                    ci["containerInstanceArn"] for ci in container_instances
                ]
            }
        ]
        mock_ecs.get_paginator.return_value = paginator
        mock_ecs.describe_container_instances.return_value = {
            "containerInstances": container_instances
        }
        mock_ec2.describe_instances.return_value = {
            "Reservations": [
                {"Instances": [{"InstanceId": iid, "State": {"Name": state}}]}
                for iid, state in ec2_states.items()
            ]
        }

        def _client(service, *a, **k):
            return {"ecs": mock_ecs, "ec2": mock_ec2}[service]

        env = {**mock_env_vars_common, "CLUSTER_NAME": "test-cluster"}
        with patch.dict("os.environ", env):
            import common_user_manager

            with patch.object(common_user_manager.boto3, "client", side_effect=_client):
                result = common_user_manager.reap_terminated_ecs_registrations()
        return mock_ecs, result

    def test_reaps_terminated_shutting_down_nomap_and_gone(self, mock_env_vars_common):
        cis = [
            {"containerInstanceArn": "arn-term", "ec2InstanceId": "i-term"},
            {"containerInstanceArn": "arn-shut", "ec2InstanceId": "i-shut"},
            {"containerInstanceArn": "arn-nomap"},
            {"containerInstanceArn": "arn-gone", "ec2InstanceId": "i-gone"},
        ]
        # i-gone deliberately omitted from states → resolves as nonexistent.
        states = {"i-term": "terminated", "i-shut": "shutting-down"}
        mock_ecs, result = self._run(cis, states, mock_env_vars_common)

        assert result["deregistered"] == 4
        reaped = {
            c.kwargs["containerInstance"]
            for c in mock_ecs.deregister_container_instance.call_args_list
        }
        assert reaped == {"arn-term", "arn-shut", "arn-nomap", "arn-gone"}
        for c in mock_ecs.deregister_container_instance.call_args_list:
            assert c.kwargs["force"] is True

    def test_does_not_reap_stopped(self, mock_env_vars_common):
        """Regression guard (PR #676): a stopped instance must survive so it
        reconnects on the next restart."""
        cis = [{"containerInstanceArn": "arn-stop", "ec2InstanceId": "i-stop"}]
        mock_ecs, result = self._run(cis, {"i-stop": "stopped"}, mock_env_vars_common)

        assert result["deregistered"] == 0
        mock_ecs.deregister_container_instance.assert_not_called()

    def test_does_not_reap_running(self, mock_env_vars_common):
        cis = [
            {
                "containerInstanceArn": "arn-run",
                "ec2InstanceId": "i-run",
                "agentConnected": True,
                "status": "ACTIVE",
            }
        ]
        mock_ecs, result = self._run(cis, {"i-run": "running"}, mock_env_vars_common)

        assert result["deregistered"] == 0
        mock_ecs.deregister_container_instance.assert_not_called()

    def test_missing_cluster_name_is_noop(self, mock_env_vars_common):
        with patch.dict("os.environ", mock_env_vars_common):
            os.environ.pop("CLUSTER_NAME", None)
            import common_user_manager

            with patch.object(common_user_manager.boto3, "client") as mock_client:
                result = common_user_manager.reap_terminated_ecs_registrations()

        assert result["deregistered"] == 0
        mock_client.assert_not_called()

    def test_batch_notfound_falls_back_per_id(self, mock_env_vars_common):
        from botocore.exceptions import ClientError

        cis = [
            {"containerInstanceArn": "arn-a", "ec2InstanceId": "i-a"},
            {"containerInstanceArn": "arn-b", "ec2InstanceId": "i-b"},
        ]
        mock_ecs = MagicMock()
        mock_ec2 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"containerInstanceArns": ["arn-a", "arn-b"]}
        ]
        mock_ecs.get_paginator.return_value = paginator
        mock_ecs.describe_container_instances.return_value = {"containerInstances": cis}
        notfound = ClientError(
            {"Error": {"Code": "InvalidInstanceID.NotFound"}}, "DescribeInstances"
        )

        def _describe(InstanceIds):
            if len(InstanceIds) > 1:
                raise notfound
            if InstanceIds == ["i-a"]:
                return {
                    "Reservations": [
                        {
                            "Instances": [
                                {"InstanceId": "i-a", "State": {"Name": "running"}}
                            ]
                        }
                    ]
                }
            raise notfound

        mock_ec2.describe_instances.side_effect = _describe

        def _client(service, *a, **k):
            return {"ecs": mock_ecs, "ec2": mock_ec2}[service]

        env = {**mock_env_vars_common, "CLUSTER_NAME": "test-cluster"}
        with patch.dict("os.environ", env):
            import common_user_manager

            with patch.object(common_user_manager.boto3, "client", side_effect=_client):
                result = common_user_manager.reap_terminated_ecs_registrations()

        # i-a running → kept; i-b NotFound → gone → reaped.
        assert result["deregistered"] == 1
        mock_ecs.deregister_container_instance.assert_called_once()
        assert (
            mock_ecs.deregister_container_instance.call_args.kwargs["containerInstance"]
            == "arn-b"
        )

    def test_dedupes_shared_ec2(self, mock_env_vars_common):
        """Two CIs on the same EC2 (dual-CI) collapse to one describe_instances
        ID; both CIs on a terminated EC2 are still reaped."""
        cis = [
            {"containerInstanceArn": "arn-a", "ec2InstanceId": "i-shared"},
            {"containerInstanceArn": "arn-b", "ec2InstanceId": "i-shared"},
        ]
        mock_ecs = MagicMock()
        mock_ec2 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"containerInstanceArns": ["arn-a", "arn-b"]}
        ]
        mock_ecs.get_paginator.return_value = paginator
        mock_ecs.describe_container_instances.return_value = {"containerInstances": cis}
        mock_ec2.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        {"InstanceId": "i-shared", "State": {"Name": "terminated"}}
                    ]
                }
            ]
        }

        def _client(service, *a, **k):
            return {"ecs": mock_ecs, "ec2": mock_ec2}[service]

        env = {**mock_env_vars_common, "CLUSTER_NAME": "test-cluster"}
        with patch.dict("os.environ", env):
            import common_user_manager

            with patch.object(common_user_manager.boto3, "client", side_effect=_client):
                result = common_user_manager.reap_terminated_ecs_registrations()

        mock_ec2.describe_instances.assert_called_once()
        assert mock_ec2.describe_instances.call_args.kwargs["InstanceIds"] == [
            "i-shared"
        ]
        assert result["deregistered"] == 2

    def test_chunks_describe_instances_over_100(self, mock_env_vars_common):
        """describe_instances is chunked at 100 IDs so a large cluster cannot
        exceed the EC2 max-IDs-per-request limit."""
        cis = [
            {"containerInstanceArn": f"arn-{n}", "ec2InstanceId": f"i-{n}"}
            for n in range(150)
        ]
        mock_ecs = MagicMock()
        mock_ec2 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"containerInstanceArns": [ci["containerInstanceArn"] for ci in cis]}
        ]
        mock_ecs.get_paginator.return_value = paginator
        by_arn = {ci["containerInstanceArn"]: ci for ci in cis}

        def _describe_cis(cluster, containerInstances):
            return {"containerInstances": [by_arn[a] for a in containerInstances]}

        mock_ecs.describe_container_instances.side_effect = _describe_cis

        chunk_sizes = []

        def _describe(InstanceIds):
            chunk_sizes.append(len(InstanceIds))
            return {
                "Reservations": [
                    {
                        "Instances": [
                            {"InstanceId": iid, "State": {"Name": "running"}}
                            for iid in InstanceIds
                        ]
                    }
                ]
            }

        mock_ec2.describe_instances.side_effect = _describe

        def _client(service, *a, **k):
            return {"ecs": mock_ecs, "ec2": mock_ec2}[service]

        env = {**mock_env_vars_common, "CLUSTER_NAME": "test-cluster"}
        with patch.dict("os.environ", env):
            import common_user_manager

            with patch.object(common_user_manager.boto3, "client", side_effect=_client):
                result = common_user_manager.reap_terminated_ecs_registrations()

        assert sorted(chunk_sizes, reverse=True) == [100, 50]
        assert result["deregistered"] == 0


class TestGetRequiredEnvVar:
    """Environment variable validation tests."""

    def test_missing_var_raises(self):
        """Missing env var without default raises ValueError."""
        import common_user_manager
        import pytest

        with pytest.raises(ValueError, match="Missing required"):
            common_user_manager.get_required_env_var("NONEXISTENT_VAR_XYZ")

    def test_default_value(self):
        """Missing env var with default returns default."""
        import common_user_manager

        result = common_user_manager.get_required_env_var(
            "NONEXISTENT_VAR_XYZ", "fallback"
        )
        assert result == "fallback"

    def test_empty_var_raises(self, mock_env_vars_common):
        """Empty string env var raises ValueError."""
        import common_user_manager
        import pytest

        with patch.dict("os.environ", {"EMPTY_TEST": ""}):
            with pytest.raises(ValueError, match="Missing required"):
                common_user_manager.get_required_env_var("EMPTY_TEST")


class TestRecoverStaleWorkflowCountsPredicates:
    """Which rows the sweep is willing to touch.

    The tests above assert the returned counts against a mocked `rowcount`, which
    holds whatever the mock was told to hold - so the conditions deciding *which*
    rows get reset are invisible to them. The statement is inspected here instead,
    because the deciding values reach the database as bind parameters.

    What makes this worth pinning: resetting `active_workflow_count` on a user
    whose workflow is still running lets them start a second one on an instance
    already busy with the first. The guard against that is the conjunction below,
    not any single condition in it.
    """

    def compiled_statements(self, mock_env_vars_common):
        with patch.dict("os.environ", mock_env_vars_common):
            import common_user_manager

            with patch.object(
                common_user_manager, "get_sqlalchemy_session"
            ) as mock_ctx:
                session = MagicMock()
                mock_ctx.return_value.__enter__ = Mock(return_value=session)
                mock_ctx.return_value.__exit__ = Mock(return_value=False)
                result = MagicMock()
                result.rowcount = 0
                session.execute.return_value = result

                common_user_manager.recover_stale_workflow_counts()

        return [
            call.args[0].compile(compile_kwargs={"literal_binds": False})
            for call in session.execute.call_args_list
        ]

    def test_both_tiers_are_swept(self, mock_env_vars_common):
        statements = self.compiled_statements(mock_env_vars_common)

        assert len(statements) == 2
        tables = [str(statement).split()[1] for statement in statements]
        assert tables == ["free_user_assignments", "premium_user_assignments"]

    @pytest.mark.parametrize("tier", [0, 1])
    def test_only_a_nonzero_counter_is_touched(self, mock_env_vars_common, tier):
        """A row already at zero is not stale work, and rewriting it to zero would
        report a recovery that did not happen."""
        statement = str(self.compiled_statements(mock_env_vars_common)[tier])

        assert "active_workflow_count > " in statement

    @pytest.mark.parametrize("tier", [0, 1])
    def test_the_counter_is_reset_rather_than_decremented(
        self, mock_env_vars_common, tier
    ):
        statements = self.compiled_statements(mock_env_vars_common)
        params = statements[tier].params

        assert params["active_workflow_count"] == 0

    @pytest.mark.parametrize("tier", [0, 1])
    def test_a_user_with_a_live_session_is_left_alone(self, mock_env_vars_common, tier):
        """The heartbeat is the whole reason a legitimate four-hour run survives
        the sweep: `last_activity` recent means the user is still at the keyboard,
        so the workflow is presumed alive however long it has been going."""
        statement = str(self.compiled_statements(mock_env_vars_common)[tier])

        assert "last_activity IS NOT NULL" in statement
        assert "last_activity < " in statement

    @pytest.mark.parametrize("tier", [0, 1])
    def test_inactivity_alone_is_not_enough(self, mock_env_vars_common, tier):
        """Inactivity is ANDed with a disjunction of two positive signals that the
        workflow is over. Dropping the OR would reset the counter of every idle
        user with a running workflow."""
        statement = str(self.compiled_statements(mock_env_vars_common)[tier])

        assert " OR " in statement
        assert "last_workflow_end >= " in statement
        assert "last_workflow_start < " in statement

    @pytest.mark.parametrize("tier", [0, 1])
    def test_null_timestamps_are_excluded_rather_than_swept(
        self, mock_env_vars_common, tier
    ):
        """A row with no `last_workflow_start` has no evidence either way. MySQL
        compares NULL as unknown, so an unguarded comparison silently drops it -
        the explicit checks keep that from depending on SQL trivia."""
        statement = str(self.compiled_statements(mock_env_vars_common)[tier])

        assert "last_workflow_end IS NOT NULL" in statement
        assert "last_workflow_start IS NOT NULL" in statement

    def test_the_cutoffs_are_the_configured_thresholds_back_from_now(
        self, mock_env_vars_common
    ):
        with patch.dict("os.environ", mock_env_vars_common):
            import common_user_manager

        before = datetime.now(timezone.utc)
        statements = self.compiled_statements(mock_env_vars_common)
        after = datetime.now(timezone.utc)

        params = statements[0].params
        cutoffs = sorted(
            value for value in params.values() if isinstance(value, datetime)
        )
        assert len(cutoffs) == 2
        very_old, inactive = cutoffs
        assert (
            before - timedelta(hours=common_user_manager.WORKFLOW_USER_INACTIVITY_HOURS)
            <= inactive
            <= after
            - timedelta(hours=common_user_manager.WORKFLOW_USER_INACTIVITY_HOURS)
        )
        assert (
            before - timedelta(hours=common_user_manager.WORKFLOW_VERY_OLD_HOURS)
            <= very_old
            <= after - timedelta(hours=common_user_manager.WORKFLOW_VERY_OLD_HOURS)
        )

    def test_the_very_old_cutoff_outlasts_a_legitimate_long_run(
        self, mock_env_vars_common
    ):
        """A real analysis run can take hours. The "very old" arm is what reclaims
        a crashed one, so it has to sit beyond the longest run a user could
        plausibly still be waiting on - and beyond the inactivity window, or the
        arm fires on workflows the inactivity check already covers.
        """
        with patch.dict("os.environ", mock_env_vars_common):
            import common_user_manager

        assert common_user_manager.WORKFLOW_VERY_OLD_HOURS >= 4
        assert (
            common_user_manager.WORKFLOW_VERY_OLD_HOURS
            > common_user_manager.WORKFLOW_USER_INACTIVITY_HOURS
        )
