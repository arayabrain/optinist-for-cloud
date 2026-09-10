"""
Premium Cleanup Lambda - Data & Resource Hygiene

Responsibilities:
- Remove stale assignments from database (>2 hours inactive)
- Clean up orphaned ALB resources (rules/target groups with no DB entry)
- Reconcile instance states (ensure DB matches AWS reality)
- Monitor standby pool health (read-only)

Does NOT:
- Make scaling decisions (premium_manager handles that)
- Stop or start instances (premium_manager handles that)
- Update ECS service count (premium_manager handles that)

Triggered by:
- CloudWatch Events hourly (full 5-step cleanup)
- EventBridge EC2 state-change events (single-instance reconcile on termination)
Coordinates with premium_manager which handles all compute/capacity decisions.
"""

import json
import os
import time
from typing import TYPE_CHECKING, Any, Dict, List

import boto3
import pymysql

# Shared constants from Lambda Layer (mounted at /opt/python by AWS Lambda)
from aws_constants import (
    DatabaseConfig,
    ECSTaskStatus,
    EnvironmentConfig,
    InstanceState,
    PremiumAssignment,
    PremiumInstanceConfig,
    RoutingHeaders,
)

# Constants
# Default hours before stale premium assignments are cleaned up
# Can be overridden by PREMIUM_IDLE_TIMEOUT_HOURS environment variable
DEFAULT_STALE_ASSIGNMENT_TIMEOUT_HOURS = 2

if TYPE_CHECKING:
    from mypy_boto3_cloudwatch import CloudWatchClient
    from mypy_boto3_ec2 import EC2Client
    from mypy_boto3_ecs import ECSClient
    from mypy_boto3_elbv2 import ElasticLoadBalancingv2Client


_cloudwatch_client: "CloudWatchClient | None" = None


def _get_cloudwatch_client() -> "CloudWatchClient":
    """Reuse one CloudWatch client across calls within a warm Lambda container."""
    global _cloudwatch_client
    if _cloudwatch_client is None:
        _cloudwatch_client = boto3.client("cloudwatch")
    return _cloudwatch_client


# Mirrored in premium_manager.py — keep both in sync if the alarm naming changes.
def _premium_tg_alarm_name(tg_arn: str) -> str | None:
    """Derive the per-TG alarm name from a TG ARN.

    Must match the name used at alarm creation so deletion targets it.
    """
    idx = tg_arn.find(":targetgroup/")
    suffix = tg_arn[idx + 1 :] if idx != -1 else tg_arn
    parts = suffix.split("/")
    if len(parts) < 2:
        return None
    return f"{EnvironmentConfig.get_env_prefix()}-{parts[1]}-unhealthy-hosts"


def _delete_premium_tg_unhealthy_alarm(tg_arn: str) -> None:
    """Best-effort delete of a per-TG UnHealthyHostCount alarm.

    Idempotent: delete_alarms ignores names that do not exist, so this is safe
    for any TG ARN (the shared autoscaling TG simply has no matching alarm).
    """
    alarm_name = _premium_tg_alarm_name(tg_arn)
    if not alarm_name:
        return
    try:
        cloudwatch = _get_cloudwatch_client()
        cloudwatch.delete_alarms(AlarmNames=[alarm_name])
        print(f"[premium-alarm] action=delete name={alarm_name} tg={tg_arn}")
    except Exception as e:
        print(f"Warning: Failed to delete unhealthy-host alarm {alarm_name}: {e}")


def _cleanup_assignment_alb_resources(
    elbv2: "ElasticLoadBalancingv2Client",
    user_id: Any,
    alb_rule_arn: str | None,
    target_group_arn: str | None,
) -> None:
    """Delete ALB rule and target group for an assignment.

    Skips standby markers and the shared autoscaling target group.
    Errors are logged but not raised — callers proceed with DB cleanup.
    """
    # Delete ALB rule (skip standby markers)
    if alb_rule_arn and alb_rule_arn.lower() != PremiumAssignment.STANDBY:
        try:
            elbv2.delete_rule(RuleArn=alb_rule_arn)
            print(f"Deleted ALB rule for user {user_id}: {alb_rule_arn}")
        except Exception as e:
            print(f"Warning: Failed to delete ALB rule {alb_rule_arn}: {e}")

    # Delete target group (skip standby markers and shared autoscaling TG)
    autoscaling_tg_arn = os.environ.get("AUTOSCALING_TARGET_GROUP_ARN")
    if (
        target_group_arn
        and target_group_arn.lower() != PremiumAssignment.STANDBY
        and target_group_arn != autoscaling_tg_arn
    ):
        try:
            _delete_premium_tg_unhealthy_alarm(target_group_arn)
            elbv2.delete_target_group(TargetGroupArn=target_group_arn)
            print(f"Deleted target group for user {user_id}: {target_group_arn}")
        except Exception as e:
            print(f"Warning: Failed to delete target group {target_group_arn}: {e}")
    elif target_group_arn == autoscaling_tg_arn:
        print(
            f"Skipping deletion of shared autoscaling "
            f"target group: {target_group_arn}"
        )


def get_required_env_var(var_name: str, default_value: str | None = None) -> str:
    """Safely get required environment variable with helpful error message"""
    value = os.environ.get(var_name, default_value)
    if value is None or value == "":
        raise ValueError(
            f"Missing required environment variable: {var_name}. "
            "Check your Terraform configuration and Lambda environment settings."
        )
    return value


def get_db_connection(auto_commit=False):
    """
    Create database connection with proper transaction management and auto-close.

    This function returns a context manager that ensures connections are properly
    closed when exiting the context, preventing connection leaks.

    Usage:
        with get_db_connection() as conn:
            # Use connection
            # Connection will be automatically closed on exit
    """
    from contextlib import contextmanager

    @contextmanager
    def connection_context():
        conn = None
        try:
            rds_host = get_required_env_var("RDS_HOST")
            conn = pymysql.connect(
                host=rds_host.split(":")[0],
                port=int(rds_host.split(":")[1])
                if ":" in rds_host
                else DatabaseConfig.DEFAULT_PORT,
                user=get_required_env_var("RDS_USER"),
                password=get_required_env_var("RDS_PASSWORD"),
                database=get_required_env_var("RDS_DATABASE"),
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=auto_commit,
                ssl={"check_hostname": False},
            )
            yield conn
        except Exception as e:
            print(f" Database connection failed: {str(e)}")
            raise
        finally:
            # CRITICAL: Always close the connection to prevent leaks
            if conn is not None:
                try:
                    conn.close()
                    print(" Database connection closed")
                except Exception as e:
                    print(f" Warning: Error closing database connection: {str(e)}")

    return connection_context()


