"""Tests for premium_manager Lambda function."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aws_constants import ECSTaskStatus, InstanceState, PremiumInstanceConfig
from conftest import MockRow, setup_db_mock

TEST_USER_ID = "test_user_12345"
TEST_INSTANCE_ID = "i-testlambda123"


def _always_acquired_lock():
    """Mock distributed_lock that always grants the lock."""
    mock = MagicMock()
    mock.return_value.__enter__ = MagicMock(return_value=True)
    mock.return_value.__exit__ = MagicMock(return_value=False)
    return mock


class _StatefulFakeElbv2:
    """Fake ELBv2 that tracks target-group create/describe/delete so the
    concurrent-assign orphaned-TG corruption is observable."""

    def __init__(self):
        self._seq = 0
        self.live = {}  # arn -> name
        self.created = []  # arns, in creation order
        self.deleted = []  # arns passed to delete_target_group

    def create_target_group(self, Name, **kwargs):
        self._seq += 1
        arn = f"arn:tg/{Name}/{self._seq}"
        self.live[arn] = Name
        self.created.append(arn)
        return {"TargetGroups": [{"TargetGroupArn": arn}]}

    def describe_target_groups(self, Names=None, **kwargs):
        if Names:
            arns = [a for a, n in self.live.items() if n in Names]
        else:
            arns = list(self.live)
        return {"TargetGroups": [{"TargetGroupArn": a} for a in arns]}

    def delete_target_group(self, TargetGroupArn=None, **kwargs):
        self.deleted.append(TargetGroupArn)
        self.live.pop(TargetGroupArn, None)
        return {}

    def register_targets(self, **kwargs):
        return {}


class TestPremiumManagerEvents:
    """Handler-level tests with realistic API Gateway events."""

    def test_assign_event(self, mock_env_vars_premium):
        """Test premium_manager Lambda with assignment event."""
        print("Testing Premium Manager Lambda - Assignment Event")
        print("=" * 50)

        api_gateway_event = {
            "httpMethod": "POST",
            "path": "/premium/assign",
            "headers": {
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
            "body": json.dumps(
                {
                    "action": "assign",
                    "user_id": TEST_USER_ID,
                    "tier": "premium",
                }
            ),
            "requestContext": {
                "requestId": "test-request-123",
                "stage": "prod",
            },
        }

        mock_context = MagicMock()
        mock_context.function_name = "subscr-premium-manager"
        mock_context.aws_request_id = "test-lambda-123"

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch(
            "premium_manager.pymysql.connect"
        ) as mock_pymysql, patch(
            "premium_manager.distributed_lock",
            new=_always_acquired_lock(),
        ):
            mock_connection = setup_db_mock(
                fetchone_values=[
                    MockRow({"id": 123}),
                    # restore_pending_release (in _assign_premium_user_impl): no pending
                    None,
                    # get_existing_user_assignment: no existing assignment
                    None,
                    # _count_active_premium_users_transaction: debug count
                    MockRow({"count": 1}),
                    # _count_active_premium_users_transaction: real count
                    MockRow({"count": 0}),
                    # try_reserve_instance: no existing reservation
                    None,
                    # store_user_assignment: no existing user assignment
                    None,
                    # store_user_assignment: free_user_assignments check
                    None,
                ],
                fetchall_values=[
                    # register_orphaned: get_available_standby (1st call)
                    [],
                    # get_available_standby (2nd call in assign flow)
                    [
                        MockRow(
                            {
                                "instance_id": TEST_INSTANCE_ID,
                                "state": "running",
                            }
                        )
                    ],
                    # get_assigned_users_for_instance: debug query
                    [],
                    # get_assigned_users_for_instance: real users
                    [],
                ],
            )
            mock_pymysql.return_value = mock_connection

            mock_ec2 = MagicMock()
            mock_elbv2 = MagicMock()
            mock_ecs = MagicMock()

            def boto3_client_side_effect(service):
                if service == "ec2":
                    return mock_ec2
                elif service == "elbv2":
                    return mock_elbv2
                elif service == "ecs":
                    return mock_ecs
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": TEST_INSTANCE_ID,
                                "State": {"Name": "running"},
                                "InstanceType": "t3.large",
                                "LaunchTime": "2025-09-17T10:00:00Z",
                                "Tags": [
                                    {
                                        "Key": "Name",
                                        "Value": (
                                            PremiumInstanceConfig.get_instance_name()
                                        ),
                                    },
                                    {
                                        "Key": "Tier",
                                        "Value": (
                                            PremiumInstanceConfig.INSTANCE_IDENTIFIER
                                        ),
                                    },
                                    {
                                        "Key": "Type",
                                        "Value": (
                                            PremiumInstanceConfig.INSTANCE_TYPE_TAG
                                        ),
                                    },
                                ],
                            }
                        ]
                    }
                ]
            }

            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": [
                    "arn:aws:ecs:region:account:" "container-instance/test"
                ]
            }
            mock_ecs.describe_container_instances.return_value = {
                "containerInstances": [
                    {
                        "containerInstanceArn": (
                            "arn:aws:ecs:region:account:" "container-instance/test"
                        ),
                        "ec2InstanceId": TEST_INSTANCE_ID,
                    }
                ]
            }
            mock_ecs.list_tasks.return_value = {
                "taskArns": ["arn:aws:ecs:region:account:task/test"]
            }
            mock_ecs.describe_tasks.return_value = {
                "tasks": [
                    {
                        "taskDefinitionArn": (
                            "arn:aws:ecs:region:account:"
                            "task-definition/optinist-premium"
                        ),
                        "lastStatus": ECSTaskStatus.RUNNING,
                        "desiredStatus": ECSTaskStatus.RUNNING,
                    }
                ]
            }

            mock_elbv2.create_target_group.return_value = {
                "TargetGroups": [
                    {
                        "TargetGroupArn": (
                            "arn:aws:elasticloadbalancing:"
                            "region:account:targetgroup/test"
                        )
                    }
                ]
            }
            mock_elbv2.create_rule.return_value = {
                "Rules": [
                    {
                        "RuleArn": (
                            "arn:aws:elasticloadbalancing:"
                            "region:account:listener-rule/test"
                        )
                    }
                ]
            }

            from premium_manager import handler

            result = handler(api_gateway_event, mock_context)

            assert isinstance(result, dict)
            assert "statusCode" in result
            assert "body" in result

            status_code = result["statusCode"]
            response_body = json.loads(result["body"])

            print(f"Status Code: {status_code}")
            print(f"Response: {json.dumps(response_body, indent=2)}")

            if status_code == 200:
                assert "instance_id" in response_body
                assert response_body["instance_id"] == TEST_INSTANCE_ID
            elif status_code == 202:
                assert "retry_after" in response_body

    def test_heartbeat_event(self, mock_env_vars_premium):
        """Test premium_manager Lambda with heartbeat event."""
        print("Testing Premium Manager Lambda - Heartbeat Event")
        print("=" * 50)

        heartbeat_event = {
            "httpMethod": "POST",
            "path": "/premium/heartbeat",
            "body": json.dumps(
                {
                    "action": "update_activity",
                    "user_id": TEST_USER_ID,
                    "tier": "premium",
                }
            ),
            "requestContext": {"requestId": "test-heartbeat-123"},
        }

        mock_context = MagicMock()
        mock_context.function_name = "subscr-premium-manager"

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql:
            mock_connection = setup_db_mock(
                fetchone_values=[
                    MockRow({"id": 12345}),
                    MockRow(
                        {
                            "user_id": 12345,
                            "instance_id": TEST_INSTANCE_ID,
                            "instance_state": "running",
                        }
                    ),
                ],
                fetchall_values=[[]],
            )
            mock_pymysql.return_value = mock_connection

            from premium_manager import handler

            result = handler(heartbeat_event, mock_context)

            assert isinstance(result, dict)
            status_code = result["statusCode"]
            response_body = json.loads(result["body"])

            print(f"Status Code: {status_code}")
            assert status_code == 200
            assert "user_id" in response_body
            assert response_body["user_id"] == 12345

    def test_release_event(self, mock_env_vars_premium):
        """Test premium_manager Lambda with release event."""
        print("Testing Premium Manager Lambda - Release Event")
        print("=" * 50)

        release_event = {
            "httpMethod": "POST",
            "path": "/premium/release",
            "body": json.dumps(
                {
                    "action": "release",
                    "user_id": TEST_USER_ID,
                    "tier": "premium",
                }
            ),
        }

        mock_context = MagicMock()

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch("premium_manager.pymysql.connect") as mock_pymysql:
            mock_connection = setup_db_mock(
                fetchone_values=[
                    MockRow(
                        {
                            "user_id": TEST_USER_ID,
                            "instance_id": TEST_INSTANCE_ID,
                            "target_group_arn": (
                                "arn:aws:elasticloadbalancing:"
                                "region:account:targetgroup/test"
                            ),
                            "alb_rule_arn": (
                                "arn:aws:elasticloadbalancing:"
                                "region:account1:"
                                "listener-rule/test"
                            ),
                        }
                    ),
                    MockRow({"count": 1}),
                    MockRow({"count": 0}),
                    MockRow({"count": 1}),
                    MockRow({"count": 3}),
                    MockRow({"count": 0}),
                    MockRow({"count": 1}),
                    MockRow({"count": 0}),
                    MockRow({"count": 1}),
                ],
                fetchall_values=[[], [], []],
            )
            mock_pymysql.return_value = mock_connection

            mock_elbv2 = MagicMock()
            mock_ec2 = MagicMock()

            def boto3_client_side_effect(service):
                if service == "elbv2":
                    return mock_elbv2
                elif service == "ec2":
                    return mock_ec2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect
            mock_ec2.describe_instances.return_value = {"Reservations": []}

            from premium_manager import handler

            result = handler(release_event, mock_context)

            assert isinstance(result, dict)
            status_code = result["statusCode"]
            response_body = json.loads(result["body"])

            print(f"Status Code: {status_code}")
            assert status_code == 200
            assert "released_instance" in response_body

    def test_enum_values_in_lambda_operations(self, mock_env_vars_premium):
        """Test Lambda operations work with enum values."""
        print("Testing Lambda with Fixed Enum Values")
        print("=" * 50)

        enum_states = [
            ("launching", "Instance starting up"),
            ("running", "Instance active"),
            ("stopping", "Instance shutting down"),
            ("stopped", "Instance in standby"),
            ("terminating", "Instance being destroyed"),
        ]

        from premium_manager import store_user_assignment, update_instance_state

        for i, (enum_state, description) in enumerate(enum_states):
            print(f"Testing enum state: '{enum_state}' " f"({description})")

            with patch.dict("os.environ", mock_env_vars_premium), patch(
                "pymysql.connect"
            ) as mock_pymysql:
                test_user_id = 1000 + i

                mock_connection = setup_db_mock()
                mock_pymysql.return_value = mock_connection

                store_user_assignment(
                    user_id=test_user_id,
                    instance_id=TEST_INSTANCE_ID,
                    target_group_arn="arn:test",
                    rule_arn="arn:test2",
                    instance_state=enum_state,
                    is_shared=False,
                )
                print(f"store_user_assignment with " f"'{enum_state}' - SUCCESS")

                update_instance_state(test_user_id, enum_state)
                print(f"update_instance_state to " f"'{enum_state}' - SUCCESS")

    def test_error_handling(self, mock_env_vars_premium):
        """Test Lambda error handling for malformed events."""
        print("Testing Lambda Error Handling")
        print("=" * 50)

        malformed_event = {
            "httpMethod": "POST",
            "body": "invalid json {{",
        }
        mock_context = MagicMock()

        with patch.dict("os.environ", mock_env_vars_premium):
            from premium_manager import handler

            result = handler(malformed_event, mock_context)

            assert isinstance(result, dict)
            assert "statusCode" in result
            assert result["statusCode"] >= 400


class TestEarlyCheckAndCleanup:
    """Assignment early check and cleanup tests."""

    def test_early_check_returns_existing_assignment(self, mock_env_vars_premium):
        """assign_premium_user returns existing assignment
        without creating new ALB rules."""
        print("Testing Early Check Returns Existing Assignment")
        print("=" * 50)

        test_user_id = 12345

        existing_assignment = {
            "user_id": test_user_id,
            "instance_id": TEST_INSTANCE_ID,
            "target_group_arn": "arn:aws:tg/existing",
            "alb_rule_arn": "arn:aws:rule/existing",
            "status": "active",
            "instance_state": "running",
            "is_shared": 0,
        }

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch(
            "premium_manager.get_existing_user_assignment"
        ) as mock_get_existing, patch(
            "premium_manager.pymysql.connect"
        ) as mock_pymysql, patch(
            "premium_manager.distributed_lock",
            new=_always_acquired_lock(),
        ):
            mock_get_existing.return_value = existing_assignment
            mock_connection = setup_db_mock(fetchone_values=[None])
            mock_pymysql.return_value = mock_connection

            mock_elbv2 = MagicMock()

            def boto3_client_side_effect(service):
                if service == "elbv2":
                    return mock_elbv2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            from premium_manager import assign_premium_user

            result = assign_premium_user(
                test_user_id, {"tier": "premium"}, "firebase_uid_123"
            )

            status_code = result["statusCode"]
            response_body = json.loads(result["body"])

            print(f"Status Code: {status_code}")
            assert status_code == 200
            assert (
                "already assigned" in response_body.get("message", "").lower()
                or response_body.get("assignment_source") == "existing"
            )
            assert not mock_elbv2.create_rule.called
            mock_get_existing.assert_called_once_with(test_user_id)

    def test_reuse_drops_assignment_when_target_group_missing(
        self, mock_env_vars_premium
    ):
        """A dedicated assignment whose target group was reaped must NOT be
        reused as-is — the stored ARNs are dead. The guard drops the stale row
        and falls through to a fresh assignment instead of returning
        ``assignment_source == "existing"``."""
        import premium_manager

        existing = {
            "user_id": 12,
            "instance_id": "i-dedicated",
            "target_group_arn": "arn:aws:tg/premium-12-gone",
            "alb_rule_arn": "arn:aws:rule/premium-12-gone",
            "status": "active",
            "instance_state": "running",
            "is_shared": 0,
        }

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager.restore_pending_release", return_value=None
        ), patch(
            "premium_manager.get_existing_user_assignment", return_value=existing
        ), patch(
            "premium_manager.target_group_exists", return_value=False
        ) as mock_tg_exists, patch(
            "premium_manager.remove_user_assignment"
        ) as mock_remove, patch(
            # First call on the fresh-assignment path: raise a sentinel so the
            # test proves control fell through without exercising the whole path.
            "premium_manager.register_orphaned_stopped_instances",
            side_effect=RuntimeError("reached fresh assignment path"),
        ):
            mock_ec2 = MagicMock()
            # Assigned instance is still running, so the EC2 state check keeps
            # the row — the missing TG is the only reason it is dropped.
            mock_ec2.describe_instances.return_value = {
                "Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]
            }
            mock_elbv2 = MagicMock()

            with pytest.raises(RuntimeError, match="reached fresh assignment path"):
                premium_manager._assign_premium_user_impl(
                    12,
                    {"tier": "premium"},
                    "uid_12",
                    mock_ec2,
                    mock_elbv2,
                    8000,
                    "vpc-123",
                    "arn:aws:listener/test",
                )

            # Healed: the dead assignment was dropped, triggered by the missing
            # TG, and it was NOT returned as an existing reuse.
            mock_tg_exists.assert_called_once_with("arn:aws:tg/premium-12-gone")
            mock_remove.assert_called_once_with(12)

    def test_reuse_kept_when_target_group_probe_fails_transiently(
        self, mock_env_vars_premium
    ):
        """Fail-open (N1): a transient (non-NotFound) target-group probe error
        must NOT drop the row or 500 — the guard keeps reusing the existing
        assignment. Only an authoritative not-found drops it."""
        import premium_manager

        existing = {
            "user_id": 12,
            "instance_id": "i-dedicated",
            "target_group_arn": "arn:aws:tg/premium-12",
            "alb_rule_arn": "arn:aws:rule/premium-12",
            "status": "active",
            "instance_state": "running",
            "is_shared": 0,
        }

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager.restore_pending_release", return_value=None
        ), patch(
            "premium_manager.get_existing_user_assignment", return_value=existing
        ), patch(
            "premium_manager.target_group_exists",
            side_effect=Exception("Throttling: Rate exceeded"),
        ), patch(
            "premium_manager.remove_user_assignment"
        ) as mock_remove, patch(
            # Must never be reached — a kept reuse returns before this path.
            "premium_manager.register_orphaned_stopped_instances",
            side_effect=RuntimeError("must not reach fresh assignment path"),
        ):
            mock_ec2 = MagicMock()
            mock_ec2.describe_instances.return_value = {
                "Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]
            }
            mock_elbv2 = MagicMock()

            result = premium_manager._assign_premium_user_impl(
                12,
                {"tier": "premium"},
                "uid_12",
                mock_ec2,
                mock_elbv2,
                8000,
                "vpc-123",
                "arn:aws:listener/test",
            )

            # Reused, not dropped: row survives the probe hiccup.
            assert result["statusCode"] == 200
            assert json.loads(result["body"])["assignment_source"] == "existing"
            mock_remove.assert_not_called()

    def test_shared_assignment_missing_tg_guard_skipped(self, mock_env_vars_premium):
        """Scope lock: the missing-TG guard only applies to dedicated rows. A
        shared row uses the shared TG, so it must never be probed or dropped by
        the guard — the ``not is_shared`` short-circuit keeps reuse intact."""
        import premium_manager

        existing = {
            "user_id": 12,
            "instance_id": "i-shared",
            "target_group_arn": "arn:aws:tg/shared-autoscaling",
            "alb_rule_arn": "arn:aws:rule/shared",
            "status": "active",
            "instance_state": "running",
            "is_shared": 1,
        }

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager.restore_pending_release", return_value=None
        ), patch(
            "premium_manager.get_existing_user_assignment", return_value=existing
        ), patch(
            # No dedicated instance available → inline migration finds nothing.
            "premium_manager.get_all_premium_instances_with_states",
            return_value=[],
        ), patch(
            "premium_manager.invoke_migration_async", return_value=None
        ), patch(
            "premium_manager.target_group_exists"
        ) as mock_tg_exists, patch(
            "premium_manager.remove_user_assignment"
        ) as mock_remove:
            mock_ec2 = MagicMock()
            mock_ec2.describe_instances.return_value = {
                "Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]
            }
            mock_elbv2 = MagicMock()

            result = premium_manager._assign_premium_user_impl(
                12,
                {"tier": "premium"},
                "uid_12",
                mock_ec2,
                mock_elbv2,
                8000,
                "vpc-123",
                "arn:aws:listener/test",
            )

            # Shared row reused; the guard never probed or dropped it.
            assert result["statusCode"] == 200
            assert json.loads(result["body"])["assignment_source"] == "existing"
            mock_tg_exists.assert_not_called()
            mock_remove.assert_not_called()

    def test_reuse_drop_survives_concurrent_removal(self, mock_env_vars_premium):
        """A concurrent reconcile heal (or second /assign) may drop the row
        before this guard's own remove runs. The resulting "No assignment
        found" must be treated as already-healed — fall through to the fresh
        assignment path, not 500 on the outer except."""
        import premium_manager

        existing = {
            "user_id": 12,
            "instance_id": "i-dedicated",
            "target_group_arn": "arn:aws:tg/premium-12-gone",
            "alb_rule_arn": "arn:aws:rule/premium-12-gone",
            "status": "active",
            "instance_state": "running",
            "is_shared": 0,
        }

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager.restore_pending_release", return_value=None
        ), patch(
            "premium_manager.get_existing_user_assignment", return_value=existing
        ), patch(
            "premium_manager.target_group_exists", return_value=False
        ), patch(
            # Row already dropped by a concurrent heal: removal raises the
            # same "No assignment found" the transaction raises on an empty row.
            "premium_manager.remove_user_assignment",
            side_effect=Exception("No assignment found for user 12"),
        ) as mock_remove, patch(
            # Sentinel proves control fell through to fresh provisioning
            # instead of returning a 500 from the outer except.
            "premium_manager.register_orphaned_stopped_instances",
            side_effect=RuntimeError("reached fresh assignment path"),
        ):
            mock_ec2 = MagicMock()
            mock_ec2.describe_instances.return_value = {
                "Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]
            }
            mock_elbv2 = MagicMock()

            with pytest.raises(RuntimeError, match="reached fresh assignment path"):
                premium_manager._assign_premium_user_impl(
                    12,
                    {"tier": "premium"},
                    "uid_12",
                    mock_ec2,
                    mock_elbv2,
                    8000,
                    "vpc-123",
                    "arn:aws:listener/test",
                )

            # The concurrent removal was swallowed as already-healed; control
            # reached the fresh path rather than 500ing.
            mock_remove.assert_called_once_with(12)

    def test_reuse_drop_reraises_unexpected_removal_error(self, mock_env_vars_premium):
        """A real DB error during the guard's removal (not "No assignment
        found") must still propagate to the outer except and 500 — fail fast,
        as before, rather than masquerading as a heal."""
        import premium_manager

        existing = {
            "user_id": 12,
            "instance_id": "i-dedicated",
            "target_group_arn": "arn:aws:tg/premium-12-gone",
            "alb_rule_arn": "arn:aws:rule/premium-12-gone",
            "status": "active",
            "instance_state": "running",
            "is_shared": 0,
        }

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager.restore_pending_release", return_value=None
        ), patch(
            "premium_manager.get_existing_user_assignment", return_value=existing
        ), patch(
            "premium_manager.target_group_exists", return_value=False
        ), patch(
            "premium_manager.remove_user_assignment",
            side_effect=Exception("Deadlock found when trying to get lock"),
        ), patch(
            # Must never be reached — the unexpected error fails fast first.
            "premium_manager.register_orphaned_stopped_instances",
            side_effect=RuntimeError("must not reach fresh assignment path"),
        ):
            mock_ec2 = MagicMock()
            mock_ec2.describe_instances.return_value = {
                "Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]
            }
            mock_elbv2 = MagicMock()

            result = premium_manager._assign_premium_user_impl(
                12,
                {"tier": "premium"},
                "uid_12",
                mock_ec2,
                mock_elbv2,
                8000,
                "vpc-123",
                "arn:aws:listener/test",
            )

            # Unexpected removal error fails fast on the outer except → 500.
            assert result["statusCode"] == 500

    def test_exception_handler_cleans_up_alb_rule(self, mock_env_vars_premium):
        """Exception handler cleans up ALB rule."""
        print("Testing Exception Handler ALB Rule Cleanup")
        print("=" * 50)

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch(
            "premium_manager.pymysql.connect"
        ) as mock_pymysql, patch(
            "premium_manager.distributed_lock",
            new=_always_acquired_lock(),
        ):
            mock_connection = setup_db_mock(
                fetchone_values=[
                    None,
                    None,
                    MockRow({"count": 0}),
                ],
                fetchall_values=[
                    [],
                    [
                        MockRow(
                            {
                                "instance_id": TEST_INSTANCE_ID,
                                "state": "running",
                            }
                        )
                    ],
                    [],
                ],
            )
            mock_pymysql.return_value = mock_connection

            mock_elbv2 = MagicMock()
            mock_ec2 = MagicMock()

            created_rule_arn = "arn:aws:rule/test-created-rule"
            mock_elbv2.create_rule.return_value = {
                "Rules": [{"RuleArn": created_rule_arn}]
            }
            mock_elbv2.describe_rules.return_value = {
                "Rules": [
                    {
                        "RuleArn": "arn:aws:rule/duplicate1",
                        "Priority": "100",
                        "Conditions": [
                            {
                                "Field": "http-header",
                                "HttpHeaderConfig": {
                                    "HttpHeaderName": ("X-Routing-ID"),
                                    "Values": ["test123abc"],
                                },
                            }
                        ],
                        "Actions": [
                            {
                                "Type": "forward",
                                "TargetGroupArn": ("arn:aws:tg/orphaned"),
                            }
                        ],
                    },
                    {
                        "RuleArn": "arn:aws:rule/duplicate2",
                        "Priority": "101",
                        "Conditions": [
                            {
                                "Field": "http-header",
                                "HttpHeaderConfig": {
                                    "HttpHeaderName": ("X-Routing-ID"),
                                    "Values": ["test123abc"],
                                },
                            }
                        ],
                        "Actions": [
                            {
                                "Type": "forward",
                                "TargetGroupArn": ("arn:aws:tg/orphaned2"),
                            }
                        ],
                    },
                ]
            }

            def boto3_client_side_effect(service):
                if service == "elbv2":
                    return mock_elbv2
                elif service == "ec2":
                    return mock_ec2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect
            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": TEST_INSTANCE_ID,
                                "State": {"Name": "running"},
                                "Tags": [
                                    {
                                        "Key": "Name",
                                        "Value": (
                                            PremiumInstanceConfig.get_instance_name()
                                        ),
                                    },
                                    {
                                        "Key": "Tier",
                                        "Value": (
                                            PremiumInstanceConfig.INSTANCE_IDENTIFIER
                                        ),
                                    },
                                ],
                            }
                        ]
                    }
                ]
            }

            from premium_manager import cleanup_duplicate_rules_for_routing_id

            test_listener_arn = mock_env_vars_premium["ALB_LISTENER_ARN"]

            deleted_count = cleanup_duplicate_rules_for_routing_id(
                test_listener_arn, "test123abc"
            )

            print(f"Deleted {deleted_count} duplicate rules")
            assert mock_elbv2.delete_rule.call_count == 2

    def test_cleanup_duplicate_alb_rules_scheduled(self, mock_env_vars_premium):
        """Scheduled duplicate cleanup in
        premium_cleanup Lambda."""
        print("Testing Scheduled Duplicate ALB Rules Cleanup")
        print("=" * 50)

        valid_rule_arn = "arn:aws:rule/valid-in-db"
        duplicate_rule_arn = "arn:aws:rule/duplicate-not-in-db"
        routing_id = "test-routing-id-123"

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch("premium_cleanup.get_db_connection") as mock_get_db:
            mock_cursor = MagicMock()
            mock_cursor.fetchall.return_value = [{"alb_rule_arn": valid_rule_arn}]

            mock_connection = MagicMock()
            mock_connection.cursor.return_value.__enter__.return_value = mock_cursor
            mock_connection.__enter__.return_value = mock_connection
            mock_connection.__exit__.return_value = None

            mock_get_db.return_value.__enter__.return_value = mock_connection
            mock_get_db.return_value.__exit__.return_value = None

            mock_elbv2 = MagicMock()
            mock_elbv2.describe_rules.return_value = {
                "Rules": [
                    {
                        "RuleArn": "default",
                        "Priority": "default",
                    },
                    {
                        "RuleArn": valid_rule_arn,
                        "Priority": "100",
                        "Conditions": [
                            {
                                "Field": "http-header",
                                "HttpHeaderConfig": {
                                    "HttpHeaderName": ("X-Routing-ID"),
                                    "Values": [routing_id],
                                },
                            },
                        ],
                        "Actions": [
                            {
                                "Type": "forward",
                                "TargetGroupArn": "arn:tg/1",
                            }
                        ],
                    },
                    {
                        "RuleArn": duplicate_rule_arn,
                        "Priority": "101",
                        "Conditions": [
                            {
                                "Field": "http-header",
                                "HttpHeaderConfig": {
                                    "HttpHeaderName": ("X-Routing-ID"),
                                    "Values": [routing_id],
                                },
                            },
                        ],
                        "Actions": [
                            {
                                "Type": "forward",
                                "TargetGroupArn": "arn:tg/2",
                            }
                        ],
                    },
                ]
            }

            def boto3_client_side_effect(service):
                if service == "elbv2":
                    return mock_elbv2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            from premium_cleanup import cleanup_duplicate_alb_rules

            result = cleanup_duplicate_alb_rules()

            print(f"Cleanup result: {result}")
            assert "duplicates_deleted" in result

            delete_calls = mock_elbv2.delete_rule.call_args_list
            deleted_arns = [call[1]["RuleArn"] for call in delete_calls]

            assert valid_rule_arn not in deleted_arns
            assert duplicate_rule_arn in deleted_arns

    def test_autoscaling_pool_triggers_migration_retry(self, mock_env_vars_premium):
        """Autoscaling-pool assignment triggers
        migration retry."""
        print("Testing Autoscaling-Pool Assignment " "Triggers Migration Retry")
        print("=" * 50)

        test_user_id = 12345

        existing_autoscaling_assignment = {
            "user_id": test_user_id,
            "instance_id": "autoscaling-pool",
            "target_group_arn": "arn:aws:tg/autoscaling",
            "alb_rule_arn": "arn:aws:rule/autoscaling",
            "status": "active",
            "instance_state": "running",
            "is_shared": 1,
        }

        with patch.dict("os.environ", mock_env_vars_premium):
            import premium_manager

            with patch.object(
                premium_manager,
                "get_existing_user_assignment",
            ) as mock_get_existing, patch.object(
                premium_manager, "invoke_migration_async"
            ) as mock_invoke_migration, patch(
                "boto3.client"
            ) as mock_boto3, patch(
                "premium_manager.pymysql.connect"
            ) as mock_pymysql, patch(
                "premium_manager.distributed_lock",
                new=_always_acquired_lock(),
            ):
                mock_get_existing.return_value = existing_autoscaling_assignment
                mock_connection = setup_db_mock(fetchone_values=[None])
                mock_pymysql.return_value = mock_connection

                mock_elbv2 = MagicMock()

                def boto3_client_side_effect(service):
                    if service == "elbv2":
                        return mock_elbv2
                    return MagicMock()

                mock_boto3.side_effect = boto3_client_side_effect

                result = premium_manager.assign_premium_user(
                    test_user_id,
                    {"tier": "premium"},
                    "firebase_uid_456",
                )

                status_code = result["statusCode"]
                response_body = json.loads(result["body"])

                assert status_code == 200
                assert response_body.get("instance_id") == "autoscaling-pool"
                assert response_body.get("assignment_source") == "existing"
                assert mock_invoke_migration.called
                mock_get_existing.assert_called_with(test_user_id)
                assert not mock_elbv2.create_rule.called

    def test_restore_pending_release_returns_alb_fields(self, mock_env_vars_premium):
        """Restored pending_release response includes
        target_group_arn and rule_arn."""
        test_user_id = 12345

        restored_assignment = {
            "user_id": test_user_id,
            "instance_id": TEST_INSTANCE_ID,
            "target_group_arn": "arn:aws:tg/restored",
            "alb_rule_arn": "arn:aws:rule/restored",
            "status": "terminating",
            "instance_state": "running",
            "is_shared": 0,
        }

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ), patch("premium_manager.restore_pending_release") as mock_restore, patch(
            "premium_manager.distributed_lock",
            new=_always_acquired_lock(),
        ):
            mock_restore.return_value = restored_assignment

            from premium_manager import assign_premium_user

            result = assign_premium_user(
                test_user_id, {"tier": "premium"}, "firebase_uid_123"
            )

            status_code = result["statusCode"]
            response_body = json.loads(result["body"])

            assert status_code == 200
            assert response_body["assignment_source"] == "restored_from_pending_release"
            assert response_body["target_group_arn"] == "arn:aws:tg/restored"
            assert response_body["rule_arn"] == "arn:aws:rule/restored"
            assert response_body["instance_id"] == TEST_INSTANCE_ID
            mock_restore.assert_called_once_with(test_user_id)

    def test_stopped_instance_restarted_on_assign(self, mock_env_vars_premium):
        """Assign restarts a stopped dedicated instance
        and returns it once ECS is ready."""
        test_user_id = 12345

        existing_assignment = {
            "user_id": test_user_id,
            "instance_id": TEST_INSTANCE_ID,
            "target_group_arn": "arn:aws:tg/existing",
            "alb_rule_arn": "arn:aws:rule/existing",
            "status": "active",
            "instance_state": "stopped",
            "is_shared": 0,
        }

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch(
            "premium_manager.get_existing_user_assignment"
        ) as mock_get_existing, patch(
            "premium_manager.check_instance_readiness_with_retry"
        ) as mock_readiness, patch(
            "premium_manager._update_instance_state_to_running"
        ) as mock_update_state, patch(
            "premium_manager.pymysql.connect"
        ) as mock_pymysql, patch(
            "premium_manager.distributed_lock",
            new=_always_acquired_lock(),
        ):
            mock_get_existing.return_value = existing_assignment
            mock_readiness.return_value = True
            mock_connection = setup_db_mock(fetchone_values=[None])
            mock_pymysql.return_value = mock_connection

            mock_ec2 = MagicMock()
            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": TEST_INSTANCE_ID,
                                "State": {"Name": "stopped"},
                            }
                        ]
                    }
                ]
            }
            mock_ec2.get_waiter.return_value = MagicMock()

            def boto3_client_side_effect(service):
                if service == "ec2":
                    return mock_ec2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            from premium_manager import assign_premium_user

            result = assign_premium_user(
                test_user_id, {"tier": "premium"}, "firebase_uid_123"
            )

            status_code = result["statusCode"]
            response_body = json.loads(result["body"])

            assert status_code == 200
            assert response_body["instance_id"] == TEST_INSTANCE_ID
            assert response_body["assignment_source"] == "restarted_instance"
            mock_ec2.start_instances.assert_called_once_with(
                InstanceIds=[TEST_INSTANCE_ID]
            )
            mock_update_state.assert_called_once_with(TEST_INSTANCE_ID)
            mock_readiness.assert_called_once_with(
                TEST_INSTANCE_ID, max_wait_seconds=120, retry_interval=10
            )

    def test_stopping_instance_waits_then_restarts(self, mock_env_vars_premium):
        """Assign waits for a stopping instance to reach stopped
        state before restarting it."""
        test_user_id = 12345

        existing_assignment = {
            "user_id": test_user_id,
            "instance_id": TEST_INSTANCE_ID,
            "target_group_arn": "arn:aws:tg/existing",
            "alb_rule_arn": "arn:aws:rule/existing",
            "status": "active",
            "instance_state": "stopping",
            "is_shared": 0,
        }

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch(
            "premium_manager.get_existing_user_assignment"
        ) as mock_get_existing, patch(
            "premium_manager.check_instance_readiness_with_retry"
        ) as mock_readiness, patch(
            "premium_manager._update_instance_state_to_running"
        ), patch(
            "premium_manager.pymysql.connect"
        ) as mock_pymysql, patch(
            "premium_manager.distributed_lock",
            new=_always_acquired_lock(),
        ):
            mock_get_existing.return_value = existing_assignment
            mock_readiness.return_value = True
            mock_connection = setup_db_mock(fetchone_values=[None])
            mock_pymysql.return_value = mock_connection

            mock_ec2 = MagicMock()
            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": TEST_INSTANCE_ID,
                                "State": {"Name": "stopping"},
                            }
                        ]
                    }
                ]
            }
            mock_stop_waiter = MagicMock()
            mock_run_waiter = MagicMock()

            def get_waiter_side_effect(waiter_name):
                if waiter_name == "instance_stopped":
                    return mock_stop_waiter
                if waiter_name == "instance_running":
                    return mock_run_waiter
                return MagicMock()

            mock_ec2.get_waiter.side_effect = get_waiter_side_effect

            def boto3_client_side_effect(service):
                if service == "ec2":
                    return mock_ec2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            from premium_manager import assign_premium_user

            result = assign_premium_user(
                test_user_id, {"tier": "premium"}, "firebase_uid_123"
            )

            status_code = result["statusCode"]
            response_body = json.loads(result["body"])

            assert status_code == 200
            assert response_body["assignment_source"] == "restarted_instance"
            mock_stop_waiter.wait.assert_called_once()
            mock_ec2.start_instances.assert_called_once_with(
                InstanceIds=[TEST_INSTANCE_ID]
            )

    def test_terminated_instance_triggers_fresh_assignment(self, mock_env_vars_premium):
        """Assign removes stale assignment for a terminated
        instance and does not return the stale record."""
        test_user_id = 12345

        existing_assignment = {
            "user_id": test_user_id,
            "instance_id": TEST_INSTANCE_ID,
            "target_group_arn": "arn:aws:tg/existing",
            "alb_rule_arn": "arn:aws:rule/existing",
            "status": "active",
            "instance_state": "running",
            "is_shared": 0,
        }

        patches = {
            "premium_manager.register_orphaned_stopped_instances": None,
            "premium_manager.get_all_premium_instances_with_states": [],
            "premium_manager.count_active_premium_users": 0,
            "premium_manager.get_available_standby_instances": [],
        }
        with patch.dict("os.environ", mock_env_vars_premium):
            import premium_manager

            patchers = []
            for target, rv in patches.items():
                p = patch(target, return_value=rv)
                p.start()
                patchers.append(p)

            try:
                with patch.object(
                    premium_manager, "get_existing_user_assignment"
                ) as mock_get_existing, patch.object(
                    premium_manager, "remove_user_assignment"
                ) as mock_remove, patch(
                    "boto3.client"
                ) as mock_boto3, patch(
                    "pymysql.connect"
                ) as mock_pymysql, patch(
                    "premium_manager.distributed_lock",
                    new=_always_acquired_lock(),
                ):
                    mock_get_existing.return_value = existing_assignment
                    mock_pymysql.return_value = setup_db_mock(
                        fetchone_values=[None] * 20,
                        fetchall_values=[[] for _ in range(10)],
                    )

                    mock_ec2 = MagicMock()
                    mock_ec2.describe_instances.return_value = {
                        "Reservations": [
                            {
                                "Instances": [
                                    {
                                        "InstanceId": TEST_INSTANCE_ID,
                                        "State": {"Name": "terminated"},
                                    }
                                ]
                            }
                        ]
                    }

                    mock_elbv2 = MagicMock()
                    mock_elbv2.create_target_group.return_value = {
                        "TargetGroups": [{"TargetGroupArn": "arn:aws:tg/new"}]
                    }
                    mock_elbv2.create_rule.return_value = {
                        "Rules": [{"RuleArn": "arn:aws:rule/new"}]
                    }

                    def boto3_client_side_effect(service):
                        if service == "ec2":
                            return mock_ec2
                        if service == "elbv2":
                            return mock_elbv2
                        return MagicMock()

                    mock_boto3.side_effect = boto3_client_side_effect

                    result = premium_manager.assign_premium_user(
                        test_user_id, {"tier": "premium"}, "firebase_uid_123"
                    )

                    mock_remove.assert_called_once_with(test_user_id)
                    # Should NOT return the stale assignment
                    response_body = json.loads(result["body"])
                    assert response_body.get("assignment_source") != "existing"
            finally:
                for p in patchers:
                    p.stop()

    def test_ec2_not_found_cleans_up_stale_assignment(self, mock_env_vars_premium):
        """Assign removes stale assignment when EC2 instance
        no longer exists (InvalidInstanceID.NotFound)."""
        from botocore.exceptions import ClientError

        test_user_id = 12345

        existing_assignment = {
            "user_id": test_user_id,
            "instance_id": "i-deleted999",
            "target_group_arn": "arn:aws:tg/existing",
            "alb_rule_arn": "arn:aws:rule/existing",
            "status": "active",
            "instance_state": "running",
            "is_shared": 0,
        }

        patches = {
            "premium_manager.register_orphaned_stopped_instances": None,
            "premium_manager.get_all_premium_instances_with_states": [],
            "premium_manager.count_active_premium_users": 0,
            "premium_manager.get_available_standby_instances": [],
        }
        with patch.dict("os.environ", mock_env_vars_premium):
            import premium_manager

            patchers = []
            for target, rv in patches.items():
                p = patch(target, return_value=rv)
                p.start()
                patchers.append(p)

            try:
                with patch.object(
                    premium_manager, "get_existing_user_assignment"
                ) as mock_get_existing, patch.object(
                    premium_manager, "remove_user_assignment"
                ) as mock_remove, patch(
                    "boto3.client"
                ) as mock_boto3, patch(
                    "pymysql.connect"
                ) as mock_pymysql, patch(
                    "premium_manager.distributed_lock",
                    new=_always_acquired_lock(),
                ):
                    mock_get_existing.return_value = existing_assignment
                    mock_pymysql.return_value = setup_db_mock(
                        fetchone_values=[None] * 20,
                        fetchall_values=[[] for _ in range(10)],
                    )

                    mock_ec2 = MagicMock()
                    mock_ec2.describe_instances.side_effect = ClientError(
                        {
                            "Error": {
                                "Code": "InvalidInstanceID.NotFound",
                                "Message": "Instance not found",
                            }
                        },
                        "DescribeInstances",
                    )

                    mock_elbv2 = MagicMock()
                    mock_elbv2.create_target_group.return_value = {
                        "TargetGroups": [{"TargetGroupArn": "arn:aws:tg/new"}]
                    }
                    mock_elbv2.create_rule.return_value = {
                        "Rules": [{"RuleArn": "arn:aws:rule/new"}]
                    }

                    def boto3_client_side_effect(service):
                        if service == "ec2":
                            return mock_ec2
                        if service == "elbv2":
                            return mock_elbv2
                        return MagicMock()

                    mock_boto3.side_effect = boto3_client_side_effect

                    result = premium_manager.assign_premium_user(
                        test_user_id, {"tier": "premium"}, "firebase_uid_123"
                    )

                    mock_remove.assert_called_once_with(test_user_id)
                    response_body = json.loads(result["body"])
                    assert response_body.get("assignment_source") != "existing"
            finally:
                for p in patchers:
                    p.stop()


class TestConcurrentAssignLock:
    """Tests for per-user distributed lock on assign_premium_user."""

    @staticmethod
    def _lock_ctx(acquired: bool):
        """Create a mock distributed_lock that returns *acquired*."""
        mock = MagicMock()
        mock.return_value.__enter__ = MagicMock(return_value=acquired)
        mock.return_value.__exit__ = MagicMock(return_value=False)
        return mock

    def test_lock_not_acquired_no_existing_returns_409(self, mock_env_vars_premium):
        """When the per-user lock times out and no completed assignment
        exists, the Lambda returns 409 so the caller retries."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ), patch(
            "premium_manager.distributed_lock",
            new=self._lock_ctx(False),
        ), patch(
            "premium_manager.get_existing_user_assignment",
            return_value=None,
        ):
            from premium_manager import assign_premium_user

            result = assign_premium_user(123, {"tier": "premium"}, "uid_123")

            assert result["statusCode"] == 409
            body = json.loads(result["body"])
            assert body["assigned"] is False
            assert "in progress" in body["message"].lower()

    def test_lock_not_acquired_existing_found_returns_200(self, mock_env_vars_premium):
        """When the per-user lock times out but the winning call stored
        an assignment, return 200 with the existing assignment."""
        existing = {
            "instance_id": "i-winner",
            "target_group_arn": "arn:tg/winner",
            "alb_rule_arn": "arn:rule/winner",
            "is_shared": 0,
        }
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ), patch(
            "premium_manager.distributed_lock",
            new=self._lock_ctx(False),
        ), patch(
            "premium_manager.get_existing_user_assignment",
            return_value=existing,
        ):
            from premium_manager import assign_premium_user

            result = assign_premium_user(123, {"tier": "premium"}, "uid_123")

            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert body["instance_id"] == "i-winner"
            assert body["assignment_source"] == "existing"

    def test_lock_name_includes_user_id(self, mock_env_vars_premium):
        """The lock name must include the user_id so different users
        do not block each other."""
        lock_mock = self._lock_ctx(True)
        impl_response = {
            "statusCode": 200,
            "body": json.dumps({"instance_id": "i-impl", "assigned": True}),
        }

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ), patch(
            "premium_manager.distributed_lock",
            new=lock_mock,
        ), patch(
            "premium_manager._assign_premium_user_impl",
            return_value=impl_response,
        ):
            from premium_manager import (
                ASSIGN_LOCK_TIMEOUT_SECONDS,
                ASSIGN_USER_LOCK_PREFIX,
                assign_premium_user,
            )

            assign_premium_user(42, {"tier": "premium"}, "uid_42")

            lock_mock.assert_called_once_with(
                f"{ASSIGN_USER_LOCK_PREFIX}42",
                timeout=ASSIGN_LOCK_TIMEOUT_SECONDS,
            )

    def test_lock_acquired_does_not_consult_the_existing_assignment(
        self, mock_env_vars_premium
    ):
        """The winner assigns; only the loser reads back what the winner stored.

        This case used to assert only ``mock_impl.assert_called_once()``, which
        ``test_assign_impl_runs_inside_the_lock`` already covers more strictly.
        The distinct claim is the branch: reading the existing assignment on the
        acquired path would hand a stale row back as the fresh assignment and
        skip the impl.
        """
        impl_response = {
            "statusCode": 200,
            "body": json.dumps({"instance_id": "i-new", "assigned": True}),
        }
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ), patch(
            "premium_manager.distributed_lock",
            new=self._lock_ctx(True),
        ), patch(
            "premium_manager.get_existing_user_assignment"
        ) as mock_existing, patch(
            "premium_manager._assign_premium_user_impl",
            return_value=impl_response,
        ) as mock_impl:
            from premium_manager import assign_premium_user

            result = assign_premium_user(123, {"tier": "premium"}, "uid_123")

        assert result is impl_response
        assert json.loads(result["body"])["instance_id"] == "i-new"
        mock_impl.assert_called_once()
        assert mock_impl.call_args.args[:3] == (123, {"tier": "premium"}, "uid_123")
        mock_existing.assert_not_called()

    def test_assign_impl_runs_inside_the_lock(self, mock_env_vars_premium):
        """The critical section (_assign_premium_user_impl) must execute strictly
        BETWEEN lock acquire and release. Guards the lock's scope, not just its
        presence: a refactor that keeps distributed_lock but moves the impl call
        outside the `with` block would reorder these events and fail here (the
        real-lock serialization is proven by the GET_LOCK integration test;
        this pins that assign runs its work under the lock). It treats impl as
        opaque, so work hoisted OUT of _assign_premium_user_impl to before the
        lock is NOT caught here - only a full concurrent-assign-vs-real-DB race
        (deferred) would."""
        from contextlib import contextmanager

        events = []

        @contextmanager
        def tracking_lock(name, timeout=None):
            events.append(("enter", name))
            try:
                yield True
            finally:
                events.append(("exit", name))

        def impl_probe(*_args, **_kwargs):
            events.append(("impl", None))
            return {
                "statusCode": 200,
                "body": json.dumps({"instance_id": "i-x", "assigned": True}),
            }

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ), patch("premium_manager.distributed_lock", new=tracking_lock), patch(
            "premium_manager._assign_premium_user_impl", side_effect=impl_probe
        ):
            from premium_manager import ASSIGN_USER_LOCK_PREFIX, assign_premium_user

            result = assign_premium_user(77, {"tier": "premium"}, "uid_77")

        assert result["statusCode"] == 200
        assert [e[0] for e in events] == ["enter", "impl", "exit"]
        assert events[0][1] == f"{ASSIGN_USER_LOCK_PREFIX}77"

    def _assign_once(self, premium_manager, fake_elbv2, ec2, *, existing_return):
        """Run one assign_premium_user for user 77 against a shared stateful
        fake ELBv2. existing_return is what get_existing_user_assignment yields
        for this call.

        NOTE: the lock is always granted here (both variants). These two tests
        do NOT exercise serialization — that is covered by test_lock_* above
        (lock timeout -> 409/200, lock name includes user_id, impl runs under
        the lock). What they isolate is the DOWNSTREAM behavior the lock's
        happens-before guarantees: whether the existing-assignment read observes
        a prior assignment (existing_return) decides orphan-cleanup vs
        short-circuit. This documents the corruption mechanism (6204); it is
        not itself proof the lock serializes."""
        from contextlib import ExitStack

        def boto3_client(service):
            if service == "elbv2":
                return fake_elbv2
            if service == "ec2":
                return ec2
            return MagicMock()

        with ExitStack() as stack:

            def stub(name, **kwargs):
                stack.enter_context(patch.object(premium_manager, name, **kwargs))

            stub("restore_pending_release", return_value=None)
            stub("get_existing_user_assignment", return_value=existing_return)
            stub("register_orphaned_stopped_instances")
            stub(
                "get_all_premium_instances_with_states",
                return_value=[{"instance_id": "i-run", "state": InstanceState.RUNNING}],
            )
            stub("count_active_premium_users", return_value=0)
            stub("get_available_standby_instances", return_value=[])
            stub("check_instance_readiness_with_retry", return_value=True)
            stub("get_assigned_users_for_instance", return_value=[])
            stub("try_reserve_instance", return_value=True)
            stub("target_group_exists", return_value=True)
            stub("_enable_sticky_sessions")
            stub("_ensure_premium_tg_unhealthy_alarm")
            stub("_delete_premium_tg_unhealthy_alarm")
            stub("cleanup_duplicate_rules_for_routing_id", return_value=0)
            stub("create_alb_rule", return_value={"Rules": [{"RuleArn": "arn:rule"}]})
            stub("store_user_assignment")
            stub("update_user_activity", return_value=True)
            stub("invoke_migration_async")
            stub("scale_premium_instances_if_needed", return_value=False)
            stack.enter_context(
                patch("premium_manager.pymysql.connect", return_value=setup_db_mock())
            )
            stack.enter_context(
                patch("premium_manager.distributed_lock", new=_always_acquired_lock())
            )
            stack.enter_context(patch("boto3.client", side_effect=boto3_client))

            return premium_manager.assign_premium_user(
                77, {"tier": "premium"}, "uid_77"
            )

    def test_stale_existing_read_orphans_target_group(self, mock_env_vars_premium):
        """Corruption mechanism (6204): when a second assign does not
        observe the first's stored assignment (the stale read the per-user lock
        prevents), its orphan-cleanup deletes the first's live target group,
        corrupting routing. This is what the lock exists to prevent; the lock
        itself is asserted by test_lock_* above, not here."""
        with patch.dict("os.environ", mock_env_vars_premium):
            import premium_manager

            fake = _StatefulFakeElbv2()
            ec2 = MagicMock()

            r1 = self._assign_once(premium_manager, fake, ec2, existing_return=None)
            r2 = self._assign_once(premium_manager, fake, ec2, existing_return=None)

        assert r1["statusCode"] == 200, r1["body"]
        assert r2["statusCode"] == 200, r2["body"]
        first_tg = fake.created[0]
        assert len(fake.created) == 2
        assert first_tg in fake.deleted
        assert first_tg not in fake.live

    def test_existing_assignment_read_short_circuits_single_target_group(
        self, mock_env_vars_premium
    ):
        """Post-serialization outcome (6204): once the second assign
        observes the first's stored assignment (what the lock's happens-before
        guarantees), it short-circuits to 'existing', creating and deleting
        nothing, so exactly one target group survives. The serialization that
        produces this read ordering is asserted by test_lock_* above, not
        here."""
        with patch.dict("os.environ", mock_env_vars_premium):
            import premium_manager

            fake = _StatefulFakeElbv2()
            ec2 = MagicMock()
            ec2.describe_instances.return_value = {
                "Reservations": [
                    {"Instances": [{"State": {"Name": InstanceState.RUNNING}}]}
                ]
            }

            r1 = self._assign_once(premium_manager, fake, ec2, existing_return=None)
            first_tg = fake.created[0]
            stored = {
                "instance_id": "i-run",
                "target_group_arn": first_tg,
                "alb_rule_arn": "arn:rule",
                "is_shared": 0,
            }
            r2 = self._assign_once(premium_manager, fake, ec2, existing_return=stored)

        assert r1["statusCode"] == 200, r1["body"]
        assert r2["statusCode"] == 200, r2["body"]
        assert json.loads(r2["body"])["assignment_source"] == "existing"
        assert fake.deleted == []
        assert list(fake.live) == [first_tg]
        assert len(fake.created) == 1


