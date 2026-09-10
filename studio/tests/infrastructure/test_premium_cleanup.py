"""Tests for premium_cleanup Lambda function."""

from unittest.mock import MagicMock, patch

from aws_constants import (
    ECSTaskStatus,
    PremiumAssignment,
    PremiumInstanceConfig,
    RoutingHeaders,
)
from conftest import MockRow, setup_db_mock

TEST_INSTANCE_ID = "i-testlambda123"


def _premium_alb_rule(rule_arn, tg_arn, routing_id, priority="100"):
    """Build a describe_rules entry shaped like a premium per-user rule."""
    return {
        "RuleArn": rule_arn,
        "Priority": priority,
        "Conditions": [
            {
                "Field": "http-header",
                "HttpHeaderConfig": {
                    "HttpHeaderName": RoutingHeaders.ROUTING_ID,
                    "Values": [routing_id],
                },
            },
            {
                "Field": "http-header",
                "HttpHeaderConfig": {
                    "HttpHeaderName": RoutingHeaders.USER_TIER,
                    "Values": ["premium"],
                },
            },
        ],
        "Actions": [{"Type": "forward", "TargetGroupArn": tg_arn}],
    }


def setup_keepset_filtering_db_mock(candidate_rows):
    """DB mock whose fetchall applies the orphan-sweep keep-set predicate using
    the params the code actually passed to execute — so the keep-set tests are
    behavioral, not SQL change-detectors.

    A candidate row is returned iff its status is among the string params
    (``status IN (...)``) OR the query carries a recency bound (an int param)
    and the row is flagged recent (``last_activity >= NOW() - grace``). Removing
    either clause from the production query drops the corresponding param, so the
    row stops being returned and the sweep reaps it — turning the test red.

    candidate_rows: dicts with alb_rule_arn, target_group_arn, user_id, status,
    is_recent.
    """
    mock_cursor = MagicMock()
    mock_cursor.rowcount = 1

    def fetchall():
        params = mock_cursor.execute.call_args.args[1]
        status_params = {p for p in params if isinstance(p, str)}
        has_recency_bound = any(isinstance(p, int) for p in params)
        kept = []
        for row in candidate_rows:
            status_match = row["status"] in status_params
            recency_match = has_recency_bound and row["is_recent"]
            if status_match or recency_match:
                kept.append(
                    MockRow(
                        {
                            "alb_rule_arn": row["alb_rule_arn"],
                            "target_group_arn": row["target_group_arn"],
                            "user_id": row["user_id"],
                        }
                    )
                )
        return kept

    mock_cursor.fetchall.side_effect = fetchall
    mock_connection = MagicMock()
    mock_connection.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connection.__enter__.return_value = mock_connection
    mock_connection.__exit__.return_value = None
    return mock_connection