def with_transaction(func):
    """Decorator to wrap database operations in transactions"""

    def wrapper(*args, **kwargs):
        with get_db_connection() as conn:
            try:
                result = func(conn, *args, **kwargs)
                conn.commit()
                return result
            except Exception as e:
                conn.rollback()
                print(f"Transaction rolled back due to error: {e}")
                raise e

    return wrapper


def get_assigned_users_for_instance(instance_id: str) -> List[str]:
    """Get list of users assigned to an instance"""
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT user_id FROM premium_user_assignments
                       WHERE instance_id = %s AND status = %s""",
                    (instance_id, PremiumAssignment.ACTIVE),
                )
                users = cursor.fetchall()
                return [user["user_id"] for user in users]
    except Exception as e:
        print(f"Error getting assigned users: {str(e)}")
        return []


def _get_ecs_container_instance_arn(
    ec2_instance_id: str, cluster_name: str
) -> str | None:
    """Map EC2 instance ID to ECS container instance ARN."""
    ecs: "ECSClient" = boto3.client("ecs")
    try:
        response = ecs.list_container_instances(
            cluster=cluster_name,
            filter="attribute:tier == premium",
        )
        arns = response.get("containerInstanceArns", [])
        if not arns:
            return None

        desc = ecs.describe_container_instances(
            cluster=cluster_name, containerInstances=arns
        )
        matches = [
            ci
            for ci in desc.get("containerInstances", [])
            if ci.get("ec2InstanceId") == ec2_instance_id
        ]
        # Prefer the live CI; a fresh CI can overlay a disconnected ghost on one EC2.
        chosen = next(
            (
                ci
                for ci in matches
                if ci.get("agentConnected") and ci.get("status") == "ACTIVE"
            ),
            matches[0] if matches else None,
        )
        return chosen.get("containerInstanceArn") if chosen else None
    except Exception as e:
        print(f"Error mapping EC2 to ECS container instance: " f"{str(e)}")
        return None


def check_instance_readiness(instance_id: str) -> bool:
    """Check if an instance has a running ECS task and is ready for user assignment"""
    ecs: "ECSClient" = boto3.client("ecs")
    cluster_name = get_required_env_var("CLUSTER_NAME")

    try:
        # Map EC2 instance ID to ECS container instance ARN
        container_arn = _get_ecs_container_instance_arn(instance_id, cluster_name)
        if not container_arn:
            print(f"No ECS container instance found for " f"EC2 instance {instance_id}")
            return False

        tasks_response = ecs.list_tasks(
            cluster=cluster_name,
            containerInstance=container_arn,
        )

        if not tasks_response["taskArns"]:
            print(f"No tasks running on instance {instance_id}")
            return False

        # Check task status
        task_details = ecs.describe_tasks(
            cluster=cluster_name, tasks=tasks_response["taskArns"]
        )

        for task in task_details["tasks"]:
            task_def_arn = task.get("taskDefinitionArn", "")
            is_premium_task = (
                task_def_arn.find(PremiumInstanceConfig.INSTANCE_IDENTIFIER) != -1
            )
            if is_premium_task and task.get("lastStatus") == ECSTaskStatus.RUNNING:
                print(f"Premium task running and ready on instance {instance_id}")
                return True

        return False

    except Exception as e:
        print(f"Error checking instance readiness: {str(e)}")
        return False


@with_transaction
def cleanup_stale_assignments(connection) -> Dict[str, Any]:
    """
    Clean up stale premium user assignments based on last activity.
    Uses transactions to prevent race conditions.
    """
    try:
        stale_threshold_hours = int(
            get_required_env_var(
                "PREMIUM_IDLE_TIMEOUT_HOURS",
                str(DEFAULT_STALE_ASSIGNMENT_TIMEOUT_HOURS),
            )
        )

        print(f"Starting cleanup of assignments idle for >{stale_threshold_hours}h")

        with connection.cursor() as cursor:
            # Use SELECT FOR UPDATE to prevent race conditions
            cursor.execute(
                """
                SELECT user_id, instance_id, target_group_arn,
                alb_rule_arn, last_activity
                FROM premium_user_assignments
                WHERE status = %s
                AND is_standby = 0
                AND last_activity < DATE_SUB(NOW(), INTERVAL %s HOUR)
                FOR UPDATE
            """,
                (PremiumAssignment.ACTIVE, stale_threshold_hours),
            )

            stale_assignments = cursor.fetchall()

            if not stale_assignments:
                print("No stale assignments found")
                return {
                    "cleaned_assignments": 0,
                    "message": "No stale assignments to clean",
                }

            print(f"Found {len(stale_assignments)} stale assignments to clean")

            # Clean up AWS resources for each stale assignment
            elbv2: "ElasticLoadBalancingv2Client" = boto3.client("elbv2")
            cleaned_count = 0

            for assignment in stale_assignments:
                user_id = assignment["user_id"]
                target_group_arn = assignment["target_group_arn"]
                alb_rule_arn = assignment["alb_rule_arn"]

                try:
                    print(f"Cleaning stale assignment for user {user_id}")
                    _cleanup_assignment_alb_resources(
                        elbv2, user_id, alb_rule_arn, target_group_arn
                    )

                    # Close usage log before deleting assignment
                    cursor.execute(
                        """UPDATE instance_usage_log SET ended_at = NOW()
                           WHERE user_id = %s AND tier = 'premium'
                           AND ended_at IS NULL""",
                        (user_id,),
                    )

                    # Remove from database
                    cursor.execute(
                        "DELETE FROM premium_user_assignments WHERE user_id = %s",
                        (user_id,),
                    )

                    cleaned_count += 1
                    print(f"Cleaned assignment for user {user_id}")

                except Exception as e:
                    print(f"Error cleaning assignment for user {user_id}: {e}")
                    # Continue with other assignments

            print(
                f"Cleanup complete: {cleaned_count}/{len(stale_assignments)} "
                f"assignments cleaned"
            )

            return {
                "cleaned_assignments": cleaned_count,
                "total_stale": len(stale_assignments),
                "message": f"Cleaned {cleaned_count} stale assignments",
            }

    except Exception as e:
        print(f"Error during stale assignment cleanup: {str(e)}")
        raise e


def cleanup_orphaned_alb_resources() -> Dict[str, Any]:
    """
    Clean up orphaned ALB listener rules and target groups that have no database entry.

    This handles cases where Lambda failed after creating ALB resources but before
    storing the assignment in the database, leaving orphaned resources.
    """
    try:
        print("Scanning for orphaned ALB resources...")

        elbv2: "ElasticLoadBalancingv2Client" = boto3.client("elbv2")
        alb_listener_arn = get_required_env_var("ALB_LISTENER_ARN")

        # Get all ALB listener rules
        rules_response = elbv2.describe_rules(ListenerArn=alb_listener_arn)
        alb_rules = rules_response.get("Rules", [])

        # Filter for premium user rules (exclude default rule)
        premium_rules = []
        for rule in alb_rules:
            if rule.get("Priority") == "default":
                continue

            # Check if rule has premium user conditions
            # (X-Routing-ID and X-User-Tier headers)
            conditions = rule.get("Conditions", [])
            has_routing_id = any(
                c.get("Field") == "http-header"
                and c.get("HttpHeaderConfig", {}).get("HttpHeaderName")
                == RoutingHeaders.ROUTING_ID
                for c in conditions
            )
            has_user_tier = any(
                c.get("Field") == "http-header"
                and c.get("HttpHeaderConfig", {}).get("HttpHeaderName")
                == RoutingHeaders.USER_TIER
                for c in conditions
            )

            if has_routing_id and has_user_tier:
                premium_rules.append(rule)

        print(f"Found {len(premium_rules)} premium user ALB rules")

        # Keep-set: ACTIVE/MIGRATING/TERMINATING (statuses that can own a live
        # rule) plus any row touched within the grace window. TERMINATING
        # aliases PENDING_RELEASE, so an ACTIVE-only filter would reap a rule
        # mid soft-release grace; the recency guard covers rows mid-transition.
        grace_seconds = PremiumAssignment.PENDING_RELEASE_GRACE_SECONDS
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT alb_rule_arn, target_group_arn, user_id
                       FROM premium_user_assignments
                       WHERE is_standby = 0
                         AND (
                             status IN (%s, %s, %s)
                             OR last_activity >= DATE_SUB(
                                 NOW(), INTERVAL %s SECOND
                             )
                         )""",
                    (
                        PremiumAssignment.ACTIVE,
                        PremiumAssignment.MIGRATING,
                        PremiumAssignment.TERMINATING,
                        grace_seconds,
                    ),
                )
                db_assignments = cursor.fetchall()

        # Filter to real ARNs: marker rows (RESERVING/AUTOSCALING_POOL) carry
        # placeholder strings, not rule ARNs, so they must not enter the keep-set.
        db_rule_arns = {
            a["alb_rule_arn"]
            for a in db_assignments
            if a["alb_rule_arn"] and a["alb_rule_arn"].startswith("arn:")
        }
        print(f"Found {len(db_rule_arns)} keep-set assignments in database")

        # Find orphaned rules (in ALB but not in database)
        orphaned_rules = [
            rule for rule in premium_rules if rule["RuleArn"] not in db_rule_arns
        ]

        if not orphaned_rules:
            print("No orphaned ALB resources found")
            return {
                "orphaned_rules_deleted": 0,
                "orphaned_target_groups_deleted": 0,
                "message": "No orphaned resources to clean",
            }

        print(f"Found {len(orphaned_rules)} orphaned ALB rules to clean up")

        # Clean up orphaned resources
        rules_deleted = 0
        target_groups_deleted = 0

        for rule in orphaned_rules:
            rule_arn = rule["RuleArn"]
            priority = rule.get("Priority")

            try:
                # Get target group ARN from rule actions
                target_group_arn = None
                for action in rule.get("Actions", []):
                    if action.get("Type") == "forward":
                        target_group_arn = action.get("TargetGroupArn")
                        break

                print(f"Deleting orphaned rule (priority {priority}): {rule_arn}")

                # Delete the ALB rule
                elbv2.delete_rule(RuleArn=rule_arn)
                rules_deleted += 1
                print("Deleted ALB rule")

                # Delete the target group if it exists (skip shared autoscaling TG)
                autoscaling_tg_arn = os.environ.get("AUTOSCALING_TARGET_GROUP_ARN")
                if target_group_arn and target_group_arn != autoscaling_tg_arn:
                    try:
                        _delete_premium_tg_unhealthy_alarm(target_group_arn)
                        elbv2.delete_target_group(TargetGroupArn=target_group_arn)
                        target_groups_deleted += 1
                        print(f"Deleted target group: {target_group_arn}")
                    except Exception as tg_error:
                        print(f"Warning: Failed to delete target group: {tg_error}")
                elif target_group_arn == autoscaling_tg_arn:
                    print(
                        f"Skipping deletion of shared autoscaling "
                        f"target group: {target_group_arn}"
                    )

            except Exception as e:
                print(f"Error deleting orphaned rule {rule_arn}: {e}")
                # Continue with other rules

        print(
            f"Orphaned resource cleanup complete: {rules_deleted} rules, "
            f"{target_groups_deleted} target groups deleted"
        )

        return {
            "orphaned_rules_deleted": rules_deleted,
            "orphaned_target_groups_deleted": target_groups_deleted,
            "total_orphaned": len(orphaned_rules),
            "message": f"Cleaned {rules_deleted} orphaned ALB rules and "
            f"{target_groups_deleted} target groups",
        }

    except Exception as e:
        print(f"Error during orphaned resource cleanup: {str(e)}")
        return {
            "orphaned_rules_deleted": 0,
            "orphaned_target_groups_deleted": 0,
            "error": str(e),
        }