class TestDictCursorFix:
    """DictCursor fix tests."""

    def test_increment_attempts(self, mock_env_vars_premium):
        """_increment_assignment_attempts_transaction
        uses dict-style access."""
        print("Testing DictCursor Fix - Increment Attempts")
        print("=" * 50)

        test_user_id = 99

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql:
            mock_connection = setup_db_mock(
                fetchone_values=[
                    {"assignment_attempts": 3},
                ],
            )
            mock_pymysql.return_value = mock_connection

            from premium_manager import _increment_assignment_attempts_transaction

            result = _increment_assignment_attempts_transaction(test_user_id)

            assert result == 4
            print(f"Correctly incremented attempts: 3 -> {result}")

    def test_store_existing_assignment(self, mock_env_vars_premium):
        """_store_user_assignment_transaction uses
        dict-style access on existing assignment."""
        print("Testing DictCursor Fix - " "Store Existing Assignment")
        print("=" * 50)

        test_user_id = 99

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql:
            mock_connection = setup_db_mock(
                fetchone_values=[
                    {
                        "user_id": test_user_id,
                        "assignment_attempts": 1,
                    },
                ],
            )
            mock_pymysql.return_value = mock_connection

            from premium_manager import _store_user_assignment_transaction

            try:
                _store_user_assignment_transaction(
                    user_id=test_user_id,
                    instance_id=TEST_INSTANCE_ID,
                    target_group_arn="arn:test",
                    rule_arn="arn:test2",
                )
                # Should raise, not reach here
                assert False, "Expected exception not raised"
            except KeyError:
                assert False, (
                    "REGRESSION: KeyError - still using " "tuple indexing on DictCursor"
                )
            except Exception as e:
                assert "already has a premium assignment" in str(e)
                print(f"Correctly raised: {e}")

    def test_increment_none_attempts(self, mock_env_vars_premium):
        """None assignment_attempts defaults to 1."""
        print("Testing DictCursor Fix - None Attempts Default")
        print("=" * 50)

        test_user_id = 99

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql:
            mock_connection = setup_db_mock(
                fetchone_values=[
                    {"assignment_attempts": None},
                ],
            )
            mock_pymysql.return_value = mock_connection

            from premium_manager import _increment_assignment_attempts_transaction

            result = _increment_assignment_attempts_transaction(test_user_id)

            assert result == 2
            print(f"Correctly defaulted None to 1, " f"incremented to {result}")