class TestPremiumCleanupHandler:
    """Handler-level tests for premium_cleanup Lambda."""

    def test_scheduled_event(self, mock_env_vars_premium):
        """Test premium_cleanup Lambda with scheduled event."""
        print("Testing Premium Cleanup Lambda - Scheduled Event")
        print("=" * 50)

        scheduled_event = {
            "source": "aws.events",
            "detail-type": "Scheduled Event",
            "detail": {"action": "cleanup"},
            "time": "2025-09-17T10:00:00Z",
        }

        mock_context = MagicMock()
        mock_context.function_name = "subscr-premium-cleanup"

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch("premium_cleanup.pymysql.connect") as mock_pymysql:
            mock_connection = setup_db_mock(
                fetchone_values=[
                    MockRow({"count": 0}),
                    MockRow({"count": 1}),
                    MockRow({"count": 0}),
                    MockRow({"count": 1}),
                    MockRow({"count": 3}),
                    MockRow({"count": 0}),
                ],
                fetchall_values=[
                    [],
                    [],
                    [],
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

            mock_ec2 = MagicMock()
            mock_boto3.return_value = mock_ec2
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
                                        "Value": "premium-instance",
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }

            from premium_cleanup import handler

            result = handler(scheduled_event, mock_context)

            assert isinstance(result, dict)
            assert result["statusCode"] == 200


class TestCheckInstanceReadiness:
    """check_instance_readiness tests."""

    def test_success(self, mock_env_vars_premium):
        """Running premium ECS task returns True."""
        test_inst = "i-ready123"
        ci_arn = "arn:aws:ecs:us-east-1:123:container-instance/ci1"
        task_arn = "arn:aws:ecs:us-east-1:123:task/t1"

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs = MagicMock()

            def boto3_client_side_effect(service):
                if service == "ecs":
                    return mock_ecs
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": [ci_arn]
            }
            mock_ecs.describe_container_instances.return_value = {
                "containerInstances": [
                    {
                        "containerInstanceArn": ci_arn,
                        "ec2InstanceId": test_inst,
                    }
                ]
            }
            mock_ecs.list_tasks.return_value = {"taskArns": [task_arn]}
            mock_ecs.describe_tasks.return_value = {
                "tasks": [
                    {
                        "taskDefinitionArn": (
                            "arn:aws:ecs:us-east-1:123:"
                            "task-definition/optinist-premium"
                        ),
                        "lastStatus": ECSTaskStatus.RUNNING,
                        "desiredStatus": ECSTaskStatus.RUNNING,
                    }
                ]
            }

            from premium_cleanup import check_instance_readiness

            result = check_instance_readiness(test_inst)
            assert result is True

    def test_no_tasks(self, mock_env_vars_premium):
        """No tasks on container returns False."""
        test_inst = "i-notasks123"
        ci_arn = "arn:aws:ecs:us-east-1:123:container-instance/ci2"

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs = MagicMock()

            def boto3_client_side_effect(service):
                if service == "ecs":
                    return mock_ecs
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": [ci_arn]
            }
            mock_ecs.describe_container_instances.return_value = {
                "containerInstances": [
                    {
                        "containerInstanceArn": ci_arn,
                        "ec2InstanceId": test_inst,
                    }
                ]
            }
            mock_ecs.list_tasks.return_value = {"taskArns": []}

            from premium_cleanup import check_instance_readiness

            result = check_instance_readiness(test_inst)
            assert result is False

    def test_no_container(self, mock_env_vars_premium):
        """No container instance returns False."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ecs = MagicMock()

            def boto3_client_side_effect(service):
                if service == "ecs":
                    return mock_ecs
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect
            mock_ecs.list_container_instances.return_value = {
                "containerInstanceArns": []
            }

            from premium_cleanup import check_instance_readiness

            result = check_instance_readiness("i-nocontainer")
            assert result is False


class TestCleanupStaleAssignments:
    """cleanup_stale_assignments tests.

    Every case here used to be handed a pre-filtered row list, so the
    ``last_activity`` staleness predicate that decides whose premium instance is
    reclaimed was never inspected: shortening the interval to seconds, or
    dropping the WHERE clause entirely, kept all three green.
    """

    @staticmethod
    def _stale_row(user_id=999, tg_arn="arn:aws:tg/stale-tg", rule_arn=None):
        return MockRow(
            {
                "user_id": user_id,
                "instance_id": f"i-stale{user_id}",
                "target_group_arn": tg_arn,
                "alb_rule_arn": rule_arn or f"arn:aws:rule/stale-{user_id}",
                "last_activity": "2025-01-01",
            }
        )

    def _cleanup(self, env, rows):
        with patch.dict("os.environ", env), patch("boto3.client") as mock_boto3, patch(
            "premium_cleanup.pymysql.connect"
        ) as mock_pymysql:
            mock_connection = setup_db_mock(fetchall_values=[rows])
            mock_pymysql.return_value = mock_connection
            cursor = mock_connection.cursor.return_value.__enter__.return_value

            mock_elbv2 = MagicMock()
            mock_boto3.side_effect = lambda service: (
                mock_elbv2 if service == "elbv2" else MagicMock()
            )

            from premium_cleanup import cleanup_stale_assignments

            return cleanup_stale_assignments(), cursor, mock_elbv2

    @staticmethod
    def _statements(cursor):
        return [" ".join(c[0][0].split()) for c in cursor.execute.call_args_list]

    @staticmethod
    def _params_for(cursor, verb):
        return [
            c[0][1]
            for c in cursor.execute.call_args_list
            if " ".join(c[0][0].split()).startswith(verb)
        ]

    def test_selects_only_rows_idle_past_the_configured_timeout(
        self, mock_env_vars_premium
    ):
        """The staleness predicate and its bind, not just "a SELECT ran"."""
        _, cursor, _ = self._cleanup(mock_env_vars_premium, [])

        sql = self._statements(cursor)[0]
        assert "FROM premium_user_assignments" in sql
        assert "WHERE status = %s" in sql
        assert "AND is_standby = 0" in sql
        assert "AND last_activity < DATE_SUB(NOW(), INTERVAL %s HOUR)" in sql
        # FOR UPDATE: the sweep and a concurrent heartbeat must not race.
        assert sql.endswith("FOR UPDATE")
        assert cursor.execute.call_args_list[0][0][1] == (
            PremiumAssignment.ACTIVE,
            int(mock_env_vars_premium["PREMIUM_IDLE_TIMEOUT_HOURS"]),
        )

    def test_no_stale_assignments(self, mock_env_vars_premium):
        """No stale assignments returns 0 cleaned and writes nothing."""
        result, cursor, elbv2 = self._cleanup(mock_env_vars_premium, [])

        assert result["cleaned_assignments"] == 0
        assert self._params_for(cursor, "DELETE") == []
        assert self._params_for(cursor, "UPDATE") == []
        elbv2.delete_rule.assert_not_called()

    def test_deletes_alb_and_db(self, mock_env_vars_premium):
        """Stale assignment triggers ALB + DB cleanup."""
        rule_arn = "arn:aws:rule/stale-rule"
        tg_arn = "arn:aws:tg/stale-tg"

        result, cursor, elbv2 = self._cleanup(
            mock_env_vars_premium,
            [self._stale_row(tg_arn=tg_arn, rule_arn=rule_arn)],
        )

        assert result["cleaned_assignments"] == 1
        elbv2.delete_rule.assert_called_once_with(RuleArn=rule_arn)
        elbv2.delete_target_group.assert_called_once_with(TargetGroupArn=tg_arn)

        # The DB half the name promises: the row goes and the usage log closes.
        deletes = [s for s in self._statements(cursor) if s.startswith("DELETE")]
        assert deletes == ["DELETE FROM premium_user_assignments WHERE user_id = %s"]
        assert self._params_for(cursor, "DELETE") == [(999,)]

        updates = [s for s in self._statements(cursor) if s.startswith("UPDATE")]
        assert len(updates) == 1
        assert updates[0].startswith("UPDATE instance_usage_log SET ended_at = NOW()")
        assert "WHERE user_id = %s AND tier = 'premium' AND ended_at IS NULL" in (
            updates[0]
        )
        assert self._params_for(cursor, "UPDATE") == [(999,)]

    def test_skips_autoscaling_tg(self, mock_env_vars_premium):
        """Shared-ASG exception: the rule is deleted, the target group is kept.

        The autoscaling target group is shared by every pooled premium instance,
        so deleting it with one user's assignment would break routing for all of
        them. The DB row still goes.
        """
        rule_arn = "arn:aws:rule/stale-asg-rule"
        asg_tg_arn = mock_env_vars_premium["AUTOSCALING_TARGET_GROUP_ARN"]

        result, cursor, elbv2 = self._cleanup(
            mock_env_vars_premium,
            [self._stale_row(user_id=888, tg_arn=asg_tg_arn, rule_arn=rule_arn)],
        )

        assert result["cleaned_assignments"] == 1
        elbv2.delete_rule.assert_called_once_with(RuleArn=rule_arn)
        elbv2.delete_target_group.assert_not_called()
        assert self._params_for(cursor, "DELETE") == [(888,)]

    def test_standby_rows_keep_their_alb_resources(self, mock_env_vars_premium):
        """A standby marker has no per-user rule or target group to delete."""
        result, cursor, elbv2 = self._cleanup(
            mock_env_vars_premium,
            [
                self._stale_row(
                    user_id=777,
                    tg_arn=PremiumAssignment.STANDBY,
                    rule_arn=PremiumAssignment.STANDBY,
                )
            ],
        )

        assert result["cleaned_assignments"] == 1
        elbv2.delete_rule.assert_not_called()
        elbv2.delete_target_group.assert_not_called()
        assert self._params_for(cursor, "DELETE") == [(777,)]


class TestCleanupOrphanedAlbResources:
    """cleanup_orphaned_alb_resources tests."""

    def test_no_orphans(self, mock_env_vars_premium):
        """All ALB rules match DB, no orphans."""
        valid_rule_arn = "arn:aws:rule/valid-in-db"

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch("premium_cleanup.pymysql.connect") as mock_pymysql:
            mock_elbv2 = MagicMock()

            def boto3_client_side_effect(service):
                if service == "elbv2":
                    return mock_elbv2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            mock_elbv2.describe_rules.return_value = {
                "Rules": [
                    {
                        "RuleArn": valid_rule_arn,
                        "Priority": "100",
                        "Conditions": [
                            {
                                "Field": "http-header",
                                "HttpHeaderConfig": {
                                    "HttpHeaderName": (RoutingHeaders.ROUTING_ID),
                                    "Values": ["rid-123"],
                                },
                            },
                            {
                                "Field": "http-header",
                                "HttpHeaderConfig": {
                                    "HttpHeaderName": (RoutingHeaders.USER_TIER),
                                    "Values": ["premium"],
                                },
                            },
                        ],
                        "Actions": [
                            {
                                "Type": "forward",
                                "TargetGroupArn": ("arn:aws:tg/v1"),
                            }
                        ],
                    }
                ]
            }

            mock_connection = setup_db_mock(
                fetchall_values=[
                    [
                        MockRow(
                            {
                                "alb_rule_arn": valid_rule_arn,
                                "target_group_arn": ("arn:aws:tg/v1"),
                                "user_id": 100,
                            }
                        )
                    ],
                ],
            )
            mock_pymysql.return_value = mock_connection

            from premium_cleanup import cleanup_orphaned_alb_resources

            result = cleanup_orphaned_alb_resources()

            assert result["orphaned_rules_deleted"] == 0
            assert not mock_elbv2.delete_rule.called
            # The whole body is wrapped in ``except Exception`` returning
            # ``orphaned_rules_deleted: 0``, so both assertions above also hold
            # when the sweep dies on its first line.
            assert "error" not in result, result

    def test_deletes_orphan(self, mock_env_vars_premium):
        """Orphaned ALB rule (not in DB) deleted."""
        orphan_arn = "arn:aws:rule/orphan-no-db"

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch("premium_cleanup.pymysql.connect") as mock_pymysql:
            mock_elbv2 = MagicMock()

            def boto3_client_side_effect(service):
                if service == "elbv2":
                    return mock_elbv2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            mock_elbv2.describe_rules.return_value = {
                "Rules": [
                    {
                        "RuleArn": orphan_arn,
                        "Priority": "200",
                        "Conditions": [
                            {
                                "Field": "http-header",
                                "HttpHeaderConfig": {
                                    "HttpHeaderName": (RoutingHeaders.ROUTING_ID),
                                    "Values": ["rid-orphan"],
                                },
                            },
                            {
                                "Field": "http-header",
                                "HttpHeaderConfig": {
                                    "HttpHeaderName": (RoutingHeaders.USER_TIER),
                                    "Values": ["premium"],
                                },
                            },
                        ],
                        "Actions": [
                            {
                                "Type": "forward",
                                "TargetGroupArn": ("arn:aws:tg/orph"),
                            }
                        ],
                    }
                ]
            }

            mock_connection = setup_db_mock(
                fetchall_values=[[]],
            )
            mock_pymysql.return_value = mock_connection

            from premium_cleanup import cleanup_orphaned_alb_resources

            result = cleanup_orphaned_alb_resources()

            assert result["orphaned_rules_deleted"] == 1
            mock_elbv2.delete_rule.assert_called_once_with(RuleArn=orphan_arn)

    def test_keeps_grace_period_assignment(self, mock_env_vars_premium):
        """Regression (#766): a soft-released row in its grace window
        (status == PENDING_RELEASE == 'terminating') still owns a live ALB
        rule and must NOT be reaped as orphaned.

        Behavioral: the DB mock applies the keep-set predicate to the params the
        code passes, so the ACTIVE-only pre-fix query would drop this row and
        reap the rule — the assertions below are genuinely red before the fix.
        """
        grace_rule_arn = "arn:aws:rule/user-12-grace"

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch("premium_cleanup.pymysql.connect") as mock_pymysql:
            mock_elbv2 = MagicMock()

            def boto3_client_side_effect(service):
                if service == "elbv2":
                    return mock_elbv2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            mock_elbv2.describe_rules.return_value = {
                "Rules": [
                    _premium_alb_rule(grace_rule_arn, "arn:aws:tg/premium-12", "rid-12")
                ]
            }

            mock_connection = setup_keepset_filtering_db_mock(
                [
                    {
                        "alb_rule_arn": grace_rule_arn,
                        "target_group_arn": "arn:aws:tg/premium-12",
                        "user_id": 12,
                        # Grace row: status aliases PENDING_RELEASE, still recent.
                        "status": PremiumAssignment.TERMINATING,
                        "is_recent": True,
                    }
                ]
            )
            mock_pymysql.return_value = mock_connection

            from premium_cleanup import cleanup_orphaned_alb_resources

            result = cleanup_orphaned_alb_resources()

            # Live rule survives the sweep (kept via the TERMINATING status).
            assert result["orphaned_rules_deleted"] == 0
            assert not mock_elbv2.delete_rule.called
            # The whole body is wrapped in ``except Exception`` returning
            # ``orphaned_rules_deleted: 0``, so both assertions above also hold
            # when the sweep dies on its first line.
            assert "error" not in result, result

    def test_keeps_recently_active_row_outside_keepset(self, mock_env_vars_premium):
        """Recency guard (#766): a row whose status is outside the keep-set but
        that was touched within the grace window must be kept, exercising the
        ``OR last_activity >= NOW() - grace`` branch on its own.

        Behavioral: with only the status broadening (no recency bound) the
        pre-fix query would drop this row and reap the rule.
        """
        recent_rule_arn = "arn:aws:rule/user-7-recent"

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch("premium_cleanup.pymysql.connect") as mock_pymysql:
            mock_elbv2 = MagicMock()

            def boto3_client_side_effect(service):
                if service == "elbv2":
                    return mock_elbv2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            mock_elbv2.describe_rules.return_value = {
                "Rules": [
                    _premium_alb_rule(recent_rule_arn, "arn:aws:tg/premium-7", "rid-7")
                ]
            }

            mock_connection = setup_keepset_filtering_db_mock(
                [
                    {
                        "alb_rule_arn": recent_rule_arn,
                        "target_group_arn": "arn:aws:tg/premium-7",
                        "user_id": 7,
                        # Status the keep-set does not enumerate: kept only by
                        # the recency guard (mid-transition row).
                        "status": "mid-transition",
                        "is_recent": True,
                    }
                ]
            )
            mock_pymysql.return_value = mock_connection

            from premium_cleanup import cleanup_orphaned_alb_resources

            result = cleanup_orphaned_alb_resources()

            # Rule survives purely because the row is recent.
            assert result["orphaned_rules_deleted"] == 0
            assert not mock_elbv2.delete_rule.called
            # The whole body is wrapped in ``except Exception`` returning
            # ``orphaned_rules_deleted: 0``, so both assertions above also hold
            # when the sweep dies on its first line.
            assert "error" not in result, result

    def test_skips_default_rule(self, mock_env_vars_premium):
        """Default ALB rule is never deleted."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch("premium_cleanup.pymysql.connect") as mock_pymysql:
            mock_elbv2 = MagicMock()

            def boto3_client_side_effect(service):
                if service == "elbv2":
                    return mock_elbv2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            mock_elbv2.describe_rules.return_value = {
                "Rules": [
                    {
                        "RuleArn": "arn:aws:rule/default",
                        "Priority": "default",
                        "Conditions": [],
                        "Actions": [
                            {
                                "Type": "forward",
                                "TargetGroupArn": ("arn:aws:tg/dflt"),
                            }
                        ],
                    }
                ]
            }

            mock_connection = setup_db_mock(
                fetchall_values=[[]],
            )
            mock_pymysql.return_value = mock_connection

            from premium_cleanup import cleanup_orphaned_alb_resources

            result = cleanup_orphaned_alb_resources()

            assert result["orphaned_rules_deleted"] == 0
            assert not mock_elbv2.delete_rule.called
            # The whole body is wrapped in ``except Exception`` returning
            # ``orphaned_rules_deleted: 0``, so both assertions above also hold
            # when the sweep dies on its first line.
            assert "error" not in result, result


class TestReconcileInstanceStates:
    """reconcile_instance_states tests."""

    def test_cleans_terminated_instance(self, mock_env_vars_premium):
        """Terminated instance assignment cleaned."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_cleanup" ".get_all_premium_instances_with_states"
        ) as mock_aws, patch("boto3.client") as mock_boto3, patch(
            "pymysql.connect"
        ) as mock_pymysql:
            mock_aws.return_value = []

            mock_elbv2 = MagicMock()

            def boto3_client_side_effect(service):
                if service == "elbv2":
                    return mock_elbv2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            mock_connection = setup_db_mock(
                fetchall_values=[
                    [
                        MockRow(
                            {
                                "id": 1,
                                "user_id": 100,
                                "instance_id": "i-gone",
                                "instance_state": "running",
                                "status": "active",
                                "target_group_arn": "standby",
                                "alb_rule_arn": "standby",
                            }
                        )
                    ],
                ],
            )
            mock_pymysql.return_value = mock_connection

            cursor = mock_connection.cursor.return_value.__enter__.return_value

            from premium_cleanup import reconcile_instance_states

            result = reconcile_instance_states()
            assert result["cleanup_count"] == 1
            # The counter alone was satisfied without the DELETE ever running.
            deletes = [
                (" ".join(c[0][0].split()), c[0][1])
                for c in cursor.execute.call_args_list
                if c[0][0].strip().startswith("DELETE")
            ]
            assert deletes == [
                ("DELETE FROM premium_user_assignments WHERE id = %s", (1,))
            ]

    def test_updates_state_mismatch(self, mock_env_vars_premium):
        """DB state updated when AWS state differs."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_cleanup" ".get_all_premium_instances_with_states"
        ) as mock_aws, patch("boto3.client") as mock_boto3, patch(
            "pymysql.connect"
        ) as mock_pymysql:
            mock_aws.return_value = [
                {
                    "instance_id": "i-1",
                    "instance_type": "t3.large",
                    "state": "stopped",
                    "launch_time": None,
                }
            ]

            mock_elbv2 = MagicMock()

            def boto3_client_side_effect(service):
                if service == "elbv2":
                    return mock_elbv2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            mock_connection = setup_db_mock(
                fetchall_values=[
                    [
                        MockRow(
                            {
                                "id": 2,
                                "user_id": 200,
                                "instance_id": "i-1",
                                "instance_state": "running",
                                "status": "active",
                                "target_group_arn": ("arn:aws:tg/u200"),
                                "alb_rule_arn": ("arn:aws:rule/u200"),
                            }
                        )
                    ],
                ],
            )
            mock_pymysql.return_value = mock_connection

            cursor = mock_connection.cursor.return_value.__enter__.return_value

            from premium_cleanup import reconcile_instance_states

            result = reconcile_instance_states()
            assert result["update_count"] == 1
            updates = [
                (" ".join(c[0][0].split()), c[0][1])
                for c in cursor.execute.call_args_list
                if c[0][0].strip().startswith("UPDATE")
            ]
            assert updates == [
                (
                    "UPDATE premium_user_assignments SET instance_state = %s, "
                    "last_state_check = NOW() WHERE id = %s",
                    ("stopped", 2),
                )
            ]

    def test_skips_autoscaling_pool(self, mock_env_vars_premium):
        """autoscaling-pool rows are skipped."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "premium_cleanup" ".get_all_premium_instances_with_states"
        ) as mock_aws, patch("boto3.client") as mock_boto3, patch(
            "pymysql.connect"
        ) as mock_pymysql:
            mock_aws.return_value = []

            mock_elbv2 = MagicMock()

            def boto3_client_side_effect(service):
                if service == "elbv2":
                    return mock_elbv2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            mock_connection = setup_db_mock(
                fetchall_values=[
                    [
                        MockRow(
                            {
                                "id": 3,
                                "user_id": 300,
                                "instance_id": (PremiumAssignment.AUTOSCALING_POOL),
                                "instance_state": "running",
                                "status": "active",
                                "target_group_arn": ("arn:aws:tg/asg"),
                                "alb_rule_arn": ("arn:aws:rule/asg"),
                            }
                        )
                    ],
                ],
            )
            mock_pymysql.return_value = mock_connection

            from premium_cleanup import reconcile_instance_states

            result = reconcile_instance_states()
            assert result["cleanup_count"] == 0
            assert result["update_count"] == 0


class TestReconcileSingleInstance:
    """Tests for EventBridge-triggered single-instance reconcile."""

    def test_cleans_up_premium_instance(self, mock_env_vars_premium):
        """Terminated premium instance with active assignments
        gets ALB and DB cleaned up."""
        instance_id = "i-terminated123"
        tg_arn = "arn:aws:elasticloadbalancing:tg/premium-user"
        rule_arn = "arn:aws:elasticloadbalancing:rule/premium-user"

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch("pymysql.connect") as mock_pymysql:
            mock_ec2 = MagicMock()
            mock_elbv2 = MagicMock()

            def boto3_client_side_effect(service):
                if service == "ec2":
                    return mock_ec2
                if service == "elbv2":
                    return mock_elbv2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": instance_id,
                                "State": {"Name": "terminated"},
                                "Tags": [
                                    {
                                        "Key": "Tier",
                                        "Value": "premium",
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }

            mock_connection = setup_db_mock(
                fetchall_values=[
                    [
                        MockRow(
                            {
                                "id": 1,
                                "user_id": 100,
                                "instance_id": instance_id,
                                "target_group_arn": tg_arn,
                                "alb_rule_arn": rule_arn,
                            }
                        )
                    ],
                ],
            )
            mock_pymysql.return_value = mock_connection

            from premium_cleanup import reconcile_single_instance

            result = reconcile_single_instance(instance_id)
            assert result["cleanup_count"] == 1
            assert result["instance_id"] == instance_id
            mock_elbv2.delete_rule.assert_called_once_with(RuleArn=rule_arn)
            mock_elbv2.delete_target_group.assert_called_once_with(
                TargetGroupArn=tg_arn
            )

    def test_skips_non_premium_instance(self, mock_env_vars_premium):
        """Non-premium instance returns skipped."""
        instance_id = "i-notpremium456"

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ec2 = MagicMock()
            mock_boto3.side_effect = lambda service: (
                mock_ec2 if service == "ec2" else MagicMock()
            )

            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": instance_id,
                                "State": {"Name": "terminated"},
                                "Tags": [
                                    {
                                        "Key": "Name",
                                        "Value": "web-server-1",
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }

            from premium_cleanup import reconcile_single_instance

            result = reconcile_single_instance(instance_id)
            assert result["skipped"] is True
            assert result["reason"] == "not_premium_instance"

    def test_idempotent_no_assignments(self, mock_env_vars_premium):
        """Instance with no DB assignments returns cleanup_count 0."""
        instance_id = "i-alreadyclean789"

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch("pymysql.connect") as mock_pymysql:
            mock_ec2 = MagicMock()

            def boto3_client_side_effect(service):
                if service == "ec2":
                    return mock_ec2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": instance_id,
                                "State": {"Name": "terminated"},
                                "Tags": [
                                    {
                                        "Key": "Tier",
                                        "Value": "premium",
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }

            mock_connection = setup_db_mock(
                fetchall_values=[[]],
            )
            mock_pymysql.return_value = mock_connection

            from premium_cleanup import reconcile_single_instance

            result = reconcile_single_instance(instance_id)
            assert result["cleanup_count"] == 0
            assert result["instance_id"] == instance_id

    def test_handler_missing_instance_id(self, mock_env_vars_premium):
        """Handler returns 400 when instance_id is missing."""
        event = {
            "action": "reconcile_instance",
            "source": "ec2_state_change",
        }
        mock_context = MagicMock()
        mock_context.function_name = "test-premium-cleanup"

        with patch.dict("os.environ", mock_env_vars_premium):
            from premium_cleanup import handler

            result = handler(event, mock_context)
            assert result["statusCode"] == 400

    def test_describe_instances_failure_falls_through_to_db(
        self, mock_env_vars_premium
    ):
        """When describe_instances fails (instance gone), still checks
        DB and cleans up if assignment exists."""
        instance_id = "i-fullygone999"
        tg_arn = "arn:aws:elasticloadbalancing:tg/premium-gone"
        rule_arn = "arn:aws:elasticloadbalancing:rule/premium-gone"

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch("pymysql.connect") as mock_pymysql:
            mock_ec2 = MagicMock()
            mock_elbv2 = MagicMock()

            def boto3_client_side_effect(service):
                if service == "ec2":
                    return mock_ec2
                if service == "elbv2":
                    return mock_elbv2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            # Simulate instance fully gone from AWS API
            mock_ec2.describe_instances.side_effect = Exception(
                "InvalidInstanceID.NotFound"
            )

            mock_connection = setup_db_mock(
                fetchall_values=[
                    [
                        MockRow(
                            {
                                "id": 5,
                                "user_id": 200,
                                "instance_id": instance_id,
                                "target_group_arn": tg_arn,
                                "alb_rule_arn": rule_arn,
                            }
                        )
                    ],
                ],
            )
            mock_pymysql.return_value = mock_connection

            from premium_cleanup import reconcile_single_instance

            result = reconcile_single_instance(instance_id)
            assert result["cleanup_count"] == 1
            assert result["instance_id"] == instance_id
            mock_elbv2.delete_rule.assert_called_once_with(RuleArn=rule_arn)
            mock_elbv2.delete_target_group.assert_called_once_with(
                TargetGroupArn=tg_arn
            )

    def test_skips_autoscaling_pool_marker(self, mock_env_vars_premium):
        """Autoscaling-pool virtual marker is skipped."""
        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3:
            mock_ec2 = MagicMock()
            mock_boto3.side_effect = lambda service: (
                mock_ec2 if service == "ec2" else MagicMock()
            )

            # describe_instances will fail for non-real instance ID
            mock_ec2.describe_instances.side_effect = Exception(
                "InvalidInstanceID.Malformed"
            )

            from premium_cleanup import reconcile_single_instance

            result = reconcile_single_instance(PremiumAssignment.AUTOSCALING_POOL)
            assert result["skipped"] is True
            assert result["reason"] == "autoscaling_pool_marker"

    def test_skips_standby_alb_resources(self, mock_env_vars_premium):
        """Standby ALB markers are not deleted."""
        instance_id = "i-standbytest123"

        with patch.dict("os.environ", mock_env_vars_premium), patch(
            "boto3.client"
        ) as mock_boto3, patch("pymysql.connect") as mock_pymysql:
            mock_ec2 = MagicMock()
            mock_elbv2 = MagicMock()

            def boto3_client_side_effect(service):
                if service == "ec2":
                    return mock_ec2
                if service == "elbv2":
                    return mock_elbv2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": instance_id,
                                "State": {"Name": "terminated"},
                                "Tags": [
                                    {
                                        "Key": "Tier",
                                        "Value": "premium",
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }

            mock_connection = setup_db_mock(
                fetchall_values=[
                    [
                        MockRow(
                            {
                                "id": 10,
                                "user_id": 300,
                                "instance_id": instance_id,
                                "target_group_arn": "standby",
                                "alb_rule_arn": "standby",
                            }
                        )
                    ],
                ],
            )
            mock_pymysql.return_value = mock_connection

            from premium_cleanup import reconcile_single_instance

            result = reconcile_single_instance(instance_id)
            assert result["cleanup_count"] == 1
            # Standby markers should NOT trigger ALB API calls
            mock_elbv2.delete_rule.assert_not_called()
            mock_elbv2.delete_target_group.assert_not_called()

    def test_skips_shared_autoscaling_target_group(self, mock_env_vars_premium):
        """Shared autoscaling target group is not deleted."""
        instance_id = "i-sharedtg123"
        shared_tg_arn = "arn:aws:elasticloadbalancing:tg/autoscaling-shared"
        rule_arn = "arn:aws:elasticloadbalancing:rule/user-rule"

        env_with_tg = {
            **mock_env_vars_premium,
            "AUTOSCALING_TARGET_GROUP_ARN": shared_tg_arn,
        }

        with patch.dict("os.environ", env_with_tg), patch(
            "boto3.client"
        ) as mock_boto3, patch("pymysql.connect") as mock_pymysql:
            mock_ec2 = MagicMock()
            mock_elbv2 = MagicMock()

            def boto3_client_side_effect(service):
                if service == "ec2":
                    return mock_ec2
                if service == "elbv2":
                    return mock_elbv2
                return MagicMock()

            mock_boto3.side_effect = boto3_client_side_effect

            mock_ec2.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": instance_id,
                                "State": {"Name": "terminated"},
                                "Tags": [
                                    {
                                        "Key": "Tier",
                                        "Value": "premium",
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }

            mock_connection = setup_db_mock(
                fetchall_values=[
                    [
                        MockRow(
                            {
                                "id": 11,
                                "user_id": 400,
                                "instance_id": instance_id,
                                "target_group_arn": shared_tg_arn,
                                "alb_rule_arn": rule_arn,
                            }
                        )
                    ],
                ],
            )
            mock_pymysql.return_value = mock_connection

            from premium_cleanup import reconcile_single_instance

            result = reconcile_single_instance(instance_id)
            assert result["cleanup_count"] == 1
            # ALB rule should be deleted, but shared TG should NOT
            mock_elbv2.delete_rule.assert_called_once_with(RuleArn=rule_arn)
            mock_elbv2.delete_target_group.assert_not_called()


class TestGetAllPremiumInstancesEnvFilter:
    """Environment prefix filter tests for get_all_premium_instances_with_states."""

    def test_same_env_instance_passes(self, mock_env_vars_premium):
        """Instance with matching env prefix is included."""
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
                                "InstanceId": "i-same-env",
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
                        ]
                    }
                ]
            }

            from premium_cleanup import get_all_premium_instances_with_states

            result = get_all_premium_instances_with_states()
            assert len(result) == 1
            assert result[0]["instance_id"] == "i-same-env"

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

            from premium_cleanup import get_all_premium_instances_with_states

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

            from premium_cleanup import get_all_premium_instances_with_states

            result = get_all_premium_instances_with_states()
            assert len(result) == 0


class TestGetEcsContainerInstanceArnPrefersLive:
    """_get_ecs_container_instance_arn must resolve the live CI when a fresh
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

            from premium_cleanup import _get_ecs_container_instance_arn

            result = _get_ecs_container_instance_arn("i-shared", "test-cluster")
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

            from premium_cleanup import _get_ecs_container_instance_arn

            result = _get_ecs_container_instance_arn("i-solo", "test-cluster")
            assert result == arn