def cleanup_duplicate_alb_rules() -> Dict[str, Any]:
    """
    Clean up duplicate ALB rules that have the same routing_id.

    This handles cases where multiple rules were created for the same user
    due to race conditions or failed cleanup. Only keeps the rule that matches
    the database entry; deletes all others.
    """
    try:
        print("Scanning for duplicate ALB rules by routing_id...")

        elbv2: "ElasticLoadBalancingv2Client" = boto3.client("elbv2")
        alb_listener_arn = get_required_env_var("ALB_LISTENER_ARN")

        # Get all ALB listener rules
        rules_response = elbv2.describe_rules(ListenerArn=alb_listener_arn)
        alb_rules = rules_response.get("Rules", [])

        # Group rules by routing_id
        rules_by_routing_id: Dict[str, list] = {}
        for rule in alb_rules:
            if rule.get("Priority") == "default":
                continue

            # Extract routing_id from conditions
            conditions = rule.get("Conditions", [])
            routing_id = None
            for cond in conditions:
                if (
                    cond.get("Field") == "http-header"
                    and cond.get("HttpHeaderConfig", {}).get("HttpHeaderName")
                    == RoutingHeaders.ROUTING_ID
                ):
                    values = cond.get("HttpHeaderConfig", {}).get("Values", [])
                    if values:
                        routing_id = values[0]
                        break

            if routing_id:
                if routing_id not in rules_by_routing_id:
                    rules_by_routing_id[routing_id] = []
                rules_by_routing_id[routing_id].append(rule)

        # Get all active assignments from database to know which rules to keep
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT alb_rule_arn FROM premium_user_assignments
                       WHERE status IN (%s, %s, %s)
                       AND is_standby = 0
                       AND alb_rule_arn NOT IN (%s, %s)""",
                    (
                        PremiumAssignment.ACTIVE,
                        PremiumAssignment.MIGRATING,
                        PremiumAssignment.TERMINATING,
                        PremiumAssignment.STANDBY,
                        "STANDBY",  # Handle legacy uppercase values
                    ),
                )
                db_assignments = cursor.fetchall()

        db_rule_arns = {a["alb_rule_arn"] for a in db_assignments}

        # Find and delete duplicates
        duplicates_deleted = 0
        target_groups_deleted = 0

        for routing_id, rules in rules_by_routing_id.items():
            if len(rules) <= 1:
                continue  # No duplicates

            print(f"Found {len(rules)} rules for routing_id {routing_id[:8]}...")

            # Keep the rule that's in the database, delete others
            for rule in rules:
                rule_arn = rule["RuleArn"]
                if rule_arn in db_rule_arns:
                    print(f"Keeping rule {rule_arn} (in database)")
                    continue

                # Delete this duplicate rule
                try:
                    print(f"Deleting duplicate rule {rule_arn}")
                    elbv2.delete_rule(RuleArn=rule_arn)
                    duplicates_deleted += 1

                    # Try to delete associated target group
                    # Note: AWS returns ResourceInUse error if TG is still
                    # referenced by another rule. This is expected behavior
                    # and the exception is logged but not fatal.
                    for action in rule.get("Actions", []):
                        if action.get("Type") == "forward":
                            tg_arn = action.get("TargetGroupArn")
                            if tg_arn:
                                try:
                                    _delete_premium_tg_unhealthy_alarm(tg_arn)
                                    elbv2.delete_target_group(TargetGroupArn=tg_arn)
                                    target_groups_deleted += 1
                                    print(f"Deleted target group {tg_arn}")
                                except Exception as tg_error:
                                    # Target group might be in use by another rule
                                    print(
                                        f"Could not delete target group : "
                                        f"{tg_arn}: {tg_error}"
                                    )

                except Exception as e:
                    print(f"Failed to delete rule {rule_arn}: {e}")

        print(
            f"Duplicate cleanup complete: {duplicates_deleted} rules, "
            f"{target_groups_deleted} target groups deleted"
        )

        return {
            "duplicates_deleted": duplicates_deleted,
            "target_groups_deleted": target_groups_deleted,
            "routing_ids_with_duplicates": sum(
                1 for rules in rules_by_routing_id.values() if len(rules) > 1
            ),
        }

    except Exception as e:
        print(f"Error during duplicate rule cleanup: {str(e)}")
        return {
            "duplicates_deleted": 0,
            "target_groups_deleted": 0,
            "error": str(e),
        }


def get_all_premium_instances_with_states():
    """Get all premium instances with their AWS states.

    Filters by environment prefix (ENV_PREFIX) to prevent cross-environment
    contamination (e.g., development Lambda discovering production instances).
    """
    ec2: "EC2Client" = boto3.client("ec2")
    env_prefix = PremiumInstanceConfig.get_env_prefix()
    try:
        # Get instances with premium tags (use multiple filters for robust discovery)
        response = ec2.describe_instances(
            Filters=[
                {
                    "Name": "instance-state-name",
                    "Values": [
                        InstanceState.PENDING,
                        InstanceState.RUNNING,
                        InstanceState.STOPPING,
                        InstanceState.STOPPED,
                    ],
                },
                # Use OR logic: Name/Tier/Type tags contain premium identifier
            ]
        )

        # Apply tag filtering in Python for more flexible matching
        def is_premium_instance(instance):
            tags = {
                tag.get("Key"): tag.get("Value") for tag in instance.get("Tags", [])
            }
            instance_id = instance["InstanceId"]

            # Check multiple criteria for premium instances
            name_tag = tags.get("Name", "")
            name_match = PremiumInstanceConfig.INSTANCE_IDENTIFIER in name_tag.lower()
            tier_match = (
                tags.get("Tier", "").lower()
                == PremiumInstanceConfig.INSTANCE_IDENTIFIER
            )
            type_match = (
                PremiumInstanceConfig.INSTANCE_IDENTIFIER
                in tags.get("Type", "").lower()
            )

            is_premium = name_match or tier_match or type_match

            # Filter by environment prefix to prevent cross-environment
            # contamination. Instance Name tags follow the pattern
            # "<env_prefix>-premium-*" (see PremiumInstanceConfig in
            # aws_constants.py). Reject instances whose Name tag doesn't
            # start with this Lambda's ENV_PREFIX, or that have no Name tag
            # at all (tagless instances cannot be verified as belonging to
            # this environment).
            if is_premium:
                if not name_tag or not name_tag.lower().startswith(env_prefix.lower()):
                    print(
                        f"Skipping instance {instance_id}: "
                        f"Name '{name_tag}' does not match "
                        f"environment prefix '{env_prefix}'"
                    )
                    return False

            # Debug logging for tag matching
            print(f"Instance {instance_id} tag analysis:")
            print(f"- Name: '{name_tag}' -> name_match: {name_match}")
            print(f"- Tier: '{tags.get('Tier', '')}' -> tier_match: {tier_match}")
            print(f"- Type: '{tags.get('Type', '')}' -> type_match: {type_match}")
            print(f"- All tags: {tags}")

            print(f"- Final match result: {is_premium}")
            return is_premium

        instances = []
        all_instances_found = 0

        for reservation in response["Reservations"]:
            for instance in reservation["Instances"]:
                all_instances_found += 1
                instance_id = instance["InstanceId"]
                state = instance["State"]["Name"]

                print(f"Evaluating instance {instance_id} (state: {state})")

                # Only include premium instances
                if is_premium_instance(instance):
                    instance_data = {
                        "instance_id": instance["InstanceId"],
                        "instance_type": instance["InstanceType"],
                        "state": instance["State"]["Name"],
                        "launch_time": instance.get("LaunchTime"),
                    }
                    instances.append(instance_data)
                    print(f"Added premium instance: {instance_data}")
                else:
                    print(f" Skipped non-premium instance: {instance_id}")

        print("Instance discovery summary:")
        print(f"- Total instances found in AWS: {all_instances_found}")
        print(f"- Premium instances matched: {len(instances)}")
        print(f"- Premium instance IDs: {[i['instance_id'] for i in instances]}")
        print(f"- States: {[(i['instance_id'], i['state']) for i in instances]}")

        return instances
    except Exception as e:
        print(f"Error getting premium instances: {str(e)}")
        return []


def get_standby_pool_status() -> Dict[str, Any]:
    """Get detailed status of the premium standby pool"""
    try:
        # Use dynamic tag-based discovery instead of hardcoded list
        all_instances = get_all_premium_instances_with_states()

        status: Dict[str, Any] = {
            "total_instances": len(all_instances),
            "running": 0,
            "stopped": 0,
            "failed": 0,
            "assigned_users": 0,
            "idle_running": 0,
            "health_issues": [],
        }

        for instance in all_instances:
            instance_id = instance["instance_id"]
            instance_state = instance["state"]

            if instance_state == InstanceState.RUNNING:
                status["running"] += 1
                assigned_users = get_assigned_users_for_instance(instance_id)
                status["assigned_users"] += len(assigned_users)

                if not assigned_users:
                    status["idle_running"] += 1
                    if not check_instance_readiness(instance_id):
                        status["health_issues"].append(
                            f"Instance {instance_id} running but not ready"
                        )

            elif instance_state == InstanceState.STOPPED:
                status["stopped"] += 1
            else:
                status["failed"] += 1
                status["health_issues"].append(
                    f"Instance {instance_id} in {instance_state} state"
                )

        return status

    except Exception as e:
        print(f"Error getting standby pool status: {str(e)}")
        return {"error": str(e)}


def ensure_standby_pool_capacity() -> Dict[str, Any]:
    """Ensure standby pool maintains required capacity and handle failed instances.

    When more instances are flagged as standby than the target pool size,
    clears excess is_standby flags so those instances become eligible for
    normal idle cleanup/termination in subsequent runs.
    """
    try:
        status = get_standby_pool_status()

        if "error" in status:
            return {"success": False, "error": status["error"]}

        target_stopped = int(os.environ.get("PREMIUM_STANDBY_POOL_SIZE", "1"))
        actions_taken = []

        # Log current status
        print(
            f"Standby pool status: {status['running']} running, "
            f"{status['stopped']} stopped, {status['failed']} failed "
            f"(target standby: {target_stopped})"
        )

        # Check for capacity issues
        if status["stopped"] < target_stopped and status["idle_running"] == 0:
            actions_taken.append("Low standby capacity detected")

        # Enforce pool size: clear excess standby flags
        if status["stopped"] > target_stopped:
            excess = status["stopped"] - target_stopped
            print(
                f"Excess standby instances: {excess} "
                f"(have {status['stopped']}, target {target_stopped}). "
                f"Clearing excess is_standby flags."
            )
            try:
                with get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        # Keep the newest target_stopped standby instances,
                        # clear is_standby on the rest (oldest first)
                        cursor.execute(
                            """UPDATE premium_user_assignments
                               SET is_standby = 0
                               WHERE is_standby = 1
                               AND id NOT IN (
                                   SELECT id FROM (
                                       SELECT id FROM premium_user_assignments
                                       WHERE is_standby = 1
                                       ORDER BY last_activity DESC
                                       LIMIT %s
                                   ) AS keep_rows
                               )""",
                            (target_stopped,),
                        )
                        cleared = cursor.rowcount
                    conn.commit()
                actions_taken.append(
                    f"Cleared is_standby flag on {cleared} excess instances"
                )
                print(f"Cleared is_standby flag on {cleared} excess instances")
            except Exception as e:
                actions_taken.append(f"Failed to clear excess standby flags: {e}")
                print(f"Error clearing excess standby flags: {e}")

        if status["failed"] > 0:
            actions_taken.append(f"Found {status['failed']} failed instances")

        if status["health_issues"]:
            actions_taken.extend([f"{issue}" for issue in status["health_issues"]])

        return {
            "success": True,
            "status": status,
            "actions_taken": actions_taken,
            "recommendations": [
                f"Target standby capacity: {target_stopped} stopped instances",
                f"Current capacity: {status['stopped']} "
                f"stopped, {status['running']} running",
            ],
        }

    except Exception as e:
        print(f"Error ensuring standby pool capacity: {str(e)}")
        return {"success": False, "error": str(e)}


def reconcile_instance_states() -> Dict[str, Any]:
    """
    Reconcile database instance states with actual AWS instance states
    Moved from premium_manager as this is maintenance, not real-time operation
    """
    try:
        # Get all instances from AWS using dynamic tag-based discovery
        all_instances = get_all_premium_instances_with_states()

        aws_instance_map = {}
        for instance in all_instances:
            aws_instance_map[instance["instance_id"]] = {
                "instance_id": instance["instance_id"],
                "state": instance["state"],
            }

        # Delegate to transaction-safe internal function
        return _reconcile_instance_states_transaction(aws_instance_map)

    except Exception as e:
        print(f"Error reconciling instance states: {str(e)}")
        return {"error": str(e)}


@with_transaction
def _reconcile_instance_states_transaction(
    connection, aws_instance_map: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Internal function: Reconcile instance states with transaction safety.
    All changes are committed together or rolled back on error.
    """
    cleanup_count = 0
    update_count = 0

    # Client for ALB cleanup when instances are gone
    elbv2: "ElasticLoadBalancingv2Client" = boto3.client("elbv2")

    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT id, user_id, instance_id, instance_state, status,
                      target_group_arn, alb_rule_arn
               FROM premium_user_assignments WHERE status = %s""",
            (PremiumAssignment.ACTIVE,),
        )
        db_assignments = cursor.fetchall()

        for assignment in db_assignments:
            assignment_id = assignment["id"]
            user_id = assignment["user_id"]
            instance_id = assignment["instance_id"]
            db_state = assignment["instance_state"]
            aws_instance = aws_instance_map.get(instance_id)

            # Skip autoscaling-pool assignments - it's a virtual marker,
            # not a real instance. Users on autoscaling-pool are waiting
            # for migration to a dedicated instance
            if instance_id == PremiumAssignment.AUTOSCALING_POOL:
                print(
                    f"Skipping autoscaling-pool assignment id={assignment_id} "
                    f"for user {user_id} (virtual marker, not a real instance)"
                )
                continue

            if not aws_instance:
                # Instance no longer exists in AWS - cleanup
                # Use assignment id for deletion (handles NULL user_id for standby)
                print(
                    f"Cleaning up assignment id={assignment_id} for "
                    f"terminated instance {instance_id} (user {user_id})"
                )

                # Clean up ALB resources before DB deletion
                target_group_arn = assignment.get("target_group_arn")
                alb_rule_arn = assignment.get("alb_rule_arn")
                _cleanup_assignment_alb_resources(
                    elbv2, user_id, alb_rule_arn, target_group_arn
                )

                cursor.execute(
                    "DELETE FROM premium_user_assignments WHERE id = %s",
                    (assignment_id,),
                )
                cleanup_count += 1
            elif aws_instance["state"] != db_state:
                # Update database state to match AWS
                # Use assignment id for update (handles NULL user_id for standby)
                aws_state = aws_instance["state"]
                print(
                    f"Updating instance state for assignment id={assignment_id} "
                    f"(user {user_id}): {db_state} → {aws_state}"
                )
                cursor.execute(
                    """UPDATE premium_user_assignments
                       SET instance_state = %s, last_state_check = NOW()
                       WHERE id = %s""",
                    (aws_state, assignment_id),
                )
                update_count += 1

    # Transaction decorator handles commit on success, rollback on error
    return {
        "cleanup_count": cleanup_count,
        "update_count": update_count,
        "total_aws_instances": len(aws_instance_map),
        "total_db_assignments": len(db_assignments),
    }


def reconcile_single_instance(instance_id: str) -> Dict[str, Any]:
    """
    Reconcile a single instance — triggered by EventBridge when an EC2
    instance enters shutting-down or terminated state.

    Fast-path complement to the full reconcile_instance_states() which
    runs hourly. Only checks the specified instance.
    """
    try:
        ec2: "EC2Client" = boto3.client("ec2")
        try:
            response = ec2.describe_instances(InstanceIds=[instance_id])
            reservations = response.get("Reservations", [])
            if reservations:
                instance = reservations[0]["Instances"][0]
                tags = {
                    tag.get("Key"): tag.get("Value") for tag in instance.get("Tags", [])
                }
                name_match = (
                    PremiumInstanceConfig.INSTANCE_IDENTIFIER
                    in tags.get("Name", "").lower()
                )
                tier_match = (
                    tags.get("Tier", "").lower()
                    == PremiumInstanceConfig.INSTANCE_IDENTIFIER
                )
                type_match = (
                    PremiumInstanceConfig.INSTANCE_IDENTIFIER
                    in tags.get("Type", "").lower()
                )
                if not (name_match or tier_match or type_match):
                    print(
                        f"Instance {instance_id} is not a "
                        f"premium instance, skipping"
                    )
                    return {
                        "skipped": True,
                        "reason": "not_premium_instance",
                    }
        except Exception as e:
            # Instance may be fully terminated and gone from API.
            # Fall through to DB check — if it's in our DB, clean it.
            print(
                f"Could not describe instance {instance_id} "
                f"(may be fully terminated, checking DB): {e}"
            )

        # Skip autoscaling-pool — it's a virtual marker, not a real instance
        if instance_id == PremiumAssignment.AUTOSCALING_POOL:
            print("Skipping autoscaling-pool marker (not a real instance)")
            return {
                "skipped": True,
                "reason": "autoscaling_pool_marker",
            }

        return _reconcile_single_instance_transaction(instance_id)

    except Exception as e:
        print(f"Error in reconcile_single_instance " f"for {instance_id}: {e}")
        return {"error": str(e), "instance_id": instance_id}


@with_transaction
def _reconcile_single_instance_transaction(
    connection, instance_id: str
) -> Dict[str, Any]:
    """
    Clean up DB records and ALB resources for a single
    terminated instance. Transaction-safe via decorator.
    """
    elbv2: "ElasticLoadBalancingv2Client" = boto3.client("elbv2")
    cleanup_count = 0

    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT id, user_id, instance_id,
                      target_group_arn, alb_rule_arn
               FROM premium_user_assignments
               WHERE instance_id = %s AND status = %s
               FOR UPDATE""",
            (instance_id, PremiumAssignment.ACTIVE),
        )
        assignments = cursor.fetchall()

        if not assignments:
            print(f"No active assignments for instance " f"{instance_id}")
            return {
                "cleanup_count": 0,
                "instance_id": instance_id,
            }

        print(
            f"Found {len(assignments)} assignments to clean "
            f"up for terminated instance {instance_id}"
        )

        for assignment in assignments:
            assignment_id = assignment["id"]
            user_id = assignment["user_id"]
            target_group_arn = assignment.get("target_group_arn")
            alb_rule_arn = assignment.get("alb_rule_arn")

            _cleanup_assignment_alb_resources(
                elbv2, user_id, alb_rule_arn, target_group_arn
            )

            cursor.execute(
                "DELETE FROM premium_user_assignments " "WHERE id = %s",
                (assignment_id,),
            )
            cleanup_count += 1

    return {
        "cleanup_count": cleanup_count,
        "instance_id": instance_id,
    }