class TestGenerateRoutingId:
    """generate_routing_id pure function tests."""

    def test_deterministic(self):
        """Same uid+key always returns the same ID."""
        from premium_manager import generate_routing_id

        uid = "user_abc_123"
        key = "secret-key-xyz"
        result1 = generate_routing_id(uid, key)
        result2 = generate_routing_id(uid, key)

        assert result1 == result2
        print(f"Deterministic: '{result1}' == '{result2}'")

    def test_length(self):
        """Output is exactly 16 hex characters."""
        from premium_manager import generate_routing_id

        result = generate_routing_id("user_1", "key_1")

        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)
        print(f"Valid 16-char hex: '{result}'")

    def test_different_keys(self):
        """Different keys produce different IDs."""
        from premium_manager import generate_routing_id

        uid = "same_user"
        id1 = generate_routing_id(uid, "key_alpha")
        id2 = generate_routing_id(uid, "key_beta")

        assert id1 != id2
        print(f"key_alpha -> '{id1}', key_beta -> '{id2}'")

    def test_different_uids(self):
        """Different UIDs produce different IDs."""
        from premium_manager import generate_routing_id

        key = "same_key"
        id1 = generate_routing_id("user_aaa", key)
        id2 = generate_routing_id("user_bbb", key)

        assert id1 != id2
        print(f"user_aaa -> '{id1}', user_bbb -> '{id2}'")


class TestCountTotalPremiumUsers:
    """count_total_premium_users tests."""

    def test_subscription_table(self, mock_env_vars_premium):
        """Primary path returns subscriber count."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql:
            mock_connection = setup_db_mock(
                fetchone_values=[MockRow({"count": 7})],
            )
            mock_pymysql.return_value = mock_connection

            from premium_manager import count_total_premium_users

            result = count_total_premium_users()
            assert result == 7
            print(f"Subscription table returned: {result}")

    def test_fallback(self, mock_env_vars_premium):
        """Subscription query fails, falls back."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql:
            mock_connection = setup_db_mock()
            mock_pymysql.return_value = mock_connection

            mock_cursor = mock_connection.cursor.return_value.__enter__.return_value
            mock_cursor.execute.side_effect = [
                Exception("Table not found"),
                None,
            ]
            mock_cursor.fetchone.side_effect = [
                MockRow({"count": 3}),
            ]

            from premium_manager import count_total_premium_users

            result = count_total_premium_users()
            assert result == 3
            print(f"Fallback returned: {result}")

    def test_all_fail(self, mock_env_vars_premium):
        """Both queries fail, returns default."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql:
            mock_connection = setup_db_mock()
            mock_pymysql.return_value = mock_connection

            mock_cursor = mock_connection.cursor.return_value.__enter__.return_value
            mock_cursor.execute.side_effect = [
                Exception("Subscription table error"),
                Exception("Assignments table error"),
            ]

            from premium_manager import (
                DEFAULT_DEVELOPMENT_CAPACITY,
                count_total_premium_users,
            )

            result = count_total_premium_users()
            assert result == DEFAULT_DEVELOPMENT_CAPACITY
            print(
                f"All-fail fallback returned: {result} "
                f"(DEFAULT_DEVELOPMENT_CAPACITY)"
            )


class TestTryReserveInstance:
    """try_reserve_instance tests."""

    def test_success(self, mock_env_vars_premium):
        """No existing assignment, reservation inserted."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql:
            mock_connection = setup_db_mock(
                fetchone_values=[None],
            )
            mock_pymysql.return_value = mock_connection

            from premium_manager import try_reserve_instance

            result = try_reserve_instance("i-new123", 42)
            assert result is True

    def test_already_reserved(self, mock_env_vars_premium):
        """Existing assignment found, returns False."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql:
            mock_connection = setup_db_mock(
                fetchone_values=[
                    MockRow({"instance_id": "i-existing"}),
                ],
            )
            mock_pymysql.return_value = mock_connection

            from premium_manager import try_reserve_instance

            result = try_reserve_instance("i-existing", 42)
            assert result is False


class TestDatabaseCommits:
    """Verify commit is called after operations."""

    def test_terminate_standby_instance_commits(self, mock_env_vars_premium):
        """Verify commit is called after DELETE."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch("premium_manager.pymysql.connect") as mock_pymysql:
            mock_connection = setup_db_mock()
            mock_pymysql.return_value = mock_connection

            mock_ec2 = MagicMock()
            mock_boto3.return_value = mock_ec2

            from premium_manager import terminate_standby_instance

            result = terminate_standby_instance("i-standby1")

            assert result is True
            mock_connection.commit.assert_called()

    def test_cleanup_failed_standby_commits(self, mock_env_vars_premium):
        """Verify commit is called after cleanup."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager" ".get_all_premium_instances_with_states"
        ) as mock_aws, patch("premium_manager.pymysql.connect") as mock_pymysql:
            mock_aws.return_value = []

            mock_connection = setup_db_mock(
                fetchall_values=[
                    [MockRow({"instance_id": "i-gone"})],
                ],
            )
            mock_pymysql.return_value = mock_connection

            from premium_manager import cleanup_failed_standby_instances

            cleanup_failed_standby_instances()
            mock_connection.commit.assert_called()

    def test_update_user_activity_commits(self, mock_env_vars_premium):
        """Verify commit is called after UPDATE."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql:
            mock_connection = setup_db_mock()
            mock_pymysql.return_value = mock_connection

            mock_cursor = mock_connection.cursor.return_value.__enter__.return_value
            mock_cursor.rowcount = 1

            from premium_manager import update_user_activity

            result = update_user_activity(42)

            assert result is True
            mock_connection.commit.assert_called()


class TestHeartbeatRestoresPendingRelease:
    """SQL widening — heartbeat restores `pending_release` rows back to `active`.

    Mirrors the user_activity_middleware behaviour so explicit /heartbeat
    calls heal a soft-release triggered by another tab's close. Guards
    the multi-tab fix on the lambda side.

    The unit harness mocks `pymysql.connect`, so semantic row-matching is
    not exercised — that's covered by integration tests. Here we assert the
    SQL shape and parameter binding, since they encode the behaviour.
    """

    def _execute_timestamp_update(self, mock_env_vars_premium, rowcount=1):
        """Run update_user_activity_timestamp under a mocked DB connection.

        Returns (result, mock_cursor, sql_text, sql_params). The cursor is
        returned so caller can inspect both the SQL text and the parameter
        tuple passed to execute().
        """
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql:
            mock_connection = setup_db_mock()
            mock_pymysql.return_value = mock_connection

            mock_cursor = mock_connection.cursor.return_value.__enter__.return_value
            mock_cursor.rowcount = rowcount

            from premium_manager import update_user_activity_timestamp

            result = update_user_activity_timestamp(42)

            execute_call = mock_cursor.execute.call_args
            sql_text = execute_call[0][0]
            sql_params = execute_call[0][1] if len(execute_call[0]) > 1 else ()
            return result, mock_connection, sql_text, sql_params

    def test_sql_matches_active_and_pending_release(self, mock_env_vars_premium):
        """Filter widened from is_standby=0 only to IN (active, pending_release)."""
        _, _, sql_text, _ = self._execute_timestamp_update(mock_env_vars_premium)
        assert "status IN" in sql_text

    def test_sql_uses_premium_assignment_status_constants(self, mock_env_vars_premium):
        """Parameter binding uses PremiumAssignment.ACTIVE / PENDING_RELEASE
        constants, not raw strings — protects against the
        'terminating' / 'pending_release' aliasing footgun in aws_constants."""
        from aws_constants import PremiumAssignment

        _, _, _, sql_params = self._execute_timestamp_update(mock_env_vars_premium)
        # CASE WHEN status = PENDING_RELEASE THEN ACTIVE, then user_id,
        # then IN(ACTIVE, PENDING_RELEASE), then OR-branch status = ACTIVE.
        assert PremiumAssignment.PENDING_RELEASE in sql_params
        assert PremiumAssignment.ACTIVE in sql_params
        # Active appears in three places (THEN clause, IN list, OR branch).
        assert sql_params.count(PremiumAssignment.ACTIVE) == 3
        # Pending_release appears in two places (CASE WHEN, IN list).
        assert sql_params.count(PremiumAssignment.PENDING_RELEASE) == 2

    def test_sql_flips_pending_release_to_active_via_case(self, mock_env_vars_premium):
        """CASE expression restores pending_release rows to active."""
        _, _, sql_text, _ = self._execute_timestamp_update(mock_env_vars_premium)
        assert "CASE" in sql_text
        assert "WHEN status = %s THEN %s" in sql_text

    def test_sql_guards_against_known_dead_instance_states(self, mock_env_vars_premium):
        """instance_state guard prevents restoring known-dead rows."""
        _, _, sql_text, _ = self._execute_timestamp_update(mock_env_vars_premium)
        assert "instance_state NOT IN" in sql_text
        for dead_state in ("terminated", "shutting-down", "stopped", "stopping"):
            assert dead_state in sql_text

    def test_returns_true_when_row_matched(self, mock_env_vars_premium):
        """rowcount > 0 → returns True (row was updated or restored)."""
        result, _, _, _ = self._execute_timestamp_update(
            mock_env_vars_premium, rowcount=1
        )
        assert result is True

    def test_returns_false_when_no_row_matched(self, mock_env_vars_premium):
        """rowcount == 0 → returns False (no row, or escape valve blocked)."""
        result, _, _, _ = self._execute_timestamp_update(
            mock_env_vars_premium, rowcount=0
        )
        assert result is False

    def test_commits_after_update(self, mock_env_vars_premium):
        """@with_transaction decorator commits on success."""
        _, mock_connection, _, _ = self._execute_timestamp_update(mock_env_vars_premium)
        mock_connection.commit.assert_called()


class TestGetAllPremiumInstances:
    """get_all_premium_instances tests."""

    def test_filters_by_tags(self, mock_env_vars_premium):
        """Only premium-tagged instances returned."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ec2 = MagicMock()
            mock_boto3.return_value = mock_ec2

            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-premium1",
                                "InstanceType": "t3.large",
                                "State": {"Name": "running"},
                                "Tags": [
                                    {
                                        "Key": "Name",
                                        "Value": (
                                            PremiumInstanceConfig.get_instance_name()
                                        ),
                                    },
                                    {
                                        "Key": "Tier",
                                        "Value": (
                                            PremiumInstanceConfig.INSTANCE_IDENTIFIER
                                        ),
                                    },
                                ],
                            },
                            {
                                "InstanceId": "i-other1",
                                "InstanceType": "t3.micro",
                                "State": {"Name": "running"},
                                "Tags": [
                                    {
                                        "Key": "Name",
                                        "Value": "web-server",
                                    },
                                ],
                            },
                        ]
                    }
                ]
            }

            from premium_manager import get_all_premium_instances_with_states

            result = get_all_premium_instances_with_states()

            assert len(result) == 1
            assert result[0]["instance_id"] == "i-premium1"

    def test_empty(self, mock_env_vars_premium):
        """No instances returns empty list."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ec2 = MagicMock()
            mock_boto3.return_value = mock_ec2
            mock_ec2.describe_instances.return_value = {"Reservations": []}

            from premium_manager import get_all_premium_instances_with_states

            result = get_all_premium_instances_with_states()
            assert result == []

    def test_cross_env_instance_skipped(self, mock_env_vars_premium):
        """Instance with different env prefix is excluded."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ec2 = MagicMock()
            mock_boto3.return_value = mock_ec2

            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-cross-env",
                                "InstanceType": "t3.large",
                                "State": {"Name": "running"},
                                "Tags": [
                                    {
                                        "Key": "Name",
                                        # <env_prefix>-<INSTANCE_NAME_SUFFIX>
                                        # see PremiumInstanceConfig
                                        "Value": "production-premium-dedicated",
                                    },
                                    {
                                        "Key": "Tier",
                                        "Value": (
                                            PremiumInstanceConfig.INSTANCE_IDENTIFIER
                                        ),
                                    },
                                ],
                            },
                        ]
                    }
                ]
            }

            from premium_manager import get_all_premium_instances_with_states

            result = get_all_premium_instances_with_states()
            assert len(result) == 0

    def test_tagless_instance_skipped(self, mock_env_vars_premium):
        """Premium instance without Name tag is excluded."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ec2 = MagicMock()
            mock_boto3.return_value = mock_ec2

            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-no-name",
                                "InstanceType": "t3.large",
                                "State": {"Name": "running"},
                                "Tags": [
                                    {
                                        "Key": "Tier",
                                        "Value": (
                                            PremiumInstanceConfig.INSTANCE_IDENTIFIER
                                        ),
                                    },
                                ],
                            },
                        ]
                    }
                ]
            }

            from premium_manager import get_all_premium_instances_with_states

            result = get_all_premium_instances_with_states()
            assert len(result) == 0


class TestStartStandbyInstance:
    """start_standby_instance tests."""

    def test_success(self, mock_env_vars_premium):
        """EC2 start + waiter + readiness + DB update."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch(
            "premium_manager.pymysql.connect"
        ) as mock_pymysql, patch(
            "premium_manager.check_instance_readiness_with_retry",
            return_value=True,
        ) as mock_readiness:
            mock_ec2 = MagicMock()
            mock_boto3.return_value = mock_ec2
            mock_waiter = MagicMock()
            mock_ec2.get_waiter.return_value = mock_waiter

            mock_connection = setup_db_mock()
            mock_pymysql.return_value = mock_connection

            from premium_manager import start_standby_instance

            result = start_standby_instance("i-standby1")

            assert result is True
            mock_ec2.start_instances.assert_called_once_with(InstanceIds=["i-standby1"])
            mock_waiter.wait.assert_called_once()
            mock_readiness.assert_called_once_with(
                "i-standby1", max_wait_seconds=120, retry_interval=10
            )
            mock_connection.commit.assert_called()

            # The state UPDATE must be scoped to the standby placeholder row
            # (is_standby = 1), not the regular-assignment guard (is_standby = 0).
            mock_cursor = mock_connection.cursor.return_value.__enter__.return_value
            update_calls = [
                c
                for c in mock_cursor.execute.call_args_list
                if "UPDATE premium_user_assignments" in c.args[0]
            ]
            assert len(update_calls) == 1
            sql, params = update_calls[0].args
            assert "is_standby = 1" in sql
            assert "is_standby = 0" not in sql
            assert params == (InstanceState.RUNNING, "i-standby1")

    def test_updates_standby_placeholder_row(self, mock_env_vars_premium):
        """State UPDATE commits even when the only matching DB row is the
        standby placeholder (is_standby = 1)."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch(
            "premium_manager.pymysql.connect"
        ) as mock_pymysql, patch(
            "premium_manager.check_instance_readiness_with_retry",
            return_value=True,
        ):
            mock_ec2 = MagicMock()
            mock_boto3.return_value = mock_ec2
            mock_ec2.get_waiter.return_value = MagicMock()

            mock_connection = setup_db_mock()
            mock_pymysql.return_value = mock_connection

            from premium_manager import start_standby_instance

            result = start_standby_instance("i-standby1")

            assert result is True
            mock_connection.commit.assert_called()
            mock_cursor = mock_connection.cursor.return_value.__enter__.return_value
            update_sql = mock_cursor.execute.call_args.args[0]
            assert "UPDATE premium_user_assignments" in update_sql
            assert "is_standby = 1" in update_sql
            assert "is_standby = 0" not in update_sql

    def test_readiness_fails(self, mock_env_vars_premium):
        """ECS task not ready after start returns False without DB update."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch(
            "premium_manager.pymysql.connect"
        ) as mock_pymysql, patch(
            "premium_manager.check_instance_readiness_with_retry",
            return_value=False,
        ):
            mock_ec2 = MagicMock()
            mock_boto3.return_value = mock_ec2
            mock_waiter = MagicMock()
            mock_ec2.get_waiter.return_value = mock_waiter

            mock_connection = setup_db_mock()
            mock_pymysql.return_value = mock_connection

            from premium_manager import start_standby_instance

            result = start_standby_instance("i-standby3")

            assert result is False
            mock_connection.commit.assert_not_called()

    def test_waiter_fails(self, mock_env_vars_premium):
        """EC2 waiter timeout returns False."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch("premium_manager.pymysql.connect") as mock_pymysql:
            mock_ec2 = MagicMock()
            mock_boto3.return_value = mock_ec2
            mock_waiter = MagicMock()
            mock_waiter.wait.side_effect = Exception(
                "Waiter instance_running timed out"
            )
            mock_ec2.get_waiter.return_value = mock_waiter

            mock_connection = setup_db_mock()
            mock_pymysql.return_value = mock_connection

            from premium_manager import start_standby_instance

            result = start_standby_instance("i-standby2")
            assert result is False