@with_transaction
def cleanup_test_user_assignments(connection, user_emails: List[str]) -> Dict[str, Any]:
    """
    Clean up premium assignments for specific test users by email.
    Designed to be called by test scripts that need to clean up test data.

    This function performs complete cleanup of premium user assignments:
    1. Looks up user IDs from email addresses
    2. Finds all premium assignments for those users
    3. Deletes AWS resources (ALB rules, target groups)
    4. Removes database records from premium_user_assignments table

    Usage (from test scripts):
        # Invoke this Lambda with:
        lambda_client = boto3.client('lambda')
        response = lambda_client.invoke(
            FunctionName='{env}-premium-cleanup',
            InvocationType='RequestResponse',
            Payload=json.dumps({
                "action": "cleanup_test_users",
                "user_emails": ["user1@test.com", "user2@test.com"]
            })
        )

    Args:
        connection: Database connection (provided by @with_transaction decorator)
        user_emails: List of user email addresses to clean up assignments for

    Returns:
        Dict with cleanup statistics:
        {
            "success": True/False,
            "message": "Description of what happened",
            "assignments_deleted": 3,
            "users_cleaned": 2
        }

    Note:
        - This is a transactional operation (uses @with_transaction)
        - AWS resource deletion is best-effort (continues on errors)
        - Database changes are rolled back if any critical error occurs
    """
    try:
        if not user_emails:
            return {
                "success": False,
                "message": "No user emails provided",
                "assignments_deleted": 0,
            }

        print(f"Cleaning up premium assignments for {len(user_emails)} test users")

        with connection.cursor() as cursor:
            # First, get user IDs for the given emails
            placeholders = ", ".join(["%s"] * len(user_emails))
            cursor.execute(
                f"""SELECT id, email FROM users
                    WHERE email IN ({placeholders})""",
                user_emails,
            )
            users = cursor.fetchall()

            if not users:
                return {
                    "success": True,
                    "message": "No users found with provided emails",
                    "assignments_deleted": 0,
                }

            user_ids = [user["id"] for user in users]
            print(f"Found {len(user_ids)} users to clean up")

            # Get assignments for these users (including ALB resources to clean)
            user_id_placeholders = ", ".join(["%s"] * len(user_ids))
            cursor.execute(
                f"""SELECT user_id, instance_id, target_group_arn, alb_rule_arn
                    FROM premium_user_assignments
                    WHERE user_id IN ({user_id_placeholders})""",
                user_ids,
            )
            assignments = cursor.fetchall()

            if not assignments:
                return {
                    "success": True,
                    "message": "No assignments found for these users",
                    "assignments_deleted": 0,
                }

            print(f"Found {len(assignments)} assignments to clean up")

            # Clean up AWS resources for each assignment
            elbv2: "ElasticLoadBalancingv2Client" = boto3.client("elbv2")
            cleaned_count = 0

            for assignment in assignments:
                user_id = assignment["user_id"]
                target_group_arn = assignment["target_group_arn"]
                alb_rule_arn = assignment["alb_rule_arn"]

                try:
                    _cleanup_assignment_alb_resources(
                        elbv2, user_id, alb_rule_arn, target_group_arn
                    )

                    # Close usage log before deleting assignment
                    cursor.execute(
                        """UPDATE instance_usage_log SET ended_at = NOW()
                           WHERE user_id = %s AND tier = 'premium'
                           AND ended_at IS NULL""",
                        (user_id,),
                    )

                    # Remove from database
                    cursor.execute(
                        "DELETE FROM premium_user_assignments WHERE user_id = %s",
                        (user_id,),
                    )
                    cleaned_count += 1
                    print(f"Deleted assignment for user {user_id}")

                except Exception as e:
                    print(f"Error cleaning assignment for user {user_id}: {e}")
                    # Continue with other assignments

            print(
                f"Test cleanup complete: "
                f"{cleaned_count}/{len(assignments)} assignments cleaned"
            )

            return {
                "success": True,
                "message": f"Cleaned {cleaned_count} test user assignments",
                "assignments_deleted": cleaned_count,
                "users_cleaned": len(user_ids),
            }

    except Exception as e:
        print(f"Error during test user cleanup: {str(e)}")
        raise e