class TestStandbyReplenishmentAsync:
    """invoke_standby_replenishment_async + create_standby dispatch + cascade."""

    def test_skips_when_lock_held(self, mock_env_vars_premium):
        """Creation lock held -> no Lambda self-invoke."""
        with patch.dict("os.environ", mock_env_vars_premium):
            import premium_manager

            with patch.object(
                premium_manager, "is_creation_lock_held", return_value=True
            ), patch("boto3.client") as mock_boto3:
                premium_manager.invoke_standby_replenishment_async()
                mock_boto3.assert_not_called()

    def test_invokes_lambda_when_lock_free(self, mock_env_vars_premium):
        """Lock free -> self-invoke with Event + create_standby action."""
        env = {**mock_env_vars_premium, "AWS_LAMBDA_FUNCTION_NAME": "premium-manager"}
        with patch.dict("os.environ", env):
            import premium_manager

            with patch.object(
                premium_manager, "is_creation_lock_held", return_value=False
            ), patch("boto3.client") as mock_boto3:
                mock_lambda = MagicMock()
                mock_boto3.return_value = mock_lambda

                premium_manager.invoke_standby_replenishment_async()

                mock_lambda.invoke.assert_called_once()
                kwargs = mock_lambda.invoke.call_args.kwargs
                assert kwargs["FunctionName"] == "premium-manager"
                assert kwargs["InvocationType"] == "Event"
                assert json.loads(kwargs["Payload"])["action"] == "create_standby"

    def test_no_op_when_function_name_absent(self, mock_env_vars_premium):
        """AWS_LAMBDA_FUNCTION_NAME absent -> warning, no invoke, no exception."""
        env = {
            k: v
            for k, v in mock_env_vars_premium.items()
            if k != "AWS_LAMBDA_FUNCTION_NAME"
        }
        with patch.dict("os.environ", env, clear=True):
            import premium_manager

            with patch.object(
                premium_manager, "is_creation_lock_held", return_value=False
            ), patch("boto3.client") as mock_boto3:
                mock_lambda = MagicMock()
                mock_boto3.return_value = mock_lambda

                premium_manager.invoke_standby_replenishment_async()

                mock_lambda.invoke.assert_not_called()

    def test_handler_routes_create_standby_action(self, mock_env_vars_premium):
        """handler dispatches action=create_standby to create_and_stop_standby."""
        mock_context = MagicMock()
        mock_context.function_name = "premium-manager"
        with patch.dict("os.environ", mock_env_vars_premium):
            import premium_manager

            with patch.object(
                premium_manager,
                "create_and_stop_standby_instance",
                return_value="i-newstandby",
            ) as mock_create:
                result = premium_manager.handler(
                    {"action": "create_standby"}, mock_context
                )

                mock_create.assert_called_once_with()
                assert result["statusCode"] == 200
                body = json.loads(result["body"])
                assert body["created_instance_id"] == "i-newstandby"

    def test_tier3_cascade_uses_async_replenishment(self, mock_env_vars_premium):
        """Standby start succeeds -> async replenishment fires and the blocking
        create_and_stop_standby_instance is never called from the assign path."""
        with patch.dict("os.environ", mock_env_vars_premium):
            import premium_manager

            with patch.object(
                premium_manager, "restore_pending_release", return_value=None
            ), patch.object(
                premium_manager, "get_existing_user_assignment", return_value=None
            ), patch.object(
                premium_manager, "register_orphaned_stopped_instances"
            ), patch.object(
                premium_manager,
                "get_all_premium_instances_with_states",
                return_value=[],
            ), patch.object(
                premium_manager, "count_active_premium_users", return_value=0
            ), patch.object(
                premium_manager,
                "get_available_standby_instances",
                return_value=[{"instance_id": "i-standby1"}],
            ), patch.object(
                premium_manager, "start_standby_instance", return_value=True
            ), patch.object(
                premium_manager, "invoke_standby_replenishment_async"
            ) as mock_replenish, patch.object(
                premium_manager, "create_and_stop_standby_instance"
            ) as mock_create_stop, patch.object(
                premium_manager, "_enable_sticky_sessions"
            ), patch.object(
                premium_manager, "_ensure_premium_tg_unhealthy_alarm"
            ), patch.object(
                premium_manager,
                "cleanup_duplicate_rules_for_routing_id",
                return_value=0,
            ), patch.object(
                premium_manager,
                "create_alb_rule",
                return_value={"Rules": [{"RuleArn": "arn:rule"}]},
            ), patch.object(
                premium_manager, "store_user_assignment"
            ), patch.object(
                premium_manager, "update_user_activity", return_value=True
            ), patch(
                "premium_manager.pymysql.connect"
            ) as mock_pymysql, patch(
                "premium_manager.distributed_lock",
                new=_always_acquired_lock(),
            ), patch(
                "boto3.client"
            ) as mock_boto3:
                mock_pymysql.return_value = setup_db_mock()

                mock_elbv2 = MagicMock()
                mock_elbv2.describe_target_groups.return_value = {"TargetGroups": []}
                mock_elbv2.create_target_group.return_value = {
                    "TargetGroups": [{"TargetGroupArn": "arn:aws:tg/standby"}]
                }

                def boto3_client_side_effect(service):
                    if service == "elbv2":
                        return mock_elbv2
                    return MagicMock()

                mock_boto3.side_effect = boto3_client_side_effect

                result = premium_manager.assign_premium_user(
                    12345, {"tier": "premium"}, "firebase_uid_cascade"
                )

                assert result["statusCode"] == 200
                body = json.loads(result["body"])
                assert body["assignment_source"] == "standby"
                mock_replenish.assert_called_once_with()
                mock_create_stop.assert_not_called()


class TestDesiredCountReservesBootingStandbys:
    """update_premium_service_desired_count counts ACTIVE ECS container
    instances plus premium EC2s still inside the boot grace period, so
    a standby that is still registering with ECS keeps its service
    slot reserved instead of having its task placement cancelled."""

    @staticmethod
    def _make_mocks(
        mock_boto3,
        *,
        desired,
        running,
        registered=(),
        ec2_instances=(),
    ):
        """Configure boto3 mocks for update_premium_service_desired_count tests.

        registered: iterable of (ci_arn, ec2_id) pairs for ECS container instances.
        ec2_instances: iterable of (instance_id, minutes_since_launch) pairs.
        """
        mock_ecs = MagicMock()
        mock_ec2 = MagicMock()

        def boto3_client_side_effect(service):
            if service == "ecs":
                return mock_ecs
            if service == "ec2":
                return mock_ec2
            return MagicMock()

        mock_boto3.side_effect = boto3_client_side_effect

        mock_ecs.describe_services.return_value = {
            "services": [{"desiredCount": desired, "runningCount": running}]
        }
        mock_ecs.list_container_instances.return_value = {
            "containerInstanceArns": [arn for arn, _ in registered]
        }
        mock_ecs.describe_container_instances.return_value = {
            "containerInstances": [
                {"containerInstanceArn": arn, "ec2InstanceId": ec2_id}
                for arn, ec2_id in registered
            ]
        }
        now = datetime.now(timezone.utc)
        reservations = (
            [
                {
                    "Instances": [
                        {
                            "InstanceId": iid,
                            "LaunchTime": now - timedelta(minutes=mins),
                        }
                        for iid, mins in ec2_instances
                    ]
                }
            ]
            if ec2_instances
            else []
        )
        mock_ec2.describe_instances.return_value = {"Reservations": reservations}

        return mock_ecs, mock_ec2

    def test_updates_from_registered_count_when_no_booting(self, mock_env_vars_premium):
        """One registered CI, no booting EC2 -> desiredCount = 1."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs, _ = self._make_mocks(
                mock_boto3,
                desired=4,
                running=1,
                registered=[("arn:aws:ecs:us-east-1:123:ci/a", "i-registered1")],
                ec2_instances=[("i-registered1", 60)],
            )

            from premium_manager import update_premium_service_desired_count

            update_premium_service_desired_count()

            mock_ecs.update_service.assert_called_once()
            assert mock_ecs.update_service.call_args[1]["desiredCount"] == 1

    def test_no_update_when_counts_match(self, mock_env_vars_premium):
        """desired already matches registered + booting -> no update."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs, _ = self._make_mocks(
                mock_boto3,
                desired=2,
                running=2,
                registered=[
                    ("arn:aws:ecs:us-east-1:123:ci/a", "i-r1"),
                    ("arn:aws:ecs:us-east-1:123:ci/b", "i-r2"),
                ],
            )

            from premium_manager import update_premium_service_desired_count

            update_premium_service_desired_count()

            mock_ecs.update_service.assert_not_called()

    def test_counts_booting_standby_within_grace_period(self, mock_env_vars_premium):
        """Standby EC2 that just started but hasn't joined ECS yet is
        counted toward desiredCount so its task placement isn't
        cancelled before the agent registers."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs, _ = self._make_mocks(
                mock_boto3,
                desired=1,
                running=1,
                registered=[("arn:aws:ecs:us-east-1:123:ci/a", "i-registered1")],
                ec2_instances=[("i-registered1", 60), ("i-booting1", 3)],
            )

            from premium_manager import update_premium_service_desired_count

            update_premium_service_desired_count()

            mock_ecs.update_service.assert_called_once()
            assert mock_ecs.update_service.call_args[1]["desiredCount"] == 2

    def test_ignores_orphan_past_grace_period(self, mock_env_vars_premium):
        """EC2 running past grace period without joining ECS is treated
        as an orphan and not counted (cleanup_orphaned_ec2_instances
        will stop it)."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs, _ = self._make_mocks(
                mock_boto3,
                desired=2,
                running=1,
                registered=[("arn:aws:ecs:us-east-1:123:ci/a", "i-registered1")],
                ec2_instances=[("i-registered1", 60), ("i-orphan1", 30)],
            )

            from premium_manager import update_premium_service_desired_count

            update_premium_service_desired_count()

            mock_ecs.update_service.assert_called_once()
            assert mock_ecs.update_service.call_args[1]["desiredCount"] == 1

    def test_does_not_double_count_registered_instance(self, mock_env_vars_premium):
        """A registered CI whose EC2 is also returned by describe_instances
        is counted once, not twice."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs, _ = self._make_mocks(
                mock_boto3,
                desired=0,
                running=0,
                registered=[("arn:aws:ecs:us-east-1:123:ci/a", "i-registered1")],
                ec2_instances=[("i-registered1", 2)],
            )

            from premium_manager import update_premium_service_desired_count

            update_premium_service_desired_count()

            mock_ecs.update_service.assert_called_once()
            assert mock_ecs.update_service.call_args[1]["desiredCount"] == 1


class TestRoutingIdContract:
    """Lambda and middleware must produce identical
    routing IDs for the same Firebase UID + secret."""

    def test_handler_passes_firebase_uid_to_assign(self, mock_env_vars_premium):
        """handler() must pass the original Firebase UID
        string (not the numeric DB ID) as user_uid to
        assign_premium_user."""
        event = {
            "httpMethod": "POST",
            "path": "/premium/assign",
            "body": json.dumps(
                {
                    "action": "assign",
                    "user_id": "firebase_xyz_999",
                    "tier": "premium",
                }
            ),
            "requestContext": {"requestId": "req-1"},
        }
        mock_context = MagicMock()

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql, patch(
            "premium_manager.assign_premium_user",
            return_value={
                "statusCode": 200,
                "body": "{}",
            },
        ) as mock_assign:
            mock_connection = setup_db_mock(
                fetchone_values=[{"id": 42}],
            )
            mock_pymysql.return_value = mock_connection

            from premium_manager import handler

            handler(event, mock_context)

            mock_assign.assert_called_once()
            _, _, user_uid_arg = mock_assign.call_args[0]
            assert user_uid_arg == "firebase_xyz_999"

    def test_same_routing_id_for_firebase_uid(self):
        """Both implementations produce identical output
        for the same Firebase UID + secret."""
        from premium_manager import generate_routing_id as lambda_gen

        from studio.app.common.core.middleware.secure_routing_middleware import (  # noqa: E501
            generate_routing_id as middleware_gen,
        )

        uid = "firebase_user_abc123"
        secret = "shared-secret-key"

        assert lambda_gen(uid, secret) == middleware_gen(uid, secret)

    def test_numeric_id_differs_from_firebase_uid(self):
        """Numeric DB ID and Firebase UID produce different
        routing IDs -- a routing mismatch root cause."""
        from premium_manager import generate_routing_id

        secret = "test-secret"
        numeric_id = generate_routing_id("123", secret)
        firebase_uid = generate_routing_id("firebaseXYZ", secret)

        assert numeric_id != firebase_uid


def _assert_no_scale_down(capsys, expected):
    """``scale_down_if_possible`` wraps its whole body in ``except Exception``, so a
    test whose only assertion is ``assert_not_called()`` also passes when the
    function dies on its first line. The printed decision is the proof it ran to
    the end, and it is also the only thing that separates a refused scale-down
    from an entered branch that found nothing to stop."""
    out = capsys.readouterr().out
    assert "Error scaling down premium instances" not in out, out
    assert expected in out, out


class TestScaleDownIfPossible:
    """scale_down_if_possible stops idle instances and registers
    them as standby in the database for later termination."""

    def _make_instance(self, instance_id, state="running"):
        return {"instance_id": instance_id, "state": state}

    def test_stops_idle_and_registers_standby(self, mock_env_vars_premium):
        """Idle instances are stopped, deregistered from ECS,
        and registered as standby in the DB."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch(
            "premium_manager.get_dynamic_max_capacity", return_value=10
        ), patch(
            "premium_manager.count_active_premium_users", return_value=0
        ), patch(
            "premium_manager.count_total_premium_users", return_value=5
        ), patch(
            "premium_manager.get_all_premium_instances_with_states"
        ) as mock_get_instances, patch(
            "premium_manager.get_assigned_users_for_instance"
        ) as mock_get_users, patch(
            "premium_manager.deregister_container_instance_from_ecs"
        ) as mock_deregister, patch(
            "premium_manager.store_user_assignment"
        ) as mock_store, patch(
            "premium_manager.update_premium_service_desired_count"
        ):
            mock_ec2 = MagicMock()
            mock_boto3.return_value = mock_ec2

            # 3 running, 0 users -> 3 idle (>= 2), min_needed = 1
            mock_get_instances.return_value = [
                self._make_instance("i-idle1"),
                self._make_instance("i-idle2"),
                self._make_instance("i-idle3"),
            ]
            mock_get_users.return_value = []

            from premium_manager import scale_down_if_possible

            scale_down_if_possible()

            # Should stop idle instances
            mock_ec2.stop_instances.assert_called_once()
            stopped_ids = mock_ec2.stop_instances.call_args[1]["InstanceIds"]
            assert len(stopped_ids) >= 1

            # Each stopped instance should be deregistered from ECS
            for iid in stopped_ids:
                mock_deregister.assert_any_call(iid)

            # Each stopped instance should be registered as standby
            assert mock_store.call_count == len(stopped_ids)
            for call in mock_store.call_args_list:
                assert call[1]["user_id"] is None
                assert call[1]["instance_state"] == "stopped"
                assert call[1]["is_standby"] is True

    def test_no_scale_down_when_idle_below_threshold(
        self, mock_env_vars_premium, capsys
    ):
        """No scale-down when fewer than 2 idle instances."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch(
            "premium_manager.get_dynamic_max_capacity", return_value=10
        ), patch(
            "premium_manager.count_active_premium_users", return_value=0
        ), patch(
            "premium_manager.count_total_premium_users", return_value=5
        ), patch(
            "premium_manager.get_all_premium_instances_with_states"
        ) as mock_get_instances, patch(
            "premium_manager.get_assigned_users_for_instance"
        ) as mock_get_users, patch(
            "premium_manager.store_user_assignment"
        ) as mock_store:
            mock_ec2 = MagicMock()
            mock_boto3.return_value = mock_ec2

            # 1 running, 0 users -> 1 idle (< 2 threshold)
            mock_get_instances.return_value = [
                self._make_instance("i-only1"),
            ]
            mock_get_users.return_value = []

            from premium_manager import scale_down_if_possible

            scale_down_if_possible()

            mock_ec2.stop_instances.assert_not_called()
            mock_store.assert_not_called()
            _assert_no_scale_down(
                capsys, "No scale-down: running=1, min_needed=1, idle=1"
            )

    def test_no_scale_down_when_only_one_of_three_running_is_idle(
        self, mock_env_vars_premium, capsys
    ):
        """``idle_instances >= 2`` declines this scale-down, but only in the log.

        With 3 running, 2 occupied and 1 active user min_running_needed is 2, so
        ``len(running) > min_running_needed`` holds and the idle count is what
        refuses. The clause is a second layer whose only observable effect is the
        printed decision: drop it and ``min(idle_instances - 1, ...)`` is 0, the
        collection loop breaks immediately and nothing is stopped either way. It
        becomes load-bearing only if that ``- 1`` is ever removed, so the printed
        line is what this test reads.
        """
        occupied = {"i-busy1": [10], "i-busy2": [11], "i-idle1": []}

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch(
            "premium_manager.get_dynamic_max_capacity", return_value=10
        ), patch(
            "premium_manager.count_active_premium_users", return_value=1
        ), patch(
            "premium_manager.count_total_premium_users", return_value=5
        ), patch(
            "premium_manager.get_all_premium_instances_with_states"
        ) as mock_get_instances, patch(
            "premium_manager.get_assigned_users_for_instance",
            side_effect=lambda iid: occupied[iid],
        ), patch(
            "premium_manager.deregister_container_instance_from_ecs"
        ) as mock_deregister, patch(
            "premium_manager.store_user_assignment"
        ) as mock_store, patch(
            "premium_manager.update_premium_service_desired_count"
        ) as mock_desired_count:
            mock_ec2 = MagicMock()
            mock_boto3.return_value = mock_ec2

            mock_get_instances.return_value = [
                self._make_instance("i-busy1"),
                self._make_instance("i-busy2"),
                self._make_instance("i-idle1"),
            ]

            from premium_manager import scale_down_if_possible

            scale_down_if_possible()

            mock_ec2.stop_instances.assert_not_called()
            mock_deregister.assert_not_called()
            mock_store.assert_not_called()
            mock_desired_count.assert_not_called()
            _assert_no_scale_down(
                capsys, "No scale-down: running=3, min_needed=2, idle=1"
            )

    def test_standby_registration_failure_does_not_block(self, mock_env_vars_premium):
        """If store_user_assignment fails for one instance, the
        remaining instances are still registered."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch(
            "premium_manager.get_dynamic_max_capacity", return_value=10
        ), patch(
            "premium_manager.count_active_premium_users", return_value=0
        ), patch(
            "premium_manager.count_total_premium_users", return_value=5
        ), patch(
            "premium_manager.get_all_premium_instances_with_states"
        ) as mock_get_instances, patch(
            "premium_manager.get_assigned_users_for_instance"
        ) as mock_get_users, patch(
            "premium_manager.deregister_container_instance_from_ecs"
        ), patch(
            "premium_manager.store_user_assignment"
        ) as mock_store, patch(
            "premium_manager.update_premium_service_desired_count"
        ):
            mock_ec2 = MagicMock()
            mock_boto3.return_value = mock_ec2

            # 4 running, 0 users -> 4 idle
            mock_get_instances.return_value = [
                self._make_instance(f"i-idle{i}") for i in range(4)
            ]
            mock_get_users.return_value = []

            # First call fails, rest succeed
            mock_store.side_effect = [
                Exception("DB error"),
                None,
                None,
            ]

            from premium_manager import scale_down_if_possible

            # Should not raise despite the DB error
            scale_down_if_possible()

            mock_ec2.stop_instances.assert_called_once()
            # All stopped instances attempted registration
            assert mock_store.call_count == len(
                mock_ec2.stop_instances.call_args[1]["InstanceIds"]
            )

    def test_occupied_instances_not_stopped(self, mock_env_vars_premium):
        """Instances with assigned users are never stopped."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch(
            "premium_manager.get_dynamic_max_capacity", return_value=10
        ), patch(
            "premium_manager.count_active_premium_users", return_value=1
        ), patch(
            "premium_manager.count_total_premium_users", return_value=5
        ), patch(
            "premium_manager.get_all_premium_instances_with_states"
        ) as mock_get_instances, patch(
            "premium_manager.get_assigned_users_for_instance"
        ) as mock_get_users, patch(
            "premium_manager.deregister_container_instance_from_ecs"
        ), patch(
            "premium_manager.store_user_assignment"
        ) as mock_store, patch(
            "premium_manager.update_premium_service_desired_count"
        ):
            mock_ec2 = MagicMock()
            mock_boto3.return_value = mock_ec2

            # 4 running: 1 occupied + 3 idle
            mock_get_instances.return_value = [
                self._make_instance("i-occupied"),
                self._make_instance("i-idle1"),
                self._make_instance("i-idle2"),
                self._make_instance("i-idle3"),
            ]

            def users_side_effect(iid):
                if iid == "i-occupied":
                    return [{"user_id": 42}]
                return []

            mock_get_users.side_effect = users_side_effect

            from premium_manager import scale_down_if_possible

            scale_down_if_possible()

            stopped_ids = mock_ec2.stop_instances.call_args[1]["InstanceIds"]
            assert "i-occupied" not in stopped_ids
            # All stopped instances registered as standby
            assert mock_store.call_count == len(stopped_ids)

    def test_every_instance_is_deregistered_from_ecs_before_it_is_stopped(
        self, mock_env_vars_premium
    ):
        """Deregistration order. ``test_stops_idle_and_registers_standby``
        asserts both calls happened but not their order, and each mock records its
        own calls only. Stopping an instance ECS still holds a registration for
        leaves a ghost registration that draws tasks to a dead host, which is the
        failure the cleanup sweep exists to mop up.
        """
        recorder = MagicMock()

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch(
            "premium_manager.get_dynamic_max_capacity", return_value=10
        ), patch(
            "premium_manager.count_active_premium_users", return_value=0
        ), patch(
            "premium_manager.count_total_premium_users", return_value=5
        ), patch(
            "premium_manager.get_all_premium_instances_with_states"
        ) as mock_get_instances, patch(
            "premium_manager.get_assigned_users_for_instance", return_value=[]
        ), patch(
            "premium_manager.deregister_container_instance_from_ecs",
            recorder.deregister,
        ), patch(
            "premium_manager.store_user_assignment"
        ), patch(
            "premium_manager.update_premium_service_desired_count"
        ):
            mock_boto3.return_value = recorder.ec2

            mock_get_instances.return_value = [
                self._make_instance(f"i-idle{index}") for index in range(4)
            ]

            from premium_manager import scale_down_if_possible

            scale_down_if_possible()

        stopped_ids = recorder.ec2.stop_instances.call_args[1]["InstanceIds"]
        assert stopped_ids, "nothing was stopped, so the ordering claim is vacuous"

        names = [call[0] for call in recorder.mock_calls]
        assert names.count("ec2.stop_instances") == 1
        deregistered_before_stop = [
            call[1][0]
            for call in recorder.mock_calls[: names.index("ec2.stop_instances")]
            if call[0] == "deregister"
        ]
        assert deregistered_before_stop == stopped_ids


def _assert_orphan_sweep_completed(capsys):
    """Both sweeps wrap their whole body in ``except Exception``, so a test whose
    only assertion is ``assert_not_called()`` also passes when the function dies
    on its first line. The tail print is the proof it ran to the end."""
    out = capsys.readouterr().out
    assert "Error cleaning up orphaned EC2 instances" not in out, out
    assert "Orphan cleanup: stopped 0 instance(s)" in out, out


def _assert_ghost_sweep_completed(capsys):
    out = capsys.readouterr().out
    assert "Error cleaning up ghost ECS registrations" not in out, out
    assert "No ghost ECS registrations found" in out, out


class TestCleanupOrphanedEC2Instances:
    """cleanup_orphaned_ec2_instances stops unregistered
    instances past the grace period and skips the rest."""

    def test_stops_orphaned_past_grace_period(self, mock_env_vars_premium):
        """EC2 instance running 20 min and NOT in ECS
        container instances gets stopped and registered as standby."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch("premium_manager.store_user_assignment") as mock_store:
            mock_ecs = MagicMock()
            mock_ec2 = MagicMock()

            def boto3_client_side_effect(service):
                if service == "ecs":
                    return mock_ecs
                if service == "ec2":
                    return mock_ec2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": []
            }

            # 20 minutes ago -- past grace period
            launch_time = datetime.now(timezone.utc) - timedelta(minutes=20)
            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-orphan1",
                                "State": {"Name": "running"},
                                "LaunchTime": launch_time,
                                "Tags": [
                                    {
                                        "Key": "Tier",
                                        "Value": (
                                            PremiumInstanceConfig.INSTANCE_IDENTIFIER
                                        ),
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }

            from premium_manager import cleanup_orphaned_ec2_instances

            cleanup_orphaned_ec2_instances()

            mock_ec2.stop_instances.assert_called_once_with(InstanceIds=["i-orphan1"])
            mock_store.assert_called_once()
            call_kwargs = mock_store.call_args[1]
            assert call_kwargs["user_id"] is None
            assert call_kwargs["instance_id"] == "i-orphan1"
            assert call_kwargs["instance_state"] == "stopped"
            assert call_kwargs["is_standby"] is True

    def test_orphan_standby_registration_failure_does_not_block(
        self, mock_env_vars_premium
    ):
        """If store_user_assignment fails for one orphan, remaining
        orphans are still stopped and registered."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch("premium_manager.store_user_assignment") as mock_store:
            mock_ecs = MagicMock()
            mock_ec2 = MagicMock()

            def boto3_client_side_effect(service):
                if service == "ecs":
                    return mock_ecs
                if service == "ec2":
                    return mock_ec2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": []
            }

            launch_time = datetime.now(timezone.utc) - timedelta(minutes=20)
            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": f"i-orphan{i}",
                                "State": {"Name": "running"},
                                "LaunchTime": launch_time,
                            }
                            for i in range(2)
                        ]
                    }
                ]
            }

            # First registration fails, second succeeds
            mock_store.side_effect = [Exception("DB error"), None]

            from premium_manager import cleanup_orphaned_ec2_instances

            cleanup_orphaned_ec2_instances()

            # Both instances should be stopped
            assert mock_ec2.stop_instances.call_count == 2
            # Both registrations attempted
            assert mock_store.call_count == 2

    def test_skips_instance_within_grace_period(self, mock_env_vars_premium, capsys):
        """EC2 instance running 5 min is within grace period
        and must NOT be stopped."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs = MagicMock()
            mock_ec2 = MagicMock()

            def boto3_client_side_effect(service):
                if service == "ecs":
                    return mock_ecs
                if service == "ec2":
                    return mock_ec2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": []
            }

            # 5 minutes ago -- within grace period
            launch_time = datetime.now(timezone.utc) - timedelta(minutes=5)
            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-young1",
                                "State": {"Name": "running"},
                                "LaunchTime": launch_time,
                                "Tags": [
                                    {
                                        "Key": "Tier",
                                        "Value": (
                                            PremiumInstanceConfig.INSTANCE_IDENTIFIER
                                        ),
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }

            from premium_manager import cleanup_orphaned_ec2_instances

            cleanup_orphaned_ec2_instances()

            mock_ec2.stop_instances.assert_not_called()
            _assert_orphan_sweep_completed(capsys)

    def test_skips_instance_registered_in_ecs(self, mock_env_vars_premium, capsys):
        """EC2 instance registered as ECS container instance
        must NOT be stopped even if old."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs = MagicMock()
            mock_ec2 = MagicMock()

            def boto3_client_side_effect(service):
                if service == "ecs":
                    return mock_ecs
                if service == "ec2":
                    return mock_ec2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": ["arn:aws:ecs:r:a:ci/registered"]
            }
            mock_ecs.describe_container_instances.return_value = {
                "containerInstances": [
                    {
                        "containerInstanceArn": ("arn:aws:ecs:r:a:ci/registered"),
                        "ec2InstanceId": "i-healthy1",
                    }
                ]
            }

            # 60 minutes ago -- old but healthy
            launch_time = datetime.now(timezone.utc) - timedelta(minutes=60)
            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-healthy1",
                                "State": {"Name": "running"},
                                "LaunchTime": launch_time,
                                "Tags": [
                                    {
                                        "Key": "Tier",
                                        "Value": (
                                            PremiumInstanceConfig.INSTANCE_IDENTIFIER
                                        ),
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }

            from premium_manager import cleanup_orphaned_ec2_instances

            cleanup_orphaned_ec2_instances()

            mock_ec2.stop_instances.assert_not_called()
            _assert_orphan_sweep_completed(capsys)


class TestHandleScheduledMonitoring:
    """handle_scheduled_monitoring acquires the distributed scaling
    lock, runs the monitor steps in order, and skips cleanly when
    another invocation holds the lock."""

    @staticmethod
    def _lock_ctx(acquired: bool):
        mock = MagicMock()
        mock.return_value.__enter__.return_value = acquired
        mock.return_value.__exit__.return_value = False
        return mock

    def test_registers_orphans_before_terminating_aged(self, mock_env_vars_premium):
        """register_orphaned_stopped_instances runs before
        terminate_aged_stopped_instances so ghost instances
        become visible to the terminator."""
        call_order = []

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager.distributed_lock", new=self._lock_ctx(True)
        ), patch("premium_manager.count_active_premium_users", return_value=0), patch(
            "premium_manager.count_total_premium_users", return_value=0
        ), patch(
            "premium_manager.get_all_premium_instances_with_states",
            return_value=[],
        ), patch(
            "premium_manager.get_assigned_users_for_instance",
            return_value=[],
        ), patch(
            "premium_manager.publish_premium_metrics"
        ), patch(
            "premium_manager.scale_down_if_possible"
        ), patch(
            "premium_manager.update_premium_service_desired_count"
        ), patch(
            "premium_manager.cleanup_failed_standby_instances"
        ), patch(
            "premium_manager.register_orphaned_stopped_instances",
            side_effect=lambda: call_order.append("register_orphans"),
        ), patch(
            "premium_manager.terminate_aged_stopped_instances",
            side_effect=lambda: call_order.append("terminate_aged"),
        ), patch(
            "premium_manager.get_standby_count", return_value=0
        ), patch(
            "premium_manager.finalize_expired_pending_releases",
            return_value=[],
        ), patch(
            "premium_manager.cleanup_ghost_ecs_registrations"
        ), patch(
            "premium_manager.cleanup_orphaned_ec2_instances"
        ), patch(
            "premium_manager.fix_incorrect_is_shared_flags",
            return_value={"fixed_count": 0},
        ), patch(
            "premium_manager.process_shared_instance_optimization",
            return_value={"migrations_performed": 0, "shared_instances_found": 0},
        ):
            from premium_manager import handle_scheduled_monitoring

            result = handle_scheduled_monitoring({"source": "test"}, None)
            assert result["statusCode"] == 200

            assert call_order == ["register_orphans", "terminate_aged"]

    def test_skips_when_lock_not_acquired(self, mock_env_vars_premium):
        """When another invocation holds the scaling lock, the handler
        returns the 'skipped' response and does not call any scaling
        step. Regression guard for the pre-fix bug where a stale
        CloudWatch-metric lock kept the handler blackholed for ~15
        minutes after every scaling op."""
        mock_step = MagicMock()

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager.distributed_lock", new=self._lock_ctx(False)
        ), patch(
            "premium_manager.count_active_premium_users", side_effect=mock_step
        ), patch(
            "premium_manager.scale_down_if_possible", side_effect=mock_step
        ), patch(
            "premium_manager.update_premium_service_desired_count",
            side_effect=mock_step,
        ), patch(
            "premium_manager.cleanup_ghost_ecs_registrations", side_effect=mock_step
        ), patch(
            "premium_manager.cleanup_orphaned_ec2_instances", side_effect=mock_step
        ):
            from premium_manager import handle_scheduled_monitoring

            result = handle_scheduled_monitoring({"source": "test"}, None)

            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert body["status"] == "skipped"
            assert "already in progress" in body["message"]
            mock_step.assert_not_called()

    def test_acquires_non_blocking_with_correct_lock_name(self, mock_env_vars_premium):
        """The monitor must call distributed_lock with timeout=0 so
        contention skips (not waits) and with PREMIUM_SCALING_LOCK so
        it interlocks with other invocations of this same handler.
        A typo or a blocking default would be a silent regression."""
        lock_mock = self._lock_ctx(False)  # not acquired → fast skip path

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager.distributed_lock", new=lock_mock
        ):
            from premium_manager import (
                PREMIUM_SCALING_LOCK,
                handle_scheduled_monitoring,
            )

            handle_scheduled_monitoring({"source": "test"}, None)

            lock_mock.assert_called_once_with(PREMIUM_SCALING_LOCK, timeout=0)

    def test_releases_lock_on_monitor_exception(self, mock_env_vars_premium):
        """When a step inside the with-block raises, the handler
        returns a structured 500 and the context manager's __exit__ is
        still called (guaranteeing lock release). Regression guard for
        a future refactor that moves try/except outside the with-block
        or re-introduces a separate release call."""
        lock_mock = self._lock_ctx(True)

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager.distributed_lock", new=lock_mock
        ), patch(
            "premium_manager.count_active_premium_users",
            side_effect=RuntimeError("boom"),
        ):
            from premium_manager import handle_scheduled_monitoring

            result = handle_scheduled_monitoring({"source": "test"}, None)

            assert result["statusCode"] == 500
            body = json.loads(result["body"])
            assert body["status"] == "error"
            assert "boom" in body["error"]
            # Context manager exit fires regardless of the exception →
            # GET_LOCK is released by the helper's `finally` on the DB
            # connection.
            lock_mock.return_value.__exit__.assert_called_once()


class TestGetUserUidFromId:
    """Reverse UID lookup from numeric DB ID."""

    def test_returns_firebase_uid(self, mock_env_vars_premium):
        """Returns the Firebase UID for a known user_id."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql:
            mock_connection = setup_db_mock(
                fetchone_values=[{"uid": "firebase_abc"}],
            )
            mock_pymysql.return_value = mock_connection

            from premium_manager import get_user_uid_from_id

            result = get_user_uid_from_id(mock_connection, 42)
            assert result == "firebase_abc"

    def test_raises_for_unknown_id(self, mock_env_vars_premium):
        """Raises ValueError when user_id is not found."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql:
            mock_connection = setup_db_mock(
                fetchone_values=[None],
            )
            mock_pymysql.return_value = mock_connection

            from premium_manager import get_user_uid_from_id

            with pytest.raises(ValueError, match="not found"):
                get_user_uid_from_id(mock_connection, 9999)


class TestGetPremiumUserStatus:
    """Tests for get_premium_user_status Lambda function."""

    def test_returns_status_for_active_user(self, mock_env_vars_premium):
        """Returns 200 with assignment details for an active user."""
        test_user_id = 12345
        assigned_at = datetime(2026, 3, 27, 2, 3, 26)

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql, patch("boto3.client") as mock_boto3:
            mock_connection = setup_db_mock(
                fetchone_values=[
                    MockRow(
                        {
                            "instance_id": TEST_INSTANCE_ID,
                            "target_group_arn": "arn:aws:tg/test",
                            "alb_rule_arn": "arn:aws:rule/test",
                            "status": "active",
                            "assigned_at": assigned_at,
                            "is_shared": 0,
                        }
                    ),
                ],
            )
            mock_pymysql.return_value = mock_connection
            # Mock EC2 describe_instances to return running instance
            mock_ec2 = MagicMock()
            mock_ec2.describe_instances.return_value = {
                "Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]
            }
            mock_boto3.return_value = mock_ec2

            from premium_manager import get_premium_user_status

            result = get_premium_user_status(test_user_id)

            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert body["user_id"] == test_user_id
            assert body["instance_id"] == TEST_INSTANCE_ID
            assert body["status"] == "active"
            assert body["assigned_at"] == assigned_at.isoformat()
            assert body["is_shared"] is False

    def test_returns_404_for_unassigned_user(self, mock_env_vars_premium):
        """Returns 404 when user has no premium assignment."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql:
            mock_connection = setup_db_mock(
                fetchone_values=[None],
            )
            mock_pymysql.return_value = mock_connection

            from premium_manager import get_premium_user_status

            result = get_premium_user_status(99999)

            assert result["statusCode"] == 404

    def test_returns_null_assigned_at_when_missing(self, mock_env_vars_premium):
        """Returns assigned_at as null when the column value is None."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql, patch("boto3.client") as mock_boto3:
            mock_connection = setup_db_mock(
                fetchone_values=[
                    MockRow(
                        {
                            "instance_id": TEST_INSTANCE_ID,
                            "target_group_arn": "arn:aws:tg/test",
                            "alb_rule_arn": "arn:aws:rule/test",
                            "status": "active",
                            "assigned_at": None,
                            "is_shared": 1,
                        }
                    ),
                ],
            )
            mock_pymysql.return_value = mock_connection
            # Mock EC2 describe_instances to return running instance
            mock_ec2 = MagicMock()
            mock_ec2.describe_instances.return_value = {
                "Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]
            }
            mock_boto3.return_value = mock_ec2

            from premium_manager import get_premium_user_status

            result = get_premium_user_status(12345)

            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert body["assigned_at"] is None
            assert body["is_shared"] is True

    def test_pending_release_restored_includes_assigned_at(self, mock_env_vars_premium):
        """When status is pending_release, restore_pending_release is called
        and the response still includes assigned_at without KeyError."""
        test_user_id = 12345
        assigned_at = datetime(2026, 3, 27, 2, 0, 0)

        restored_assignment = {
            "user_id": test_user_id,
            "instance_id": TEST_INSTANCE_ID,
            "target_group_arn": "arn:aws:tg/restored",
            "alb_rule_arn": "arn:aws:rule/restored",
            "status": "terminating",
            "instance_state": "running",
            "is_shared": 0,
            "assigned_at": assigned_at,
        }

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql, patch(
            "premium_manager.restore_pending_release"
        ) as mock_restore:
            mock_connection = setup_db_mock(
                fetchone_values=[
                    # Initial query returns pending_release status
                    MockRow(
                        {
                            "instance_id": TEST_INSTANCE_ID,
                            "target_group_arn": "arn:aws:tg/test",
                            "alb_rule_arn": "arn:aws:rule/test",
                            "status": "terminating",
                            "assigned_at": assigned_at,
                            "is_shared": 0,
                        }
                    ),
                ],
            )
            mock_pymysql.return_value = mock_connection
            mock_restore.return_value = restored_assignment

            from premium_manager import get_premium_user_status

            result = get_premium_user_status(test_user_id)

            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert body["assigned_at"] == assigned_at.isoformat()
            assert body["status"] == "active"
            mock_restore.assert_called_once_with(test_user_id)

    def test_pending_release_restore_fails_gracefully(self, mock_env_vars_premium):
        """When restore_pending_release raises, the original assignment
        is still returned."""
        test_user_id = 12345
        assigned_at = datetime(2026, 3, 27, 2, 0, 0)

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql, patch(
            "premium_manager.restore_pending_release"
        ) as mock_restore:
            mock_connection = setup_db_mock(
                fetchone_values=[
                    MockRow(
                        {
                            "instance_id": TEST_INSTANCE_ID,
                            "target_group_arn": "arn:aws:tg/test",
                            "alb_rule_arn": "arn:aws:rule/test",
                            "status": "terminating",
                            "assigned_at": assigned_at,
                            "is_shared": 0,
                        }
                    ),
                ],
            )
            mock_pymysql.return_value = mock_connection
            mock_restore.side_effect = Exception("restore failed")

            from premium_manager import get_premium_user_status

            result = get_premium_user_status(test_user_id)

            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            # Original assignment returned with terminating status
            assert body["status"] == "terminating"
            assert body["assigned_at"] == assigned_at.isoformat()


class TestCleanupGhostECSRegistrations:
    """cleanup_ghost_ecs_registrations must only deregister premium
    instances, and must apply a grace period for agent disconnects
    on running EC2 instances."""

    def _make_clients(self, mock_boto3):
        mock_ecs = MagicMock()
        mock_ec2 = MagicMock()

        def client_factory(service):
            if service == "ecs":
                return mock_ecs
            if service == "ec2":
                return mock_ec2
            return MagicMock()

        mock_boto3.side_effect = client_factory
        return mock_ecs, mock_ec2

    def test_uses_premium_tier_filter(self, mock_env_vars_premium):
        """list_container_instances must include the premium tier filter."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs, _ = self._make_clients(mock_boto3)
            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": []
            }

            from premium_manager import cleanup_ghost_ecs_registrations

            cleanup_ghost_ecs_registrations()

            mock_ecs.list_container_instances.assert_called_once_with(
                cluster="test-cluster",
                filter="attribute:tier == premium",
            )

    def test_deregisters_stopped_ec2_immediately(self, mock_env_vars_premium):
        """Instance whose EC2 is stopped gets deregistered with no grace."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs, mock_ec2 = self._make_clients(mock_boto3)
            ci_arn = "arn:aws:ecs:r:a:ci/ghost1"
            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": [ci_arn]
            }
            mock_ecs.describe_container_instances.return_value = {
                "containerInstances": [
                    {
                        "containerInstanceArn": ci_arn,
                        "ec2InstanceId": "i-stopped1",
                        "agentConnected": False,
                        "status": "ACTIVE",
                    }
                ]
            }
            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-stopped1",
                                "State": {"Name": "stopped"},
                                "Tags": [],
                            }
                        ]
                    }
                ]
            }

            from premium_manager import cleanup_ghost_ecs_registrations

            cleanup_ghost_ecs_registrations()

            mock_ecs.deregister_container_instance.assert_called_once_with(
                cluster="test-cluster",
                containerInstance=ci_arn,
                force=True,
            )

    def test_deregisters_shutting_down_ec2_immediately(self, mock_env_vars_premium):
        """Instance whose EC2 is shutting-down (the typical transient state
        during this deregistration race) gets deregistered with no grace."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs, mock_ec2 = self._make_clients(mock_boto3)
            ci_arn = "arn:aws:ecs:r:a:ci/shutdown1"
            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": [ci_arn]
            }
            mock_ecs.describe_container_instances.return_value = {
                "containerInstances": [
                    {
                        "containerInstanceArn": ci_arn,
                        "ec2InstanceId": "i-shutdown1",
                        "agentConnected": False,
                        "status": "ACTIVE",
                    }
                ]
            }
            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-shutdown1",
                                "State": {"Name": "shutting-down"},
                                "Tags": [],
                            }
                        ]
                    }
                ]
            }

            from premium_manager import cleanup_ghost_ecs_registrations

            cleanup_ghost_ecs_registrations()

            mock_ecs.deregister_container_instance.assert_called_once_with(
                cluster="test-cluster",
                containerInstance=ci_arn,
                force=True,
            )

    def test_tags_running_instance_on_first_disconnect(self, mock_env_vars_premium):
        """Running EC2 with disconnected agent gets tagged but not deregistered."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs, mock_ec2 = self._make_clients(mock_boto3)
            ci_arn = "arn:aws:ecs:r:a:ci/disc1"
            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": [ci_arn]
            }
            mock_ecs.describe_container_instances.return_value = {
                "containerInstances": [
                    {
                        "containerInstanceArn": ci_arn,
                        "ec2InstanceId": "i-running1",
                        "agentConnected": False,
                        "status": "ACTIVE",
                    }
                ]
            }
            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-running1",
                                "State": {"Name": "running"},
                                "Tags": [],
                            }
                        ]
                    }
                ]
            }

            from premium_manager import cleanup_ghost_ecs_registrations

            cleanup_ghost_ecs_registrations()

            mock_ecs.deregister_container_instance.assert_not_called()
            mock_ec2.create_tags.assert_called_once()
            tag_call = mock_ec2.create_tags.call_args
            assert tag_call.kwargs["Resources"] == ["i-running1"]
            assert tag_call.kwargs["Tags"][0]["Key"] == "optinist:agent-disconnected-at"

    def test_pending_ec2_is_not_deregistered(self, mock_env_vars_premium):
        """EC2 still booting (state=pending) is treated as alive — not in the
        STOPPED/TERMINATED/SHUTTING_DOWN dead set. With agent disconnected
        (not yet registered), the grace-period tag is started; deregister is
        not called. Pins the alive-set boundary so a future "deregister
        anything not running" tweak can't silently kill booting CIs."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs, mock_ec2 = self._make_clients(mock_boto3)
            ci_arn = "arn:aws:ecs:r:a:ci/pending1"
            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": [ci_arn]
            }
            mock_ecs.describe_container_instances.return_value = {
                "containerInstances": [
                    {
                        "containerInstanceArn": ci_arn,
                        "ec2InstanceId": "i-pending1",
                        "agentConnected": False,
                        "status": "ACTIVE",
                    }
                ]
            }
            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-pending1",
                                "State": {"Name": "pending"},
                                "Tags": [],
                            }
                        ]
                    }
                ]
            }

            from premium_manager import cleanup_ghost_ecs_registrations

            cleanup_ghost_ecs_registrations()

            mock_ecs.deregister_container_instance.assert_not_called()
            mock_ec2.create_tags.assert_called_once()
            tag_call = mock_ec2.create_tags.call_args
            assert tag_call.kwargs["Resources"] == ["i-pending1"]
            assert tag_call.kwargs["Tags"][0]["Key"] == "optinist:agent-disconnected-at"

    def test_skips_within_grace_period(self, mock_env_vars_premium, capsys):
        """Running EC2 tagged recently is within grace period — skip."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs, mock_ec2 = self._make_clients(mock_boto3)
            ci_arn = "arn:aws:ecs:r:a:ci/grace1"
            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": [ci_arn]
            }
            mock_ecs.describe_container_instances.return_value = {
                "containerInstances": [
                    {
                        "containerInstanceArn": ci_arn,
                        "ec2InstanceId": "i-grace1",
                        "agentConnected": False,
                        "status": "ACTIVE",
                    }
                ]
            }
            # Tagged 2 minutes ago — within the 5-minute grace
            recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-grace1",
                                "State": {"Name": "running"},
                                "Tags": [
                                    {
                                        "Key": "optinist:agent-disconnected-at",
                                        "Value": recent,
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }

            from premium_manager import cleanup_ghost_ecs_registrations

            cleanup_ghost_ecs_registrations()

            mock_ecs.deregister_container_instance.assert_not_called()
            _assert_ghost_sweep_completed(capsys)

    def test_deregisters_after_grace_period(self, mock_env_vars_premium):
        """Running EC2 tagged over 5 minutes ago gets deregistered."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs, mock_ec2 = self._make_clients(mock_boto3)
            ci_arn = "arn:aws:ecs:r:a:ci/expired1"
            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": [ci_arn]
            }
            mock_ecs.describe_container_instances.return_value = {
                "containerInstances": [
                    {
                        "containerInstanceArn": ci_arn,
                        "ec2InstanceId": "i-expired1",
                        "agentConnected": False,
                        "status": "ACTIVE",
                    }
                ]
            }
            # Tagged 10 minutes ago — past the 5-minute grace
            old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-expired1",
                                "State": {"Name": "running"},
                                "Tags": [
                                    {
                                        "Key": "optinist:agent-disconnected-at",
                                        "Value": old,
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }

            from premium_manager import cleanup_ghost_ecs_registrations

            cleanup_ghost_ecs_registrations()

            mock_ecs.deregister_container_instance.assert_called_once_with(
                cluster="test-cluster",
                containerInstance=ci_arn,
                force=True,
            )

    def test_clears_tag_on_reconnect(self, mock_env_vars_premium):
        """Connected agent triggers delete_tags to clear disconnect tag."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs, mock_ec2 = self._make_clients(mock_boto3)
            ci_arn = "arn:aws:ecs:r:a:ci/healthy1"
            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": [ci_arn]
            }
            mock_ecs.describe_container_instances.return_value = {
                "containerInstances": [
                    {
                        "containerInstanceArn": ci_arn,
                        "ec2InstanceId": "i-healthy1",
                        "agentConnected": True,
                        "status": "ACTIVE",
                    }
                ]
            }
            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-healthy1",
                                "State": {"Name": "running"},
                                "Tags": [],
                            }
                        ]
                    }
                ]
            }

            from premium_manager import cleanup_ghost_ecs_registrations

            cleanup_ghost_ecs_registrations()

            mock_ecs.deregister_container_instance.assert_not_called()
            mock_ec2.delete_tags.assert_called_once_with(
                Resources=["i-healthy1"],
                Tags=[{"Key": "optinist:agent-disconnected-at"}],
            )

    def test_mixed_cluster_only_deregisters_ghost(self, mock_env_vars_premium):
        """With a healthy and a stopped instance in the filtered results,
        only the stopped one is deregistered."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs, mock_ec2 = self._make_clients(mock_boto3)
            healthy_arn = "arn:aws:ecs:r:a:ci/healthy"
            ghost_arn = "arn:aws:ecs:r:a:ci/ghost"
            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": [healthy_arn, ghost_arn]
            }
            mock_ecs.describe_container_instances.return_value = {
                "containerInstances": [
                    {
                        "containerInstanceArn": healthy_arn,
                        "ec2InstanceId": "i-healthy",
                        "agentConnected": True,
                        "status": "ACTIVE",
                    },
                    {
                        "containerInstanceArn": ghost_arn,
                        "ec2InstanceId": "i-ghost",
                        "agentConnected": False,
                        "status": "ACTIVE",
                    },
                ]
            }
            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-healthy",
                                "State": {"Name": "running"},
                                "Tags": [],
                            },
                            {
                                "InstanceId": "i-ghost",
                                "State": {"Name": "terminated"},
                                "Tags": [],
                            },
                        ]
                    }
                ]
            }

            from premium_manager import cleanup_ghost_ecs_registrations

            cleanup_ghost_ecs_registrations()

            # Only the ghost gets deregistered
            mock_ecs.deregister_container_instance.assert_called_once_with(
                cluster="test-cluster",
                containerInstance=ghost_arn,
                force=True,
            )
            # describe_instances is batched across both IDs
            mock_ec2.describe_instances.assert_called_once_with(
                InstanceIds=["i-healthy", "i-ghost"]
            )
            # Healthy instance's tag is cleared
            mock_ec2.delete_tags.assert_any_call(
                Resources=["i-healthy"],
                Tags=[{"Key": "optinist:agent-disconnected-at"}],
            )

    def test_deregisters_connected_agent_when_ec2_terminated(
        self, mock_env_vars_premium
    ):
        """Regression: a CI reporting agentConnected=True is still
        deregistered when its underlying EC2 has been terminated."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs, mock_ec2 = self._make_clients(mock_boto3)
            ci_arn = "arn:aws:ecs:r:a:ci/term1"
            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": [ci_arn]
            }
            mock_ecs.describe_container_instances.return_value = {
                "containerInstances": [
                    {
                        "containerInstanceArn": ci_arn,
                        "ec2InstanceId": "i-term1",
                        "agentConnected": True,
                        "status": "ACTIVE",
                    }
                ]
            }
            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-term1",
                                "State": {"Name": "terminated"},
                                "Tags": [],
                            }
                        ]
                    }
                ]
            }

            from premium_manager import cleanup_ghost_ecs_registrations

            cleanup_ghost_ecs_registrations()

            mock_ecs.deregister_container_instance.assert_called_once_with(
                cluster="test-cluster",
                containerInstance=ci_arn,
                force=True,
            )
            # delete_tags fires once via the post-deregister cleanup, never as
            # a "reconnect" tag-clear — the CI was not treated as healthy.
            mock_ec2.delete_tags.assert_called_once_with(
                Resources=["i-term1"],
                Tags=[{"Key": "optinist:agent-disconnected-at"}],
            )

    def test_falls_back_to_per_id_describe_on_invalid_instance_id(
        self, mock_env_vars_premium
    ):
        """When one EC2 ID is long-gone (past AWS's ~1h visibility window),
        the batched describe_instances raises InvalidInstanceID.NotFound for
        the whole call. The cleanup must fall back to per-ID describe so the
        live CI is processed normally and the gone CI is deregistered via the
        state=None branch, instead of the outer except swallowing the error
        and skipping the entire cycle."""
        from botocore.exceptions import ClientError

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs, mock_ec2 = self._make_clients(mock_boto3)
            live_arn = "arn:aws:ecs:r:a:ci/live1"
            gone_arn = "arn:aws:ecs:r:a:ci/gone1"
            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": [live_arn, gone_arn]
            }
            mock_ecs.describe_container_instances.return_value = {
                "containerInstances": [
                    {
                        "containerInstanceArn": live_arn,
                        "ec2InstanceId": "i-live",
                        "agentConnected": True,
                        "status": "ACTIVE",
                    },
                    {
                        "containerInstanceArn": gone_arn,
                        "ec2InstanceId": "i-gone",
                        "agentConnected": True,
                        "status": "ACTIVE",
                    },
                ]
            }

            not_found = ClientError(
                {
                    "Error": {
                        "Code": "InvalidInstanceID.NotFound",
                        "Message": "The instance ID 'i-gone' does not exist",
                    }
                },
                "DescribeInstances",
            )

            def describe_side_effect(**kwargs):
                ids = kwargs["InstanceIds"]
                if len(ids) > 1:
                    raise not_found
                if ids == ["i-live"]:
                    return {
                        "Reservations": [
                            {
                                "Instances": [
                                    {
                                        "InstanceId": "i-live",
                                        "State": {"Name": "running"},
                                        "Tags": [],
                                    }
                                ]
                            }
                        ]
                    }
                raise not_found

            mock_ec2.describe_instances.side_effect = describe_side_effect

            from premium_manager import cleanup_ghost_ecs_registrations

            cleanup_ghost_ecs_registrations()

            # Exactly the gone CI is deregistered; the live one is not.
            mock_ecs.deregister_container_instance.assert_called_once_with(
                cluster="test-cluster",
                containerInstance=gone_arn,
                force=True,
            )
            # One batch call + one per-ID call per original ID on fallback.
            id_lists = [
                c.kwargs["InstanceIds"]
                for c in mock_ec2.describe_instances.call_args_list
            ]
            assert ["i-live", "i-gone"] in id_lists
            assert ["i-live"] in id_lists
            assert ["i-gone"] in id_lists

    def test_deregisters_disconnected_without_ec2_id(self, mock_env_vars_premium):
        """Container instance with disconnected agent and no ec2InstanceId
        is deregistered immediately."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs, mock_ec2 = self._make_clients(mock_boto3)
            ci_arn = "arn:aws:ecs:r:a:ci/no-ec2"
            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": [ci_arn]
            }
            mock_ecs.describe_container_instances.return_value = {
                "containerInstances": [
                    {
                        "containerInstanceArn": ci_arn,
                        "ec2InstanceId": "",
                        "agentConnected": False,
                        "status": "ACTIVE",
                    }
                ]
            }

            from premium_manager import cleanup_ghost_ecs_registrations

            cleanup_ghost_ecs_registrations()

            mock_ecs.deregister_container_instance.assert_called_once_with(
                cluster="test-cluster",
                containerInstance=ci_arn,
                force=True,
            )
            # No EC2 calls should be made
            mock_ec2.describe_instances.assert_not_called()

    def test_deregisters_connected_without_ec2_id(self, mock_env_vars_premium):
        """CI with no ec2InstanceId is deregistered even when
        agentConnected=True (the earlier short-circuit treated any connected
        agent as healthy regardless of EC2 mapping)."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs, mock_ec2 = self._make_clients(mock_boto3)
            ci_arn = "arn:aws:ecs:r:a:ci/no-ec2-conn"
            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": [ci_arn]
            }
            mock_ecs.describe_container_instances.return_value = {
                "containerInstances": [
                    {
                        "containerInstanceArn": ci_arn,
                        "ec2InstanceId": "",
                        "agentConnected": True,
                        "status": "ACTIVE",
                    }
                ]
            }

            from premium_manager import cleanup_ghost_ecs_registrations

            cleanup_ghost_ecs_registrations()

            mock_ecs.deregister_container_instance.assert_called_once_with(
                cluster="test-cluster",
                containerInstance=ci_arn,
                force=True,
            )
            mock_ec2.describe_instances.assert_not_called()

    def test_superseded_sibling_deregistered_immediately(self, mock_env_vars_premium):
        """Disconnected CI sharing an EC2 with a live CI is reaped with no
        grace; the live sibling is left untouched (dual-CI case)."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs, mock_ec2 = self._make_clients(mock_boto3)
            live_arn = "arn:aws:ecs:r:a:ci/live"
            ghost_arn = "arn:aws:ecs:r:a:ci/ghost"
            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": [live_arn, ghost_arn]
            }
            mock_ecs.describe_container_instances.return_value = {
                "containerInstances": [
                    {
                        "containerInstanceArn": live_arn,
                        "ec2InstanceId": "i-shared",
                        "agentConnected": True,
                        "status": "ACTIVE",
                    },
                    {
                        "containerInstanceArn": ghost_arn,
                        "ec2InstanceId": "i-shared",
                        "agentConnected": False,
                        "status": "ACTIVE",
                    },
                ]
            }
            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-shared",
                                "State": {"Name": "running"},
                                "Tags": [],
                            }
                        ]
                    }
                ]
            }

            from premium_manager import cleanup_ghost_ecs_registrations

            cleanup_ghost_ecs_registrations()

            mock_ecs.deregister_container_instance.assert_called_once_with(
                cluster="test-cluster",
                containerInstance=ghost_arn,
                force=True,
            )
            # The live sibling's EC2 must not be tagged for a disconnect grace.
            mock_ec2.create_tags.assert_not_called()