def get_user_assignment(user_email: str) -> Dict[str, Any]:
    """
    Get premium_user_assignment for a user by email.

    Args:
        user_email: The user's email address

    Returns:
        Dict with assignment info or error
    """
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM users WHERE email = %s",
                    (user_email,),
                )
                user = cursor.fetchone()
                if not user:
                    return {
                        "success": False,
                        "message": f"User not found: {user_email}",
                    }

                user_id = user["id"]
                cursor.execute(
                    """SELECT user_id, instance_id, status,
                              target_group_arn, alb_rule_arn,
                              instance_state, last_activity,
                              is_standby
                       FROM premium_user_assignments
                       WHERE user_id = %s""",
                    (user_id,),
                )
                assignment = cursor.fetchone()
                if not assignment:
                    return {
                        "success": True,
                        "message": (f"No assignment for {user_email}"),
                        "user_id": user_id,
                        "instance_id": None,
                    }

                print(
                    f"User {user_email} (id={user_id}) "
                    f"assigned to {assignment['instance_id']}"
                )
                return {
                    "success": True,
                    "user_id": assignment["user_id"],
                    "instance_id": assignment["instance_id"],
                    "status": assignment["status"],
                    "instance_state": assignment["instance_state"],
                    "target_group_arn": assignment["target_group_arn"],
                    "alb_rule_arn": assignment["alb_rule_arn"],
                    "is_standby": assignment["is_standby"],
                    "last_activity": (
                        assignment["last_activity"].isoformat()
                        if assignment["last_activity"]
                        else None
                    ),
                }

    except Exception as e:
        print(f"Error getting user assignment: {str(e)}")
        return {"success": False, "error": str(e)}