class TestGetEcsContainerInstanceIdPrefersLive:
    """get_ecs_container_instance_id must resolve the live CI when a fresh
    CI overlays a disconnected ghost on the same EC2 (dual-CI case)."""

    def _ecs(self, mock_boto3):
        mock_ecs = MagicMock()
        mock_boto3.return_value = mock_ecs
        return mock_ecs

    def test_prefers_connected_active_ci(self, mock_env_vars_premium):
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs = self._ecs(mock_boto3)
            ghost_arn = "arn:aws:ecs:r:a:ci/ghost"
            live_arn = "arn:aws:ecs:r:a:ci/live"
            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": [ghost_arn, live_arn]
            }
            # Ghost listed first to prove order does not decide the winner.
            mock_ecs.describe_container_instances.return_value = {
                "containerInstances": [
                    {
                        "containerInstanceArn": ghost_arn,
                        "ec2InstanceId": "i-shared",
                        "agentConnected": False,
                        "status": "ACTIVE",
                    },
                    {
                        "containerInstanceArn": live_arn,
                        "ec2InstanceId": "i-shared",
                        "agentConnected": True,
                        "status": "ACTIVE",
                    },
                ]
            }

            from premium_manager import get_ecs_container_instance_id

            result = get_ecs_container_instance_id("i-shared", "test-cluster")
            assert result == live_arn

    def test_single_ci_unchanged(self, mock_env_vars_premium):
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs = self._ecs(mock_boto3)
            arn = "arn:aws:ecs:r:a:ci/only"
            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": [arn]
            }
            mock_ecs.describe_container_instances.return_value = {
                "containerInstances": [
                    {
                        "containerInstanceArn": arn,
                        "ec2InstanceId": "i-solo",
                        "agentConnected": True,
                        "status": "ACTIVE",
                    }
                ]
            }

            from premium_manager import get_ecs_container_instance_id

            result = get_ecs_container_instance_id("i-solo", "test-cluster")
            assert result == arn

    def test_no_match_returns_none(self, mock_env_vars_premium):
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs = self._ecs(mock_boto3)
            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": ["arn:aws:ecs:r:a:ci/other"]
            }
            mock_ecs.describe_container_instances.return_value = {
                "containerInstances": [
                    {
                        "containerInstanceArn": "arn:aws:ecs:r:a:ci/other",
                        "ec2InstanceId": "i-different",
                        "agentConnected": True,
                        "status": "ACTIVE",
                    }
                ]
            }

            from premium_manager import get_ecs_container_instance_id

            result = get_ecs_container_instance_id("i-missing", "test-cluster")
            assert result is None


class TestGetHostPortForInstance:
    """get_host_port_for_instance polls describe_tasks → networkBindings,
    filters by CONTAINER_PORT, and soft-fails to None."""

    def _ecs_client(self, mock_boto3):
        mock_ecs = MagicMock()

        def client_factory(service):
            if service == "ecs":
                return mock_ecs
            return MagicMock()

        mock_boto3.side_effect = client_factory
        return mock_ecs

    def test_returns_host_port_on_first_attempt(self, mock_env_vars_premium):
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch(
            "premium_manager.get_ecs_container_instance_id",
            return_value="ci-arn",
        ), patch(
            "premium_manager.time.sleep"
        ):
            mock_ecs = self._ecs_client(mock_boto3)
            mock_ecs.list_tasks.return_value = {"taskArns": ["t1"]}
            mock_ecs.describe_tasks.return_value = {
                "tasks": [
                    {
                        "lastStatus": "RUNNING",
                        "containers": [
                            {
                                "networkBindings": [
                                    {"containerPort": 8000, "hostPort": 32769}
                                ]
                            }
                        ],
                    }
                ]
            }

            from premium_manager import get_host_port_for_instance

            assert get_host_port_for_instance("i-test") == 32769
            mock_ecs.describe_tasks.assert_called_once()

    def test_polls_through_empty_bindings(self, mock_env_vars_premium):
        """First call returns empty networkBindings; second returns populated."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch(
            "premium_manager.get_ecs_container_instance_id",
            return_value="ci-arn",
        ), patch(
            "premium_manager.time.sleep"
        ) as mock_sleep:
            mock_ecs = self._ecs_client(mock_boto3)
            mock_ecs.list_tasks.return_value = {"taskArns": ["t1"]}
            mock_ecs.describe_tasks.side_effect = [
                {
                    "tasks": [
                        {
                            "lastStatus": "RUNNING",
                            "containers": [{"networkBindings": []}],
                        }
                    ]
                },
                {
                    "tasks": [
                        {
                            "lastStatus": "RUNNING",
                            "containers": [
                                {
                                    "networkBindings": [
                                        {"containerPort": 8000, "hostPort": 32770}
                                    ]
                                }
                            ],
                        }
                    ]
                },
            ]

            from premium_manager import get_host_port_for_instance

            assert get_host_port_for_instance("i-test") == 32770
            assert mock_ecs.describe_tasks.call_count == 2
            mock_sleep.assert_called()

    def test_returns_none_after_max_attempts(self, mock_env_vars_premium):
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch(
            "premium_manager.get_ecs_container_instance_id",
            return_value="ci-arn",
        ), patch(
            "premium_manager.time.sleep"
        ):
            mock_ecs = self._ecs_client(mock_boto3)
            mock_ecs.list_tasks.return_value = {"taskArns": ["t1"]}
            mock_ecs.describe_tasks.return_value = {
                "tasks": [
                    {
                        "lastStatus": "RUNNING",
                        "containers": [{"networkBindings": []}],
                    }
                ]
            }

            from premium_manager import get_host_port_for_instance

            assert get_host_port_for_instance("i-test", max_attempts=3) is None
            assert mock_ecs.describe_tasks.call_count == 3

    def test_filters_by_container_port(self, mock_env_vars_premium):
        """A second port mapping that is NOT containerPort 8000 must be ignored."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch(
            "premium_manager.get_ecs_container_instance_id",
            return_value="ci-arn",
        ), patch(
            "premium_manager.time.sleep"
        ):
            mock_ecs = self._ecs_client(mock_boto3)
            mock_ecs.list_tasks.return_value = {"taskArns": ["t1"]}
            mock_ecs.describe_tasks.return_value = {
                "tasks": [
                    {
                        "lastStatus": "RUNNING",
                        "containers": [
                            {
                                "networkBindings": [
                                    {"containerPort": 9000, "hostPort": 32700},
                                    {"containerPort": 8000, "hostPort": 32769},
                                ]
                            }
                        ],
                    }
                ]
            }

            from premium_manager import get_host_port_for_instance

            assert get_host_port_for_instance("i-test") == 32769

    def test_raises_on_container_port_mismatch(self, mock_env_vars_premium):
        """Bindings present but none matching the expected containerPort is
        permanent config drift — must raise so the caller counts an error
        rather than silently skipping every row."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch(
            "premium_manager.get_ecs_container_instance_id",
            return_value="ci-arn",
        ), patch(
            "premium_manager.time.sleep"
        ):
            mock_ecs = self._ecs_client(mock_boto3)
            mock_ecs.list_tasks.return_value = {"taskArns": ["t1"]}
            mock_ecs.describe_tasks.return_value = {
                "tasks": [
                    {
                        "lastStatus": "RUNNING",
                        "containers": [
                            {
                                "networkBindings": [
                                    {"containerPort": 9000, "hostPort": 32700},
                                ]
                            }
                        ],
                    }
                ]
            }

            from premium_manager import get_host_port_for_instance

            with pytest.raises(RuntimeError, match="containerPort mismatch"):
                get_host_port_for_instance("i-test")
            # Must stop polling on first observation; not loop max_attempts.
            assert mock_ecs.describe_tasks.call_count == 1

    def test_skips_non_running_task(self, mock_env_vars_premium):
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch(
            "premium_manager.get_ecs_container_instance_id",
            return_value="ci-arn",
        ), patch(
            "premium_manager.time.sleep"
        ):
            mock_ecs = self._ecs_client(mock_boto3)
            mock_ecs.list_tasks.return_value = {"taskArns": ["t1", "t2"]}
            mock_ecs.describe_tasks.return_value = {
                "tasks": [
                    {
                        "lastStatus": "STOPPED",
                        "containers": [
                            {
                                "networkBindings": [
                                    {"containerPort": 8000, "hostPort": 99999}
                                ]
                            }
                        ],
                    },
                    {
                        "lastStatus": "RUNNING",
                        "containers": [
                            {
                                "networkBindings": [
                                    {"containerPort": 8000, "hostPort": 32777}
                                ]
                            }
                        ],
                    },
                ]
            }

            from premium_manager import get_host_port_for_instance

            assert get_host_port_for_instance("i-test") == 32777

    def test_returns_none_when_no_container_instance_mapping(
        self, mock_env_vars_premium
    ):
        """get_ecs_container_instance_id returning None (e.g. terminated EC2)
        should drain attempts and return None, not raise."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ), patch(
            "premium_manager.get_ecs_container_instance_id",
            return_value=None,
        ), patch(
            "premium_manager.time.sleep"
        ):
            from premium_manager import get_host_port_for_instance

            assert get_host_port_for_instance("i-gone", max_attempts=2) is None


class TestGetRegisteredPortsForInstance:
    """get_registered_ports_for_instance returns the full set of (Port)
    entries for instance_id in target_group_arn."""

    def test_returns_all_ports_for_instance(self, mock_env_vars_premium):
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_elbv2 = MagicMock()
            mock_boto3.return_value = mock_elbv2
            mock_elbv2.describe_target_health.return_value = {
                "TargetHealthDescriptions": [
                    {"Target": {"Id": "i-test", "Port": 32768}},
                    {"Target": {"Id": "i-test", "Port": 32769}},
                    {"Target": {"Id": "i-test", "Port": 32770}},
                ]
            }

            from premium_manager import get_registered_ports_for_instance

            ports = get_registered_ports_for_instance("tg-arn", "i-test")
            assert sorted(ports) == [32768, 32769, 32770]

    def test_filters_other_instances(self, mock_env_vars_premium):
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_elbv2 = MagicMock()
            mock_boto3.return_value = mock_elbv2
            mock_elbv2.describe_target_health.return_value = {
                "TargetHealthDescriptions": [
                    {"Target": {"Id": "i-test", "Port": 32768}},
                    {"Target": {"Id": "i-other", "Port": 32769}},
                ]
            }

            from premium_manager import get_registered_ports_for_instance

            assert get_registered_ports_for_instance("tg-arn", "i-test") == [32768]


class TestReconcilePremiumTargetGroupPorts:
    """reconcile_premium_target_group_ports compares each per-user TG's
    registered ports against the actual host port and converges drift."""

    def _make_row(self, user_id, instance_id, tg_arn):
        return MockRow(
            {
                "user_id": user_id,
                "instance_id": instance_id,
                "target_group_arn": tg_arn,
            }
        )

    def test_disabled_via_kill_switch(self, mock_env_vars_premium):
        env = {**mock_env_vars_premium, "RECONCILE_PREMIUM_TG_PORTS_ENABLED": "false"}
        with patch.dict("os.environ", env, clear=False), patch(
            "premium_manager.get_db_connection"
        ) as mock_conn, patch("boto3.client") as mock_boto3:
            from premium_manager import reconcile_premium_target_group_ports

            result = reconcile_premium_target_group_ports()

            assert result == {"disabled": True}
            mock_conn.assert_not_called()
            mock_boto3.assert_not_called()

    def test_converged_no_drift_is_noop(self, mock_env_vars_premium):
        """registered == actual_port: zero register/deregister calls."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager.get_db_connection"
        ) as mock_db, patch("boto3.client") as mock_boto3, patch(
            "premium_manager.target_group_exists", return_value=True
        ), patch(
            "premium_manager.get_host_port_for_instance", return_value=8000
        ), patch(
            "premium_manager.get_registered_ports_for_instance",
            return_value=[8000],
        ):
            mock_db.return_value = setup_db_mock(
                fetchall_values=[[self._make_row(12, "i-aaa", "arn:tg/premium-12-tg")]]
            )
            mock_elbv2 = MagicMock()
            mock_boto3.return_value = mock_elbv2

            from premium_manager import reconcile_premium_target_group_ports

            summary = reconcile_premium_target_group_ports()

            assert summary["assignments_scanned"] == 1
            assert summary["drift_detected"] == 0
            assert summary["drift_fixed"] == 0
            mock_elbv2.register_targets.assert_not_called()
            mock_elbv2.deregister_targets.assert_not_called()

    def test_drift_detected_and_fixed(self, mock_env_vars_premium):
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager.get_db_connection"
        ) as mock_db, patch("boto3.client") as mock_boto3, patch(
            "premium_manager.target_group_exists", return_value=True
        ), patch(
            "premium_manager.get_host_port_for_instance", return_value=32769
        ), patch(
            "premium_manager.get_registered_ports_for_instance",
            return_value=[32768],
        ):
            mock_db.return_value = setup_db_mock(
                fetchall_values=[[self._make_row(12, "i-aaa", "arn:tg/premium-12-tg")]]
            )
            mock_elbv2 = MagicMock()
            mock_boto3.return_value = mock_elbv2

            from premium_manager import reconcile_premium_target_group_ports

            summary = reconcile_premium_target_group_ports()

            assert summary["drift_detected"] == 1
            assert summary["drift_fixed"] == 1
            mock_elbv2.register_targets.assert_called_once_with(
                TargetGroupArn="arn:tg/premium-12-tg",
                Targets=[{"Id": "i-aaa", "Port": 32769}],
            )
            mock_elbv2.deregister_targets.assert_called_once_with(
                TargetGroupArn="arn:tg/premium-12-tg",
                Targets=[{"Id": "i-aaa", "Port": 32768}],
            )

    def test_register_then_deregister_order(self, mock_env_vars_premium):
        """The reconciler must register first, then deregister, so the TG
        is never empty mid-transition."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager.get_db_connection"
        ) as mock_db, patch("boto3.client") as mock_boto3, patch(
            "premium_manager.target_group_exists", return_value=True
        ), patch(
            "premium_manager.get_host_port_for_instance", return_value=32769
        ), patch(
            "premium_manager.get_registered_ports_for_instance",
            return_value=[32768],
        ):
            mock_db.return_value = setup_db_mock(
                fetchall_values=[[self._make_row(12, "i-aaa", "arn:tg/premium-12-tg")]]
            )
            mock_elbv2 = MagicMock()
            mock_boto3.return_value = mock_elbv2

            from premium_manager import reconcile_premium_target_group_ports

            reconcile_premium_target_group_ports()

            method_names = [call[0] for call in mock_elbv2.method_calls]
            register_idx = method_names.index("register_targets")
            deregister_idx = method_names.index("deregister_targets")
            assert register_idx < deregister_idx

    def test_partial_fix_both_ports_registered(self, mock_env_vars_premium):
        """registered=[old, new], actual=new: no extra register; only
        deregister old."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager.get_db_connection"
        ) as mock_db, patch("boto3.client") as mock_boto3, patch(
            "premium_manager.target_group_exists", return_value=True
        ), patch(
            "premium_manager.get_host_port_for_instance", return_value=32769
        ), patch(
            "premium_manager.get_registered_ports_for_instance",
            return_value=[32768, 32769],
        ):
            mock_db.return_value = setup_db_mock(
                fetchall_values=[[self._make_row(12, "i-aaa", "arn:tg/premium-12-tg")]]
            )
            mock_elbv2 = MagicMock()
            mock_boto3.return_value = mock_elbv2

            from premium_manager import reconcile_premium_target_group_ports

            summary = reconcile_premium_target_group_ports()

            assert summary["drift_fixed"] == 1
            mock_elbv2.register_targets.assert_not_called()
            mock_elbv2.deregister_targets.assert_called_once_with(
                TargetGroupArn="arn:tg/premium-12-tg",
                Targets=[{"Id": "i-aaa", "Port": 32768}],
            )

    def test_multiple_stale_ports_all_deregistered(self, mock_env_vars_premium):
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager.get_db_connection"
        ) as mock_db, patch("boto3.client") as mock_boto3, patch(
            "premium_manager.target_group_exists", return_value=True
        ), patch(
            "premium_manager.get_host_port_for_instance", return_value=40000
        ), patch(
            "premium_manager.get_registered_ports_for_instance",
            return_value=[32768, 32769, 32770],
        ):
            mock_db.return_value = setup_db_mock(
                fetchall_values=[[self._make_row(12, "i-aaa", "arn:tg/premium-12-tg")]]
            )
            mock_elbv2 = MagicMock()
            mock_boto3.return_value = mock_elbv2

            from premium_manager import reconcile_premium_target_group_ports

            reconcile_premium_target_group_ports()

            assert mock_elbv2.register_targets.call_count == 1
            assert mock_elbv2.deregister_targets.call_count == 3

    def test_heals_missing_tg(self, mock_env_vars_premium):
        """A missing TG for an active row is a permanent strand, so reconcile
        drops the row (heal) instead of skipping — the next assign reprovisions
        the rule/TG."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager.get_db_connection"
        ) as mock_db, patch("boto3.client") as mock_boto3, patch(
            "premium_manager.target_group_exists", return_value=False
        ), patch(
            "premium_manager.remove_user_assignment"
        ) as mock_remove, patch(
            "premium_manager.get_host_port_for_instance"
        ) as mock_host_port:
            mock_db.return_value = setup_db_mock(
                fetchall_values=[[self._make_row(12, "i-aaa", "arn:tg/gone")]]
            )
            mock_elbv2 = MagicMock()
            mock_boto3.return_value = mock_elbv2

            from premium_manager import reconcile_premium_target_group_ports

            summary = reconcile_premium_target_group_ports()

            # Healed, not skipped: row dropped and no port work attempted.
            assert summary["healed_missing_tg"] == 1
            assert summary["drift_detected"] == 0
            mock_remove.assert_called_once_with(12)
            mock_host_port.assert_not_called()
            mock_elbv2.register_targets.assert_not_called()

    def test_missing_tg_removal_failure_counts_error_not_heal(
        self, mock_env_vars_premium
    ):
        """The heal counter is incremented only after remove_user_assignment
        succeeds. If removal raises (concurrent removal / DB error) the outer
        except records an error and the row is NOT counted as healed."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager.get_db_connection"
        ) as mock_db, patch("boto3.client") as mock_boto3, patch(
            "premium_manager.target_group_exists", return_value=False
        ), patch(
            "premium_manager.remove_user_assignment",
            side_effect=Exception("No assignment found for user 12"),
        ) as mock_remove:
            mock_db.return_value = setup_db_mock(
                fetchall_values=[[self._make_row(12, "i-aaa", "arn:tg/gone")]]
            )
            mock_boto3.return_value = MagicMock()

            from premium_manager import reconcile_premium_target_group_ports

            summary = reconcile_premium_target_group_ports()

            # Removal raised → error counted, heal not counted.
            assert summary["healed_missing_tg"] == 0
            assert summary["errors"] == 1
            mock_remove.assert_called_once_with(12)

    def test_heals_only_stranded_row_in_mixed_batch(self, mock_env_vars_premium):
        """In a batch of one missing-TG row and one healthy row, only the
        stranded row is dropped (``continue``); the healthy row is still
        reconciled in the same scan."""
        gone_tg = "arn:tg/premium-12-gone"

        def tg_exists_side_effect(tg_arn):
            return tg_arn != gone_tg

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager.get_db_connection"
        ) as mock_db, patch("boto3.client") as mock_boto3, patch(
            "premium_manager.target_group_exists",
            side_effect=tg_exists_side_effect,
        ), patch(
            "premium_manager.remove_user_assignment"
        ) as mock_remove, patch(
            "premium_manager.get_host_port_for_instance", return_value=None
        ) as mock_host_port:
            mock_db.return_value = setup_db_mock(
                fetchall_values=[
                    [
                        self._make_row(12, "i-aaa", gone_tg),
                        self._make_row(13, "i-bbb", "arn:tg/premium-13-tg"),
                    ]
                ]
            )
            mock_elbv2 = MagicMock()
            mock_boto3.return_value = mock_elbv2

            from premium_manager import reconcile_premium_target_group_ports

            summary = reconcile_premium_target_group_ports()

            # Stranded row healed; healthy row reached port reconciliation
            # (which no-ops here on an unresolved host port).
            assert summary["assignments_scanned"] == 2
            assert summary["healed_missing_tg"] == 1
            assert summary["skipped_no_host_port"] == 1
            mock_remove.assert_called_once_with(12)
            mock_host_port.assert_called_once_with("i-bbb")

    def test_skips_when_host_port_unresolved(self, mock_env_vars_premium):
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager.get_db_connection"
        ) as mock_db, patch("boto3.client") as mock_boto3, patch(
            "premium_manager.target_group_exists", return_value=True
        ), patch(
            "premium_manager.get_host_port_for_instance", return_value=None
        ):
            mock_db.return_value = setup_db_mock(
                fetchall_values=[[self._make_row(12, "i-aaa", "arn:tg/premium-12-tg")]]
            )
            mock_elbv2 = MagicMock()
            mock_boto3.return_value = mock_elbv2

            from premium_manager import reconcile_premium_target_group_ports

            summary = reconcile_premium_target_group_ports()

            assert summary["skipped_no_host_port"] == 1
            mock_elbv2.register_targets.assert_not_called()
            mock_elbv2.deregister_targets.assert_not_called()

    def test_iam_deny_per_row_increments_errors_and_continues(
        self, mock_env_vars_premium
    ):
        """First row's describe_target_health raises; second row's
        succeeds. Errors counted once; second row still processed."""

        def host_port_side_effect(instance_id, *a, **kw):
            return 32769

        def registered_side_effect(tg, iid):
            if iid == "i-aaa":
                raise Exception("AccessDeniedException")
            return [32768]

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager.get_db_connection"
        ) as mock_db, patch("boto3.client") as mock_boto3, patch(
            "premium_manager.target_group_exists", return_value=True
        ), patch(
            "premium_manager.get_host_port_for_instance",
            side_effect=host_port_side_effect,
        ), patch(
            "premium_manager.get_registered_ports_for_instance",
            side_effect=registered_side_effect,
        ):
            mock_db.return_value = setup_db_mock(
                fetchall_values=[
                    [
                        self._make_row(12, "i-aaa", "arn:tg/premium-12-tg"),
                        self._make_row(13, "i-bbb", "arn:tg/premium-13-tg"),
                    ]
                ]
            )
            mock_elbv2 = MagicMock()
            mock_boto3.return_value = mock_elbv2

            from premium_manager import reconcile_premium_target_group_ports

            summary = reconcile_premium_target_group_ports()

            assert summary["errors"] == 1
            assert summary["drift_fixed"] == 1
            assert summary["assignments_scanned"] == 2

    def test_db_read_failure_returns_errors_summary(self, mock_env_vars_premium):
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager.get_db_connection",
            side_effect=Exception("RDS connection refused"),
        ):
            from premium_manager import reconcile_premium_target_group_ports

            summary = reconcile_premium_target_group_ports()

            assert summary["errors"] == 1
            assert summary["drift_detected"] == 0
            assert summary["assignments_scanned"] == 0


class TestHandleScheduledMonitoringReconcile:
    """handle_scheduled_monitoring must invoke the reconciler after
    cleanup_ghost_ecs_registrations and propagate drift counts into
    publish_premium_metrics."""

    @staticmethod
    def _lock_ctx(acquired: bool):
        mock = MagicMock()
        mock.return_value.__enter__.return_value = acquired
        mock.return_value.__exit__.return_value = False
        return mock

    def _common_patches(self, mock_env_vars_premium, call_order, reconcile_summary):
        return {
            "env": patch.dict("os.environ", mock_env_vars_premium),
            "lock": patch("premium_manager.distributed_lock", new=self._lock_ctx(True)),
            "active": patch(
                "premium_manager.count_active_premium_users", return_value=0
            ),
            "total": patch("premium_manager.count_total_premium_users", return_value=0),
            "instances": patch(
                "premium_manager.get_all_premium_instances_with_states",
                return_value=[],
            ),
            "assigned": patch(
                "premium_manager.get_assigned_users_for_instance", return_value=[]
            ),
            "scale": patch("premium_manager.scale_down_if_possible"),
            "desired": patch("premium_manager.update_premium_service_desired_count"),
            "cleanup_standby": patch(
                "premium_manager.cleanup_failed_standby_instances"
            ),
            "register_orphans": patch(
                "premium_manager.register_orphaned_stopped_instances"
            ),
            "terminate_aged": patch("premium_manager.terminate_aged_stopped_instances"),
            "standby_count": patch("premium_manager.get_standby_count", return_value=0),
            "replenish": patch("premium_manager.invoke_standby_replenishment_async"),
            "finalize": patch(
                "premium_manager.finalize_expired_pending_releases",
                return_value=[],
                side_effect=lambda: call_order.append("finalize") or [],
            ),
            "ghost": patch(
                "premium_manager.cleanup_ghost_ecs_registrations",
                side_effect=lambda: call_order.append("ghost"),
            ),
            "reconcile": patch(
                "premium_manager.reconcile_premium_target_group_ports",
                side_effect=lambda: call_order.append("reconcile") or reconcile_summary,
            ),
            "publish": patch(
                "premium_manager.publish_premium_metrics",
                side_effect=lambda **kw: call_order.append(("publish", kw)),
            ),
            "orphaned_ec2": patch("premium_manager.cleanup_orphaned_ec2_instances"),
            "fix_shared": patch(
                "premium_manager.fix_incorrect_is_shared_flags",
                return_value={"fixed_count": 0},
            ),
            "shared_opt": patch(
                "premium_manager.process_shared_instance_optimization",
                return_value={
                    "migrations_performed": 0,
                    "shared_instances_found": 0,
                },
            ),
        }

    def _run(self, mock_env_vars_premium, call_order, reconcile_summary):
        patches = self._common_patches(
            mock_env_vars_premium, call_order, reconcile_summary
        )
        ctx_managers = [p for p in patches.values()]
        for cm in ctx_managers:
            cm.__enter__()
        try:
            from premium_manager import handle_scheduled_monitoring

            return handle_scheduled_monitoring({"source": "test"}, None)
        finally:
            for cm in reversed(ctx_managers):
                cm.__exit__(None, None, None)

    def test_reconcile_runs_after_ghost_cleanup(self, mock_env_vars_premium):
        call_order: list = []
        result = self._run(
            mock_env_vars_premium,
            call_order,
            {"drift_detected": 0, "drift_fixed": 0},
        )

        assert result["statusCode"] == 200
        ghost_idx = call_order.index("ghost")
        reconcile_idx = call_order.index("reconcile")
        finalize_idx = call_order.index("finalize")
        assert ghost_idx < reconcile_idx
        assert finalize_idx < reconcile_idx

    def test_publish_metrics_runs_after_reconcile_with_drift_kwargs(
        self, mock_env_vars_premium
    ):
        call_order: list = []
        self._run(
            mock_env_vars_premium,
            call_order,
            {"drift_detected": 3, "drift_fixed": 2, "healed_missing_tg": 1},
        )

        publish_calls = [c for c in call_order if isinstance(c, tuple)]
        assert len(publish_calls) == 1
        kwargs = publish_calls[0][1]
        assert kwargs["tg_port_drift_detected"] == 3
        assert kwargs["tg_port_drift_fixed"] == 2
        assert kwargs["tg_healed_missing"] == 1

        reconcile_idx = call_order.index("reconcile")
        publish_idx = call_order.index(publish_calls[0])
        assert reconcile_idx < publish_idx

    def test_reconcile_exception_does_not_break_monitor(self, mock_env_vars_premium):
        """If the reconciler raises, the monitor still returns 200 and
        publish_premium_metrics still runs with drift kwargs defaulting
        to 0."""
        call_order: list = []
        patches = self._common_patches(
            mock_env_vars_premium, call_order, {"drift_detected": 0, "drift_fixed": 0}
        )
        patches["reconcile"] = patch(
            "premium_manager.reconcile_premium_target_group_ports",
            side_effect=RuntimeError("kaboom"),
        )
        ctx_managers = [p for p in patches.values()]
        for cm in ctx_managers:
            cm.__enter__()
        try:
            from premium_manager import handle_scheduled_monitoring

            result = handle_scheduled_monitoring({"source": "test"}, None)
        finally:
            for cm in reversed(ctx_managers):
                cm.__exit__(None, None, None)

        assert result["statusCode"] == 200
        publish_calls = [c for c in call_order if isinstance(c, tuple)]
        assert len(publish_calls) == 1
        # reconcile_summary fell back to {"errors": 1}; drift kwargs default to 0.
        assert publish_calls[0][1]["tg_port_drift_detected"] == 0
        assert publish_calls[0][1]["tg_port_drift_fixed"] == 0


class TestScheduledMonitoringStandbyConvergence:
    """handle_scheduled_monitoring must converge the standby pool toward
    target: replenish when below, trim when above, do nothing at target.
    This is the backstop for the best-effort async create on the assign path."""

    def _run(self, mock_env_vars_premium, standby_count, pool_size):
        base = TestHandleScheduledMonitoringReconcile()
        patches = base._common_patches(
            mock_env_vars_premium, [], {"drift_detected": 0, "drift_fixed": 0}
        )
        patches["env"] = patch.dict(
            "os.environ",
            {**mock_env_vars_premium, "PREMIUM_STANDBY_POOL_SIZE": str(pool_size)},
        )
        patches["standby_count"] = patch(
            "premium_manager.get_standby_count", return_value=standby_count
        )
        replenish = MagicMock()
        trim = MagicMock()
        patches["replenish"] = patch(
            "premium_manager.invoke_standby_replenishment_async", replenish
        )
        patches["trim"] = patch(
            "premium_manager.cleanup_excess_standby_instances", trim
        )
        ctx_managers = list(patches.values())
        for cm in ctx_managers:
            cm.__enter__()
        try:
            from premium_manager import handle_scheduled_monitoring

            result = handle_scheduled_monitoring({"source": "test"}, None)
        finally:
            for cm in reversed(ctx_managers):
                cm.__exit__(None, None, None)
        return result, replenish, trim

    def test_replenishes_when_below_target(self, mock_env_vars_premium):
        """standby_count < pool_size -> async replenishment, no trim."""
        result, replenish, trim = self._run(mock_env_vars_premium, 0, 2)
        assert result["statusCode"] == 200
        replenish.assert_called_once_with()
        trim.assert_not_called()

    def test_no_action_when_at_target(self, mock_env_vars_premium):
        """standby_count == pool_size -> neither replenish nor trim."""
        result, replenish, trim = self._run(mock_env_vars_premium, 2, 2)
        assert result["statusCode"] == 200
        replenish.assert_not_called()
        trim.assert_not_called()

    def test_trims_when_above_target(self, mock_env_vars_premium):
        """standby_count > pool_size -> trim excess, no replenish."""
        result, replenish, trim = self._run(mock_env_vars_premium, 3, 2)
        assert result["statusCode"] == 200
        trim.assert_called_once_with(1)
        replenish.assert_not_called()


class TestMigrateUserToDedicatedInstanceOrder:
    """The dedicated-to-dedicated migration branch must register the new
    instance before deregistering the old, so the TG is never empty
    across the swap."""

    def test_dedicated_branch_registers_before_deregisters(self, mock_env_vars_premium):
        from aws_constants import PremiumAssignment

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch(
            "premium_manager.pymysql.connect"
        ) as mock_pymysql, patch(
            "premium_manager.can_migrate_user", return_value=True, create=True
        ), patch(
            "premium_manager.try_reserve_instance_for_migration",
            return_value=True,
        ), patch(
            "premium_manager.target_group_exists", return_value=True
        ), patch(
            "premium_manager.trigger_experiment_sync", return_value=True
        ), patch(
            "premium_user_utils.can_migrate_user", return_value=True
        ):
            mock_elbv2 = MagicMock()
            mock_boto3.return_value = mock_elbv2

            mock_connection = setup_db_mock(
                fetchone_values=[
                    MockRow(
                        {
                            "instance_id": "i-old",
                            "target_group_arn": "arn:tg/premium-12-tg",
                            "alb_rule_arn": "arn:rule/r1",
                            "active_workflow_count": 0,
                        }
                    ),
                ]
            )
            mock_pymysql.return_value = mock_connection

            from premium_manager import migrate_user_to_dedicated_instance

            # old_instance_id is not AUTOSCALING_POOL → dedicated branch
            assert PremiumAssignment.AUTOSCALING_POOL != "i-old"
            migrate_user_to_dedicated_instance(12, "i-new")

            method_names = [call[0] for call in mock_elbv2.method_calls]
            register_idx = method_names.index("register_targets")
            deregister_idx = method_names.index("deregister_targets")
            assert register_idx < deregister_idx, (
                "register_targets must precede deregister_targets in "
                "migrate.dedicated"
            )


class TestPublishPremiumMetricsDriftKwargs:
    """publish_premium_metrics emits TargetGroupPortDriftDetected and
    TargetGroupPortDriftFixed; kwargs default to 0 for back-compat."""

    def test_emits_drift_metrics_with_values(self, mock_env_vars_premium):
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_cw = MagicMock()
            mock_boto3.return_value = mock_cw

            from premium_manager import publish_premium_metrics

            publish_premium_metrics(
                active_users=1,
                idle_users=2,
                running_instances=3,
                idle_instances=4,
                tg_port_drift_detected=5,
                tg_port_drift_fixed=6,
                tg_healed_missing=7,
            )

            args, kwargs = mock_cw.put_metric_data.call_args
            metric_data = kwargs["MetricData"]
            metric_by_name = {m["MetricName"]: m["Value"] for m in metric_data}
            assert metric_by_name["TargetGroupPortDriftDetected"] == 5
            assert metric_by_name["TargetGroupPortDriftFixed"] == 6
            assert metric_by_name["HealedMissingTargetGroup"] == 7

    def test_drift_metrics_default_to_zero(self, mock_env_vars_premium):
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_cw = MagicMock()
            mock_boto3.return_value = mock_cw

            from premium_manager import publish_premium_metrics

            publish_premium_metrics(
                active_users=0,
                idle_users=0,
                running_instances=0,
                idle_instances=0,
            )

            args, kwargs = mock_cw.put_metric_data.call_args
            metric_by_name = {m["MetricName"]: m["Value"] for m in kwargs["MetricData"]}
            assert metric_by_name["TargetGroupPortDriftDetected"] == 0
            assert metric_by_name["TargetGroupPortDriftFixed"] == 0
            assert metric_by_name["HealedMissingTargetGroup"] == 0


class TestPremiumTgUnhealthyAlarm:
    """Ephemeral per-user UnHealthyHostCount alarm, created on assign and
    deleted on release. Because it is recreated on every assign/release, OK
    actions are intentionally dropped so churn does not page "recovered" to
    the critical SNS topic; alarm actions are kept so genuine health failures
    still page."""

    TG_ARN = (
        "arn:aws:elasticloadbalancing:region:account:"
        "targetgroup/premium-6120-tg/abc123"
    )
    ALB_ARN = (
        "arn:aws:elasticloadbalancing:region:account:"
        "loadbalancer/app/test-alb/def456"
    )
    TOPIC_ARN = "arn:aws:sns:region:account:test-optinist-critical-alerts"
    EXPECTED_NAME = "test-premium-6120-tg-unhealthy-hosts"

    def _env(self, base):
        return {
            **base,
            "ALB_ARN": self.ALB_ARN,
            "CRITICAL_ALERTS_TOPIC_ARN": self.TOPIC_ARN,
        }

    def test_create_pages_on_alarm_but_not_on_ok(self, mock_env_vars_premium, capsys):
        with patch.dict("os.environ", self._env(mock_env_vars_premium)):
            import premium_manager

            mock_cw = MagicMock()
            with patch.object(
                premium_manager, "_get_cloudwatch_client", return_value=mock_cw
            ):
                premium_manager._ensure_premium_tg_unhealthy_alarm(self.TG_ARN)

            mock_cw.put_metric_alarm.assert_called_once()
            kwargs = mock_cw.put_metric_alarm.call_args.kwargs
            assert kwargs["AlarmName"] == self.EXPECTED_NAME
            assert kwargs["AlarmActions"] == [self.TOPIC_ARN]
            assert kwargs["OKActions"] == []
            assert "[premium-alarm] action=create" in capsys.readouterr().out

    def test_create_without_topic_wires_no_actions(self, mock_env_vars_premium):
        env = self._env(mock_env_vars_premium)
        env["CRITICAL_ALERTS_TOPIC_ARN"] = ""
        with patch.dict("os.environ", env):
            import premium_manager

            mock_cw = MagicMock()
            with patch.object(
                premium_manager, "_get_cloudwatch_client", return_value=mock_cw
            ):
                premium_manager._ensure_premium_tg_unhealthy_alarm(self.TG_ARN)

            kwargs = mock_cw.put_metric_alarm.call_args.kwargs
            assert kwargs["AlarmActions"] == []
            assert kwargs["OKActions"] == []

    def test_delete_removes_alarm_by_derived_name(self, mock_env_vars_premium, capsys):
        with patch.dict("os.environ", self._env(mock_env_vars_premium)):
            import premium_manager

            mock_cw = MagicMock()
            with patch.object(
                premium_manager, "_get_cloudwatch_client", return_value=mock_cw
            ):
                premium_manager._delete_premium_tg_unhealthy_alarm(self.TG_ARN)

            mock_cw.delete_alarms.assert_called_once_with(
                AlarmNames=[self.EXPECTED_NAME]
            )
            assert "[premium-alarm] action=delete" in capsys.readouterr().out


class TestAssignCascadeTiers:
    """Every reachable branch of the assign cascade emits the correct
    (assignment_source, is_shared) tuple, so each premium tier is exercised at
    least once.

    _assign_premium_user_impl has five cascade branches:
        dedicated / False, shared / True, standby / False,
        autoscaling_temp / True, aws_fallback / False.
    Only the first four are reachable; see
    test_aws_fallback_is_shadowed_by_autoscaling for why aws_fallback is dead.
    """

    @staticmethod
    def _run_assign(
        premium_manager,
        *,
        all_instances,
        standby,
        assigned_users=None,
        reserve=True,
        active_users=0,
    ):
        """Force one cascade branch via pool-state stubs, run the real assign
        path, and return ``(body, stubs)``.

        ``stubs`` exposes the follow-up calls each branch is supposed to fire.
        They were stubbed out and never asserted, so the standby branch's
        replacement-standby creation and the whole autoscaling-pool branch
        (the pool row is scaled and migrated to a dedicated instance) rested
        on nothing.
        """
        from contextlib import ExitStack

        assigned_users = [] if assigned_users is None else assigned_users

        mock_elbv2 = MagicMock()
        mock_elbv2.describe_target_groups.return_value = {"TargetGroups": []}
        mock_elbv2.create_target_group.return_value = {
            "TargetGroups": [{"TargetGroupArn": "arn:aws:tg/cascade"}]
        }

        def boto3_client(service):
            return mock_elbv2 if service == "elbv2" else MagicMock()

        stubs = {}

        with ExitStack() as stack:

            def stub(name, **kwargs):
                stubs[name] = stack.enter_context(
                    patch.object(premium_manager, name, **kwargs)
                )

            stub("restore_pending_release", return_value=None)
            stub("get_existing_user_assignment", return_value=None)
            stub("register_orphaned_stopped_instances")
            stub("get_all_premium_instances_with_states", return_value=all_instances)
            stub("count_active_premium_users", return_value=active_users)
            stub("get_available_standby_instances", return_value=standby)
            stub("check_instance_readiness_with_retry", return_value=True)
            stub("get_assigned_users_for_instance", return_value=assigned_users)
            stub("try_reserve_instance", return_value=reserve)
            stub("start_standby_instance", return_value=True)
            stub("invoke_standby_replenishment_async")
            stub("scale_premium_instances_if_needed", return_value=False)
            stub("invoke_migration_async")
            stub("_enable_sticky_sessions")
            stub("_ensure_premium_tg_unhealthy_alarm")
            stub("cleanup_duplicate_rules_for_routing_id", return_value=0)
            stub("create_alb_rule", return_value={"Rules": [{"RuleArn": "arn:rule"}]})
            stub("store_user_assignment")
            stub("update_user_activity", return_value=True)
            stack.enter_context(
                patch("premium_manager.pymysql.connect", return_value=setup_db_mock())
            )
            stack.enter_context(
                patch("premium_manager.distributed_lock", new=_always_acquired_lock())
            )
            stack.enter_context(patch("boto3.client", side_effect=boto3_client))

            result = premium_manager.assign_premium_user(
                12345, {"tier": "premium"}, "firebase_uid_cascade"
            )

        assert result["statusCode"] == 200, result["body"]
        return json.loads(result["body"]), SimpleNamespace(**stubs)

    def test_tier1_dedicated(self, mock_env_vars_premium):
        """Idle running instance with 0 users -> dedicated, is_shared False."""
        with patch.dict("os.environ", mock_env_vars_premium):
            import premium_manager

            body, stubs = self._run_assign(
                premium_manager,
                all_instances=[
                    {"instance_id": "i-ded", "state": InstanceState.RUNNING}
                ],
                standby=[],
                assigned_users=[],
                reserve=True,
            )
        assert body["assignment_source"] == "dedicated"
        assert body["is_shared"] is False
        # A dedicated instance needs no follow-up: no pool consumed, no
        # migration owed.
        stubs.invoke_standby_replenishment_async.assert_not_called()
        stubs.invoke_migration_async.assert_not_called()
        stubs.scale_premium_instances_if_needed.assert_not_called()

    def test_tier2_shared(self, mock_env_vars_premium):
        """All running instances occupied -> shared on least-loaded, is_shared True."""
        with patch.dict("os.environ", mock_env_vars_premium):
            import premium_manager

            body, stubs = self._run_assign(
                premium_manager,
                all_instances=[
                    {"instance_id": "i-run", "state": InstanceState.RUNNING}
                ],
                standby=[],
                assigned_users=[{"user_id": 999}],
                active_users=1,
            )
        assert body["assignment_source"] == "shared"
        assert body["is_shared"] is True
        stubs.invoke_standby_replenishment_async.assert_not_called()

    def test_tier3_standby(self, mock_env_vars_premium):
        """No running instances, standby available -> standby, is_shared False."""
        with patch.dict("os.environ", mock_env_vars_premium):
            import premium_manager

            body, stubs = self._run_assign(
                premium_manager,
                all_instances=[],
                standby=[{"instance_id": "i-standby1"}],
            )
        assert body["assignment_source"] == "standby"
        assert body["is_shared"] is False
        # 6206 expected #3: consuming the last standby must schedule a
        # replacement, or the next fresh login pays the full boot latency.
        stubs.invoke_standby_replenishment_async.assert_called_once_with()

    def test_tier3_5_autoscaling_pool(self, mock_env_vars_premium):
        """No premium capacity at all -> autoscaling_temp sentinel, is_shared True."""
        env = {
            **mock_env_vars_premium,
            "AUTOSCALING_TARGET_GROUP_ARN": "arn:aws:tg/autoscaling",
        }
        with patch.dict("os.environ", env):
            import premium_manager

            body, stubs = self._run_assign(
                premium_manager, all_instances=[], standby=[]
            )
        assert body["assignment_source"] == "autoscaling_temp"
        assert body["is_shared"] is True
        assert body["instance_id"] == premium_manager.PremiumAssignment.AUTOSCALING_POOL
        # The sentinel row is temporary. Without these the user stays on the
        # shared free pool indefinitely.
        stubs.scale_premium_instances_if_needed.assert_called_once_with()
        stubs.invoke_migration_async.assert_called_once_with()

    def test_aws_fallback_is_shadowed_by_autoscaling(self, mock_env_vars_premium):
        """PRIORITY 4 (aws_fallback) is unreachable. With only a stopped AWS
        instance and no running/standby capacity, PRIORITY 3.5
        (autoscaling_temp) always catches first: no_premium_available is
        necessarily true whenever instance_to_use is still None (a truthy
        available_dedicated would already have been assigned at PRIORITY 1).
        Documents the dead branch instead of pretending to cover it.
        """
        env = {
            **mock_env_vars_premium,
            "AUTOSCALING_TARGET_GROUP_ARN": "arn:aws:tg/autoscaling",
        }
        with patch.dict("os.environ", env):
            import premium_manager

            body, _ = self._run_assign(
                premium_manager,
                all_instances=[
                    {"instance_id": "i-stopped", "state": InstanceState.STOPPED}
                ],
                standby=[],
            )
        assert body["assignment_source"] == "autoscaling_temp"
        assert body["assignment_source"] != "aws_fallback"


class TestMigrationWorkflowGuard:
    """can_migrate_user blocks migration while a workflow is active (6217).
    The active_workflow_count guard returns False for count > 0 and for a
    missing row, and True only when no workflow is running."""

    @staticmethod
    def _can_migrate(premium_user_utils, row):
        """Run can_migrate_user against a single stubbed fetchone row."""
        cursor = MagicMock()
        cursor.fetchone.return_value = row
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        conn.cursor.return_value.__exit__.return_value = False
        db_cm = MagicMock()
        db_cm.__enter__.return_value = conn
        db_cm.__exit__.return_value = False
        with patch.object(premium_user_utils, "get_db_connection", return_value=db_cm):
            return premium_user_utils.can_migrate_user(42)

    def test_blocks_while_workflow_active(self, mock_env_vars_premium):
        with patch.dict("os.environ", mock_env_vars_premium):
            import premium_user_utils

            row = MockRow({"active_workflow_count": 2, "instance_id": "i-x"})
            assert self._can_migrate(premium_user_utils, row) is False

    def test_permits_when_no_active_workflow(self, mock_env_vars_premium):
        with patch.dict("os.environ", mock_env_vars_premium):
            import premium_user_utils

            row = MockRow({"active_workflow_count": 0, "instance_id": "i-x"})
            assert self._can_migrate(premium_user_utils, row) is True

    def test_permits_when_count_null(self, mock_env_vars_premium):
        """NULL active_workflow_count coalesces to 0 -> migration permitted."""
        with patch.dict("os.environ", mock_env_vars_premium):
            import premium_user_utils

            row = MockRow({"active_workflow_count": None, "instance_id": "i-x"})
            assert self._can_migrate(premium_user_utils, row) is True

    def test_blocks_when_user_not_found(self, mock_env_vars_premium):
        with patch.dict("os.environ", mock_env_vars_premium):
            import premium_user_utils

            assert self._can_migrate(premium_user_utils, None) is False

    def test_migrate_aborts_before_side_effects_when_workflow_active(
        self, mock_env_vars_premium
    ):
        """The real migrate path honours the guard: can_migrate_user False
        returns without reserving the target or touching ELB."""
        with patch.dict("os.environ", mock_env_vars_premium):
            import premium_manager

            with patch(
                "premium_user_utils.can_migrate_user", return_value=False
            ), patch.object(
                premium_manager, "try_reserve_instance_for_migration"
            ) as mock_reserve, patch(
                "boto3.client"
            ) as mock_boto3:
                result = premium_manager.migrate_user_to_dedicated_instance(42, "i-new")

        assert result is False
        mock_reserve.assert_not_called()
        mock_boto3.assert_not_called()

    @staticmethod
    def _run_blocked_migrate(premium_manager, fetchone_values):
        """Run migrate_user_to_dedicated_instance(42, "i-new") past
        can_migrate_user (reservation granted) with the given assignment-row
        fetch, capturing the reserve/release/elbv2 mocks. Returns
        (result, mock_reserve, mock_release, mock_elbv2)."""
        mock_elbv2 = MagicMock()
        with patch(
            "premium_user_utils.can_migrate_user", return_value=True
        ), patch.object(
            premium_manager,
            "try_reserve_instance_for_migration",
            return_value=True,
        ) as mock_reserve, patch.object(
            premium_manager, "release_instance_reservation"
        ) as mock_release, patch(
            "boto3.client", return_value=mock_elbv2
        ), patch(
            "premium_manager.pymysql.connect",
            return_value=setup_db_mock(fetchone_values=fetchone_values),
        ):
            result = premium_manager.migrate_user_to_dedicated_instance(42, "i-new")
        return result, mock_reserve, mock_release, mock_elbv2

    _ACTIVE_WORKFLOW_ROW = [
        MockRow(
            {
                "instance_id": "i-old",
                "target_group_arn": "arn:tg/old",
                "alb_rule_arn": "arn:rule/old",
                "active_workflow_count": 3,
            }
        )
    ]

    def test_migrate_aborts_when_record_shows_active_workflow(
        self, mock_env_vars_premium
    ):
        """Defense in depth: even past can_migrate_user, a stale
        active_workflow_count > 0 on the assignment row blocks the swap before
        any target-group mutation (no register/deregister). The reserve is a
        transient SELECT..FOR UPDATE lock that writes no persistent row, so the
        abort has nothing to release (no release_instance_reservation call)."""
        with patch.dict("os.environ", mock_env_vars_premium):
            import premium_manager

            result, mock_reserve, mock_release, mock_elbv2 = self._run_blocked_migrate(
                premium_manager, self._ACTIVE_WORKFLOW_ROW
            )

        assert result is False
        mock_elbv2.register_targets.assert_not_called()
        mock_elbv2.deregister_targets.assert_not_called()
        mock_reserve.assert_called_once()
        mock_release.assert_not_called()

    def test_migrate_aborts_when_no_assignment_record(self, mock_env_vars_premium):
        """No assignment row past the reserve also aborts before any
        target-group mutation. Same transient-lock reserve, so nothing to
        release on abort."""
        with patch.dict("os.environ", mock_env_vars_premium):
            import premium_manager

            result, mock_reserve, mock_release, mock_elbv2 = self._run_blocked_migrate(
                premium_manager, [None]
            )

        assert result is False
        mock_elbv2.register_targets.assert_not_called()
        mock_elbv2.deregister_targets.assert_not_called()
        mock_reserve.assert_called_once()
        mock_release.assert_not_called()


class TestInlineMigrationOnAdoption:
    """A user holding a shared / autoscaling-pool assignment is migrated to a
    ready idle dedicated instance inline, in a single assign invocation, and
    does not fall through to the async migration path (6233).

    An autoscaling-pool existing assignment skips the EC2-state precheck (guarded
    by instance_id != AUTOSCALING_POOL) and enters the inline-migration block
    directly, so this drives the real adoption path end to end.
    """

    def test_inline_migration_returns_dedicated_without_async_fallback(
        self, mock_env_vars_premium
    ):
        from aws_constants import PremiumAssignment

        pool_row = MockRow(
            {
                "instance_id": PremiumAssignment.AUTOSCALING_POOL,
                "is_shared": True,
                "target_group_arn": "arn:tg/shared",
                "alb_rule_arn": "arn:rule/shared",
            }
        )
        migrated_row = MockRow(
            {
                "instance_id": "i-dedicated",
                "is_shared": False,
                "target_group_arn": "arn:tg/premium-77-tg",
                "alb_rule_arn": "arn:rule/premium-77",
            }
        )
        ready_dedicated = [
            {"instance_id": "i-dedicated", "state": InstanceState.RUNNING}
        ]
        with patch.dict("os.environ", mock_env_vars_premium):
            import premium_manager

            with patch.object(
                premium_manager, "restore_pending_release", return_value=None
            ), patch.object(
                premium_manager,
                "get_existing_user_assignment",
                side_effect=[pool_row, migrated_row],
            ), patch.object(
                premium_manager,
                "get_all_premium_instances_with_states",
                return_value=ready_dedicated,
            ), patch.object(
                premium_manager, "get_assigned_users_for_instance", return_value=[]
            ), patch.object(
                premium_manager,
                "check_instance_readiness_with_retry",
                return_value=True,
            ), patch.object(
                premium_manager,
                "migrate_user_to_dedicated_instance",
                return_value=True,
            ) as mock_migrate, patch.object(
                premium_manager, "invoke_migration_async"
            ) as mock_async, patch(
                "premium_manager.pymysql.connect", return_value=setup_db_mock()
            ), patch(
                "premium_manager.distributed_lock", new=_always_acquired_lock()
            ), patch(
                "boto3.client", return_value=MagicMock()
            ):
                result = premium_manager.assign_premium_user(
                    77, {"tier": "premium"}, "firebase_uid_inline"
                )

        assert result["statusCode"] == 200, result["body"]
        body = json.loads(result["body"])
        assert body["assignment_source"] == "inline_migration"
        assert body["is_shared"] is False
        assert body["instance_id"] == "i-dedicated"
        mock_migrate.assert_called_once_with(77, "i-dedicated")
        mock_async.assert_not_called()


class TestIdleUserSelectorExcludesActiveWorkflows:
    """get_idle_premium_users_for_instance selects only workflow-free users
    (6217 "migrate query excludes count > 0"). The exclusion lives
    purely in SQL, so assert the query filters active_workflow_count = 0 rather
    than a Python-side filter that does not exist."""

    def test_query_filters_on_zero_active_workflows(self, mock_env_vars_premium):
        with patch.dict("os.environ", mock_env_vars_premium):
            import premium_user_utils

            cursor = MagicMock()
            cursor.fetchall.return_value = [
                MockRow({"user_id": 11}),
                MockRow({"user_id": 22}),
            ]
            conn = MagicMock()
            conn.cursor.return_value.__enter__.return_value = cursor
            conn.cursor.return_value.__exit__.return_value = False
            db_cm = MagicMock()
            db_cm.__enter__.return_value = conn
            db_cm.__exit__.return_value = False

            with patch.object(
                premium_user_utils, "get_db_connection", return_value=db_cm
            ):
                result = premium_user_utils.get_idle_premium_users_for_instance(
                    "i-target"
                )

        assert result == [11, 22]
        sql = cursor.execute.call_args[0][0]
        params = cursor.execute.call_args[0][1]
        normalized = " ".join(sql.split())
        assert "instance_id = %s" in normalized
        assert "active_workflow_count = 0" in normalized
        assert "status = 'active'" in normalized
        assert params == ("i-target",)


class TestSoftReleaseUserAssignment:
    """The soft release keeps the row and its ALB resources.

    A beacon-driven release has to leave the assignment recoverable: the row
    flipped to pending_release with its rule and target group still standing. A
    release that deleted the row outright looks identical to its caller but
    makes the restore impossible.
    """

    def _soft_release(self, mock_env_vars_premium, row):
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql, patch("boto3.client") as mock_boto3:
            mock_connection = setup_db_mock(fetchone_values=[row])
            mock_pymysql.return_value = mock_connection
            cursor = mock_connection.cursor.return_value.__enter__.return_value
            aws = MagicMock()
            mock_boto3.return_value = aws

            from premium_manager import soft_release_user_assignment

            return soft_release_user_assignment(42), cursor, aws

    def test_flips_the_row_to_pending_release_without_deleting_it(
        self, mock_env_vars_premium
    ):
        from aws_constants import PremiumAssignment

        result, cursor, aws = self._soft_release(
            mock_env_vars_premium,
            MockRow(
                {
                    "instance_id": TEST_INSTANCE_ID,
                    "target_group_arn": "arn:aws:tg/user-42",
                    "alb_rule_arn": "arn:aws:rule/user-42",
                    "status": PremiumAssignment.ACTIVE,
                }
            ),
        )

        statements = [
            (" ".join(call[0][0].split()), call[0][1])
            for call in cursor.execute.call_args_list
        ]
        assert not any(sql.startswith("DELETE") for sql, _ in statements)
        row_updates = [
            (sql, params)
            for sql, params in statements
            if sql.startswith("UPDATE premium_user_assignments")
        ]
        assert len(row_updates) == 1
        assert "assigned_at" not in row_updates[0][0]
        assert row_updates[0][1] == (
            PremiumAssignment.PENDING_RELEASE,
            42,
            PremiumAssignment.ACTIVE,
        )
        # Usage log closed so grace time is not billed as active premium use.
        assert any(sql.startswith("UPDATE instance_usage_log") for sql, _ in statements)
        # The ALB rule and target group must survive, or the restore would have
        # to recreate them (the whole point of the grace window).
        aws.delete_rule.assert_not_called()
        aws.delete_target_group.assert_not_called()
        assert result["instance_id"] == TEST_INSTANCE_ID

    def test_no_active_assignment_is_a_noop(self, mock_env_vars_premium):
        """A second beacon (or one after the grace expired) must not write."""
        result, cursor, _ = self._soft_release(mock_env_vars_premium, None)

        assert result is None
        assert len(cursor.execute.call_args_list) == 1

    def test_soft_release_does_not_scale_down_but_hard_release_does(
        self, mock_env_vars_premium
    ):
        """The instance is still allocated during the grace, so scaling it down
        would strand a user who is about to come back."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_manager.soft_release_user_assignment",
            return_value={"instance_id": TEST_INSTANCE_ID},
        ), patch(
            "premium_manager.remove_user_assignment",
            return_value={
                "instance_id": TEST_INSTANCE_ID,
                "target_group_arn": "arn:aws:tg/user-42",
                "alb_rule_arn": "arn:aws:rule/user-42",
            },
        ), patch(
            "premium_manager._teardown_alb_resources", return_value=[]
        ), patch(
            "premium_manager.count_active_premium_users", return_value=1
        ), patch(
            "premium_manager.scale_down_if_possible"
        ) as mock_scale_down:
            from premium_manager import release_premium_user

            soft = release_premium_user(42)
            assert soft["statusCode"] == 200
            assert "soft release completed" in json.loads(soft["body"])["message"]
            mock_scale_down.assert_not_called()

            hard = release_premium_user(42, hard=True)
            assert "hard release completed" in json.loads(hard["body"])["message"]
            mock_scale_down.assert_called_once()