@with_transaction
def migrate_user(
    connection,
    user_email: str,
    target_instance_id: str,
) -> Dict[str, Any]:
    """
    Migrate a premium user to a different instance by swapping
    the EC2 instance registered in the user's target group.

    Args:
        connection: DB connection (provided by @with_transaction)
        user_email: Email of the user to migrate
        target_instance_id: EC2 instance to migrate to

    Returns:
        Dict with migration result
    """
    try:
        elbv2: "ElasticLoadBalancingv2Client" = boto3.client("elbv2")

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM users WHERE email = %s",
                (user_email,),
            )
            user = cursor.fetchone()
            if not user:
                return {
                    "success": False,
                    "message": f"User not found: {user_email}",
                }

            user_id = user["id"]
            cursor.execute(
                """SELECT instance_id, target_group_arn,
                          status
                   FROM premium_user_assignments
                   WHERE user_id = %s""",
                (user_id,),
            )
            assignment = cursor.fetchone()
            if not assignment:
                return {
                    "success": False,
                    "message": (f"No assignment for {user_email}"),
                }

            source = assignment["instance_id"]
            if source == target_instance_id:
                return {
                    "success": False,
                    "message": ("Source and target are the same" f": {source}"),
                }

            status = assignment["status"]
            if status != PremiumAssignment.ACTIVE:
                return {
                    "success": False,
                    "message": (
                        f"Assignment status is '{status}'," " expected 'active'"
                    ),
                }

            tg_arn = assignment["target_group_arn"]
            if not tg_arn or tg_arn.lower() == PremiumAssignment.STANDBY:
                return {
                    "success": False,
                    "message": "No target group for user",
                }

            # Swap instance in target group
            backend_port = PremiumInstanceConfig.get_backend_port()
            try:
                elbv2.deregister_targets(
                    TargetGroupArn=tg_arn,
                    Targets=[{"Id": source, "Port": backend_port}],
                )
                print(f"Deregistered {source} from {tg_arn}")
            except Exception as e:
                print(f"Warning: deregister failed: {e}")

            elbv2.register_targets(
                TargetGroupArn=tg_arn,
                Targets=[{"Id": target_instance_id, "Port": backend_port}],
            )
            print(f"Registered {target_instance_id} in {tg_arn}")

            # Update DB
            cursor.execute(
                """UPDATE premium_user_assignments
                   SET instance_id = %s,
                       instance_state = 'running',
                       last_activity = NOW()
                   WHERE user_id = %s""",
                (target_instance_id, user_id),
            )

            print(
                f"Migrated {user_email} (id={user_id})"
                f" {source} -> {target_instance_id}"
            )
            return {
                "success": True,
                "message": "Migration successful",
                "user_id": user_id,
                "source_instance": source,
                "target_instance": target_instance_id,
            }

    except Exception as e:
        print(f"Error migrating user: {str(e)}")
        raise e


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Premium Cleanup Lambda Handler - Data & Resource Hygiene

    Responsibilities:
    1. Remove stale assignments (>2 hours inactive)
    2. Clean up orphaned ALB resources
    3. Reconcile instance states with AWS reality
    4. Monitor standby pool health

    Does NOT make scaling decisions - that's premium_manager's job.

    Also supports manual invocations:
    - cleanup_test_users: Clean up premium assignments for specific test users
      Event format:
      {"action": "cleanup_test_users", "user_emails": ["email1@example.com", ...]}

    - get_user_assignment: Look up a user's premium assignment
      Event: {"action": "get_user_assignment", "user_email": "u@test.com"}

    - migrate_user: Migrate a user to a different instance
      Event: {"action": "migrate_user", "user_email": "u@test.com",
              "target_instance_id": "i-abc123"}

    - get_instance_users: List users assigned to a specific instance
      Event: {"action": "get_instance_users",
              "instance_id": "i-abc123"}

    - reconcile: Reconcile DB instance states with AWS reality
      Event: {"action": "reconcile"}

    - reconcile_instance: Reconcile a single instance (EventBridge)
      Event: {"action": "reconcile_instance",
              "instance_id": "i-abc123",
              "instance_state": "terminated",
              "source": "ec2_state_change"}
    """

    print(f"Premium cleanup triggered by event: {json.dumps(event)}")
    print(f"Lambda context: {context.function_name if context else 'No context'}")

    try:
        # Check if this is a manual test cleanup invocation
        action = event.get("action")
        if action == "cleanup_test_users":
            user_emails = event.get("user_emails", [])
            print(f"Manual test cleanup invocation for {len(user_emails)} users")
            cleanup_result = cleanup_test_user_assignments(user_emails)
            return {
                "statusCode": 200,
                "body": json.dumps(
                    {"message": cleanup_result.get("message"), "result": cleanup_result}
                ),
            }

        elif action == "get_user_assignment":
            user_email = event.get("user_email")
            if not user_email:
                return {
                    "statusCode": 400,
                    "body": json.dumps(
                        {"error": "Missing required parameter: user_email"}
                    ),
                }
            result = get_user_assignment(user_email)
            return {
                "statusCode": 200,
                "body": json.dumps({"result": result}),
            }

        elif action == "migrate_user":
            user_email = event.get("user_email")
            target_instance_id = event.get("target_instance_id")
            if not user_email or not target_instance_id:
                return {
                    "statusCode": 400,
                    "body": json.dumps(
                        {
                            "error": (
                                "Missing required parameters:"
                                " user_email, target_instance_id"
                            )
                        }
                    ),
                }
            result = migrate_user(user_email, target_instance_id)
            return {
                "statusCode": 200,
                "body": json.dumps({"result": result}),
            }

        elif action == "get_instance_users":
            instance_id = event.get("instance_id")
            if not instance_id:
                return {
                    "statusCode": 400,
                    "body": json.dumps(
                        {"error": ("Missing required parameter:" " instance_id")}
                    ),
                }
            user_ids = get_assigned_users_for_instance(instance_id)
            return {
                "statusCode": 200,
                "body": json.dumps(
                    {
                        "result": {
                            "instance_id": instance_id,
                            "user_ids": user_ids,
                            "count": len(user_ids),
                        }
                    }
                ),
            }

        elif action == "reconcile_instance":
            instance_id = event.get("instance_id")
            if not instance_id:
                return {
                    "statusCode": 400,
                    "body": json.dumps(
                        {"error": ("Missing required parameter:" " instance_id")}
                    ),
                }
            source = event.get("source", "manual")
            print(
                f"Targeted instance reconciliation for "
                f"{instance_id} (source: {source})"
            )
            result = reconcile_single_instance(instance_id)
            return {
                "statusCode": 200,
                "body": json.dumps({"result": result}),
            }

        elif action == "reconcile":
            result = reconcile_instance_states()
            return {
                "statusCode": 200,
                "body": json.dumps({"result": result}),
            }

        # Otherwise, proceed with normal scheduled cleanup
        # Initialize results
        results: Dict[str, Any] = {
            "cleanup_stats": {},
            "orphaned_cleanup_stats": {},
            "duplicate_cleanup_stats": {},
            "reconciliation_stats": {},
            "capacity_check": {},
            "timestamp": time.time(),
        }

        # 1. Cleanup stale assignments from database
        print("Step 1: Cleaning up stale assignments...")
        results["cleanup_stats"] = cleanup_stale_assignments()

        # 2. Cleanup orphaned ALB resources (rules/target groups with no DB entry)
        print("Step 2: Cleaning up orphaned ALB resources...")
        results["orphaned_cleanup_stats"] = cleanup_orphaned_alb_resources()

        # 3. Cleanup duplicate ALB rules (multiple rules with same routing_id)
        print("Step 3: Cleaning up duplicate ALB rules...")
        results["duplicate_cleanup_stats"] = cleanup_duplicate_alb_rules()

        # 4. Reconcile instance states (update DB to match AWS reality)
        print("Step 4: Reconciling instance states...")
        results["reconciliation_stats"] = reconcile_instance_states()

        # 5. Monitor and enforce standby pool capacity
        print("Step 5: Checking standby pool capacity...")
        results["capacity_check"] = ensure_standby_pool_capacity()

        # Summary
        total_operations = (
            results["cleanup_stats"].get("cleaned_assignments", 0)
            + results["orphaned_cleanup_stats"].get("orphaned_rules_deleted", 0)
            + results["duplicate_cleanup_stats"].get("duplicates_deleted", 0)
            + results["reconciliation_stats"].get("cleanup_count", 0)
            + results["reconciliation_stats"].get("update_count", 0)
        )

        print(
            f"Premium cleanup complete: {total_operations} total operations performed"
        )

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": f"Premium cleanup completed successfully. "
                    f"{total_operations} operations performed.",
                    "results": results,
                }
            ),
        }

    except Exception as e:
        print(f"Error during premium cleanup: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps(
                {
                    "error": f"Premium cleanup failed: {str(e)}",
                    "results": results if "results" in locals() else {},
                }
            ),
        }