class TestRestorePendingReleaseTransaction:
    """restore_pending_release restores the SAME row.

    A reopen inside the grace window has to land the user back on the row they
    already had: same id, same assigned_at, status back to active, and no ALB
    resources recreated. TestHeartbeatRestoresPendingRelease covers the
    heartbeat route to the same outcome; this is the status-check route.
    """

    ASSIGNED_AT = datetime(2026, 7, 30, 9, 0, 0)

    def _pending_row(self, instance_id=TEST_INSTANCE_ID, **overrides):
        from aws_constants import PremiumAssignment

        row = {
            "user_id": 42,
            "instance_id": instance_id,
            "target_group_arn": "arn:aws:tg/user-42",
            "alb_rule_arn": "arn:aws:rule/user-42",
            "status": PremiumAssignment.PENDING_RELEASE,
            "instance_state": "running",
            "is_shared": 0,
            "assigned_at": self.ASSIGNED_AT,
        }
        row.update(overrides)
        return MockRow(row)

    def _restore(self, mock_env_vars_premium, row, ec2_state="running"):
        """Run the real restore transaction; returns (result, cursor, aws)."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql, patch("boto3.client") as mock_boto3:
            mock_connection = setup_db_mock(fetchone_values=[row])
            mock_pymysql.return_value = mock_connection
            cursor = mock_connection.cursor.return_value.__enter__.return_value
            aws = MagicMock()
            if ec2_state is None:
                aws.describe_instances.return_value = {"Reservations": []}
            else:
                aws.describe_instances.return_value = {
                    "Reservations": [{"Instances": [{"State": {"Name": ec2_state}}]}]
                }
            mock_boto3.return_value = aws

            from premium_manager import restore_pending_release

            return restore_pending_release(42), cursor, aws

    @staticmethod
    def _statements(cursor):
        return [" ".join(call[0][0].split()) for call in cursor.execute.call_args_list]

    def test_restores_same_row_to_active(self, mock_env_vars_premium):
        """The UPDATE flips status only - assigned_at is never re-stamped, so the
        restored row stays indistinguishable from the one the user had."""
        from aws_constants import PremiumAssignment

        result, cursor, _ = self._restore(mock_env_vars_premium, self._pending_row())

        updates = [s for s in self._statements(cursor) if s.startswith("UPDATE")]
        assert len(updates) == 1
        assert "SET status = %s, last_activity = NOW()" in updates[0]
        assert "assigned_at" not in updates[0]
        assert cursor.execute.call_args_list[-1][0][1] == (
            PremiumAssignment.ACTIVE,
            42,
            PremiumAssignment.PENDING_RELEASE,
        )
        assert not any(s.startswith("DELETE") for s in self._statements(cursor))
        assert result["assigned_at"] == self.ASSIGNED_AT

    def test_restore_creates_no_alb_resources(self, mock_env_vars_premium):
        """The grace window exists so the restore can reuse the rule and target
        group; recreating them would defeat the point of keeping the row."""
        _, _, aws = self._restore(mock_env_vars_premium, self._pending_row())

        aws.create_target_group.assert_not_called()
        aws.create_rule.assert_not_called()
        aws.delete_rule.assert_not_called()
        aws.delete_target_group.assert_not_called()

    def test_autoscaling_pool_skips_the_ec2_liveness_check(self, mock_env_vars_premium):
        """The pool marker is not a real instance, so describe_instances on it
        would raise and drop a restorable assignment."""
        from aws_constants import PremiumAssignment

        result, cursor, aws = self._restore(
            mock_env_vars_premium,
            self._pending_row(instance_id=PremiumAssignment.AUTOSCALING_POOL),
        )

        aws.describe_instances.assert_not_called()
        assert result is not None
        assert any(s.startswith("UPDATE") for s in self._statements(cursor))

    def test_dead_instance_deletes_the_row_instead_of_restoring(
        self, mock_env_vars_premium
    ):
        """A terminated instance must not be restored: the row is deleted so the
        next login assigns fresh (and the ALB leftovers are torn down)."""
        from aws_constants import PremiumAssignment

        with patch(
            "premium_manager._teardown_alb_resources", return_value=[]
        ) as mock_teardown:
            result, cursor, _ = self._restore(
                mock_env_vars_premium,
                self._pending_row(),
                ec2_state=InstanceState.TERMINATED,
            )

        assert result is None
        deletes = [s for s in self._statements(cursor) if s.startswith("DELETE")]
        assert len(deletes) == 1
        assert deletes[0].startswith("DELETE FROM premium_user_assignments")
        delete_params = [
            call[0][1]
            for call in cursor.execute.call_args_list
            if call[0][0].strip().startswith("DELETE")
        ]
        assert delete_params == [(42, PremiumAssignment.PENDING_RELEASE)]
        mock_teardown.assert_called_once_with(
            42, "arn:aws:rule/user-42", "arn:aws:tg/user-42"
        )

    def test_returns_none_when_no_pending_release_row(self, mock_env_vars_premium):
        """Nothing in grace - the caller keeps whatever status it already read."""
        result, cursor, _ = self._restore(mock_env_vars_premium, None)

        assert result is None
        statements = self._statements(cursor)
        assert len(statements) == 1
        assert statements[0].startswith("SELECT")


class TestFinalizeExpiredPendingReleases:
    """finalize_expired_pending_releases deletes rows past the grace.

    Once the grace lapses the old row has to be gone, so the next login assigns
    fresh instead of restoring a row whose ALB resources are being torn down.
    """

    def _finalize(self, mock_env_vars_premium, expired_rows):
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "pymysql.connect"
        ) as mock_pymysql:
            mock_connection = setup_db_mock(fetchall_values=[expired_rows])
            mock_pymysql.return_value = mock_connection
            cursor = mock_connection.cursor.return_value.__enter__.return_value

            from premium_manager import finalize_expired_pending_releases

            return finalize_expired_pending_releases(), cursor

    def test_selects_only_rows_past_the_grace_window(self, mock_env_vars_premium):
        from aws_constants import PremiumAssignment

        _, cursor = self._finalize(mock_env_vars_premium, [])

        sql = " ".join(cursor.execute.call_args_list[0][0][0].split())
        params = cursor.execute.call_args_list[0][0][1]
        assert "last_activity < DATE_SUB(NOW(), INTERVAL %s SECOND)" in sql
        # FOR UPDATE: the sweep and a concurrent restore must not race.
        assert sql.endswith("FOR UPDATE")
        assert params == (
            PremiumAssignment.PENDING_RELEASE,
            PremiumAssignment.PENDING_RELEASE_GRACE_SECONDS,
        )

    def test_deletes_each_expired_row_and_returns_it_for_teardown(
        self, mock_env_vars_premium
    ):
        from aws_constants import PremiumAssignment

        rows = [
            MockRow(
                {
                    "user_id": 11,
                    "instance_id": "i-dedicated",
                    "target_group_arn": "arn:aws:tg/user-11",
                    "alb_rule_arn": "arn:aws:rule/user-11",
                }
            ),
            MockRow(
                {
                    "user_id": 22,
                    "instance_id": PremiumAssignment.AUTOSCALING_POOL,
                    "target_group_arn": "",
                    "alb_rule_arn": "",
                }
            ),
        ]
        expired, cursor = self._finalize(mock_env_vars_premium, rows)

        assert expired == rows
        statements = [
            (" ".join(call[0][0].split()), call[0][1])
            for call in cursor.execute.call_args_list
        ]
        deletes = [p for sql, p in statements if sql.startswith("DELETE")]
        assert deletes == [
            (11, PremiumAssignment.PENDING_RELEASE),
            (22, PremiumAssignment.PENDING_RELEASE),
        ]
        # Usage log closed per row, so premium minutes stop billing at release.
        usage_closes = [
            p for sql, p in statements if sql.startswith("UPDATE instance_usage_log")
        ]
        assert usage_closes == [(11,), (22,)]

    def test_no_expired_rows_deletes_nothing(self, mock_env_vars_premium):
        expired, cursor = self._finalize(mock_env_vars_premium, [])

        assert expired == []
        assert not any(
            call[0][0].strip().startswith("DELETE")
            for call in cursor.execute.call_args_list
        )

    def test_teardown_drops_the_per_user_tg_but_never_the_shared_one(
        self, mock_env_vars_premium
    ):
        """The monitor hands every finalized row to _teardown_alb_resources, and a
        pool row carries the shared ASG target group - deleting that one would
        break routing for every autoscaling-pool user at once."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            elbv2 = MagicMock()
            mock_boto3.return_value = elbv2

            from premium_manager import _teardown_alb_resources

            shared_tg = mock_env_vars_premium["AUTOSCALING_TARGET_GROUP_ARN"]
            assert _teardown_alb_resources(22, "arn:aws:rule/user-22", shared_tg) == []
            elbv2.delete_rule.assert_called_once_with(RuleArn="arn:aws:rule/user-22")
            elbv2.delete_target_group.assert_not_called()

            assert _teardown_alb_resources(11, None, "arn:aws:tg/user-11") == []
            elbv2.delete_target_group.assert_called_once_with(
                TargetGroupArn="arn:aws:tg/user-11"
            )
