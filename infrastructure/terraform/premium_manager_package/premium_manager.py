"""
Premium Manager Lambda Function - Compute & Capacity Management

PRIMARY RESPONSIBILITIES:
- Real-time assignment of premium users to instances (API-triggered)
- Real-time release of premium users from instances (API-triggered)
- Scaling and instance management (both real-time and scheduled)
- ALB routing rule creation and deletion
- Scheduled monitoring (every 15 min) to make scaling decisions
- Standby pool management (ensure capacity, cleanup excess)

SCALING STRATEGY:
- Triggered by: User logout, scheduled monitoring (every 15 min)
- Algorithm: scale_down_if_possible() - conservative (keeps active_users + 1)
- Coordinates with: premium_cleanup (which cleans data, not compute)

ARCHITECTURE NOTES:
- This Lambda handles ALL compute and capacity decisions
- Data cleanup is handled by premium_cleanup.py (removes:
     stale assignments, orphaned resources)
- Division of labor:
  - premium_manager: ALL scaling decisions, instance lifecycle, capacity management
  - premium_cleanup: Data hygiene, resource reconciliation (hourly)

Required Environment Variables:
- RDS_HOST: Database host (format: host:port)
- RDS_USER: Database username
- RDS_PASSWORD: Database password
- RDS_DATABASE: Database name
- VPC_ID: VPC ID for target group creation
- ALB_LISTENER_ARN: ALB listener ARN for routing rules
- CLUSTER_NAME: ECS cluster name
- PREMIUM_EXTRA_CAPACITY: Extra capacity buffer (default: 2)
- PREMIUM_IDLE_TIMEOUT_HOURS: Must match premium_cleanup.py value
"""

import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import boto3
import pymysql

# Shared constants from Lambda Layer (mounted at /opt/python by AWS Lambda)
from aws_constants import (
    DatabaseConfig,
    ECSTaskStatus,
    EnvironmentConfig,
    EphemeralPortConfig,
    InstanceState,
    PremiumAssignment,
    PremiumInstanceConfig,
    RoutingHeaders,
)
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_cloudwatch import CloudWatchClient
    from mypy_boto3_ec2 import EC2Client
    from mypy_boto3_ecs import ECSClient
    from mypy_boto3_elbv2 import ElasticLoadBalancingv2Client
    from mypy_boto3_lambda import LambdaClient

# Constants
DEFAULT_DEVELOPMENT_CAPACITY = 3  # Fallback capacity for dev/testing
DEFAULT_IDLE_TIMEOUT_HOURS = 3  # Hours before idle instances become standby
STICKY_SESSION_DURATION_SECONDS = 300  # Match ALB target group stickiness settings
MAX_PREMIUM_PRIORITY = 199  # Reserve 200+ for non-premium tier routing.

# MySQL GET_LOCK names for preventing concurrent instance creation
CREATE_STANDBY_LOCK = "create_standby_lock"
CREATE_RUNNING_LOCK = "create_running_lock"
MIGRATE_USERS_LOCK = "migrate_users_lock"
PREMIUM_SCALING_LOCK = "premium_scaling_lock"
ASSIGN_USER_LOCK_PREFIX = "assign_user_"
ASSIGN_LOCK_TIMEOUT_SECONDS = (
    10  # Short acquisition timeout so 409 fallback is reachable within Lambda timeout
)
LOCK_TIMEOUT_SECONDS = 60

# Wait before first migration attempt to let instances boot
MIGRATION_INITIAL_DELAY_SECONDS = 60

STANDBY_READINESS_TIMEOUT_SECONDS = 120
STANDBY_READINESS_RETRY_INTERVAL_SECONDS = 10

# Skip TG reconciliation for rows whose write paths touched them within this
# window — avoids racing in-flight assign/migrate work.
RECONCILE_RECENT_MUTATION_SECONDS = 30


def generate_routing_id(uid: str, secret_key: str) -> str:
    """Generate non-reversible routing ID from UID using HMAC-SHA256

    Creates a cryptographically secure, non-reversible identifier from the user's UID.
    This routing ID is used in ALB routing rules instead of exposing the raw UID.

    Security properties:
    - Cannot be reverse-engineered to extract the UID
    - Deterministic (same UID always produces same routing_id)
    - Requires the secret key to generate (client cannot forge)

    Args:
        uid: Firebase user ID
        secret_key: Secret key for HMAC signature

    Returns:
        16-character hex string (64 bits of entropy)
    """
    signature = hmac.new(
        secret_key.encode("utf-8"), uid.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return signature[:16]  # 16 hex chars = 64 bits


def get_required_env_var(var_name: str, default_value: str | None = None) -> str:
    """
    Safely get required environment variable with helpful error messages.

    Args:
        var_name: Name of the environment variable
        default_value: Optional default value if not provided

    Returns:
        Environment variable value

    Raises:
        ValueError: If required environment variable is missing and no default provided
    """
    value = os.environ.get(var_name, default_value)
    if value is None or value == "":
        raise ValueError(
            f"Missing required environment variable: {var_name}. "
            f"Check your Terraform configuration and Lambda environment settings."
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
            host = rds_host.split(":")[0] if ":" in rds_host else rds_host

            conn = pymysql.connect(
                host=host,
                port=DatabaseConfig.DEFAULT_PORT,
                user=get_required_env_var("RDS_USER"),
                password=get_required_env_var("RDS_PASSWORD"),
                database=get_required_env_var("RDS_DATABASE"),
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=auto_commit,
                ssl={"check_hostname": False},
            )
            yield conn
        except ValueError as e:
            print(
                f" Database connection failed "
                f"- environment configuration error: {str(e)}"
            )
            raise
        except pymysql.MySQLError as e:
            print(f" Database connection failed - connection error: {str(e)}")
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


def distributed_lock(lock_name, timeout=LOCK_TIMEOUT_SECONDS):
    """Acquire a MySQL GET_LOCK held for the block's lifetime.

    GET_LOCK is session-scoped: the connection that acquired it
    must stay open or the lock is silently released.  This context
    manager keeps a dedicated connection alive until the caller
    exits the ``with`` block.

    Yields ``True`` if the lock was acquired, ``False`` otherwise.
    The caller should check the value and skip work when False.
    """
    from contextlib import contextmanager

    @contextmanager
    def _lock_context():
        conn = None
        acquired = False
        try:
            rds_host = get_required_env_var("RDS_HOST")
            host = rds_host.split(":")[0] if ":" in rds_host else rds_host
            conn = pymysql.connect(
                host=host,
                port=DatabaseConfig.DEFAULT_PORT,
                user=get_required_env_var("RDS_USER"),
                password=get_required_env_var("RDS_PASSWORD"),
                database=get_required_env_var("RDS_DATABASE"),
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
                ssl={"check_hostname": False},
            )
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT GET_LOCK(%s, %s) as lock_result",
                    (lock_name, timeout),
                )
                result = cursor.fetchone()
                acquired = result["lock_result"] == 1

            if acquired:
                print(f"Acquired distributed lock '{lock_name}'")
            else:
                print(
                    f"Failed to acquire lock '{lock_name}' "
                    f"(held by another session)"
                )
            yield acquired
        finally:
            if acquired and conn:
                try:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            "SELECT RELEASE_LOCK(%s)",
                            (lock_name,),
                        )
                    print(f"Released lock '{lock_name}'")
                except Exception as e:
                    print(f"Failed to release lock " f"'{lock_name}': {e}")
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    return _lock_context()


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
                raise

    return wrapper


def get_user_id_from_uid(connection, user_uid: str) -> int:
    """
    Look up the numeric database user ID from the Firebase UID.

    Args:
        connection: Database connection
        user_uid: Firebase UID string

    Returns:
        Numeric user ID from the users table

    Raises:
        ValueError: If user not found
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT id FROM users
               WHERE uid = %s AND active = 1""",
            (user_uid,),
        )
        result = cursor.fetchone()

        if not result:
            raise ValueError(f"User not found with UID: {user_uid}")

        return result["id"]


def get_user_uid_from_id(connection, user_id: int) -> str:
    """Reverse lookup: get Firebase UID from numeric DB user_id.

    Needed when only numeric ID is available (e.g. migration).
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT uid FROM users
               WHERE id = %s AND active = 1""",
            (user_id,),
        )
        result = cursor.fetchone()

        if not result:
            raise ValueError(f"User not found with ID: {user_id}")

        return result["uid"]


@with_transaction
def _increment_assignment_attempts_transaction(connection, user_id: int) -> int:
    """Internal function: Increment assignment attempts for
    retry scenarios with transaction safety"""
    with connection.cursor() as cursor:
        # Check if user has existing assignment and increment attempts
        cursor.execute(
            """SELECT assignment_attempts FROM premium_user_assignments
               WHERE user_id = %s FOR UPDATE""",
            (user_id,),
        )
        existing = cursor.fetchone()

        if existing:
            current_attempts = existing.get("assignment_attempts") or 1
            new_attempts = current_attempts + 1

            cursor.execute(
                """UPDATE premium_user_assignments
                   SET assignment_attempts = %s, last_state_check = NOW()
                   WHERE user_id = %s""",
                (new_attempts, user_id),
            )

            print(
                f"Incremented assignment attempts for user {user_id} to {new_attempts}"
            )
            return new_attempts
        else:
            # No existing assignment, this is first attempt
            print(
                f"No existing assignment for user {user_id}, treating as first attempt"
            )
            return 1


def increment_assignment_attempts(user_id: int) -> int:
    """Increment assignment attempts for retry scenarios"""
    return _increment_assignment_attempts_transaction(user_id)


@with_transaction
def _store_user_assignment_transaction(
    connection,
    user_id: Optional[int],
    instance_id: str,
    target_group_arn: str,
    rule_arn: str,
    instance_state: str = InstanceState.LAUNCHING,
    is_shared: bool = False,
    is_standby: bool = False,
):
    """Internal function: Store user assignment with transaction safety

    Args:
        user_id: User ID (int) or None for standby instances
        instance_id: EC2 instance ID
        target_group_arn: ALB target group ARN
        rule_arn: ALB listener rule ARN
        instance_state: Current state of instance
        is_shared: Whether instance is shared
        is_standby: Whether this is a standby pool assignment (user_id should be None)
    """
    active_workflows_from_free = 0

    with connection.cursor() as cursor:
        # For standby instances, check by instance_id (NULL user_id won't match)
        if is_standby or user_id is None:
            cursor.execute(
                """SELECT instance_id FROM premium_user_assignments
                   WHERE instance_id = %s FOR UPDATE""",
                (instance_id,),
            )
            existing_instance = cursor.fetchone()
            if existing_instance:
                print(
                    f"Instance {instance_id} already has an assignment entry, "
                    f"skipping duplicate standby creation"
                )
                raise Exception(
                    f"Instance {instance_id} already exists in assignments table"
                )
        else:
            # For regular user assignments, check by user_id
            cursor.execute(
                """SELECT user_id, assignment_attempts FROM premium_user_assignments
                   WHERE user_id = %s FOR UPDATE""",
                (user_id,),
            )
            existing = cursor.fetchone()

            if existing:
                # User already has assignment - increment attempts counter
                current_attempts = existing.get("assignment_attempts") or 1
                new_attempts = current_attempts + 1

                cursor.execute(
                    """UPDATE premium_user_assignments
                       SET assignment_attempts = %s, last_state_check = NOW()
                       WHERE user_id = %s""",
                    (new_attempts, user_id),
                )

                print(
                    f"User {user_id} already has assignment, "
                    f"incremented attempts to {new_attempts}"
                )
                raise Exception(
                    f"User {user_id} already has a premium "
                    f"assignment (attempt #{new_attempts})"
                )

            # Check for active workflows to preserve before removing free tier record
            cursor.execute(
                """SELECT active_workflow_count FROM free_user_assignments
                   WHERE user_id = %s""",
                (user_id,),
            )
            free_record = cursor.fetchone()
            active_workflows_from_free = 0
            if free_record:
                active_workflows_from_free = (
                    free_record.get("active_workflow_count", 0) or 0
                )
                if active_workflows_from_free > 0:
                    print(
                        f"User {user_id} has {active_workflows_from_free} active "
                        f"workflows - will preserve count in premium assignment"
                    )

            # Close free-tier usage log before deleting assignment
            if user_id is not None:
                cursor.execute(
                    """UPDATE instance_usage_log SET ended_at = NOW()
                       WHERE user_id = %s AND tier = 'free'
                       AND ended_at IS NULL""",
                    (user_id,),
                )

            cursor.execute(
                """DELETE FROM free_user_assignments WHERE user_id = %s""",
                (user_id,),
            )
            deleted_free = cursor.rowcount
            if deleted_free > 0:
                print(
                    f"Cleaned up free_user_assignments record for user {user_id} "
                    f"(user upgraded to premium)"
                )

        # Preserve active workflow count from free tier
        preserved_workflow_count = (
            active_workflows_from_free if not is_standby and user_id else 0
        )

        cursor.execute(
            """
            INSERT INTO premium_user_assignments
            (user_id, instance_id, target_group_arn, alb_rule_arn, status,
             instance_state, is_shared, is_standby,
             assignment_attempts, last_state_check, standby_created_at,
             active_workflow_count)
            VALUES (%s, %s, %s, %s, 'active', %s, %s, %s, 1, NOW(),
                    CASE WHEN %s = 1 THEN NOW() ELSE NULL END, %s)
        """,
            (
                user_id,
                instance_id,
                target_group_arn,
                rule_arn,
                instance_state,
                1 if is_shared else 0,  # Explicit int conversion for MySQL
                1 if is_standby else 0,  # Explicit int conversion for MySQL
                1 if is_standby else 0,  # For the CASE WHEN
                preserved_workflow_count,  # Preserve workflow count from free tier
            ),
        )

        # Log premium usage session (skip standby  - no real user)
        if user_id is not None and not is_standby:
            cursor.execute(
                """INSERT INTO instance_usage_log
                   (user_id, instance_id, tier, started_at)
                   VALUES (%s, %s, 'premium', NOW())""",
                (user_id, instance_id),
            )

    print(
        f"Stored assignment in RDS: user {user_id} -> instance {instance_id} "
        f"(state: {instance_state}, shared: {is_shared}, standby: {is_standby})"
    )


def store_user_assignment(
    user_id: Optional[int],
    instance_id: str,
    target_group_arn: str,
    rule_arn: str,
    instance_state: str = InstanceState.LAUNCHING,
    is_shared: bool = False,
    is_standby: bool = False,
):
    """Store user assignment in RDS with proper transaction isolation and locking"""
    return _store_user_assignment_transaction(
        user_id,
        instance_id,
        target_group_arn,
        rule_arn,
        instance_state,
        is_shared,
        is_standby,
    )


@with_transaction
def _remove_user_assignment_transaction(connection, user_id: int):
    """Internal function: Remove user assignment with transaction safety"""
    with connection.cursor() as cursor:
        # Get assignment details before deletion with lock to prevent race conditions
        cursor.execute(
            """SELECT instance_id, target_group_arn, alb_rule_arn
               FROM premium_user_assignments
               WHERE user_id = %s FOR UPDATE""",
            (user_id,),
        )
        assignment = cursor.fetchone()

        if not assignment:
            raise Exception(f"No assignment found for user {user_id}")

        # Close usage log BEFORE delete (crash-safe: orphan assignment
        # is recoverable, missing ended_at is not)
        cursor.execute(
            """UPDATE instance_usage_log SET ended_at = NOW()
               WHERE user_id = %s AND tier = 'premium'
               AND ended_at IS NULL""",
            (user_id,),
        )

        # Delete assignment
        cursor.execute(
            "DELETE FROM premium_user_assignments WHERE user_id = %s",
            (user_id,),
        )

    print(
        f"Removed assignment from RDS: user {user_id} -> "
        f"instance {assignment['instance_id']}"
    )
    return assignment


def remove_user_assignment(user_id: int):
    """Remove user assignment from RDS with proper transaction isolation"""
    return _remove_user_assignment_transaction(user_id)


@with_transaction
def _update_instance_state_to_running(connection, instance_id: str):
    """Update instance state to running after restart"""
    with connection.cursor() as cursor:
        cursor.execute(
            """UPDATE premium_user_assignments
               SET instance_state = %s,
                   last_state_check = NOW()
               WHERE instance_id = %s
               AND is_standby = 0""",
            (InstanceState.RUNNING, instance_id),
        )


@with_transaction
def _soft_release_user_assignment_transaction(connection, user_id: int):
    """Mark assignment as pending_release instead of deleting it.

    Keeps the ALB rule and target group intact so a page refresh can
    restore the assignment instantly without recreating AWS resources.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT instance_id, target_group_arn, alb_rule_arn, status
               FROM premium_user_assignments
               WHERE user_id = %s AND status = %s AND is_standby = 0
               FOR UPDATE""",
            (user_id, PremiumAssignment.ACTIVE),
        )
        assignment = cursor.fetchone()

        if not assignment:
            print(f"No active assignment to soft-release for user {user_id}")
            return None

        cursor.execute(
            """UPDATE premium_user_assignments
               SET status = %s, last_activity = NOW()
               WHERE user_id = %s AND status = %s""",
            (PremiumAssignment.PENDING_RELEASE, user_id, PremiumAssignment.ACTIVE),
        )
        # Close usage log so idle time is tracked accurately
        cursor.execute(
            """UPDATE instance_usage_log SET ended_at = NOW()
               WHERE user_id = %s AND tier = 'premium'
               AND ended_at IS NULL""",
            (user_id,),
        )

    print(
        f"Soft-released assignment: user {user_id} -> "
        f"instance {assignment['instance_id']} (pending_release)"
    )
    return assignment


def soft_release_user_assignment(user_id: int):
    """Soft-release: mark as pending_release, keep ALB/TG intact."""
    return _soft_release_user_assignment_transaction(user_id)


@with_transaction
def _restore_pending_release_transaction(connection, user_id: int):
    """Restore a pending_release assignment back to active.

    Before restoring, verifies the assigned EC2 instance still exists and is
    not terminated. If the instance is gone, deletes the stale assignment and
    cleans up ALB resources so the caller can trigger a fresh assignment.

    Returns the restored assignment dict, or None if no pending_release exists
    (or if the assignment was stale and removed).
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT user_id, instance_id, target_group_arn, alb_rule_arn,
                      status, instance_state, is_shared, assigned_at
               FROM premium_user_assignments
               WHERE user_id = %s AND status = %s AND is_standby = 0
               FOR UPDATE""",
            (user_id, PremiumAssignment.PENDING_RELEASE),
        )
        assignment = cursor.fetchone()

        if not assignment:
            return None

        instance_id = assignment["instance_id"]

        # Autoscaling pool is a virtual marker, not a real EC2 instance
        if instance_id != PremiumAssignment.AUTOSCALING_POOL:
            try:
                ec2: "EC2Client" = boto3.client("ec2")
                resp = ec2.describe_instances(InstanceIds=[instance_id])
                reservations = resp.get("Reservations", [])
                if reservations and reservations[0].get("Instances"):
                    ec2_state = reservations[0]["Instances"][0]["State"]["Name"]
                else:
                    ec2_state = None
            except ClientError:
                # Instance ID not recognised by AWS (already terminated/gone)
                ec2_state = None

            if ec2_state in (
                InstanceState.TERMINATED,
                InstanceState.SHUTTING_DOWN,
                InstanceState.STOPPED,
                InstanceState.STOPPING,
                None,
            ):
                # Instance is gone or not running — delete the stale DB
                # record so the frontend triggers a fresh assignment which
                # can restart the instance or pick a different one.
                print(
                    f"Instance {instance_id} is {ec2_state or 'not found'} "
                    f"— removing stale assignment for user {user_id}"
                )
                cursor.execute(
                    "DELETE FROM premium_user_assignments "
                    "WHERE user_id = %s AND status = %s",
                    (user_id, PremiumAssignment.PENDING_RELEASE),
                )
                # Close usage log if open
                cursor.execute(
                    """UPDATE instance_usage_log SET ended_at = NOW()
                       WHERE user_id = %s AND tier = 'premium'
                       AND ended_at IS NULL""",
                    (user_id,),
                )
                connection.commit()

                # Best-effort ALB resource cleanup
                target_group_arn = (
                    assignment.get("target_group_arn") or ""
                ).strip() or None
                rule_arn = (assignment.get("alb_rule_arn") or "").strip() or None
                if target_group_arn or rule_arn:
                    try:
                        _teardown_alb_resources(user_id, rule_arn, target_group_arn)
                    except Exception as alb_err:
                        print(
                            f"ALB cleanup warning for stale user {user_id}: "
                            f"{alb_err}"
                        )

                return None

        cursor.execute(
            """UPDATE premium_user_assignments
               SET status = %s, last_activity = NOW()
               WHERE user_id = %s AND status = %s""",
            (PremiumAssignment.ACTIVE, user_id, PremiumAssignment.PENDING_RELEASE),
        )

    print(
        f"Restored pending_release -> active: user {user_id} -> "
        f"instance {assignment['instance_id']}"
    )
    return assignment


def restore_pending_release(user_id: int):
    """Restore a pending_release assignment back to active."""
    return _restore_pending_release_transaction(user_id)


@with_transaction
def _finalize_expired_pending_releases_transaction(connection):
    """Find and delete pending_release assignments past the grace period.

    Returns list of assignments to finalize (caller handles ALB/TG teardown).
    """
    grace_seconds = PremiumAssignment.PENDING_RELEASE_GRACE_SECONDS
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT user_id, instance_id, target_group_arn, alb_rule_arn
               FROM premium_user_assignments
               WHERE status = %s
               AND last_activity < DATE_SUB(NOW(), INTERVAL %s SECOND)
               FOR UPDATE""",
            (PremiumAssignment.PENDING_RELEASE, grace_seconds),
        )
        expired = cursor.fetchall()

        for assignment in expired:
            uid = assignment["user_id"]
            # Close usage log defensively before delete (soft_release should
            # have already closed it, but guard against edge cases)
            cursor.execute(
                """UPDATE instance_usage_log SET ended_at = NOW()
                   WHERE user_id = %s AND tier = 'premium'
                   AND ended_at IS NULL""",
                (uid,),
            )
            cursor.execute(
                "DELETE FROM premium_user_assignments"
                " WHERE user_id = %s AND status = %s",
                (uid, PremiumAssignment.PENDING_RELEASE),
            )
            print(
                f"[premium-trace] finalize-deleted user={uid} "
                f"instance={assignment['instance_id']}"
            )

    return expired


def finalize_expired_pending_releases():
    """Delete expired pending_release rows and return them for AWS cleanup."""
    return _finalize_expired_pending_releases_transaction()


@with_transaction
def _get_existing_user_assignment_transaction(
    connection, user_id: int
) -> Optional[Dict[str, Any]]:
    """Check if user already has an active premium assignment.

    Returns the assignment details if found, None otherwise.
    This is used for early-exit optimization to avoid creating resources
    for users who are already assigned.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT user_id, instance_id, target_group_arn, alb_rule_arn,
                      status, instance_state, is_shared
               FROM premium_user_assignments
               WHERE user_id = %s AND status = %s
               AND is_standby = 0""",
            (user_id, PremiumAssignment.ACTIVE),
        )
        result = cursor.fetchone()
        return result


def get_existing_user_assignment(user_id: int) -> Optional[Dict[str, Any]]:
    """Check if user already has an active premium assignment."""
    return _get_existing_user_assignment_transaction(user_id)


@with_transaction
def _get_assigned_users_for_instance_transaction(connection, instance_id: str):
    """Get list of users assigned to an instance with transaction safety"""
    with connection.cursor() as cursor:
        # First, get all assignments including standby for debugging
        cursor.execute(
            """SELECT user_id, is_shared, instance_state, is_standby, status
               FROM premium_user_assignments
               WHERE instance_id = %s""",
            (instance_id,),
        )
        all_assignments = cursor.fetchall()

        print(f" All assignments for instance {instance_id}:")
        for assignment in all_assignments:
            user_id = assignment.get("user_id", "N/A")
            is_standby = assignment.get("is_standby", 0)
            status = assignment.get("status", "N/A")
            print(f"- User: {user_id}, Standby: {is_standby}, Status: {status}")

        # Now get only real user assignments (exclude standby entries) with lock
        # Include pending_release so instance isn't treated as idle during grace
        cursor.execute(
            """SELECT user_id, is_shared, instance_state
               FROM premium_user_assignments
               WHERE instance_id = %s AND status IN ('active', %s)
               AND is_standby = 0
               FOR UPDATE""",
            (instance_id, PremiumAssignment.PENDING_RELEASE),
        )
        real_users = cursor.fetchall()

        print(f"Real user assignments (excluding standby): {len(real_users)}")
        for user in real_users:
            print(f"- Real user: {user.get('user_id', 'N/A')}")

        return real_users


def get_assigned_users_for_instance(instance_id: str):
    """Get list of users assigned to an instance (excluding standby entries)"""
    try:
        return _get_assigned_users_for_instance_transaction(instance_id)
    except Exception as e:
        print(f"Error getting assigned users for {instance_id}: {str(e)}")
        return []


def get_all_premium_instances_with_states():
    """Get all premium instances with their AWS states.

    Filters by environment prefix (ENV_PREFIX) to prevent cross-environment
    contamination (e.g., development Lambda discovering production instances).
    """
    ec2: "EC2Client" = boto3.client("ec2")
    env_prefix = EnvironmentConfig.get_env_prefix()
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


@with_transaction
def _count_active_premium_users_transaction(connection):
    """Count users with active premium assignments with transaction safety.

    Includes pending_release users because their instance and resources
    are still allocated during the grace period.
    """
    with connection.cursor() as cursor:
        # First count all assignments for debugging
        cursor.execute(
            "SELECT COUNT(*) as total_count, "
            "SUM(CASE WHEN is_standby = 1 THEN 1 ELSE 0 END) as standby_count "
            "FROM premium_user_assignments WHERE status IN ('active', %s)",
            (PremiumAssignment.PENDING_RELEASE,),
        )
        debug_result = cursor.fetchone()
        total_count = debug_result["total_count"] if debug_result else 0
        standby_count = debug_result["standby_count"] if debug_result else 0

        # Count only real user assignments (exclude standby)
        cursor.execute(
            "SELECT COUNT(*) as count FROM premium_user_assignments "
            "WHERE status IN ('active', %s) AND is_standby = 0",
            (PremiumAssignment.PENDING_RELEASE,),
        )
        result = cursor.fetchone()
        real_user_count = result["count"] if result else 0

        print(" User count analysis:")
        print(f"- Total active assignments: {total_count}")
        print(f"- Standby assignments: {standby_count}")
        print(f"- Real user assignments: {real_user_count}")

        return real_user_count


def count_active_premium_users():
    """Count users with active premium assignments (excluding standby entries)"""
    try:
        return _count_active_premium_users_transaction()
    except Exception as e:
        print(f" Error counting active premium users: {str(e)}")
        return 0


def count_total_premium_users():
    """Count total premium users with active subscriptions (for capacity planning)"""
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                # First try to query using subscription tables if they exist
                try:
                    cursor.execute(
                        """SELECT COUNT(DISTINCT su.user_id) as count
                           FROM subscription_users su
                           JOIN subscription_plans sp ON su.plan_id = sp.id
                           WHERE sp.name = 'Premium'
                           AND su.expiration > NOW()
                           AND su.sync_status = 'synced'"""
                    )
                    result = cursor.fetchone()
                    if result and result["count"] > 0:
                        total_premium = result["count"]
                        print(
                            f"Total premium subscribers (from subscription tables):"
                            f" {total_premium}"
                        )
                        return total_premium
                except Exception as subscription_error:
                    print(
                        f"Subscription tables query failed: {str(subscription_error)}"
                    )

                # Fallback: Try to count from premium_user_assignments
                # (real users, not standby)
                try:
                    cursor.execute(
                        """SELECT COUNT(DISTINCT user_id) as count
                           FROM premium_user_assignments
                           WHERE status = 'active' AND is_standby = 0"""
                    )
                    result = cursor.fetchone()
                    active_assignments = result["count"] if result else 0

                    # For capacity planning, assume at least some premium users
                    # aren't currently assigned
                    # Use active assignments as minimum, but add buffer for
                    # unassigned premium users
                    estimated_premium = max(
                        active_assignments, 1
                    )  # At least 1 for testing

                    print(
                        f"Estimated premium subscribers (from assignments): "
                        f"{estimated_premium} (based on {active_assignments} "
                        f"active assignments)"
                    )
                    return estimated_premium

                except Exception as assignment_error:
                    print(f"Assignment table query failed: {str(assignment_error)}")

                # Last resort fallback for development/testing
                print("All database queries failed, using development fallback")
                return DEFAULT_DEVELOPMENT_CAPACITY  # Conservative estimate

    except Exception as e:
        print(f"Error counting total premium users: {str(e)}")
        # Fallback to a reasonable default if we can't query the database
        return DEFAULT_DEVELOPMENT_CAPACITY


def get_dynamic_max_capacity():
    """Get dynamic maximum capacity based on premium subscribers with safety buffer"""
    # Get subscriber count for capacity planning
    total_premium_subscribers = count_total_premium_users()

    # Configuration - use existing environment variables where possible
    # Combine buffer and standby into one concept: "extra capacity"
    EXTRA_CAPACITY = int(
        os.environ.get("PREMIUM_EXTRA_CAPACITY", "2")
    )  # Extra instances beyond current subscribers for quick response + standby
    ABSOLUTE_MAX = int(os.environ.get("ABSOLUTE_MAX", "20"))  # Use existing variable

    # Get current standby count from existing table for information
    standby_count = get_standby_count()

    # Calculate dynamic max capacity
    # Logic: We need capacity for all premium subscribers + extra capacity for:
    #   - Quick response to new logins
    #   - Standby instances for fast assignment
    #   - Safety buffer for concurrent logins
    if total_premium_subscribers == 0:
        # Development/testing scenario - minimal capacity
        max_capacity = (
            DEFAULT_DEVELOPMENT_CAPACITY  # Allow testing with 2 running + 1 standby
        )
    else:
        # Production scenario - scale based on subscriber count
        max_capacity = min(total_premium_subscribers + EXTRA_CAPACITY, ABSOLUTE_MAX)

    print("Dynamic capacity calculation:")
    print(f"- Premium subscribers: {total_premium_subscribers}")
    print(f"- Extra capacity (buffer + standby): {EXTRA_CAPACITY}")
    print(f"- Current standby count: {standby_count}")
    print(f"- Calculated max capacity: {max_capacity}")
    print(f"- Absolute maximum: {ABSOLUTE_MAX}")
    calculated_capacity = (
        total_premium_subscribers + EXTRA_CAPACITY
        if total_premium_subscribers > 0
        else DEFAULT_DEVELOPMENT_CAPACITY
    )
    print(
        f"- Logic: {total_premium_subscribers} subscribers + "
        f"{EXTRA_CAPACITY} extra = {calculated_capacity} (capped at {ABSOLUTE_MAX})"
    )

    return max_capacity


def update_instance_state(user_id: int, new_state: str):
    """Update instance state for a user assignment"""
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """UPDATE premium_user_assignments
                       SET instance_state = %s, last_state_check = NOW()
                       WHERE user_id = %s""",
                    (new_state, user_id),
                )
        print(f"Updated instance state for user {user_id} to {new_state}")
    except Exception as e:
        print(f"Error updating instance state: {str(e)}")


# ===== SIMPLIFIED STANDBY POOL FUNCTIONS =====


def get_standby_count():
    """Get count of standby instances from existing assignments table"""
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT COUNT(*) as count FROM premium_user_assignments
                       WHERE is_standby = 1 AND status = 'active'"""
                )
                result = cursor.fetchone()
                return result["count"] if result else 0
    except Exception as e:
        print(f"Error getting standby count: {str(e)}")
        return 0


def is_creation_lock_held(lock_name: str) -> bool:
    """Check if a creation lock is currently held by another session.

    Fail-closed: returns True on error so callers assume a lock
    is held rather than risk duplicate creation.
    """
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT IS_FREE_LOCK(%s) as is_free",
                    (lock_name,),
                )
                result = cursor.fetchone()
                # IS_FREE_LOCK returns 1=free, 0=held, NULL=error
                # Treat NULL as held (fail-closed)
                is_held = result["is_free"] != 1
                if is_held:
                    print(f"Lock '{lock_name}' is held")
                return is_held
    except Exception as e:
        print(f"Error checking lock '{lock_name}': {e}")
        return True


def get_available_standby_instances():
    """Get standby instances from database (AWS state checked separately)"""
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT instance_id, standby_created_at
                        FROM premium_user_assignments
                       WHERE is_standby = 1 AND status = 'active'
                       ORDER BY standby_created_at ASC
                       """
                )
                db_standby_instances = cursor.fetchall()

        # Filter to only include instances that are actually stopped in AWS
        available_standby = []
        if db_standby_instances:
            # Get current AWS states for all standby instances
            standby_instance_ids = [
                inst["instance_id"] for inst in db_standby_instances
            ]
            ec2: "EC2Client" = boto3.client("ec2")

            try:
                response = ec2.describe_instances(InstanceIds=standby_instance_ids)
                aws_states = {}
                for reservation in response["Reservations"]:
                    for instance in reservation["Instances"]:
                        aws_states[instance["InstanceId"]] = instance["State"]["Name"]

                # Only return instances that are actually stopped in AWS
                for inst in db_standby_instances:
                    instance_id = inst["instance_id"]
                    if aws_states.get(instance_id) == InstanceState.STOPPED:
                        available_standby.append(inst)

                print(
                    f"Standby instances: {len(db_standby_instances)} in DB, "
                    f"{len(available_standby)} actually stopped in AWS"
                )

            except Exception as aws_error:
                print(f"Failed to check AWS states for standby instances: {aws_error}")
                # Fall back to database list if AWS check fails
                available_standby = db_standby_instances

        return available_standby

    except Exception as e:
        print(f"Error getting available standby instances: {str(e)}")
        return []


def register_orphaned_stopped_instances():
    """Register stopped premium instances that aren't in the standby pool database"""
    try:
        # Get all premium instances from AWS
        all_aws_instances = get_all_premium_instances_with_states()
        stopped_aws_instances = [
            i for i in all_aws_instances if i["state"] == InstanceState.STOPPED
        ]

        # Get existing standby instances from database
        existing_standby_instances = get_available_standby_instances()
        existing_standby_ids = {
            inst["instance_id"] for inst in existing_standby_instances
        }

        # Find orphaned stopped instances (in AWS but not in database)
        orphaned_instances = []
        for instance in stopped_aws_instances:
            instance_id = instance["instance_id"]
            if instance_id not in existing_standby_ids:
                # Check if this instance is already assigned to a user
                assigned_users = get_assigned_users_for_instance(instance_id)
                if not assigned_users:  # Truly orphaned
                    orphaned_instances.append(instance)

        print(
            f"Found {len(orphaned_instances)} orphaned stopped "
            f"instances to register as standby"
        )

        # Register each orphaned instance as standby
        registered_count = 0
        for instance in orphaned_instances:
            instance_id = instance["instance_id"]
            try:
                # Store as standby assignment with is_standby flag
                # Use NULL user_id for standby instances (no real user assigned)
                store_user_assignment(
                    user_id=None,
                    instance_id=instance_id,
                    target_group_arn=PremiumAssignment.STANDBY,
                    rule_arn=PremiumAssignment.STANDBY,
                    instance_state=InstanceState.STOPPED,
                    is_shared=False,
                    is_standby=True,
                )

                print(f"Registered orphaned instance {instance_id} as standby")
                registered_count += 1

            except Exception as e:
                print(f"Failed to register orphaned instance {instance_id}: {str(e)}")
                continue

        print(
            f"Successfully registered {registered_count} orphaned instances as standby"
        )
        return registered_count

    except Exception as e:
        print(f"Error registering orphaned stopped instances: {str(e)}")
        return 0


def create_running_instance():
    """Create a new instance and leave it running for immediate assignment"""
    ec2: "EC2Client" = boto3.client("ec2")

    try:
        # Get launch template ID and instance type from environment
        launch_template_id = get_required_env_var("PREMIUM_LAUNCH_TEMPLATE_ID")
        instance_type = get_required_env_var("PREMIUM_INSTANCE_TYPE")

        # Get subnet IDs from environment
        subnet_ids = get_required_env_var("SUBNET_IDS").split(",")

        # Try each subnet (different AZ) until one succeeds
        for i, subnet_id in enumerate(subnet_ids):
            try:
                print(
                    f"Attempting to launch instance in subnet "
                    f"{subnet_id} (attempt {i + 1}/{len(subnet_ids)})"
                )

                # Launch instance using the premium launch template
                env_label = EnvironmentConfig.get_environment_label()
                inst_name = PremiumInstanceConfig.get_instance_name()
                response = ec2.run_instances(
                    LaunchTemplate={
                        "LaunchTemplateId": launch_template_id,
                        "Version": "$Latest",
                    },
                    InstanceType=instance_type,
                    SubnetId=subnet_id,
                    MinCount=1,
                    MaxCount=1,
                    TagSpecifications=[
                        {
                            "ResourceType": "instance",
                            "Tags": [
                                {
                                    "Key": "Name",
                                    "Value": PremiumInstanceConfig.get_instance_name(),
                                },
                                {
                                    "Key": "Type",
                                    "Value": PremiumInstanceConfig.INSTANCE_TYPE_TAG,
                                },
                                {
                                    "Key": "Tier",
                                    "Value": PremiumInstanceConfig.INSTANCE_IDENTIFIER,
                                },
                                {
                                    "Key": "Service",
                                    "Value": PremiumInstanceConfig.SERVICE_TAG,
                                },
                                {
                                    "Key": "Environment",
                                    "Value": env_label,
                                },
                            ],
                        },
                        {
                            "ResourceType": "volume",
                            "Tags": [
                                {
                                    "Key": "Name",
                                    "Value": f"{inst_name}-vol",
                                },
                                {
                                    "Key": "Environment",
                                    "Value": env_label,
                                },
                            ],
                        },
                    ],
                )

                instance_id = response["Instances"][0]["InstanceId"]
                print(
                    f"Successfully created instance {instance_id} in subnet "
                    f"{subnet_id} (launching in background)"
                )
                return instance_id

            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                if error_code == "InsufficientInstanceCapacity":
                    print(
                        f"InsufficientInstanceCapacity in subnet {subnet_id}, "
                        f"trying next subnet..."
                    )
                    continue
                else:
                    # For other errors, don't retry - fail immediately
                    print(f"Non-capacity error ({error_code}), not retrying: {str(e)}")
                    raise

        # All subnets exhausted
        print(
            f"Failed to launch instance in all {len(subnet_ids)} "
            f"available subnets due to insufficient capacity"
        )
        return None

    except Exception as e:
        print(f"Error creating running instance: {str(e)}")
        return None


def _create_running_instances_locked(count: int) -> bool:
    """Create running instances under a distributed lock."""
    with distributed_lock(CREATE_RUNNING_LOCK) as acquired:
        if not acquired:
            print("Another Lambda is already creating " "running instances, skipping")
            return False

        try:
            created_any = False
            for i in range(count):
                instance_id = create_running_instance()
                if instance_id:
                    print(f"Created running instance " f"{instance_id}")
                    created_any = True
                else:
                    # No standby fallback here (nested lock)
                    print(
                        "Failed to create running instance, "
                        "standby replenishment deferred to "
                        "scheduled monitoring"
                    )
            return created_any
        except Exception as e:
            print(f"Error in locked running creation: {e}")
            return False


@with_transaction
def try_reserve_instance_transaction(
    connection, instance_id: str, user_id: int
) -> bool:
    """
    Try to reserve an instance for a user using database-level locking.
    Returns True if reservation successful, False if instance already reserved.
    """
    with connection.cursor() as cursor:
        # Lock the instance row to prevent concurrent reservations
        cursor.execute(
            """SELECT instance_id FROM premium_user_assignments
               WHERE instance_id = %s FOR UPDATE""",
            (instance_id,),
        )
        existing = cursor.fetchone()

        if existing:
            # Instance already has an assignment, cannot reserve
            print(f"Instance {instance_id} already reserved/assigned")
            return False

        # Create a temporary reservation using actual user_id
        cursor.execute(
            """INSERT INTO premium_user_assignments
               (user_id, instance_id, target_group_arn, alb_rule_arn,
                status, instance_state, is_shared, assignment_attempts,
                last_state_check)
               VALUES (%s, %s, %s, %s, %s, %s, 0, 1, NOW())
            """,
            (
                user_id,
                instance_id,
                PremiumAssignment.RESERVING,
                PremiumAssignment.RESERVING,
                PremiumAssignment.ACTIVE,
                InstanceState.LAUNCHING,
            ),
        )
        print(f"Reserved instance {instance_id} for user {user_id}")
        return True


def try_reserve_instance(instance_id: str, user_id: int) -> bool:
    """Try to reserve an instance for assignment"""
    try:
        return try_reserve_instance_transaction(instance_id, user_id)
    except Exception as e:
        print(f"Failed to reserve instance {instance_id}: {str(e)}")
        return False


def release_instance_reservation(instance_id: str, user_id: int):
    """Release a reservation if assignment fails"""
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """DELETE FROM premium_user_assignments
                       WHERE instance_id = %s
                       AND user_id = %s
                       AND target_group_arn = %s""",
                    (instance_id, user_id, PremiumAssignment.RESERVING),
                )
                connection.commit()
                print(f"Released reservation for instance {instance_id}")
    except Exception as e:
        print(f"Error releasing reservation: {str(e)}")


@with_transaction
def try_reserve_instance_for_migration_transaction(
    connection, instance_id: str, user_id: int
) -> bool:
    """
    Reserve instance for migration using database-level locking.
    Returns True if successful, False if instance already has users.
    """
    with connection.cursor() as cursor:
        # Lock ALL rows for this instance
        cursor.execute(
            """SELECT user_id, is_standby FROM premium_user_assignments
               WHERE instance_id = %s FOR UPDATE""",
            (instance_id,),
        )
        existing = cursor.fetchall()

        # Check for real user assignments (not standby)
        real_users = [
            a
            for a in existing
            if a.get("is_standby", 0) == 0 and a.get("user_id") is not None
        ]

        if real_users:
            print(f"Instance {instance_id} already has {len(real_users)} user(s)")
            return False

        # Clean up standby entries
        cursor.execute(
            "DELETE FROM premium_user_assignments "
            "WHERE instance_id = %s AND is_standby = 1",
            (instance_id,),
        )

        print(f"Reserved instance {instance_id} " f"for migration of user {user_id}")
        return True


def try_reserve_instance_for_migration(instance_id: str, user_id: int) -> bool:
    """Wrapper with error handling"""
    try:
        return try_reserve_instance_for_migration_transaction(instance_id, user_id)
    except Exception as e:
        print(f"Failed to reserve instance {instance_id} for migration: {str(e)}")
        return False


def create_and_stop_standby_instance():
    """
    Create instance and immediately stop it for standby use.
    Uses database-level locking to prevent concurrent Lambda
    executions from creating duplicate standbys.
    """
    ec2: "EC2Client" = boto3.client("ec2")

    with distributed_lock(CREATE_STANDBY_LOCK) as acquired:
        if not acquired:
            print("Another Lambda is already creating a " "standby instance, skipping")
            return None

        try:
            standby_count = get_standby_count()
            standby_pool_size = int(os.environ.get("PREMIUM_STANDBY_POOL_SIZE", "1"))

            if standby_count >= standby_pool_size:
                print(
                    f"Standby pool already full "
                    f"({standby_count}/{standby_pool_size})"
                    f", another Lambda already created one"
                )
                return None

            print(
                f"Standby pool has capacity "
                f"({standby_count}/{standby_pool_size}), "
                f"proceeding with creation"
            )

            launch_template_id = get_required_env_var("PREMIUM_LAUNCH_TEMPLATE_ID")
            instance_type = get_required_env_var("PREMIUM_INSTANCE_TYPE")
            subnet_ids = get_required_env_var("SUBNET_IDS").split(",")

            instance_id = None
            for i, subnet_id in enumerate(subnet_ids):
                try:
                    print(
                        f"Attempting to launch standby "
                        f"instance in subnet {subnet_id} "
                        f"(attempt {i + 1}"
                        f"/{len(subnet_ids)})"
                    )

                    env_label = EnvironmentConfig.get_environment_label()
                    env_prefix = EnvironmentConfig.get_env_prefix()
                    inst_id = PremiumInstanceConfig.INSTANCE_IDENTIFIER
                    response = ec2.run_instances(
                        LaunchTemplate={
                            "LaunchTemplateId": launch_template_id,
                            "Version": "$Latest",
                        },
                        InstanceType=instance_type,
                        SubnetId=subnet_id,
                        MinCount=1,
                        MaxCount=1,
                        TagSpecifications=[
                            {
                                "ResourceType": "instance",
                                "Tags": [
                                    {
                                        "Key": "Name",
                                        "Value": (
                                            f"{env_prefix}-" f"{inst_id}-standby"
                                        ),
                                    },
                                    {
                                        "Key": "Type",
                                        "Value": (
                                            PremiumInstanceConfig.INSTANCE_TYPE_TAG
                                        ),
                                    },
                                    {
                                        "Key": "Tier",
                                        "Value": inst_id,
                                    },
                                    {
                                        "Key": "Service",
                                        "Value": (PremiumInstanceConfig.SERVICE_TAG),
                                    },
                                    {
                                        "Key": "Environment",
                                        "Value": env_label,
                                    },
                                ],
                            },
                            {
                                "ResourceType": "volume",
                                "Tags": [
                                    {
                                        "Key": "Name",
                                        "Value": (
                                            f"{env_prefix}-" f"{inst_id}-standby-vol"
                                        ),
                                    },
                                    {
                                        "Key": "Environment",
                                        "Value": env_label,
                                    },
                                ],
                            },
                        ],
                    )

                    instance_id = response["Instances"][0]["InstanceId"]
                    print(
                        f"Successfully created standby "
                        f"instance {instance_id} in "
                        f"subnet {subnet_id}, "
                        f"waiting to stop..."
                    )
                    break

                except ClientError as e:
                    error_code = e.response["Error"]["Code"]
                    if error_code == ("InsufficientInstanceCapacity"):
                        print(
                            f"InsufficientInstanceCapacity"
                            f" in subnet {subnet_id}, "
                            f"trying next subnet..."
                        )
                        continue
                    else:
                        print(
                            f"Non-capacity error "
                            f"({error_code}), "
                            f"not retrying: {e}"
                        )
                        raise

            if not instance_id:
                raise Exception(
                    f"Failed to launch standby instance "
                    f"in all {len(subnet_ids)} available "
                    f"subnets due to insufficient capacity"
                )

            waiter = ec2.get_waiter("instance_running")
            waiter.wait(
                InstanceIds=[instance_id],
                WaiterConfig={
                    "Delay": 15,
                    "MaxAttempts": 40,
                },
            )

            ec2.stop_instances(InstanceIds=[instance_id])

            waiter = ec2.get_waiter("instance_stopped")
            waiter.wait(
                InstanceIds=[instance_id],
                WaiterConfig={
                    "Delay": 15,
                    "MaxAttempts": 20,
                },
            )

            store_user_assignment(
                user_id=None,
                instance_id=instance_id,
                target_group_arn=PremiumAssignment.STANDBY,
                rule_arn=PremiumAssignment.STANDBY,
                instance_state=InstanceState.STOPPED,
                is_shared=False,
                is_standby=True,
            )

            print(
                f"Successfully created and stopped " f"standby instance {instance_id}"
            )
            return instance_id

        except Exception as e:
            print(f"Error creating standby instance: " f"{str(e)}")
            return None


def start_standby_instance(instance_id: str):
    """Start a stopped standby instance and prepare for user assignment"""
    ec2: "EC2Client" = boto3.client("ec2")

    try:
        print(f"Starting standby instance {instance_id}")

        # Start the instance
        ec2.start_instances(InstanceIds=[instance_id])

        # Wait for running state
        waiter = ec2.get_waiter("instance_running")
        waiter.wait(
            InstanceIds=[instance_id],
            WaiterConfig={"Delay": 5, "MaxAttempts": 24},
        )

        # Checkpoint clear is handled by the ecs-clear-checkpoint.service
        # systemd unit (Before=ecs.service) on every boot; no SSM action
        # needed here.

        if not check_instance_readiness_with_retry(
            instance_id,
            max_wait_seconds=STANDBY_READINESS_TIMEOUT_SECONDS,
            retry_interval=STANDBY_READINESS_RETRY_INTERVAL_SECONDS,
        ):
            print(
                f"Standby instance {instance_id} started but ECS task "
                f"not ready after 120s"
            )
            return False

        # The only row for this instance now is its standby placeholder
        # (is_standby = 1); scope the update to it so a stray regular
        # assignment row for the same instance can never be touched.
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """UPDATE premium_user_assignments
                       SET instance_state = %s,
                           last_state_check = NOW()
                       WHERE instance_id = %s AND is_standby = 1""",
                    (InstanceState.RUNNING, instance_id),
                )
                connection.commit()  # Commit the state update

        print(f"Standby instance {instance_id} started successfully")
        return True

    except Exception as e:
        print(f"Error starting standby instance {instance_id}: {str(e)}")
        return False


def get_premium_user_status(user_id: int) -> Dict[str, Any]:
    """Get premium user assignment status"""
    print(f"get_premium_user_status called for user_id={user_id}")
    try:
        print(" Opening database connection...")
        with get_db_connection() as connection:
            print("Database connection established")
            with connection.cursor() as cursor:
                print(f"Executing query for user_id={user_id}")
                cursor.execute(
                    """SELECT instance_id, target_group_arn, alb_rule_arn, status,
                    assigned_at, is_shared FROM premium_user_assignments
                    WHERE user_id = %s""",
                    (user_id,),
                )
                assignment = cursor.fetchone()
                print(f"Query result: {assignment}")

                if not assignment:
                    print(f" No assignment found for user {user_id}")
                    return {
                        "statusCode": 404,
                        "body": json.dumps(
                            {"error": f"No premium assignment found for user {user_id}"}
                        ),
                    }

                # Autoscaling pool is a temporary fallback — return 404
                # so the frontend calls /premium/assign which runs the
                # full assignment logic and can find a dedicated instance.
                instance_id = assignment["instance_id"]
                if instance_id == PremiumAssignment.AUTOSCALING_POOL:
                    print(
                        f"User {user_id} is on autoscaling-pool "
                        f"(temporary) — returning 404 to trigger "
                        f"fresh assignment"
                    )
                    return {
                        "statusCode": 404,
                        "body": json.dumps(
                            {
                                "error": (
                                    f"No premium assignment found "
                                    f"for user {user_id}"
                                )
                            }
                        ),
                    }

                # Verify instance liveness for active assignments
                if assignment["status"] == PremiumAssignment.ACTIVE:
                    try:
                        ec2: "EC2Client" = boto3.client("ec2")
                        resp = ec2.describe_instances(InstanceIds=[instance_id])
                        reservations = resp.get("Reservations", [])
                        if reservations and reservations[0].get("Instances"):
                            ec2_state = reservations[0]["Instances"][0]["State"]["Name"]
                        else:
                            ec2_state = None
                    except ClientError:
                        ec2_state = None

                    if ec2_state in (
                        InstanceState.TERMINATED,
                        InstanceState.SHUTTING_DOWN,
                        InstanceState.STOPPED,
                        InstanceState.STOPPING,
                        None,
                    ):
                        print(
                            f"Instance {instance_id} is "
                            f"{ec2_state or 'not found'} — removing "
                            f"stale active assignment for user {user_id}"
                        )
                        try:
                            remove_user_assignment(user_id)
                        except Exception as cleanup_err:
                            print(
                                f"Warning: cleanup failed for user "
                                f"{user_id}: {cleanup_err}"
                            )
                        return {
                            "statusCode": 404,
                            "body": json.dumps(
                                {
                                    "error": (
                                        f"No premium assignment found "
                                        f"for user {user_id}"
                                    )
                                }
                            ),
                        }

                # Restore pending_release on status check (user refreshed)
                if assignment["status"] == PremiumAssignment.PENDING_RELEASE:
                    try:
                        restored = restore_pending_release(user_id)
                        if restored:
                            assignment = restored
                            assignment["status"] = PremiumAssignment.ACTIVE
                            print(
                                f"Restored pending_release on status check "
                                f"for user {user_id}"
                            )
                        else:
                            # Stale assignment was removed (instance terminated)
                            # Return 404 so frontend triggers a fresh assign
                            print(
                                f"Stale assignment removed for user {user_id} "
                                f" - returning 404 for fresh assignment"
                            )
                            return {
                                "statusCode": 404,
                                "body": json.dumps(
                                    {
                                        "error": (
                                            f"No premium assignment found "
                                            f"for user {user_id}"
                                        )
                                    }
                                ),
                            }
                    except Exception as restore_err:
                        print(
                            f"Failed to restore pending_release: " f"{str(restore_err)}"
                        )

                print(
                    f"Found assignment - "
                    f"instance_id={assignment['instance_id']}, "
                    f"status={assignment['status']}, "
                    f"is_shared={assignment['is_shared']}"
                )
                return {
                    "statusCode": 200,
                    "body": json.dumps(
                        {
                            "user_id": user_id,
                            "instance_id": assignment["instance_id"],
                            "target_group_arn": assignment["target_group_arn"],
                            "alb_rule_arn": assignment["alb_rule_arn"],
                            "status": assignment["status"],
                            "assigned_at": (
                                assignment["assigned_at"].isoformat()
                                if assignment["assigned_at"]
                                else None
                            ),
                            "is_shared": bool(assignment["is_shared"]),
                        }
                    ),
                }

    except Exception as e:
        print(f"Error getting premium user status: {str(e)}")
        print(f"Full exception details: {type(e).__name__}: {e}")
        import traceback

        print(f"Traceback: {traceback.format_exc()}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)}),
        }


def publish_premium_metrics(
    active_users: int,
    idle_users: int,
    running_instances: int,
    idle_instances: int,
    tg_port_drift_detected: int = 0,
    tg_port_drift_fixed: int = 0,
    tg_healed_missing: int = 0,
) -> None:
    """
    Publish premium tier monitoring metrics to CloudWatch.

    Metrics published to namespace OptiNiSt/PremiumManager/{env_prefix}:
    - ActivePremiumUsers: Count of users with active assignments
    - IdlePremiumUsers: Count of users with inactive/no assignments
    - RunningInstances: Count of running EC2 instances
    - IdleInstances: Count of instances with no assigned users
    - TargetGroupPortDriftDetected: drifting per-user TGs observed this cycle
    - TargetGroupPortDriftFixed: drifting per-user TGs converged this cycle
    - HealedMissingTargetGroup: stranded rows (TG gone) dropped for reprovision
      this cycle — a non-zero value means a #766-class strand actually occurred
    """
    cloudwatch: "CloudWatchClient" = boto3.client("cloudwatch")

    try:
        cloudwatch.put_metric_data(
            Namespace=(
                "OptiNiSt/PremiumManager/" f"{PremiumInstanceConfig.get_env_prefix()}"
            ),
            MetricData=[
                {
                    "MetricName": "ActivePremiumUsers",
                    "Value": active_users,
                    "Unit": "Count",
                    "Timestamp": datetime.now(timezone.utc),
                },
                {
                    "MetricName": "IdlePremiumUsers",
                    "Value": idle_users,
                    "Unit": "Count",
                    "Timestamp": datetime.now(timezone.utc),
                },
                {
                    "MetricName": "RunningInstances",
                    "Value": running_instances,
                    "Unit": "Count",
                    "Timestamp": datetime.now(timezone.utc),
                },
                {
                    "MetricName": "IdleInstances",
                    "Value": idle_instances,
                    "Unit": "Count",
                    "Timestamp": datetime.now(timezone.utc),
                },
                {
                    "MetricName": "TargetGroupPortDriftDetected",
                    "Value": tg_port_drift_detected,
                    "Unit": "Count",
                    "Timestamp": datetime.now(timezone.utc),
                },
                {
                    "MetricName": "TargetGroupPortDriftFixed",
                    "Value": tg_port_drift_fixed,
                    "Unit": "Count",
                    "Timestamp": datetime.now(timezone.utc),
                },
                {
                    "MetricName": "HealedMissingTargetGroup",
                    "Value": tg_healed_missing,
                    "Unit": "Count",
                    "Timestamp": datetime.now(timezone.utc),
                },
            ],
        )
        print(
            f"Published metrics: active_users={active_users}, idle_users={idle_users}, "
            f"running_instances={running_instances}, idle_instances={idle_instances}, "
            f"tg_port_drift_detected={tg_port_drift_detected}, "
            f"tg_port_drift_fixed={tg_port_drift_fixed}, "
            f"tg_healed_missing={tg_healed_missing}"
        )

    except Exception as e:
        print(f"Error publishing metrics: {str(e)}")


def handle_scheduled_monitoring(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Handle scheduled monitoring events for premium tier.

    Uses distributed_lock (MySQL GET_LOCK) for mutual exclusion so a
    crashed holder releases on connection teardown instead of blocking
    every subsequent run for ~15 minutes.

    Responsibilities:
    - Acquire the scaling lock (skip if another invocation holds it)
    - Call scale_down_if_possible() to stop instances with NO assigned users
    - Publish monitoring metrics to CloudWatch
    - Update ECS service desired count

    Triggered every 15 minutes by CloudWatch Events.
    Coordinates with premium_cleanup lambda (runs hourly).

    Returns:
        Dictionary with monitoring and scaling results
    """
    print(f"Premium monitoring triggered by event: {json.dumps(event)}")

    with distributed_lock(PREMIUM_SCALING_LOCK, timeout=0) as acquired:
        if not acquired:
            print("Scaling already in progress, skipping this run")
            return {
                "statusCode": 200,
                "body": json.dumps(
                    {
                        "status": "skipped",
                        "message": "Scaling operation already in progress",
                    }
                ),
            }

        try:
            # 3. Get current state
            active_users = count_active_premium_users()
            total_premium_users = count_total_premium_users()
            all_instances = get_all_premium_instances_with_states()
            running_instances = [
                i for i in all_instances if i["state"] == InstanceState.RUNNING
            ]

            # Count instances with no assigned users
            idle_instances = 0
            for instance in running_instances:
                instance_id = instance["instance_id"]
                assigned_users = get_assigned_users_for_instance(instance_id)
                if not assigned_users:
                    idle_instances += 1

            print(
                f"Monitoring: {active_users} active users, "
                f"{total_premium_users} total users, "
                f"{len(running_instances)} running instances, "
                f"{idle_instances} idle instances"
            )

            # 5. Call existing scaling logic to stop idle instances
            scale_down_if_possible()

            # 6. Update ECS service desired count to match running instances
            update_premium_service_desired_count()

            # 7. Cleanup failed standby instances
            # (remove DB entries for terminated instances)
            cleanup_failed_standby_instances()

            # 8a. Register any stopped instances that are missing
            # from the database (e.g. store_user_assignment failed
            # after ec2.stop_instances, or a waiter timed out in
            # convert_running_instance_to_standby).
            register_orphaned_stopped_instances()

            # 8b. Terminate stopped standby instances older than
            # PREMIUM_STOPPED_MAX_AGE_HOURS
            terminate_aged_stopped_instances()

            # 9. Converge standby pool toward target size. Trimming excess
            # was always here; the replenish branch is the backstop for the
            # best-effort async create on the assign path, so a dropped or
            # lock-skipped invocation can't leave the pool depleted.
            standby_count = get_standby_count()
            standby_pool_size = int(os.environ.get("PREMIUM_STANDBY_POOL_SIZE", "1"))
            if standby_count > standby_pool_size:
                excess = standby_count - standby_pool_size
                print(f"Standby pool has {excess} excess instances, trimming")
                cleanup_excess_standby_instances(excess)
            elif standby_count < standby_pool_size:
                print(
                    f"Standby pool below target "
                    f"({standby_count}/{standby_pool_size}), replenishing"
                )
                invoke_standby_replenishment_async()

            # 10a. Finalize expired pending_release assignments
            try:
                expired = finalize_expired_pending_releases()
                if expired:
                    print(
                        f"Finalizing {len(expired)} expired "
                        f"pending_release assignments"
                    )
                    for assignment in expired:
                        rule_arn = (
                            assignment.get("alb_rule_arn") or ""
                        ).strip() or None
                        tg_arn = (
                            assignment.get("target_group_arn") or ""
                        ).strip() or None
                        teardown_errors = _teardown_alb_resources(
                            assignment["user_id"], rule_arn, tg_arn
                        )
                        if teardown_errors:
                            print(
                                f"Teardown warnings for user "
                                f"{assignment['user_id']}: {teardown_errors}"
                            )
            except Exception:
                print("WARNING: finalize_expired_pending_releases() failed")
                import traceback

                traceback.print_exc()

            # 10b. Cleanup ghost ECS container instance registrations
            # (deregister container instances where EC2 is stopped/terminated)
            cleanup_ghost_ecs_registrations()

            # 10c. Reconcile per-user TG host ports against actual container
            # bindings. Closes the same-instance task-restart drift class.
            # Idempotent under fixed host ports; non-trivial only when
            # ephemeral ports are re-introduced.
            try:
                reconcile_summary = reconcile_premium_target_group_ports()
            except Exception:
                print("WARNING: reconcile_premium_target_group_ports() failed")
                import traceback

                traceback.print_exc()
                reconcile_summary = {"errors": 1}

            # 10d. Publish metrics to CloudWatch (after reconcile so drift
            # counters are populated in the same cycle).
            publish_premium_metrics(
                active_users=active_users,
                idle_users=total_premium_users - active_users,
                running_instances=len(running_instances),
                idle_instances=idle_instances,
                tg_port_drift_detected=reconcile_summary.get("drift_detected", 0),
                tg_port_drift_fixed=reconcile_summary.get("drift_fixed", 0),
                tg_healed_missing=reconcile_summary.get("healed_missing_tg", 0),
            )

            # 11. Stop orphaned EC2 instances not in ECS cluster
            cleanup_orphaned_ec2_instances()

            # 12. Optimize shared instances (safety net)
            try:
                try:
                    fix_result = fix_incorrect_is_shared_flags()
                    if fix_result.get("fixed_count", 0) > 0:
                        print(
                            f"Fixed {fix_result['fixed_count']} stale is_shared flags"
                        )
                except Exception:
                    print("WARNING: fix_incorrect_is_shared_flags() failed")
                    import traceback

                    traceback.print_exc()

                shared_result = process_shared_instance_optimization()
                shared_migrations = shared_result.get("migrations_performed", 0)
                shared_found = shared_result.get("shared_instances_found", 0)
                if shared_found > 0 or shared_migrations > 0:
                    print(
                        f"Shared optimization: "
                        f"{shared_migrations} migrations, "
                        f"{shared_found} shared instances"
                    )
                if shared_found > 0 and shared_migrations == 0:
                    print(
                        "Shared users found but no instances "
                        "ready, triggering async migration..."
                    )
                    invoke_migration_async()
            except Exception as shared_error:
                print(f"Shared optimization error: " f"{str(shared_error)}")

            return {
                "statusCode": 200,
                "body": json.dumps(
                    {
                        "status": "success",
                        "active_users": active_users,
                        "running_instances": len(running_instances),
                        "idle_instances": idle_instances,
                    }
                ),
            }

        except Exception as e:
            error_msg = f"Error in scheduled monitoring: {str(e)}"
            print(error_msg)
            import traceback

            traceback.print_exc()

            return {
                "statusCode": 500,
                "body": json.dumps({"status": "error", "error": error_msg}),
            }


def _handle_migrate_shared_users(event):
    """Run migration loop under a distributed lock."""
    with distributed_lock(MIGRATE_USERS_LOCK) as acquired:
        if not acquired:
            print("Another Lambda is already running " "migrations, skipping")
            return {
                "statusCode": 200,
                "body": json.dumps({"message": "Migration skipped - lock held"}),
            }

        max_wait_seconds = event.get("max_wait_seconds", 600)
        retry_interval = event.get("retry_interval", 10)

        print(
            f"Async migration triggered, will retry "
            f"every {retry_interval}s "
            f"for up to {max_wait_seconds}s..."
        )

        migrations_performed = 0
        elapsed = 0
        migration_result: Dict[str, Any] = {}

        # Let instances boot before first attempt
        print(
            f"Waiting {MIGRATION_INITIAL_DELAY_SECONDS}s " f"for instances to boot..."
        )
        time.sleep(MIGRATION_INITIAL_DELAY_SECONDS)
        elapsed += MIGRATION_INITIAL_DELAY_SECONDS

        while elapsed < max_wait_seconds:
            update_premium_service_desired_count()

            migration_result = process_shared_instance_optimization()
            migrations_performed = migration_result.get("migrations_performed", 0)

            print(
                f"Migration attempt at {elapsed}s: "
                f"{migrations_performed} migrations"
            )

            autoscaling_users = get_assigned_users_for_instance(
                PremiumAssignment.AUTOSCALING_POOL
            )
            remaining_autoscaling = len(autoscaling_users)
            remaining_shared = migration_result.get("shared_instances_found", 0)

            if migrations_performed > 0:
                print(
                    f"Migrated {migrations_performed} users, "
                    f"{remaining_autoscaling} on autoscaling "
                    f"pool, {remaining_shared} shared "
                    f"instances remaining"
                )

            if remaining_autoscaling == 0 and remaining_shared == 0:
                print(f"Migration completed after {elapsed}s" f" - all users migrated")
                return {
                    "statusCode": 200,
                    "body": json.dumps(
                        {
                            "message": (f"Migration completed " f"after {elapsed}s"),
                            "result": migration_result,
                        }
                    ),
                }

            time.sleep(retry_interval)
            elapsed += retry_interval

        print(f"Migration timeout after {elapsed}s, " f"no instances ready")
        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": ("Migration timeout - " "instances not ready yet"),
                    "result": migration_result,
                }
            ),
        }


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Handle premium user assignment lifecycle events and scheduled cleanup
    """

    print(f"Premium manager received event: {json.dumps(event)}")
    print(f"Lambda context: {context.function_name if context else 'No context'}")

    try:
        # Handle async standby replenishment invocation
        if event.get("action") == "create_standby":
            print("Running async standby replenishment...")
            instance_id = create_and_stop_standby_instance()
            return {
                "statusCode": 200,
                "body": json.dumps({"created_instance_id": instance_id}),
            }

        # Handle async migration invocation
        if event.get("action") == "migrate_shared_users":
            return _handle_migrate_shared_users(event)

        # Handle fix_shared_flags action (one-time data cleanup)
        if event.get("action") == "fix_shared_flags":
            print("Running is_shared flag cleanup...")
            result = fix_incorrect_is_shared_flags()
            return {"statusCode": 200, "body": json.dumps(result)}

        # Handle cleanup_all_dynamic action (called by dev scheduler before stop)
        if event.get("action") == "cleanup_all_dynamic":
            print("Cleaning up all dynamic premium instances...")
            result = cleanup_all_dynamic_instances(
                base_instance_ids=event.get("base_instance_ids", [])
            )
            return {"statusCode": 200, "body": json.dumps(result)}

        # Handle scheduled monitoring events
        if (
            event.get("source") == "aws.events"
            and event.get("detail-type") == "Scheduled Event"
        ):
            print("Scheduled monitoring event received")
            return handle_scheduled_monitoring(event, context)

        # Handle API Gateway events
        http_method = event.get("httpMethod", "POST")

        if http_method == "GET":
            # Handle status request (GET /premium/status?user_id=xxx)
            query_params = event.get("queryStringParameters") or {}
            user_uid = query_params.get("user_id")

            if not user_uid:
                return {
                    "statusCode": 400,
                    "body": json.dumps({"error": "Missing user_id query parameter"}),
                }

            # Convert Firebase UID to numeric database ID
            try:
                with get_db_connection() as conn:
                    user_id = get_user_id_from_uid(conn, user_uid)
            except ValueError as e:
                return {
                    "statusCode": 404,
                    "body": json.dumps({"error": str(e)}),
                }

            return get_premium_user_status(user_id)

        else:
            # Handle POST requests (assign/release)
            # Parse body from API Gateway
            body = event.get("body", "{}")
            if isinstance(body, str):
                try:
                    body_data = json.loads(body)
                except json.JSONDecodeError:
                    body_data = {}
            else:
                body_data = body or {}

            action = body_data.get("action")
            user_uid = body_data.get("user_id")

            if not action or not user_uid:
                return {
                    "statusCode": 400,
                    "body": json.dumps({"error": "Missing action or user_id"}),
                }

            # Convert Firebase UID to numeric database ID
            try:
                with get_db_connection() as conn:
                    user_id = get_user_id_from_uid(conn, user_uid)
            except ValueError as e:
                return {
                    "statusCode": 404,
                    "body": json.dumps({"error": str(e)}),
                }

            if action == PremiumAssignment.ACTION_ASSIGN:
                return assign_premium_user(user_id, body_data, user_uid)
            elif action == PremiumAssignment.ACTION_RELEASE:
                hard = body_data.get("hard", False)
                return release_premium_user(user_id, hard=hard)
            elif action == PremiumAssignment.ACTION_UPDATE_ACTIVITY:
                return handle_activity_update(user_id)
            else:
                return {
                    "statusCode": 400,
                    "body": json.dumps({"error": f"Unknown action: {action}"}),
                }

    except Exception as e:
        print(f"Error processing event: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}


def cleanup_duplicate_rules_for_routing_id(listener_arn: str, routing_id: str) -> int:
    """
    Find and delete any existing ALB rules with the same routing_id.

    This prevents duplicate rules from accumulating when a user is reassigned.
    The routing_id is deterministic (HMAC of user_id), so all rules for the
    same user will have the same routing_id.

    Args:
        listener_arn: ALB listener ARN to check
        routing_id: The routing ID to search for

    Returns:
        Number of rules deleted
    """
    elbv2: "ElasticLoadBalancingv2Client" = boto3.client("elbv2")

    try:
        # Get all existing rules for this listener
        response = elbv2.describe_rules(ListenerArn=listener_arn)
        rules = response.get("Rules", [])

        rules_deleted = 0
        for rule in rules:
            if rule.get("Priority") == "default":
                continue

            # Check if this rule has the matching routing_id
            conditions = rule.get("Conditions", [])
            for condition in conditions:
                http_header_config = condition.get("HttpHeaderConfig", {})
                if (
                    http_header_config.get("HttpHeaderName")
                    == RoutingHeaders.ROUTING_ID
                ):
                    values = http_header_config.get("Values", [])
                    if routing_id in values:
                        # Found a rule with this routing_id - delete it
                        rule_arn = rule["RuleArn"]
                        try:
                            print(
                                f"Deleting existing rule for routing_id "
                                f"{routing_id[:8]}...: {rule_arn}"
                            )
                            elbv2.delete_rule(RuleArn=rule_arn)
                            rules_deleted += 1

                            # Also try to delete the associated target group
                            for action in rule.get("Actions", []):
                                if action.get("Type") == "forward":
                                    tg_arn = action.get("TargetGroupArn")
                                    autoscaling_tg = os.environ.get(
                                        "AUTOSCALING_TARGET_GROUP_ARN"
                                    )
                                    if tg_arn and tg_arn != autoscaling_tg:
                                        try:
                                            _delete_premium_tg_unhealthy_alarm(tg_arn)
                                            elbv2.delete_target_group(
                                                TargetGroupArn=tg_arn
                                            )
                                            print(
                                                f"Deleted associated "
                                                f"target group: {tg_arn}"
                                            )
                                        except Exception as tg_error:
                                            # Target group might be
                                            # in use or already deleted
                                            print(
                                                f"Could not delete target group "
                                                f"{tg_arn}: {tg_error}"
                                            )
                        except Exception as delete_error:
                            print(f"Failed to delete rule {rule_arn}: {delete_error}")
                        break  # Move to next rule

        if rules_deleted > 0:
            print(
                f"Cleaned up {rules_deleted} duplicate rule(s) "
                f"for routing_id {routing_id[:8]}..."
            )

        return rules_deleted

    except Exception as e:
        print(f"Error cleaning up duplicate rules: {str(e)}")
        return 0


def target_group_exists(target_group_arn: str) -> bool:
    """
    Check if a target group exists in AWS.

    Args:
        target_group_arn: The ARN of the target group to check

    Returns:
        True if target group exists, False otherwise
    """
    if not target_group_arn or not target_group_arn.strip():
        return False

    elbv2: "ElasticLoadBalancingv2Client" = boto3.client("elbv2")

    try:
        elbv2.describe_target_groups(TargetGroupArns=[target_group_arn])
        return True
    except elbv2.exceptions.TargetGroupNotFoundException:
        return False
    except Exception as e:
        if "TargetGroupNotFound" in str(e):
            return False
        print(f"Error checking target group {target_group_arn}: {e}")
        raise


def _enable_sticky_sessions(
    elbv2: "ElasticLoadBalancingv2Client", target_group_arn: str
) -> None:
    """Enable ALB sticky sessions on a target group (matches compute.tf main TG)."""
    try:
        elbv2.modify_target_group_attributes(
            TargetGroupArn=target_group_arn,
            Attributes=[
                {"Key": "stickiness.enabled", "Value": "true"},
                {"Key": "stickiness.type", "Value": "lb_cookie"},
                {
                    "Key": "stickiness.lb_cookie.duration_seconds",
                    "Value": str(STICKY_SESSION_DURATION_SECONDS),
                },
            ],
        )
    except Exception as e:
        print(f"WARNING: Failed to enable sticky sessions on {target_group_arn}: {e}")


def _alb_arn_suffix(alb_arn: str) -> str:
    """ALB ARN -> CloudWatch LoadBalancer dimension value (app/name/hash)."""
    marker = ":loadbalancer/"
    idx = alb_arn.find(marker)
    return alb_arn[idx + len(marker) :] if idx != -1 else alb_arn


def _tg_arn_suffix(tg_arn: str) -> str:
    """Target group ARN -> CloudWatch TargetGroup dimension value."""
    idx = tg_arn.find(":targetgroup/")
    return tg_arn[idx + 1 :] if idx != -1 else tg_arn


_cloudwatch_client: Optional["CloudWatchClient"] = None


def _get_cloudwatch_client() -> "CloudWatchClient":
    """Reuse one CloudWatch client across calls within a warm Lambda container."""
    global _cloudwatch_client
    if _cloudwatch_client is None:
        _cloudwatch_client = boto3.client("cloudwatch")
    return _cloudwatch_client


# Mirrored in premium_cleanup.py — keep both in sync if the alarm naming changes.
def _premium_tg_alarm_name(tg_arn: str) -> str | None:
    """Derive the per-TG alarm name (e.g. subscr-premium-123-tg-unhealthy-hosts)."""
    parts = _tg_arn_suffix(tg_arn).split("/")
    if len(parts) < 2:
        return None
    return f"{EnvironmentConfig.get_env_prefix()}-{parts[1]}-unhealthy-hosts"


def _ensure_premium_tg_unhealthy_alarm(tg_arn: str) -> None:
    """Create/update an UnHealthyHostCount alarm for a premium user's TG.

    Best-effort: a failure here must not block provisioning.
    """
    alb_arn = os.environ.get("ALB_ARN")
    alarm_name = _premium_tg_alarm_name(tg_arn)
    if not alb_arn or not alarm_name:
        return
    topic_arn = os.environ.get("CRITICAL_ALERTS_TOPIC_ARN", "")
    actions = [topic_arn] if topic_arn else []
    try:
        cloudwatch = _get_cloudwatch_client()
        cloudwatch.put_metric_alarm(
            AlarmName=alarm_name,
            ComparisonOperator="GreaterThanThreshold",
            EvaluationPeriods=2,
            MetricName="UnHealthyHostCount",
            Namespace="AWS/ApplicationELB",
            Period=60,
            Statistic="Maximum",
            Threshold=0,
            AlarmDescription=(
                "Premium TG has unhealthy targets — the user's dedicated "
                "instance may be failing health checks."
            ),
            AlarmActions=actions,
            # Ephemeral per-assign/release alarm; OK actions would page a
            # "recovered" notice to the critical topic on every recreate.
            OKActions=[],
            TreatMissingData="missing",
            Dimensions=[
                {"Name": "LoadBalancer", "Value": _alb_arn_suffix(alb_arn)},
                {"Name": "TargetGroup", "Value": _tg_arn_suffix(tg_arn)},
            ],
        )
        print(f"[premium-alarm] action=create name={alarm_name} tg={tg_arn}")
    except Exception as e:
        print(f"WARNING: Failed to create unhealthy-host alarm {alarm_name}: {e}")


def _delete_premium_tg_unhealthy_alarm(tg_arn: str) -> None:
    """Best-effort delete of a per-TG UnHealthyHostCount alarm.

    Idempotent: delete_alarms ignores names that do not exist, so this is safe
    to call for any TG ARN (including the shared autoscaling TG, whose derived
    name will simply not match an existing alarm).
    """
    alarm_name = _premium_tg_alarm_name(tg_arn)
    if not alarm_name:
        return
    try:
        cloudwatch = _get_cloudwatch_client()
        cloudwatch.delete_alarms(AlarmNames=[alarm_name])
        print(f"[premium-alarm] action=delete name={alarm_name} tg={tg_arn}")
    except Exception as e:
        print(f"WARNING: Failed to delete unhealthy-host alarm {alarm_name}: {e}")


def create_or_get_target_group(user_id: int, vpc_id: str) -> str:
    """
    Create a new target group for a premium user, or return existing one if
    a target group with the same name already exists.

    This handles the edge case where a previous migration partially failed
    and left an orphaned target group.

    Args:
        user_id: The user ID for naming the target group
        vpc_id: The VPC ID for the target group

    Returns:
        The ARN of the created or existing target group
    """
    elbv2: "ElasticLoadBalancingv2Client" = boto3.client("elbv2")
    target_group_name = f"premium-{user_id}-tg"

    try:
        response = elbv2.create_target_group(
            Name=target_group_name,
            Protocol="HTTP",
            Port=PremiumInstanceConfig.get_backend_port(),
            VpcId=vpc_id,
            HealthCheckPath="/health",
            HealthCheckProtocol="HTTP",
            HealthCheckIntervalSeconds=30,
            HealthyThresholdCount=2,
            UnhealthyThresholdCount=3,
            Tags=[
                {"Key": "UserID", "Value": str(user_id)},
                {"Key": "Type", "Value": "premium-user"},
                {"Key": "Service", "Value": "optinist-premium"},
            ],
        )
        tg_arn = response["TargetGroups"][0]["TargetGroupArn"]
        _enable_sticky_sessions(elbv2, tg_arn)
        _ensure_premium_tg_unhealthy_alarm(tg_arn)
        return tg_arn

    except Exception as e:
        if "DuplicateTargetGroupName" in str(e):
            print(
                f"Target group {target_group_name} already exists, "
                f"retrieving existing ARN"
            )
            try:
                response = elbv2.describe_target_groups(Names=[target_group_name])
                existing_arn = response["TargetGroups"][0]["TargetGroupArn"]
                _enable_sticky_sessions(elbv2, existing_arn)
                _ensure_premium_tg_unhealthy_alarm(existing_arn)
                print(f"Found existing target group: {existing_arn}")
                return existing_arn
            except Exception as describe_error:
                print(f"Failed to retrieve existing target group: {describe_error}")
                raise
        raise


def create_alb_rule(
    listener_arn: str,
    conditions: list,
    actions: list,
    start_priority: int = 100,
    max_retries: int = 3,
) -> dict:
    """Create an ALB rule, retrying with a fresh priority on PriorityInUse.

    Concurrent Lambda invocations can race between get_next_available_priority()
    and create_rule(). This wrapper catches PriorityInUse and re-queries for the
    next free priority, up to max_retries times.
    """
    elbv2: "ElasticLoadBalancingv2Client" = boto3.client("elbv2")

    for attempt in range(1, max_retries + 1):
        priority = get_next_available_priority(listener_arn, start_priority)
        if priority > MAX_PREMIUM_PRIORITY:
            raise ValueError(
                f"Premium ALB rule priority {priority} exceeds "
                f"MAX_PREMIUM_PRIORITY ({MAX_PREMIUM_PRIORITY})."
            )
        try:
            response = elbv2.create_rule(
                ListenerArn=listener_arn,
                Priority=priority,
                Conditions=conditions,
                Actions=actions,
            )
            return response
        except ClientError as e:
            if e.response["Error"]["Code"] == "PriorityInUse":
                print(
                    f"Priority {priority} taken (attempt {attempt}/{max_retries}), "
                    f"retrying with next available priority"
                )
                if attempt == max_retries:
                    raise
            else:
                raise


def get_next_available_priority(
    listener_arn: str,
    start_priority: int = 100,
    max_priority: int = MAX_PREMIUM_PRIORITY,
) -> int:
    """
    Find next available ALB rule priority by querying existing rules.

    Args:
        listener_arn: ALB listener ARN to check
        start_priority: Starting priority to search from (default: 100)
        max_priority: Hard cap on returned priority.

    Returns:
        Next available priority number in [start_priority, max_priority]

    Raises:
        ValueError: If start_priority > max_priority
        Exception: If no priorities available in the allowed range
    """
    if start_priority > max_priority:
        raise ValueError(
            f"start_priority ({start_priority}) cannot exceed "
            f"max_priority ({max_priority})."
        )

    elbv2: "ElasticLoadBalancingv2Client" = boto3.client("elbv2")

    try:
        # Get all existing rules for this listener
        response = elbv2.describe_rules(ListenerArn=listener_arn)
        rules = response.get("Rules", [])

        # Extract used priorities (excluding default rule which has priority "default")
        used_priorities = set()
        for rule in rules:
            rule_priority = rule.get("Priority")
            if rule_priority and rule_priority != "default":
                try:
                    used_priorities.add(int(rule_priority))
                except (ValueError, TypeError):
                    # Skip if priority is not a valid integer
                    continue

        print(
            f"Found {len(used_priorities)} existing ALB rules "
            f"with priorities: {sorted(used_priorities)}"
        )

        # Find first available priority starting from start_priority
        priority = start_priority
        while priority in used_priorities:
            priority += 1
            if priority > max_priority:
                raise Exception(
                    f"No available ALB rule priorities in "
                    f"[{start_priority}, {max_priority}]."
                )

        print(f"Allocated priority {priority} for new ALB rule")
        return priority

    except Exception as e:
        print(f"Error finding available priority: {str(e)}")
        raise


def _assign_premium_user_impl(
    user_id: int,
    event: Dict[str, Any],
    user_uid: Optional[str],
    ec2: "EC2Client",
    elbv2: "ElasticLoadBalancingv2Client",
    backend_port: int,
    vpc_id: str,
    alb_listener_arn: str,
) -> Dict[str, Any]:
    """Core premium user assignment logic.

    Called from assign_premium_user UNDER the per-user distributed
    lock to prevent concurrent calls from corrupting ALB resources
    (issue #630).  The lock uses ASSIGN_LOCK_TIMEOUT_SECONDS (10 s)
    so the 409 fallback is reliably reachable within the Lambda
    invocation timeout (60 s).
    """
    # Restore pending_release if user refreshed (beacon fired but user is back)
    try:
        restored = restore_pending_release(user_id)
        if restored:
            print(
                f"Restored pending_release for user {user_id} -> "
                f"instance {restored['instance_id']} (page refresh detected)"
            )
            return {
                "statusCode": 200,
                "body": json.dumps(
                    {
                        "message": "Premium assignment restored",
                        "instance_id": restored["instance_id"],
                        "target_group_arn": restored["target_group_arn"],
                        "rule_arn": restored["alb_rule_arn"],
                        "assigned": True,
                        "is_shared": bool(restored.get("is_shared", False)),
                        "assignment_source": "restored_from_pending_release",
                    }
                ),
            }
    except Exception as restore_error:
        print(f"Pending release restore check failed: {str(restore_error)}")

    # Return existing assignment if user is already assigned
    try:
        existing_assignment = get_existing_user_assignment(user_id)
        if existing_assignment:
            existing_instance_id = existing_assignment["instance_id"]
            print(
                f"User {user_id} already has active assignment to "
                f"instance {existing_instance_id}"
            )

            # Check if the assigned instance is stopped and restart it
            if existing_instance_id != PremiumAssignment.AUTOSCALING_POOL:
                try:
                    resp = ec2.describe_instances(InstanceIds=[existing_instance_id])
                    reservations = resp.get("Reservations", [])
                    ec2_state = None
                    if reservations and reservations[0].get("Instances"):
                        ec2_state = reservations[0]["Instances"][0]["State"]["Name"]

                    if ec2_state == InstanceState.STOPPING:
                        print(
                            f"Assigned instance {existing_instance_id} is "
                            f"stopping  - waiting for stopped state"
                        )
                        stop_waiter = ec2.get_waiter("instance_stopped")
                        stop_waiter.wait(
                            InstanceIds=[existing_instance_id],
                            WaiterConfig={"Delay": 5, "MaxAttempts": 24},
                        )
                        ec2_state = InstanceState.STOPPED

                    if ec2_state == InstanceState.STOPPED:
                        print(
                            f"Assigned instance {existing_instance_id} is "
                            f"stopped  - restarting for user {user_id}"
                        )
                        ec2.start_instances(InstanceIds=[existing_instance_id])
                        waiter = ec2.get_waiter("instance_running")
                        waiter.wait(
                            InstanceIds=[existing_instance_id],
                            WaiterConfig={"Delay": 5, "MaxAttempts": 24},
                        )
                        _update_instance_state_to_running(existing_instance_id)

                        # Wait for ECS task readiness before returning
                        if check_instance_readiness_with_retry(
                            existing_instance_id,
                            max_wait_seconds=STANDBY_READINESS_TIMEOUT_SECONDS,
                            retry_interval=STANDBY_READINESS_RETRY_INTERVAL_SECONDS,
                        ):
                            print(
                                f"Restarted instance {existing_instance_id} "
                                f"is ready for user {user_id}"
                            )
                            return {
                                "statusCode": 200,
                                "body": json.dumps(
                                    {
                                        "message": f"User {user_id} assigned "
                                        f"to restarted instance "
                                        f"{existing_instance_id}",
                                        "instance_id": existing_instance_id,
                                        "target_group_arn": existing_assignment[
                                            "target_group_arn"
                                        ],
                                        "rule_arn": existing_assignment["alb_rule_arn"],
                                        "is_shared": bool(
                                            existing_assignment.get("is_shared", False)
                                        ),
                                        "assignment_source": "restarted_instance",
                                    }
                                ),
                            }
                        else:
                            print(
                                f"WARNING: Instance {existing_instance_id} "
                                f"started but ECS not ready after 120s, "
                                f"cleaning up stale assignment"
                            )
                            existing_assignment = None
                            remove_user_assignment(user_id)

                    elif (
                        ec2_state
                        in (
                            InstanceState.TERMINATED,
                            InstanceState.SHUTTING_DOWN,
                        )
                        or ec2_state is None
                    ):
                        print(
                            f"Assigned instance {existing_instance_id} is "
                            f"{ec2_state or 'gone'}  - removing stale "
                            f"assignment for user {user_id}"
                        )
                        existing_assignment = None
                        remove_user_assignment(user_id)

                except ClientError as ec2_err:
                    error_code = ec2_err.response["Error"]["Code"]
                    if error_code == "InvalidInstanceID.NotFound":
                        print(
                            f"Instance {existing_instance_id} no longer "
                            f"exists  - removing stale assignment"
                        )
                        existing_assignment = None
                        remove_user_assignment(user_id)
                    else:
                        raise
                except Exception as state_err:
                    print(
                        f"Error checking instance state for "
                        f"{existing_instance_id}: {state_err}"
                    )
                    existing_assignment = None
                    remove_user_assignment(user_id)

            # Trigger migration for autoscaling-pool or shared
            if existing_assignment and (
                existing_instance_id == PremiumAssignment.AUTOSCALING_POOL
                or existing_assignment.get("is_shared")
            ):
                print(
                    f"User {user_id} needs migration "
                    f"(instance={existing_instance_id}, "
                    f"shared={existing_assignment.get('is_shared')})"
                    f", attempting inline migration..."
                )

                # Try inline migration: find a ready dedicated instance now
                all_instances = get_all_premium_instances_with_states()
                running_instances = [
                    i for i in all_instances if i["state"] == InstanceState.RUNNING
                ]
                for instance in running_instances:
                    candidate_id = instance["instance_id"]
                    assigned = get_assigned_users_for_instance(candidate_id)
                    if len(assigned) > 0:
                        continue
                    if not check_instance_readiness_with_retry(
                        candidate_id, max_wait_seconds=10, retry_interval=5
                    ):
                        continue
                    # Found a ready, empty dedicated instance - migrate now
                    print(
                        f"Inline migration: migrating user {user_id} "
                        f"from {existing_instance_id} to {candidate_id}"
                    )
                    if migrate_user_to_dedicated_instance(user_id, candidate_id):
                        # Re-fetch updated assignment after migration
                        migrated = get_existing_user_assignment(user_id)
                        if migrated:
                            print(
                                f"Inline migration successful: user {user_id} "
                                f"now on {migrated['instance_id']}"
                            )
                            return {
                                "statusCode": 200,
                                "body": json.dumps(
                                    {
                                        "message": f"User {user_id} migrated to "
                                        f"instance {migrated['instance_id']}",
                                        "instance_id": migrated["instance_id"],
                                        "target_group_arn": migrated[
                                            "target_group_arn"
                                        ],
                                        "rule_arn": migrated["alb_rule_arn"],
                                        "is_shared": False,
                                        "assignment_source": "inline_migration",
                                    }
                                ),
                            }

                # No inline migration possible - fall back to async
                print(
                    f"Inline migration not possible for user {user_id}, "
                    f"falling back to async migration"
                )
                invoke_migration_async()

            # If a dedicated assignment's target group is authoritatively gone,
            # its stored ARNs are dead. Drop the row and fall through to a fresh
            # assignment that recreates the rule/TG, rather than reusing a broken
            # route. Shared/pool rows use the shared TG (handled above).
            # Fail-open: a transient probe error (throttle/5xx) is not a
            # confirmed miss, so keep reusing rather than 500 or drop the row.
            if (
                existing_assignment
                and not existing_assignment.get("is_shared")
                and existing_instance_id != PremiumAssignment.AUTOSCALING_POOL
            ):
                existing_tg_arn = existing_assignment.get("target_group_arn")
                try:
                    tg_confirmed_missing = not target_group_exists(existing_tg_arn)
                except Exception as tg_probe_error:
                    tg_confirmed_missing = False
                    print(
                        f"Target group probe failed for user {user_id} "
                        f"({existing_tg_arn}): {tg_probe_error} — reusing "
                        f"existing assignment (fail-open)"
                    )
                if tg_confirmed_missing:
                    # Drops the DB row only; any orphaned ALB rule is reaped by
                    # premium_cleanup. Next assign reprovisions rule + TG.
                    print(
                        f"Existing assignment for user {user_id} has a missing "
                        f"target group ({existing_tg_arn}) - dropping the stale "
                        f"row so a fresh assignment reprovisions ALB routing"
                    )
                    try:
                        remove_user_assignment(user_id)
                    except Exception as remove_error:
                        # Row already gone (concurrent heal / second assign) is
                        # the wanted outcome — fall through. Re-raise real DB
                        # errors to fail fast.
                        if "No assignment found" not in str(remove_error):
                            raise
                    existing_assignment = None

            if existing_assignment:
                return {
                    "statusCode": 200,
                    "body": json.dumps(
                        {
                            "message": f"User {user_id} already assigned to "
                            f"instance {existing_instance_id}",
                            "instance_id": existing_instance_id,
                            "target_group_arn": existing_assignment["target_group_arn"],
                            "rule_arn": existing_assignment["alb_rule_arn"],
                            "is_shared": bool(
                                existing_assignment.get("is_shared", False)
                            ),
                            "assignment_source": "existing",
                        }
                    ),
                }
    except Exception as check_error:
        # Fail fast if we can't verify assignment status
        print(f"Error: Failed to check existing assignment: {check_error}")
        return {
            "statusCode": 500,
            "body": json.dumps(
                {
                    "error": "Internal error",
                    "message": "Unable to verify assignment status. Please retry.",
                    "assigned": False,
                }
            ),
        }

    # Initialize variables for exception handling scope
    target_group_arn = None
    rule_arn = None
    assignment_stored = False  # Track if DB write happened for cleanup

    try:
        # 0. Register any orphaned stopped instances as standby first
        # Uses NULL user_id for standby instances (no real user)
        register_orphaned_stopped_instances()

        # 1. Get comprehensive instance state information
        all_instances = get_all_premium_instances_with_states()
        running_instances = [
            i for i in all_instances if i["state"] == InstanceState.RUNNING
        ]
        launching_instances = [
            i for i in all_instances if i["state"] == InstanceState.PENDING
        ]
        active_users = count_active_premium_users()

        # Get standby pool status (now includes newly registered instances)
        standby_instances = get_available_standby_instances()
        standby_count = len(standby_instances)

        print(" === PREMIUM USER ASSIGNMENT START ===")
        print(f"Target user: {user_id}")
        print(" Assignment context:")
        print(f"- Running instances: {len(running_instances)}")
        print(f"- Launching instances: {len(launching_instances)}")
        print(f"- Active users: {active_users}")
        print(f"- Standby available: {standby_count}")
        print(f"- Total instances: {len(all_instances)}")

        print("Instance details:")
        for instance in all_instances:
            print(f"- {instance['instance_id']}: {instance['state']}")

        stopped_instances = [
            i for i in all_instances if i["state"] == InstanceState.STOPPED
        ]
        print(
            f" Stopped instances found in AWS: "
            f"{[i['instance_id'] for i in stopped_instances]}"
        )
        print(
            f" Standby instances in database: "
            f"{[i['instance_id'] for i in standby_instances]}"
        )
        print(" === STARTING ASSIGNMENT LOGIC ===")
        print()

        # Initialize assignment variables
        instance_to_use = None
        is_shared = False
        instance_state = None
        assignment_source = None
        needs_scaling = False  # Track if shared assignment requires scaling

        # 2. PRIORITY 1: Available dedicated running instances (immediate assignment)
        available_dedicated = None
        least_loaded_instance = None
        min_users = float("inf")

        print(
            f"Evaluating {len(running_instances)} running "
            f"instances for immediate assignment"
        )

        for i, instance in enumerate(running_instances):
            instance_id = instance["instance_id"]
            print(f"[{i+1}/{len(running_instances)}] Evaluating instance {instance_id}")

            print(f"Checking readiness for instance {instance_id}...")
            is_ready = check_instance_readiness_with_retry(
                instance_id, max_wait_seconds=30, retry_interval=10
            )
            print(f"Readiness result: {is_ready}")

            if not is_ready:
                print(f"Skipping {instance_id} - not ready")
                continue

            # Check assigned users
            print(f"Checking assigned users for instance {instance_id}...")
            assigned_users = get_assigned_users_for_instance(instance_id)
            user_count = len(assigned_users)
            print(
                f"Found {user_count} assigned users: "
                f"{[u.get('user_id', u) for u in assigned_users]}"
            )

            if user_count == 0:
                # Found potential dedicated instance - try to reserve it
                print(
                    f"Found available instance {instance_id}, attempting reservation..."
                )
                if try_reserve_instance(instance_id, user_id):
                    available_dedicated = instance
                    print(f"Reserved dedicated instance: {instance_id}")
                    break
                else:
                    print(
                        f"Failed to reserve {instance_id} " f"(another user claimed it)"
                    )
                    continue
            elif user_count < min_users:
                # Track least loaded for sharing
                least_loaded_instance = instance
                min_users = user_count
                print(f"Tracking as least loaded: {instance_id} ({user_count} users)")
            else:
                print(f"Instance {instance_id} has {user_count} users (not optimal)")

        print("Dedicated instance search results:")
        print(
            f"- Available dedicated: "
            f"{available_dedicated['instance_id'] if available_dedicated else 'None'}"  # noqa: E501
        )
        print(
            f"- Least loaded: "
            f"{least_loaded_instance['instance_id'] if least_loaded_instance else 'None'}"  # noqa: E501
            f" ({min_users} users)"
        )

        # Use dedicated instance if available (PRIORITY 1)
        if available_dedicated:
            instance_to_use = available_dedicated
            is_shared = False
            instance_state = InstanceState.RUNNING
            assignment_source = "dedicated"
            print(
                f"Using dedicated running instance "
                f"{instance_to_use['instance_id']} for user {user_id}"
            )
        else:
            print("No dedicated instances available")

        # PRIORITY 2: Share with least loaded instance
        if not instance_to_use and least_loaded_instance:
            instance_to_use = least_loaded_instance
            is_shared = True
            instance_state = InstanceState.RUNNING
            assignment_source = "shared"
            print(
                f"Sharing instance {instance_to_use['instance_id']} "
                f"for user {user_id} (least loaded with {min_users} users)"
            )

            # Trigger scaling if we're under-provisioned (more users than instances)
            if (
                len(launching_instances) == 0
                and len(running_instances) < active_users + 1
            ):
                needs_scaling = True
                print("-> Flagged for background scaling after assignment")

        # 3. PRIORITY 3: Start standby instance (5-15 second assignment)
        # NOTE: Must run BEFORE autoscaling pool fallback, so that stopped
        # standby instances are started instead of sending users to the
        # shared pool where migration may never complete.
        if not instance_to_use and standby_instances:
            print(
                f"No running instances available, "
                f"starting standby instance ({standby_count} available)"
            )

            # Use oldest standby instance
            standby_to_start = standby_instances[0]
            standby_instance_id = standby_to_start["instance_id"]

            # Start the standby instance
            if start_standby_instance(standby_instance_id):
                # Create replacement standby instance asynchronously so the
                # 3-5 min create/stop chain does not block this assignment.
                invoke_standby_replenishment_async()

                # Proceed with assignment to the started instance
                instance_to_use = {"instance_id": standby_instance_id}
                is_shared = False
                instance_state = InstanceState.RUNNING
                assignment_source = "standby"

                print(
                    f"Assigning user {user_id} to started standby "
                    f"instance {standby_instance_id}"
                )
            else:
                print(
                    f"Failed to start standby instance {standby_instance_id}, "
                    f"falling back to other options"
                )

        # PRIORITY 3.5: Temporary assignment to autoscaling pool for immediate login
        # Only used when no standby instances are available either
        if not instance_to_use:
            no_premium_available = (
                len(running_instances) == 0 or not available_dedicated
            )
            if no_premium_available:
                print(
                    "No premium or standby instances ready "
                    "- using autoscaling pool for immediate login"
                )

                # Use special marker for autoscaling pool assignment
                instance_to_use = {"instance_id": PremiumAssignment.AUTOSCALING_POOL}
                is_shared = True  # This is a temporary shared assignment
                instance_state = InstanceState.RUNNING
                assignment_source = "autoscaling_temp"
                needs_scaling = True  # Always trigger scaling for premium instance

                print(f"-> User {user_id} will login via autoscaling pool")
                print("-> Scaling premium instances in background")
                print("-> User will be migrated to dedicated instance once ready")

        # 4. PRIORITY 4: Fallback to AWS stopped instances not in database
        if not instance_to_use:
            # Find stopped instances directly from AWS that
            # are not in our standby database
            stopped_aws_instances = [
                i for i in all_instances if i["state"] == InstanceState.STOPPED
            ]

            # Filter out instances that are already in standby pool
            standby_instance_ids = {inst["instance_id"] for inst in standby_instances}
            aws_only_stopped = [
                i
                for i in stopped_aws_instances
                if i["instance_id"] not in standby_instance_ids
            ]

            if aws_only_stopped:
                fallback_instance = aws_only_stopped[0]
                fallback_instance_id = fallback_instance["instance_id"]
                print(
                    f"No standby instances available, using AWS stopped "
                    f"instance {fallback_instance_id}"
                )

                # Start this AWS instance directly
                try:
                    print(f"Starting AWS stopped instance {fallback_instance_id}")
                    ec2.start_instances(InstanceIds=[fallback_instance_id])

                    # Wait for running state
                    waiter = ec2.get_waiter("instance_running")
                    waiter.wait(
                        InstanceIds=[fallback_instance_id],
                        WaiterConfig={"Delay": 15, "MaxAttempts": 24},  # 6 minutes max
                    )

                    # Proceed with assignment
                    instance_to_use = {"instance_id": fallback_instance_id}
                    is_shared = False
                    instance_state = InstanceState.RUNNING
                    assignment_source = "aws_fallback"

                    print(
                        f"Successfully started and using AWS "
                        f"instance {fallback_instance_id}"
                    )

                except Exception as start_error:
                    print(
                        f"Failed to start AWS instance "
                        f"{fallback_instance_id}: {str(start_error)}"
                    )

        # 5. PRIORITY 5: Scale up - create new instance (last resort, slowest)
        if not instance_to_use:
            if len(launching_instances) > 0:
                # Already scaling - track retry attempt
                attempts = increment_assignment_attempts(user_id)
                return {
                    "statusCode": 202,
                    "body": json.dumps(
                        {
                            "message": f"Premium capacity scaling in progress "
                            f"({len(launching_instances)} instances launching). "
                            f"Please retry in 2-3 minutes. (attempt #{attempts})",
                            "retry_after": 180,
                        }
                    ),
                }
            else:
                # Try to scale
                scaled = scale_premium_instances_if_needed()
                if scaled:
                    # Scaling initiated - track retry attempt
                    attempts = increment_assignment_attempts(user_id)
                    return {
                        "statusCode": 202,
                        "body": json.dumps(
                            {
                                "message": "Scaling premium capacity. "
                                f"Please retry in 2-3 minutes. (attempt #{attempts})",
                                "retry_after": 180,
                            }
                        ),
                    }
                else:
                    # Generate detailed error message for debugging
                    stopped_instances = [
                        i for i in all_instances if i["state"] == InstanceState.STOPPED
                    ]
                    error_details = {
                        "error": "No available premium instances "
                        "and cannot scale further",
                        "debug_info": {
                            "total_instances": len(all_instances),
                            "running_instances": len(running_instances),
                            "launching_instances": len(launching_instances),
                            "stopped_instances": len(stopped_instances),
                            "standby_instances": len(standby_instances),
                            "active_users": active_users,
                            "stopped_instance_ids": [
                                i["instance_id"] for i in stopped_instances
                            ],
                            "standby_instance_ids": [
                                i["instance_id"] for i in standby_instances
                            ],
                        },
                    }
                    print(f"Assignment failed with debug info: {error_details}")
                    return {
                        "statusCode": 503,
                        "body": json.dumps(error_details),
                    }

        # Final check: Ensure we have an instance assigned
        if not instance_to_use:
            print(" === ASSIGNMENT FAILURE ANALYSIS ===")

            # Analyze why each priority failed
            failure_reasons = []

            if len(running_instances) == 0:
                failure_reasons.append(" Priority 1: No running instances found")
            else:
                failure_reasons.append(
                    f" Priority 1: {len(running_instances)} running instances "
                    f"found but all failed readiness/assignment checks"
                )

            if len(standby_instances) == 0:
                failure_reasons.append(
                    " Priority 2: No standby instances available in database"
                )
            else:
                failure_reasons.append(
                    f" Priority 2: {len(standby_instances)} standby instances "
                    f"found but failed to start"
                )

            if len(stopped_instances) == 0:
                failure_reasons.append(
                    " Priority 2.5: No stopped instances found in AWS"
                )
            else:
                failure_reasons.append(
                    f" Priority 2.5: {len(stopped_instances)} stopped "
                    f"instances found but failed to start"
                )

            if least_loaded_instance:
                failure_reasons.append(
                    "Priority 3: Sharing available but conditions not met"
                )
            else:
                failure_reasons.append("Priority 3: No instances available for sharing")

            failure_reasons.append(" Priority 4: Scaling failed or blocked")

            print(" Failure analysis:")
            for reason in failure_reasons:
                print(f"{reason}")

            error_details = {
                "error": "Could not assign premium instance - "
                "all assignment paths failed",
                "debug_info": {
                    "user_id": user_id,
                    "total_instances": len(all_instances),
                    "running_instances": len(running_instances),
                    "launching_instances": len(launching_instances),
                    "stopped_instances": len(stopped_instances),
                    "standby_instances": len(standby_instances),
                    "active_users": active_users,
                    "running_instance_ids": [
                        i["instance_id"] for i in running_instances
                    ],
                    "stopped_instance_ids": [
                        i["instance_id"] for i in stopped_instances
                    ],
                    "standby_instance_ids": [
                        i["instance_id"] for i in standby_instances
                    ],
                    "failure_reasons": failure_reasons,
                    "has_least_loaded": least_loaded_instance is not None,
                    "min_users_on_least_loaded": (
                        min_users if least_loaded_instance else None
                    ),
                },
            }
            print(f" Final assignment failure details: {error_details}")
            print(" === ASSIGNMENT FAILED ===")

            return {
                "statusCode": 503,
                "body": json.dumps(error_details),
            }

        # 6. Create target group for the user (or use existing autoscaling pool)
        print("=== ASSIGNMENT SUCCESS ===")
        print(f"Assigning user {user_id} to instance {instance_to_use['instance_id']}")
        print("Assignment details:")
        print(f"- Instance ID: {instance_to_use['instance_id']}")
        print(f"- Assignment source: {assignment_source}")
        print(f"- Instance state: {instance_state}")
        print(f"- Is shared: {is_shared}")
        print("=== PROCEEDING WITH TARGET GROUP CREATION ===")
        print()

        instance_id = instance_to_use["instance_id"]

        # Special handling for autoscaling pool assignment
        if instance_id == PremiumAssignment.AUTOSCALING_POOL:
            print("Using existing autoscaling target group for temporary assignment")
            # Use the autoscaling target group instead of creating a new one
            target_group_arn = os.environ.get("AUTOSCALING_TARGET_GROUP_ARN")
            if not target_group_arn:
                raise ValueError(
                    "AUTOSCALING_TARGET_GROUP_ARN environment variable not set"
                )
            print(f"Autoscaling target group: {target_group_arn}")
        else:
            # Clean up any orphaned target group with the same name before
            # creating a new one, to avoid reusing a stale ARN that a
            # concurrent cleanup may be deleting.
            tg_name = f"premium-{user_id}-tg"
            try:
                old_tgs = elbv2.describe_target_groups(Names=[tg_name])
                for old_tg in old_tgs.get("TargetGroups", []):
                    old_arn = old_tg["TargetGroupArn"]
                    print(
                        f"Cleaning up orphaned target group {tg_name} "
                        f"({old_arn}) before creating new one"
                    )
                    try:
                        _delete_premium_tg_unhealthy_alarm(old_arn)
                        elbv2.delete_target_group(TargetGroupArn=old_arn)
                    except Exception as del_err:
                        print(f"Warning: could not delete orphaned TG: {del_err}")
            except ClientError as desc_err:
                if "TargetGroupNotFound" not in str(desc_err):
                    raise
                # No existing TG with this name - proceed normally

            # Normal path: create a dedicated target group for the premium instance
            target_group_response = elbv2.create_target_group(
                Name=tg_name,
                Protocol="HTTP",
                Port=backend_port,
                VpcId=vpc_id,
                HealthCheckPath="/health",
                HealthCheckProtocol="HTTP",
                HealthCheckIntervalSeconds=30,
                HealthyThresholdCount=2,
                UnhealthyThresholdCount=3,
                Tags=[
                    {"Key": "UserID", "Value": str(user_id)},
                    {"Key": "Type", "Value": "premium-user"},
                    {"Key": "Service", "Value": "optinist-premium"},
                    {"Key": "Shared", "Value": str(is_shared)},
                    {"Key": "Source", "Value": assignment_source or "unknown"},
                ],
            )

            target_group_arn = target_group_response["TargetGroups"][0][
                "TargetGroupArn"
            ]
            _enable_sticky_sessions(elbv2, target_group_arn)
            _ensure_premium_tg_unhealthy_alarm(target_group_arn)

            # 8. Register instance to target group
            print(
                f"[premium-tg-mutate] site=assign action=register "
                f"user={user_id} instance={instance_id} "
                f"tg={target_group_arn} "
                f"port={backend_port}"
            )
            elbv2.register_targets(
                TargetGroupArn=target_group_arn,
                Targets=[{"Id": instance_id, "Port": backend_port}],
            )

        # Create ALB listener rule for user routing
        if not user_uid:
            raise ValueError("user_uid is required to generate routing ID")
        routing_secret_key = get_required_env_var("ROUTING_SECRET_KEY")
        routing_id = generate_routing_id(user_uid, routing_secret_key)
        print(
            f"Generated routing_id for user: {routing_id[:8]}... "
            f"(truncated for security)"
        )

        cleanup_duplicate_rules_for_routing_id(alb_listener_arn, routing_id)

        rule_response = create_alb_rule(
            listener_arn=alb_listener_arn,
            conditions=[
                {
                    "Field": "http-header",
                    "HttpHeaderConfig": {
                        "HttpHeaderName": RoutingHeaders.USER_TIER,
                        "Values": [PremiumInstanceConfig.INSTANCE_IDENTIFIER],
                    },
                },
                {
                    "Field": "http-header",
                    "HttpHeaderConfig": {
                        "HttpHeaderName": RoutingHeaders.ROUTING_ID,
                        "Values": [routing_id],
                    },
                },
            ],
            actions=[{"Type": "forward", "TargetGroupArn": target_group_arn}],
        )

        rule_arn = rule_response["Rules"][0]["RuleArn"]

        # Clean up standby placeholder before storing new assignment
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM premium_user_assignments "
                    "WHERE instance_id = %s AND is_standby = 1",
                    (instance_id,),
                )
                deleted_count = cursor.rowcount
                connection.commit()  # Commit the DELETE before the next transaction
                if deleted_count > 0:
                    print(
                        f"Removed {deleted_count} standby placeholder assignment(s) "
                        f"for instance {instance_id}"
                    )

        # Clean up reservation placeholder
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """DELETE FROM premium_user_assignments
                       WHERE instance_id = %s
                       AND user_id = %s
                       AND target_group_arn = %s""",
                    (instance_id, user_id, PremiumAssignment.RESERVING),
                )
                connection.commit()

        # Store assignment before scaling so that active_users count
        # then scale_premium_instances_if_needed
        store_user_assignment(
            user_id,
            instance_id,
            target_group_arn,
            rule_arn,
            instance_state or InstanceState.LAUNCHING,
            is_shared,
        )
        assignment_stored = True

        if needs_scaling:
            print("Triggering scaling for shared assignment...")
            scale_premium_instances_if_needed()
            print("Triggering async migration for autoscaling-pool user...")
            invoke_migration_async()

        # Initialize activity tracking for the new assignment
        try:
            update_user_activity(user_id)
            print(f"Initialized activity tracking for user {user_id}")
        except Exception as activity_error:
            print(f" Failed to initialize activity tracking: {str(activity_error)}")
            # Don't fail the assignment for activity tracking errors

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": f"Premium user {user_id} "
                    f"assigned to instance {instance_id} "
                    f"({assignment_source}{' shared' if is_shared else ''})",
                    "instance_id": instance_id,
                    "target_group_arn": target_group_arn,
                    "rule_arn": rule_arn,
                    "is_shared": is_shared,
                    "assignment_source": assignment_source,
                }
            ),
        }

    except Exception as e:
        print(f"Error assigning premium user: {str(e)}")

        # Cleanup on failure
        try:
            # Clean up ALB rule if created (MUST be done before target group)
            if rule_arn:
                elbv2.delete_rule(RuleArn=rule_arn)
                print(f"Cleaned up ALB rule after error: {rule_arn}")
        except Exception as rule_cleanup_error:
            print(f"Failed to cleanup ALB rule: {str(rule_cleanup_error)}")

        try:
            # Clean up target group if created (skip placeholder markers)
            if target_group_arn and target_group_arn != PremiumAssignment.RESERVING:
                _delete_premium_tg_unhealthy_alarm(target_group_arn)
                elbv2.delete_target_group(TargetGroupArn=target_group_arn)
                print(f"Cleaned up target group after error: {target_group_arn}")
        except Exception as cleanup_error:
            print(f"Failed to cleanup target group: {str(cleanup_error)}")

        try:
            # Release instance reservation if it exists
            if instance_to_use:
                instance_id = instance_to_use.get("instance_id")
                if instance_id:
                    release_instance_reservation(instance_id, user_id)
                    print(f"Released reservation for instance {instance_id}")
        except Exception as reservation_error:
            print(f"Failed to release reservation: {str(reservation_error)}")

        # Clean up DB entry if it was written (defense-in-depth)
        try:
            if assignment_stored:
                print(f"Cleaning up DB assignment for user {user_id} after error")
                with get_db_connection() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "DELETE FROM premium_user_assignments WHERE user_id = %s",
                            (user_id,),
                        )
                        connection.commit()
                print(f"Cleaned up DB assignment for user {user_id}")
        except Exception as db_cleanup_error:
            print(f"Failed to cleanup DB assignment: {str(db_cleanup_error)}")

        raise


def assign_premium_user(
    user_id: int,
    event: Dict[str, Any],
    user_uid: Optional[str] = None,
) -> Dict[str, Any]:
    """Enhanced assignment with standby pool support -
    prefer stopped instances for fast startup"""

    ec2: "EC2Client" = boto3.client("ec2")
    elbv2: "ElasticLoadBalancingv2Client" = boto3.client("elbv2")
    backend_port = PremiumInstanceConfig.get_backend_port()

    try:
        vpc_id = get_required_env_var("VPC_ID")
        alb_listener_arn = get_required_env_var("ALB_LISTENER_ARN")
    except ValueError as e:
        print(f" Assignment failed - environment configuration error: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps(
                {"error": "Configuration error", "message": str(e), "assigned": False}
            ),
        }

    # Serialize concurrent assign calls for the same user_id.
    # Without this lock, two concurrent calls can both pass the
    # "existing assignment?" check and race through TG creation,
    # causing the second call's orphan-cleanup to delete the first
    # call's freshly-created target group (issue #630).
    #
    # The entire _assign_premium_user_impl runs under the lock.
    # Lock timeout is short (ASSIGN_LOCK_TIMEOUT_SECONDS = 10) so
    # the 409 fallback is reliably reachable within the Lambda
    # invocation timeout (60 s).
    lock_name = f"{ASSIGN_USER_LOCK_PREFIX}{user_id}"
    with distributed_lock(lock_name, timeout=ASSIGN_LOCK_TIMEOUT_SECONDS) as acquired:
        if not acquired:
            # Lock timed out -- another invocation held it.
            # Check whether that call stored a valid assignment
            # before giving up.
            print(
                f"Per-user assign lock timed out for user {user_id}, "
                f"checking for completed assignment"
            )
            try:
                existing = get_existing_user_assignment(user_id)
                if existing:
                    print(
                        f"Found assignment completed by concurrent invocation "
                        f"for user {user_id}: {existing.get('instance_id')}"
                    )
                    return {
                        "statusCode": 200,
                        "body": json.dumps(
                            {
                                "message": f"User {user_id} already assigned to "
                                f"instance {existing.get('instance_id')}",
                                "instance_id": existing.get("instance_id"),
                                "target_group_arn": existing.get("target_group_arn"),
                                "rule_arn": existing.get("alb_rule_arn"),
                                "is_shared": bool(existing.get("is_shared", False)),
                                "assignment_source": "existing",
                            }
                        ),
                    }
            except Exception as fallback_err:
                print(f"Fallback assignment check failed: {fallback_err}")

            return {
                "statusCode": 409,
                "body": json.dumps(
                    {
                        "error": "Assignment in progress",
                        "message": "Another assignment is in progress "
                        "for this user. Please retry.",
                        "assigned": False,
                    }
                ),
            }

        # ── Under lock: full assignment logic ──
        return _assign_premium_user_impl(
            user_id,
            event,
            user_uid,
            ec2,
            elbv2,
            backend_port,
            vpc_id,
            alb_listener_arn,
        )


def invoke_migration_async():
    """
    Invoke this Lambda asynchronously for migration.
    Skips invocation if a migration Lambda already holds the lock.
    """
    if is_creation_lock_held(MIGRATE_USERS_LOCK):
        print("Migration Lambda already running, skipping")
        return

    try:
        lambda_client: "LambdaClient" = boto3.client("lambda")
        function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")

        if not function_name:
            print(
                "Warning: Cannot invoke async migration - function name not available"
            )
            return

        # Invoke this function with a special event to trigger migration
        # Retry every 10s for up to 180s waiting for instances to be ready
        payload = json.dumps(
            {
                "action": "migrate_shared_users",
                "max_wait_seconds": 600,
                "retry_interval": 10,
            }
        )

        lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="Event",  # Async invocation
            Payload=payload,
        )

        print(f"Triggered async migration via Lambda function: {function_name}")

    except Exception as e:
        print(f"Warning: Failed to trigger async migration: {str(e)}")
        # Don't fail the main request if async invocation fails


def invoke_standby_replenishment_async():
    """
    Invoke this Lambda asynchronously to create one replacement standby.
    Skips invocation if a standby-creation Lambda already holds the lock.
    """
    if is_creation_lock_held(CREATE_STANDBY_LOCK):
        print("Standby creation Lambda already running, skipping async replenishment")
        return

    try:
        lambda_client: "LambdaClient" = boto3.client("lambda")
        function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")

        if not function_name:
            print(
                "Warning: Cannot invoke async standby replenishment "
                "- function name not available"
            )
            return

        lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="Event",  # Async invocation
            Payload=json.dumps({"action": "create_standby"}),
        )

        print(
            f"Triggered async standby replenishment via "
            f"Lambda function: {function_name}"
        )

    except Exception as e:
        print(f"Warning: Failed to trigger async standby replenishment: {str(e)}")
        # Don't fail the main request if async invocation fails


def scale_premium_instances_if_needed():
    """
    Scale up premium instances by starting stopped instances or creating new ones.
    Now accounts for pending standby creations to prevent over-provisioning.
    """
    ec2: "EC2Client" = boto3.client("ec2")

    try:
        # Get dynamic capacity limits based on premium users
        max_capacity = get_dynamic_max_capacity()

        # Get all premium instances with their states
        all_instances = get_all_premium_instances_with_states()
        active_users = count_active_premium_users()

        running_instances = [
            i for i in all_instances if i["state"] == InstanceState.RUNNING
        ]
        launching_instances = [
            i
            for i in all_instances
            if i["state"] in [InstanceState.PENDING, InstanceState.LAUNCHING]
        ]

        running_count = len(running_instances)
        launching_count = len(launching_instances)
        total_instances = len(all_instances)

        # Get subscriber count for comparison
        total_subscribers = count_total_premium_users()

        # Check if creation locks are held (another Lambda creating)
        standby_creating = is_creation_lock_held(CREATE_STANDBY_LOCK)
        running_creating = is_creation_lock_held(CREATE_RUNNING_LOCK)

        effective_capacity = running_count + launching_count

        print("Enhanced premium instance analysis:")
        print(f"- Running instances: {running_count}")
        print(f"- Launching instances: {launching_count}")
        print(f"- Standby creation in progress: {standby_creating}")
        print(f"- Running creation in progress: {running_creating}")
        print(f"- Effective capacity: {effective_capacity}")
        print(f"- Total instances: {total_instances}")
        print(f"- Maximum capacity: {max_capacity}")
        print(f"- Active assignments: {active_users}")
        print(f"- Premium subscribers: {total_subscribers}")

        if launching_count > 0:
            print(f"Scaling blocked: {launching_count} " f"instances already launching")
            return False

        # Block scaling while another Lambda is creating instances;
        # we can't know the exact count, so let it finish first.
        if running_creating:
            print("Scaling blocked: running instance " "creation already in progress")
            return False

        # Key decision: Scale based on ACTIVE ASSIGNMENTS, not subscribers
        # This represents current demand (logged-in users) vs available capacity
        if running_count >= active_users:
            print(
                f"Scaling not needed: {running_count} running >= "
                f"{active_users} active assignments"
            )
            print(
                f"(Note: {total_subscribers} total subscribers, "
                f"but only {active_users} currently logged in)"
            )
            return False

        # SCALE UP CONDITIONS:
        needed_capacity = active_users - running_count

        if needed_capacity > 0 and total_instances < max_capacity:
            # Try to start stopped instances first
            stopped_instances = [
                i for i in all_instances if i["state"] == InstanceState.STOPPED
            ]

            if stopped_instances:
                # Start stopped instances
                instances_to_start = min(len(stopped_instances), needed_capacity)
                instance_ids_to_start = [
                    inst["instance_id"]
                    for inst in stopped_instances[:instances_to_start]
                ]

                print(
                    f"Starting {instances_to_start} stopped instances:"
                    f" {instance_ids_to_start}"
                )
                ec2.start_instances(InstanceIds=instance_ids_to_start)

                # Userdata systemd unit clears the checkpoint on every
                # boot; just wait for the instances to be running.
                waiter = ec2.get_waiter("instance_running")
                try:
                    waiter.wait(
                        InstanceIds=instance_ids_to_start,
                        WaiterConfig={
                            "Delay": 5,
                            "MaxAttempts": 24,
                        },
                    )
                except Exception as e:
                    print(f"Waiter error: {e}")

                # Invoke this Lambda asynchronously to handle migration
                # after instances are ready (avoids blocking the user's request)
                invoke_migration_async()

                # Update ECS service desired count to match instance count
                update_premium_service_desired_count()

                return True
            else:
                if total_instances + needed_capacity > max_capacity:
                    print(
                        f"Cannot scale: would exceed max capacity "
                        f"({total_instances + needed_capacity} "
                        f"> {max_capacity})"
                    )
                    return False

                print(
                    f"No stopped instances available, creating "
                    f"{needed_capacity} new running instance(s)"
                )
                created = _create_running_instances_locked(needed_capacity)
                if created:
                    invoke_migration_async()
                    update_premium_service_desired_count()
                return created

        elif total_instances >= max_capacity:
            print(
                f"Scaling blocked: already at maximum capacity "
                f"({total_instances}/{max_capacity})"
            )
            return False

        print(
            f"No scaling needed: running={running_count}, active_users={active_users},"
            f" total={total_instances}, max={max_capacity}"
        )
        return False

    except Exception as e:
        print(f"Error scaling premium instances: {str(e)}")
        return False


def get_ecs_container_instance_id(
    ec2_instance_id: str, cluster_name: str
) -> str | None:
    """Map EC2 instance ID to ECS container instance ID"""
    ecs: "ECSClient" = boto3.client("ecs")

    try:
        print(f"Looking up ECS container instance for EC2 instance {ec2_instance_id}")

        # List only premium container instances in the cluster
        response = ecs.list_container_instances(
            cluster=cluster_name,
            filter="attribute:tier == premium",
        )
        container_instance_arns = response.get("containerInstanceArns", [])

        if not container_instance_arns:
            print(f"No premium container instances found in cluster {cluster_name}")
            print(
                f"[premium-ecs-map] ec2={ec2_instance_id} "
                f"cluster={cluster_name} result=None "
                f"reason=no_premium_container_instances"
            )
            return None

        print(
            f" Found {len(container_instance_arns)} premium container instances "
            f"in cluster"
        )

        # Describe container instances to find the one matching our EC2 instance
        describe_response = ecs.describe_container_instances(
            cluster=cluster_name, containerInstances=container_instance_arns
        )

        matches = [
            ci
            for ci in describe_response.get("containerInstances", [])
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

        if chosen:
            container_instance_id = chosen.get("containerInstanceArn")
            print(f" Found ECS container instance: {container_instance_id}")
            print(
                f"[premium-ecs-map] ec2={ec2_instance_id} "
                f"cluster={cluster_name} "
                f"result={container_instance_id} reason=found "
                f"agent_connected={chosen.get('agentConnected')}"
            )
            return container_instance_id

        print(f"No ECS container instance found for EC2 instance " f"{ec2_instance_id}")
        print(
            f"[premium-ecs-map] ec2={ec2_instance_id} "
            f"cluster={cluster_name} result=None "
            f"reason=no_matching_ec2_in_premium_set "
            f"premium_count={len(container_instance_arns)}"
        )
        return None

    except Exception as e:
        print(f"Error mapping EC2 to ECS container instance: {str(e)}")
        print(
            f"[premium-ecs-map] ec2={ec2_instance_id} "
            f"cluster={cluster_name} result=None "
            f"reason=exception error={str(e)}"
        )
        return None


def deregister_container_instance_from_ecs(ec2_instance_id: str) -> bool:
    """
    Deregister a container instance from ECS before stopping the EC2 instance.

    This prevents "ghost" registrations where stopped EC2 instances remain
    registered in ECS with disconnected agents, confusing the ECS scheduler.

    Args:
        ec2_instance_id: The EC2 instance ID to deregister

    Returns:
        True if deregistration succeeded, False otherwise
    """
    ecs: "ECSClient" = boto3.client("ecs")

    try:
        cluster_name = get_required_env_var("CLUSTER_NAME")
    except ValueError as e:
        print(f"Cannot deregister container instance - missing CLUSTER_NAME: {str(e)}")
        return False

    try:
        # Find the container instance ARN for this EC2 instance
        container_instance_arn = get_ecs_container_instance_id(
            ec2_instance_id, cluster_name
        )

        if not container_instance_arn:
            print(
                f"No container instance found for EC2 {ec2_instance_id} - "
                f"may already be deregistered"
            )
            return True  # Not an error if already gone

        # Deregister the container instance from ECS
        print(
            f"Deregistering container instance {container_instance_arn} "
            f"for EC2 {ec2_instance_id}"
        )
        ecs.deregister_container_instance(
            cluster=cluster_name,
            containerInstance=container_instance_arn,
            force=True,  # Force deregistration even if tasks are running
        )

        print(f"Successfully deregistered container instance for EC2 {ec2_instance_id}")
        return True

    except Exception as e:
        print(
            f"Error deregistering container instance for EC2 {ec2_instance_id}: "
            f"{str(e)}"
        )
        return False


def check_instance_readiness(instance_id: str) -> bool:
    """Check if an instance has a running ECS task and is ready for user assignment"""
    ecs: "ECSClient" = boto3.client("ecs")

    try:
        cluster_name = get_required_env_var("CLUSTER_NAME")
    except ValueError as e:
        print(
            f" Instance readiness check failed - environment configuration error: "
            f"{str(e)}"
        )
        return False

    print(f"Checking readiness for EC2 instance {instance_id}")

    try:
        # First, get the ECS container instance ID from the EC2 instance ID
        ecs_container_instance_id = get_ecs_container_instance_id(
            instance_id, cluster_name
        )

        if not ecs_container_instance_id:
            print(
                f"Cannot find ECS container instance for EC2 instance " f"{instance_id}"
            )
            print("Instance not ready: No ECS container instance mapping")
            return False

        # Get ECS tasks running on this container instance
        print("Listing tasks on ECS container instance...")
        tasks_response = ecs.list_tasks(
            cluster=cluster_name, containerInstance=ecs_container_instance_id
        )

        task_arns = tasks_response.get("taskArns", [])
        print(f" Found {len(task_arns)} tasks on container instance")

        if not task_arns:
            print(f"No tasks running on container instance {ecs_container_instance_id}")
            print("Instance not ready: No ECS tasks running")
            return False

        # Check task status
        print(f"Describing {len(task_arns)} tasks...")
        task_details = ecs.describe_tasks(cluster=cluster_name, tasks=task_arns)

        premium_tasks_running = 0
        for task in task_details.get("tasks", []):
            task_def_arn = task.get("taskDefinitionArn", "")
            last_status = task.get("lastStatus", "")
            desired_status = task.get("desiredStatus", "")

            print(f"- Task: {task_def_arn}")
            print(f"Status: {last_status} (desired: {desired_status})")

            if (
                PremiumInstanceConfig.INSTANCE_IDENTIFIER in task_def_arn.lower()
                and last_status == ECSTaskStatus.RUNNING
            ):
                premium_tasks_running += 1
                print("Premium task running!")

        print(f"Found {premium_tasks_running} running premium tasks")

        if premium_tasks_running > 0:
            print(f" Instance {instance_id} is ready (has running premium tasks)")
            return True
        else:
            print("No running premium tasks found")
            print("Instance not ready: No premium ECS tasks running")
            return False

    except Exception as e:
        print(f"Error checking instance readiness for {instance_id}: {str(e)}")
        print("Instance not ready: Error during readiness check")
        return False


def check_instance_readiness_with_retry(
    instance_id: str, max_wait_seconds: int = 600, retry_interval: int = 10
) -> bool:
    """
    Check if an instance has a running ECS task with retry logic.

    Retries every retry_interval seconds for up to max_wait_seconds.
    This is useful when instances are in 'running' state but ECS tasks
    are still launching (can take 7-10 minutes for cold starts with RDS connection).

    Args:
        instance_id: EC2 instance ID to check
        max_wait_seconds: Maximum time to retry (default 600s / 10 minutes)
        retry_interval: Seconds between retry attempts (default 10s)

    Returns:
        True if instance becomes ready within max_wait_seconds, False otherwise
    """
    elapsed = 0
    attempt = 0
    max_attempts = max_wait_seconds // retry_interval

    print(
        f"Checking instance readiness with retry: {instance_id} "
        f"(max {max_wait_seconds}s, interval {retry_interval}s)"
    )

    while elapsed < max_wait_seconds:
        attempt += 1
        print(f"Attempt {attempt}/{max_attempts} for instance {instance_id}")

        is_ready = check_instance_readiness(instance_id)

        if is_ready:
            print(
                f"Instance {instance_id} became ready after {elapsed}s "
                f"({attempt} attempts)"
            )
            return True

        if elapsed + retry_interval >= max_wait_seconds:
            print(
                f"Instance {instance_id} not ready after {elapsed}s "
                f"({attempt} attempts), giving up"
            )
            break

        print(
            f"Instance {instance_id} not ready yet, waiting {retry_interval}s "
            f"before retry..."
        )
        time.sleep(retry_interval)
        elapsed += retry_interval

    return False


def update_premium_service_desired_count():
    """Update the ECS premium service desired count to match the number
    of premium instances that are either ECS-registered or still inside
    the boot grace period.

    Booting standbys must keep a service slot reserved. If desiredCount
    is dropped during the gap between EC2 `running` and ECS agent
    registration, ECS cancels the pending task placement and never
    re-fires it once the agent finally registers, leaving the instance
    with no premium task and the user stuck on the waiting popup.

    The grace period mirrors cleanup_orphaned_ec2_instances() so the two
    are symmetric: instances we don't yet treat as orphans must keep
    their service slot.
    """
    try:
        cluster_name = get_required_env_var("CLUSTER_NAME")
        service_name = get_required_env_var("PREMIUM_SERVICE_NAME")

        ecs: "ECSClient" = boto3.client("ecs")
        ec2: "EC2Client" = boto3.client("ec2")

        service_response = ecs.describe_services(
            cluster=cluster_name, services=[service_name]
        )

        if not service_response.get("services"):
            print(
                f"Premium service {service_name} not found "
                f"in cluster {cluster_name}"
            )
            return

        current_desired_count = service_response["services"][0]["desiredCount"]
        current_running_count = service_response["services"][0]["runningCount"]

        registered_ec2_ids = _list_premium_ecs_registered_ec2_ids(ecs, cluster_name)
        registered_count = len(registered_ec2_ids)

        now = datetime.now(timezone.utc)
        booting_count = 0
        for instance in _list_premium_ec2_instances_running(ec2):
            if instance["InstanceId"] in registered_ec2_ids:
                continue
            launch_time = instance.get("LaunchTime")
            if not launch_time:
                continue
            age_minutes = (now - launch_time).total_seconds() / 60
            if age_minutes < ORPHAN_GRACE_PERIOD_MINUTES:
                booting_count += 1

        running_premium_count = registered_count + booting_count

        print(
            f"ECS Service Status: "
            f"desired={current_desired_count}, "
            f"running={current_running_count}"
        )
        print(
            f"Premium instances: {registered_count} registered + "
            f"{booting_count} booting"
        )

        if running_premium_count != current_desired_count:
            print(
                f"Updating ECS service desired count: {current_desired_count} "
                f"-> {running_premium_count}"
            )
            ecs.update_service(
                cluster=cluster_name,
                service=service_name,
                desiredCount=running_premium_count,
            )
            print(
                f"ECS service {service_name} updated to desired count "
                f"{running_premium_count}"
            )
        else:
            print(
                f"ECS service desired count already matches instance count "
                f"({running_premium_count})"
            )

    except Exception as e:
        print(f"Error updating premium service desired count: {str(e)}")
        import traceback

        traceback.print_exc()


def trigger_experiment_sync(user_id: int) -> bool:
    """
    Trigger experiment metadata sync for user on their new instance.

    Called after a successful migration to ensure the user's experiment
    metadata is downloaded to the new instance from S3.

    Args:
        user_id: Database user ID to sync experiments for

    Returns:
        True if sync was initiated successfully, False otherwise
    """
    import ssl
    import urllib.request

    alb_dns = os.environ.get("ALB_DNS_NAME")
    internal_secret = os.environ.get("INTERNAL_API_SECRET")

    if not alb_dns or not internal_secret:
        print(
            "Warning: ALB_DNS_NAME or INTERNAL_API_SECRET not configured, "
            "skipping experiment sync"
        )
        return False

    url = f"https://{alb_dns}/system-internal/sync-experiments/{user_id}"
    headers = {
        "X-Internal-Secret": internal_secret,
        "Content-Type": "application/json",
    }

    try:
        req = urllib.request.Request(url, method="POST", headers=headers, data=b"")
        # Skip SSL verification for internal VPC traffic;
        # ALB cert doesn't match AWS-generated hostname
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=10.0, context=context) as response:
            if response.status == 200:
                print(f"Experiment sync initiated for user {user_id}")
                return True
            else:
                print(
                    f"Experiment sync request failed for user {user_id}: "
                    f"status {response.status}"
                )
                return False
    except Exception as e:
        # Don't fail migration if sync fails - user can still work
        print(f"Failed to trigger experiment sync for user {user_id}: {e}")
        return False


def migrate_user_to_dedicated_instance(user_id: int, new_instance_id: str) -> bool:
    """
    Migrate user from shared instance to dedicated instance.

    IMPORTANT: Only migrates users with no active workflows to prevent
    workflow interruption and data loss.
    After successful migration, triggers experiment metadata sync
    on the new instance.
    """
    # Import the utility function
    from premium_user_utils import can_migrate_user

    backend_port = PremiumInstanceConfig.get_backend_port()

    # Check if user can be safely migrated (no active workflows)
    if not can_migrate_user(user_id):
        print(
            f"Cannot migrate user {user_id}: user has active workflows running. "
            f"Will retry on next migration attempt."
        )
        return False

    # Lock-based reserve (no RESERVING row written); aborts have nothing to release
    if not try_reserve_instance_for_migration(new_instance_id, user_id):
        print(
            f"Cannot migrate user {user_id}: instance {new_instance_id} not available"
        )
        return False

    elbv2: "ElasticLoadBalancingv2Client" = boto3.client("elbv2")

    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Get current assignment
                cursor.execute(
                    """SELECT instance_id, target_group_arn,
                       alb_rule_arn, active_workflow_count
                       FROM premium_user_assignments
                       WHERE user_id = %s""",
                    (user_id,),
                )
                assignment = cursor.fetchone()

                if not assignment:
                    print(f"No assignment found for " f"user {user_id}")
                    return False

                # Double-check workflow count (defense in depth)
                active_workflows = assignment.get("active_workflow_count", 0) or 0
                if active_workflows > 0:
                    print(
                        f"Cannot migrate user {user_id}: "
                        f"{active_workflows} active workflows"
                        f" detected in assignment record"
                    )
                    return False

                old_instance_id = assignment["instance_id"]
                # Normalize empty/whitespace strings to None
                old_target_group_arn = (
                    assignment["target_group_arn"] or ""
                ).strip() or None
                old_rule_arn = (assignment["alb_rule_arn"] or "").strip() or None

                # Special handling for autoscaling-pool migration
                if old_instance_id == PremiumAssignment.AUTOSCALING_POOL:
                    # Create or get existing target group (handles duplicate names)
                    vpc_id = get_required_env_var("VPC_ID")
                    new_target_group_arn = create_or_get_target_group(user_id, vpc_id)

                    # Register new instance to new target group
                    print(
                        f"[premium-tg-mutate] site=migrate.autoscaling "
                        f"action=register user={user_id} "
                        f"instance={new_instance_id} "
                        f"tg={new_target_group_arn} "
                        f"port={backend_port}"
                    )
                    elbv2.register_targets(
                        TargetGroupArn=new_target_group_arn,
                        Targets=[{"Id": new_instance_id, "Port": backend_port}],
                    )

                    # Check if old ALB rule exists, create new one if not
                    rule_exists = False
                    if old_rule_arn:
                        try:
                            elbv2.describe_rules(RuleArns=[old_rule_arn])
                            rule_exists = True
                        except elbv2.exceptions.RuleNotFoundException:
                            print(
                                f"Old ALB rule {old_rule_arn} not found, "
                                f"creating new rule"
                            )
                        except Exception as e:
                            if "RuleNotFound" in str(e):
                                print(
                                    f"Old ALB rule not found (error: {e}), "
                                    f"creating new rule"
                                )
                            else:
                                raise

                    if rule_exists:
                        # Update existing ALB rule to point to new target group
                        elbv2.modify_rule(
                            RuleArn=old_rule_arn,
                            Actions=[
                                {
                                    "Type": "forward",
                                    "TargetGroupArn": new_target_group_arn,
                                }
                            ],
                        )
                        new_rule_arn = old_rule_arn
                    else:
                        # Create new ALB rule for this user
                        alb_listener_arn = get_required_env_var("ALB_LISTENER_ARN")
                        routing_secret_key = get_required_env_var("ROUTING_SECRET_KEY")
                        user_uid = get_user_uid_from_id(connection, user_id)
                        routing_id = generate_routing_id(user_uid, routing_secret_key)

                        rule_response = create_alb_rule(
                            listener_arn=alb_listener_arn,
                            conditions=[
                                {
                                    "Field": "http-header",
                                    "HttpHeaderConfig": {
                                        "HttpHeaderName": RoutingHeaders.USER_TIER,
                                        "Values": [
                                            PremiumInstanceConfig.INSTANCE_IDENTIFIER
                                        ],
                                    },
                                },
                                {
                                    "Field": "http-header",
                                    "HttpHeaderConfig": {
                                        "HttpHeaderName": RoutingHeaders.ROUTING_ID,
                                        "Values": [routing_id],
                                    },
                                },
                            ],
                            actions=[
                                {
                                    "Type": "forward",
                                    "TargetGroupArn": new_target_group_arn,
                                }
                            ],
                        )
                        new_rule_arn = rule_response["Rules"][0]["RuleArn"]
                        print(f"Created new ALB rule: {new_rule_arn}")

                    # Update assignment with new target group
                    cursor.execute(
                        """UPDATE premium_user_assignments
                           SET instance_id = %s,
                               target_group_arn = %s,
                               alb_rule_arn = %s,
                               is_shared = 0,
                               last_state_check = NOW()
                           WHERE user_id = %s""",
                        (
                            new_instance_id,
                            new_target_group_arn,
                            new_rule_arn,
                            user_id,
                        ),
                    )
                else:
                    # Normal migration: register new, then deregister old.
                    # Register-before-deregister keeps the TG non-empty across
                    # the swap; reversing the order leaves a brief window where
                    # ALB has zero targets and falls through to the free-tier
                    # default action.
                    if not target_group_exists(old_target_group_arn):
                        print(
                            f"Target group {old_target_group_arn} not found, "
                            f"creating new one for user {user_id}"
                        )
                        vpc_id = get_required_env_var("VPC_ID")
                        old_target_group_arn = create_or_get_target_group(
                            user_id, vpc_id
                        )

                    print(
                        f"[premium-tg-mutate] site=migrate.dedicated "
                        f"action=register user={user_id} "
                        f"instance={new_instance_id} "
                        f"tg={old_target_group_arn} "
                        f"port={backend_port}"
                    )
                    elbv2.register_targets(
                        TargetGroupArn=old_target_group_arn,
                        Targets=[{"Id": new_instance_id, "Port": backend_port}],
                    )

                    print(
                        f"[premium-tg-mutate] site=migrate.dedicated "
                        f"action=deregister user={user_id} "
                        f"instance={old_instance_id} "
                        f"tg={old_target_group_arn} "
                        f"port={backend_port}"
                    )
                    elbv2.deregister_targets(
                        TargetGroupArn=old_target_group_arn,
                        Targets=[{"Id": old_instance_id, "Port": backend_port}],
                    )

                    # Update RDS assignment
                    cursor.execute(
                        """UPDATE premium_user_assignments
                           SET instance_id = %s,
                               target_group_arn = %s,
                               is_shared = 0,
                               last_state_check = NOW()
                           WHERE user_id = %s""",
                        (
                            new_instance_id,
                            old_target_group_arn,
                            user_id,
                        ),
                    )

                connection.commit()

                print(
                    f"Migrated user {user_id} from "
                    f"{old_instance_id} to {new_instance_id}"
                )
                # Trigger experiment sync (fire-and-forget)
                trigger_experiment_sync(user_id)
                return True

    except Exception as e:
        print(f"Error migrating user {user_id}: {str(e)}")
        return False


def _teardown_alb_resources(user_id, rule_arn, target_group_arn):
    """Delete ALB rule and target group for a released user."""
    elbv2: "ElasticLoadBalancingv2Client" = boto3.client("elbv2")
    errors = []

    if rule_arn:
        try:
            elbv2.delete_rule(RuleArn=rule_arn)
            print(f"Deleted ALB rule: {rule_arn}")
        except Exception as rule_error:
            error_msg = f"Error deleting ALB rule: {str(rule_error)}"
            print(error_msg)
            errors.append(error_msg)

    autoscaling_tg_arn = os.environ.get("AUTOSCALING_TARGET_GROUP_ARN")
    if (
        target_group_arn
        and target_group_arn != PremiumAssignment.STANDBY
        and target_group_arn != autoscaling_tg_arn
    ):
        try:
            _delete_premium_tg_unhealthy_alarm(target_group_arn)
            elbv2.delete_target_group(TargetGroupArn=target_group_arn)
            print(f"Deleted target group: {target_group_arn}")
        except Exception as tg_error:
            error_msg = f"Error deleting target group: {str(tg_error)}"
            print(error_msg)
            errors.append(error_msg)
    elif target_group_arn == autoscaling_tg_arn:
        print(
            f"Skipping deletion of shared autoscaling "
            f"target group: {target_group_arn}"
        )

    return errors


def release_premium_user(user_id: int, hard: bool = False) -> Dict[str, Any]:
    """Release premium user from assigned instance.

    By default performs a soft-release (keeps ALB/TG intact for grace period).
    Set hard=True to immediately delete everything (used by finalization and
    explicit logout).

    Always succeeds to prevent logout blocking.
    """

    instance_id = None
    success = True
    errors = []

    try:
        if hard:
            # Hard release: delete row + ALB resources immediately
            try:
                assignment = remove_user_assignment(user_id)
                instance_id = assignment["instance_id"]
                target_group_arn = (
                    assignment["target_group_arn"] or ""
                ).strip() or None
                rule_arn = (assignment["alb_rule_arn"] or "").strip() or None
                print(f"Hard-released user {user_id} from instance {instance_id}")
            except Exception as assignment_error:
                print(
                    f"No assignment found for user {user_id}: "
                    f"{str(assignment_error)}"
                )
                target_group_arn = None
                rule_arn = None

            errors = _teardown_alb_resources(user_id, rule_arn, target_group_arn)
        else:
            # Soft release: mark as pending_release, keep ALB/TG intact
            assignment = soft_release_user_assignment(user_id)
            if assignment:
                instance_id = assignment["instance_id"]
                print(
                    f"Soft-released user {user_id} from instance "
                    f"{instance_id} (grace period "
                    f"{PremiumAssignment.PENDING_RELEASE_GRACE_SECONDS}s)"
                )
            else:
                print(
                    f"No active assignment to release for user {user_id} "
                    f"(may already be pending_release or removed)"
                )

        # Skip scale-down for soft releases (instance still allocated)
        if hard:
            try:
                scale_down_if_possible()
            except Exception as scale_error:
                print(f" Scale down failed but continuing: {str(scale_error)}")

            try:
                active_users = count_active_premium_users()
                if active_users == 0:
                    print(
                        "No premium users remaining, converting idle "
                        "instances to standby immediately"
                    )
                    converted_count = convert_idle_instances_to_standby_immediate()
                    if converted_count > 0:
                        print(
                            f"Immediately converted {converted_count} idle "
                            f"instances to standby after user logout"
                        )
            except Exception as standby_error:
                print(
                    f" Standby conversion failed but continuing: "
                    f"{str(standby_error)}"
                )

        release_type = "hard" if hard else "soft"
        message = f"Premium user {user_id} {release_type} release completed"
        if instance_id:
            message += f" from instance {instance_id}"
        if errors:
            message += f" (with {len(errors)} warnings)"

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": message,
                    "released_instance": instance_id,
                    "success": success,
                    "warnings": errors,
                }
            ),
        }

    except Exception as e:
        error_msg = f"Error releasing premium user {user_id}: {str(e)}"
        print(f" {error_msg}")
        return {
            "statusCode": 200,  # Still return 200 to not block logout
            "body": json.dumps(
                {
                    "message": f"Premium user {user_id} release completed with errors",
                    "released_instance": instance_id,
                    "success": False,
                    "error": error_msg,
                }
            ),
        }


def scale_down_if_possible():
    """Scale down premium instances by stopping idle instances"""
    ec2: "EC2Client" = boto3.client("ec2")

    try:
        # Get dynamic capacity settings
        max_capacity = get_dynamic_max_capacity()
        active_users = count_active_premium_users()
        total_premium_users = count_total_premium_users()

        # Get all premium instances with their states
        all_instances = get_all_premium_instances_with_states()
        running_instances = [
            i for i in all_instances if i["state"] == InstanceState.RUNNING
        ]

        total_instances = len(all_instances)
        occupied_instances = 0

        # Count occupied instances
        for instance in running_instances:
            instance_id = instance["instance_id"]
            assigned_users = get_assigned_users_for_instance(instance_id)
            if assigned_users:
                occupied_instances += 1

        idle_instances = len(running_instances) - occupied_instances

        print(
            f"Scale-down analysis: {total_instances} total, "
            f"{occupied_instances} occupied, "
            f"{idle_instances} idle, {active_users} active users, "
            f"{total_premium_users} total premium users"
            f" (max capacity: {max_capacity})"
        )

        # Conservative scale-down logic:
        # - Keep at least 1 running instance always
        # - Keep enough capacity for quick assignment (active users + 1)
        # - Only stop instances if we have significantly more than needed
        min_running_needed = max(
            1, active_users + 1
        )  # Active users + 1 for quick response

        if len(running_instances) > min_running_needed and idle_instances >= 2:
            # Stop idle instances conservatively
            instances_to_stop = min(
                idle_instances - 1, len(running_instances) - min_running_needed
            )

            # Find idle instances to stop
            idle_instance_ids = []
            for instance in running_instances:
                if len(idle_instance_ids) >= instances_to_stop:
                    break
                instance_id = instance["instance_id"]
                assigned_users = get_assigned_users_for_instance(instance_id)
                if not assigned_users:
                    idle_instance_ids.append(instance_id)

            if idle_instance_ids:
                print(
                    f"Stopping {len(idle_instance_ids)} idle "
                    f"instances: {idle_instance_ids} "
                    f"(min running needed: {min_running_needed})"
                )

                # Deregister from ECS before stopping to prevent ghost registrations
                for instance_id in idle_instance_ids:
                    deregister_container_instance_from_ecs(instance_id)

                ec2.stop_instances(InstanceIds=idle_instance_ids)

                # Register stopped instances as standby in DB so
                # terminate_aged_stopped_instances() can find and
                # terminate them after PREMIUM_STOPPED_MAX_AGE_HOURS.
                for instance_id in idle_instance_ids:
                    try:
                        store_user_assignment(
                            user_id=None,
                            instance_id=instance_id,
                            target_group_arn=PremiumAssignment.STANDBY,
                            rule_arn=PremiumAssignment.STANDBY,
                            instance_state=InstanceState.STOPPED,
                            is_shared=False,
                            is_standby=True,
                        )
                        print(
                            f"Registered stopped instance {instance_id} "
                            f"as standby in database"
                        )
                    except Exception as e:
                        print(
                            f"Failed to register standby for "
                            f"{instance_id}: {str(e)}"
                        )

                # Update ECS service desired count to match remaining running instances
                update_premium_service_desired_count()
            else:
                print("No idle instances found to stop")

        else:
            print(
                f"No scale-down: running={len(running_instances)}, "
                f"min_needed={min_running_needed}, "
                f"idle={idle_instances}"
            )

    except Exception as e:
        print(f"Error scaling down premium instances: {str(e)}")


def get_standby_pool_count():
    """Get count of standby instances by status"""
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT instance_state, COUNT(*) as count
                       FROM premium_user_assignments
                       WHERE is_standby = 1 AND status = 'active'
                       GROUP BY instance_state"""
                )
                results = cursor.fetchall()

                status_counts = {
                    InstanceState.STOPPED: 0,
                    InstanceState.RUNNING: 0,
                    InstanceState.PENDING: 0,
                    InstanceState.STOPPING: 0,
                    InstanceState.STARTING: 0,
                }
                for result in results:
                    status_counts[result["instance_state"]] = result["count"]

                return status_counts
    except Exception as e:
        print(f"Error getting standby pool count: {str(e)}")
        return {
            InstanceState.STOPPED: 0,
            InstanceState.RUNNING: 0,
            InstanceState.PENDING: 0,
            InstanceState.STOPPING: 0,
            InstanceState.STARTING: 0,
        }


def convert_idle_instances_to_standby_immediate() -> int:
    """
    Immediately convert idle running instances to standby for cost optimization.
    This is called after user logout when no premium users remain.
    Returns the number of instances converted.
    """
    cleanup_count = 0

    try:
        # Get all running premium instances
        all_instances = get_all_premium_instances_with_states()
        running_instances = [
            i for i in all_instances if i["state"] == InstanceState.RUNNING
        ]

        for instance in running_instances:
            instance_id = instance["instance_id"]

            # Check if instance has any assigned users
            assigned_users = get_assigned_users_for_instance(instance_id)

            if len(assigned_users) == 0:
                # Instance is idle - convert immediately
                print(
                    f"Converting idle instance {instance_id} to standby "
                    f"(immediate - no premium users)"
                )
                if convert_running_instance_to_standby(instance_id):
                    cleanup_count += 1

        print(f"Immediately converted {cleanup_count} idle instances to standby")
        return cleanup_count

    except Exception as e:
        print(f"Error in immediate standby conversion: {str(e)}")
        return 0


def convert_running_instance_to_standby(instance_id: str):
    """Convert a running instance with no users to a standby instance"""
    ec2: "EC2Client" = boto3.client("ec2")

    try:
        deregister_container_instance_from_ecs(instance_id)

        print(f"Stopping instance {instance_id} to convert to standby")
        ec2.stop_instances(InstanceIds=[instance_id])

        waiter = ec2.get_waiter("instance_stopped")
        waiter.wait(
            InstanceIds=[instance_id], WaiterConfig={"Delay": 15, "MaxAttempts": 20}
        )

        store_user_assignment(
            user_id=None,
            instance_id=instance_id,
            target_group_arn=PremiumAssignment.STANDBY,
            rule_arn=PremiumAssignment.STANDBY,
            instance_state=InstanceState.STOPPED,
            is_shared=False,
            is_standby=True,  # Set standby flag in initial INSERT
        )

        print(f"Successfully converted instance {instance_id} to standby")
        return True

    except Exception as e:
        print(f"Error converting instance {instance_id} to standby: {str(e)}")
        return False


def cleanup_excess_standby_instances(excess_count: int):
    """Remove excess standby instances (terminate oldest ones)"""
    removed_count = 0

    try:
        # Get all standby instances ordered by creation time (oldest first)
        standby_instances = get_available_standby_instances()

        # Terminate the oldest excess instances
        for i in range(min(excess_count, len(standby_instances))):
            instance_data = standby_instances[i]
            instance_id = instance_data["instance_id"]

            if terminate_standby_instance(instance_id):
                removed_count += 1

        return removed_count

    except Exception as e:
        print(f"Error cleaning up excess standby instances: {str(e)}")
        return 0


def _parse_stop_time(state_transition_reason: str):
    """Parse the stop timestamp from EC2 StateTransitionReason.

    EC2 returns reasons like 'User initiated (2024-01-15 10:30:00 GMT)'.
    Returns a datetime or None if parsing fails.
    """
    match = re.search(
        r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) GMT\)", state_transition_reason
    )
    if match:
        return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    return None


def terminate_aged_stopped_instances():
    """Terminate standby instances that have been stoppedlonger than
    PREMIUM_STOPPED_MAX_AGE_HOURS.

    Uses EC2 StateTransitionReason to determine when each instance was actually
    stopped. Falls back to standby_created_at when the EC2 timestamp is not
    parseable (e.g. Server.InternalError, crashes).

    Note: standby_created_at is set when the standby row is created, which
    roughly coincides with when the instance is stopped via
    convert_running_instance_to_standby(). It is not a precise "stopped at"
    timestamp but serves as a conservative fallback.
    """
    max_age_hours = int(os.environ.get("PREMIUM_STOPPED_MAX_AGE_HOURS", "4"))
    ec2: "EC2Client" = boto3.client("ec2")

    try:
        # Get stopped standby instances with fallback timestamp from the database
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT instance_id, standby_created_at
                       FROM premium_user_assignments
                       WHERE is_standby = 1 AND status = 'active'
                         AND instance_state = 'stopped'"""
                )
                standby_rows = cursor.fetchall()

        if not standby_rows:
            print("No stopped standby instances to check")
            return 0

        instance_ids = [row["instance_id"] for row in standby_rows]
        db_fallback = {
            row["instance_id"]: row["standby_created_at"] for row in standby_rows
        }

        # Query EC2 for actual stop times via StateTransitionReason
        response = ec2.describe_instances(InstanceIds=instance_ids)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        cutoff = timedelta(hours=max_age_hours)
        env_prefix = EnvironmentConfig.get_env_prefix()
        aged_instances = []

        for reservation in response["Reservations"]:
            for instance in reservation["Instances"]:
                instance_id = instance["InstanceId"]

                # Defense-in-depth: verify instance belongs to this environment
                tags = {t.get("Key"): t.get("Value") for t in instance.get("Tags", [])}
                name_tag = tags.get("Name", "")
                if not name_tag.lower().startswith(env_prefix.lower()):
                    print(
                        f"Skipping instance {instance_id}: "
                        f"Name '{name_tag}' does not match "
                        f"environment prefix '{env_prefix}'"
                    )
                    continue

                reason = instance.get("StateTransitionReason", "")
                stop_time = _parse_stop_time(reason)
                if stop_time is None:
                    # Fallback to standby_created_at for unparsable reasons
                    # (e.g. Server.InternalError, crashes, empty string)
                    fallback = db_fallback.get(instance_id)
                    if fallback:
                        stop_time = fallback.replace(tzinfo=None)
                        print(
                            f"Using standby_created_at fallback for {instance_id} "
                            f"(reason: '{reason}')"
                        )
                if stop_time and (now - stop_time) > cutoff:
                    aged_instances.append((instance_id, stop_time))

        if not aged_instances:
            print(f"No stopped standby instances older than {max_age_hours} hours")
            return 0

        print(
            f"Found {len(aged_instances)} stopped standby instances "
            f"older than {max_age_hours} hours"
        )

        terminated = 0
        for instance_id, stop_time in aged_instances:
            if terminate_standby_instance(instance_id):
                terminated += 1
                print(
                    f"Terminated aged stopped instance {instance_id} "
                    f"(stopped since: {stop_time})"
                )

        print(f"Terminated {terminated} aged stopped instances")
        return terminated

    except Exception as e:
        print(f"Error terminating aged stopped instances: {str(e)}")
        return 0


def terminate_standby_instance(instance_id: str):
    """Terminate a standby instance and clean up database entry"""
    ec2: "EC2Client" = boto3.client("ec2")

    try:
        print(f"Terminating excess standby instance {instance_id}")

        # Terminate the instance
        ec2.terminate_instances(InstanceIds=[instance_id])

        # Remove from database
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM premium_user_assignments "
                    "WHERE instance_id = %s AND is_standby = 1",
                    (instance_id,),
                )
                connection.commit()

        print(f"Successfully terminated standby instance {instance_id}")
        return True

    except Exception as e:
        print(f"Error terminating standby instance {instance_id}: {str(e)}")
        return False


def cleanup_all_dynamic_instances(base_instance_ids: list) -> dict:
    """
    Terminate all dynamic premium instances and clean up their DB entries.
    Called by the dev scheduler before stopping the environment.

    Dynamic instances are those with tag Service: premium-tier that are NOT
    in the base_instance_ids list (Terraform-managed base instances).

    Args:
        base_instance_ids: List of Terraform-managed instance IDs to preserve
        (stop, not terminate)
    """
    ec2_client: "EC2Client" = boto3.client("ec2")
    base_set = set(base_instance_ids)
    result = {"terminated": [], "errors": [], "db_cleaned": 0}

    try:
        # Query premium-tier instances filtered by environment prefix
        response = ec2_client.describe_instances(
            Filters=[
                {"Name": "tag:Service", "Values": [PremiumInstanceConfig.SERVICE_TAG]},
                {
                    "Name": "tag:Name",
                    "Values": [PremiumInstanceConfig.get_instance_name_pattern()],
                },
                {
                    "Name": "instance-state-name",
                    "Values": ["running", "stopped", "pending", "stopping"],
                },
            ]
        )

        dynamic_ids = []
        for reservation in response.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instance_id = instance["InstanceId"]
                if instance_id not in base_set:
                    dynamic_ids.append(instance_id)

        if not dynamic_ids:
            print("No dynamic premium instances found")
            return result

        print(
            f"Found {len(dynamic_ids)} dynamic premium instances to terminate: "
            f"{dynamic_ids}"
        )

        # Terminate dynamic instances
        try:
            ec2_client.terminate_instances(InstanceIds=dynamic_ids)
            result["terminated"] = dynamic_ids
            print(f"Terminated {len(dynamic_ids)} dynamic instances")
        except Exception as e:
            print(f"Error terminating dynamic instances: {e}")
            result["errors"].append(str(e))

        # Clean up DB entries for terminated instances
        try:
            with get_db_connection() as connection:
                with connection.cursor() as cursor:
                    placeholders = ", ".join(["%s"] * len(dynamic_ids))
                    cursor.execute(
                        f"DELETE FROM premium_user_assignments "
                        f"WHERE instance_id IN ({placeholders})",
                        tuple(dynamic_ids),
                    )
                    result["db_cleaned"] = cursor.rowcount
                    connection.commit()
            print(f"Cleaned up {result['db_cleaned']} DB entries")
        except Exception as e:
            print(f"Error cleaning up DB entries: {e}")
            result["errors"].append(f"db_cleanup: {e}")

    except Exception as e:
        print(f"Error querying dynamic instances: {e}")
        result["errors"].append(str(e))

    return result


def cleanup_failed_standby_instances():
    """Clean up database entries for standby instances that no longer exist in AWS"""
    try:
        # Get all AWS premium instances
        aws_instances = get_all_premium_instances_with_states()
        aws_instance_ids = {i["instance_id"] for i in aws_instances}

        # Get all standby assignments from database
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT instance_id FROM premium_user_assignments
                       WHERE is_standby = 1 AND status = 'active'"""
                )
                db_standby_instances = [row["instance_id"] for row in cursor.fetchall()]

                # Remove database entries for instances that no longer exist
                cleanup_count = 0
                for instance_id in db_standby_instances:
                    if instance_id not in aws_instance_ids:
                        print(
                            f"Cleaning up database entry for terminated "
                            f"standby instance {instance_id}"
                        )
                        cursor.execute(
                            "DELETE FROM premium_user_assignments "
                            "WHERE instance_id = %s AND is_standby = 1",
                            (instance_id,),
                        )
                        cleanup_count += 1

                if cleanup_count > 0:
                    connection.commit()
                    print(f"Cleaned up {cleanup_count} failed standby instance entries")

    except Exception as e:
        print(f"Error cleaning up failed standby instances: {str(e)}")


_DISCONNECT_TAG_KEY = "optinist:agent-disconnected-at"
_AGENT_DISCONNECT_GRACE_SECONDS = 300


def get_host_port_for_instance(
    instance_id: str,
    max_attempts: int = EphemeralPortConfig.RESOLVE_MAX_ATTEMPTS,
    delay: float = EphemeralPortConfig.RESOLVE_DELAY_SECONDS,
) -> Optional[int]:
    """Resolve the studio container's host port on instance_id.

    Returns None if not resolvable within the poll window (networkBindings
    is briefly empty after a task starts). Raises if bindings exist but
    none match the expected containerPort — permanent config drift, not
    transient.
    """
    cluster_name = get_required_env_var("CLUSTER_NAME")
    container_port = PremiumInstanceConfig.get_backend_port()
    ecs_client: "ECSClient" = boto3.client("ecs")

    last_err: Optional[str] = None
    mismatch_error: Optional[str] = None
    for _ in range(max_attempts):
        try:
            ecs_container_instance_id = get_ecs_container_instance_id(
                instance_id, cluster_name
            )
            if not ecs_container_instance_id:
                last_err = "no ECS container instance mapping"
                time.sleep(delay)
                continue

            task_arns = ecs_client.list_tasks(
                cluster=cluster_name,
                containerInstance=ecs_container_instance_id,
            ).get("taskArns", [])
            if not task_arns:
                last_err = "no tasks on container instance"
                time.sleep(delay)
                continue

            task_details = ecs_client.describe_tasks(
                cluster=cluster_name, tasks=task_arns
            )
            observed_container_ports: List[Any] = []
            for task in task_details.get("tasks", []):
                if task.get("lastStatus") != ECSTaskStatus.RUNNING:
                    continue
                for container in task.get("containers", []):
                    bindings = container.get("networkBindings") or []
                    for b in bindings:
                        observed_container_ports.append(b.get("containerPort"))
                    match = next(
                        (
                            b
                            for b in bindings
                            if b.get("containerPort") == container_port
                        ),
                        None,
                    )
                    if match and match.get("hostPort"):
                        return int(match["hostPort"])
            if observed_container_ports:
                mismatch_error = (
                    f"containerPort mismatch on {instance_id}: expected "
                    f"{container_port}, got {observed_container_ports}"
                )
                break
            last_err = "task running but networkBindings still empty"
        except Exception as e:
            last_err = str(e)
        time.sleep(delay)

    if mismatch_error:
        raise RuntimeError(mismatch_error)

    print(
        f"get_host_port_for_instance: could not resolve port for "
        f"{instance_id} after {max_attempts} attempts: {last_err}"
    )
    return None


def get_registered_ports_for_instance(
    target_group_arn: str, instance_id: str
) -> List[int]:
    """Return every port currently registered for instance_id in
    target_group_arn.

    Plural because partial-fix scenarios can leave both the stale and the
    current port registered simultaneously; the reconciler needs the
    full set to deregister all stale entries.
    """
    elbv2: "ElasticLoadBalancingv2Client" = boto3.client("elbv2")
    response = elbv2.describe_target_health(TargetGroupArn=target_group_arn)
    return [
        int(desc["Target"]["Port"])
        for desc in response.get("TargetHealthDescriptions", [])
        if (desc.get("Target") or {}).get("Id") == instance_id
        and (desc.get("Target") or {}).get("Port") is not None
    ]


def reconcile_premium_target_group_ports() -> Dict[str, Any]:
    """Reconcile per-user premium target groups so each registered
    (Id, Port) pair matches the studio container's actual host port on
    that instance.

    Idempotent. Under fixed-port deployments every comparison resolves
    equal and the function is a no-op apart from the read calls. Called
    every 15 minutes by handle_scheduled_monitoring under
    PREMIUM_SCALING_LOCK.
    """
    if os.environ.get("RECONCILE_PREMIUM_TG_PORTS_ENABLED", "true").lower() != "true":
        print("reconcile_premium_target_group_ports: disabled via kill-switch")
        return {"disabled": True}

    elbv2: "ElasticLoadBalancingv2Client" = boto3.client("elbv2")

    summary: Dict[str, Any] = {
        "assignments_scanned": 0,
        "drift_detected": 0,
        "drift_fixed": 0,
        "skipped_no_host_port": 0,
        "healed_missing_tg": 0,
        "errors": 0,
    }

    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT user_id, instance_id, target_group_arn
                       FROM premium_user_assignments
                       WHERE status IN (%s, %s)
                         AND is_standby = 0
                         AND instance_id <> %s
                         AND target_group_arn IS NOT NULL
                         AND target_group_arn <> ''
                         AND target_group_arn NOT IN (%s, %s)
                         AND last_state_check < (NOW() - INTERVAL %s SECOND)""",
                    (
                        PremiumAssignment.ACTIVE,
                        PremiumAssignment.PENDING_RELEASE,
                        PremiumAssignment.AUTOSCALING_POOL,
                        PremiumAssignment.STANDBY,
                        PremiumAssignment.RESERVING,
                        RECONCILE_RECENT_MUTATION_SECONDS,
                    ),
                )
                assignments = cursor.fetchall()
    except Exception as e:
        print(f"reconcile_premium_target_group_ports: DB read failed: {e}")
        summary["errors"] += 1
        return summary

    for row in assignments:
        summary["assignments_scanned"] += 1
        user_id = row["user_id"]
        instance_id = row["instance_id"]
        tg_arn = row["target_group_arn"]

        try:
            if not target_group_exists(tg_arn):
                # Heal instead of skip: a missing TG leaves the row a permanent
                # strand. Drop the row (any orphaned ALB rule is reaped by
                # premium_cleanup) so the next assign reprovisions the rule/TG.
                # A transient probe raise is caught below as an error, not a heal.
                print(
                    f"reconcile: user {user_id} TG {tg_arn} gone — dropping "
                    f"stale assignment so next assign reprovisions routing"
                )
                # Count the heal only after the drop succeeds; a raise is
                # caught below as an error, not a heal.
                remove_user_assignment(user_id)
                summary["healed_missing_tg"] += 1
                continue

            actual_port = get_host_port_for_instance(instance_id)
            if actual_port is None:
                summary["skipped_no_host_port"] += 1
                continue

            registered_ports = get_registered_ports_for_instance(tg_arn, instance_id)
            stale_ports = [p for p in registered_ports if p != actual_port]
            current_registered = actual_port in registered_ports

            if not stale_ports and current_registered:
                continue

            summary["drift_detected"] += 1
            print(
                f"reconcile: user {user_id} instance {instance_id} drift "
                f"detected — registered={registered_ports}, "
                f"actual_host_port={actual_port}, tg={tg_arn}"
            )

            # Register first (additive), then deregister stale, so the TG
            # always has at least one entry across the transition.
            if not current_registered:
                elbv2.register_targets(
                    TargetGroupArn=tg_arn,
                    Targets=[{"Id": instance_id, "Port": actual_port}],
                )
            for stale in stale_ports:
                elbv2.deregister_targets(
                    TargetGroupArn=tg_arn,
                    Targets=[{"Id": instance_id, "Port": stale}],
                )

            summary["drift_fixed"] += 1
            print(
                f"reconcile: user {user_id} converged TG {tg_arn} to port "
                f"{actual_port} (removed stale {stale_ports})"
            )
        except Exception as e:
            summary["errors"] += 1
            print(f"reconcile: user {user_id} instance {instance_id} " f"failed: {e}")
            continue

    print(f"reconcile_premium_target_group_ports summary: {summary}")
    return summary


def cleanup_ghost_ecs_registrations():
    """Deregister ghost premium container instances from the ECS cluster.

    Only targets instances with attribute:tier == premium.

    Deregistration rules (EC2 state decides first; agentConnected is consulted
    only after the instance is alive):
      - No ec2InstanceId mapping: deregister immediately.
      - EC2 stopped/terminated/shutting-down/gone: deregister immediately.
      - EC2 running + agent connected: clear any stale disconnect tag.
      - EC2 running + agent disconnected: tag with a timestamp on first
        sighting, deregister after _AGENT_DISCONNECT_GRACE_SECONDS. The tag
        is cleared automatically if the agent reconnects.

    Called every 15 minutes by handle_scheduled_monitoring.
    """
    ecs: "ECSClient" = boto3.client("ecs")
    ec2: "EC2Client" = boto3.client("ec2")

    try:
        cluster_name = get_required_env_var("CLUSTER_NAME")
    except ValueError as e:
        print(
            f"Cannot cleanup ghost ECS registrations - missing CLUSTER_NAME: {str(e)}"
        )
        return

    try:
        # List only premium container instances in the cluster
        response = ecs.list_container_instances(
            cluster=cluster_name,
            filter="attribute:tier == premium",
        )
        container_instance_arns = response.get("containerInstanceArns", [])

        if not container_instance_arns:
            print(
                "No premium container instances found in cluster - nothing to cleanup"
            )
            return

        # Describe container instances to check agent status and EC2 mapping
        describe_response = ecs.describe_container_instances(
            cluster=cluster_name, containerInstances=container_instance_arns
        )

        ghost_instances = []
        reconnected_ec2_ids = []

        container_instances = describe_response.get("containerInstances", [])

        ec2_with_live_ci = {
            ci["ec2InstanceId"]
            for ci in container_instances
            if ci.get("agentConnected")
            and ci.get("status") == "ACTIVE"
            and ci.get("ec2InstanceId")
        }

        # Batched describe_instances has no partial-result mode
        # — any unknown ID fails the call.
        ec2_ids = [
            ci["ec2InstanceId"] for ci in container_instances if ci.get("ec2InstanceId")
        ]
        ec2_state_by_id: dict = {}
        ec2_tags_by_id: dict = {}

        def _record(reservations):
            for reservation in reservations:
                for instance in reservation.get("Instances", []):
                    inst_id = instance.get("InstanceId")
                    if not inst_id:
                        continue
                    ec2_state_by_id[inst_id] = instance["State"]["Name"]
                    ec2_tags_by_id[inst_id] = {
                        t["Key"]: t["Value"] for t in instance.get("Tags", [])
                    }

        if ec2_ids:
            try:
                ec2_response = ec2.describe_instances(InstanceIds=ec2_ids)
                _record(ec2_response.get("Reservations", []))
            except ClientError as e:
                if e.response["Error"]["Code"] != "InvalidInstanceID.NotFound":
                    raise
                for ec2_id in ec2_ids:
                    try:
                        per = ec2.describe_instances(InstanceIds=[ec2_id])
                        _record(per.get("Reservations", []))
                    except ClientError as inner:
                        if (
                            inner.response["Error"]["Code"]
                            != "InvalidInstanceID.NotFound"
                        ):
                            raise

        for container_instance in container_instances:
            container_instance_arn = container_instance.get("containerInstanceArn")
            ec2_instance_id = container_instance.get("ec2InstanceId")
            agent_connected = container_instance.get("agentConnected", False)
            status = container_instance.get("status", "UNKNOWN")

            # EC2 state decides first — a terminated EC2 is a ghost even
            # if the ECS agent still briefly reports as connected.
            if not ec2_instance_id:
                ghost_instances.append(
                    {
                        "container_instance_arn": container_instance_arn,
                        "ec2_instance_id": ec2_instance_id,
                        "reason": "Container instance has no EC2 mapping",
                        "status": status,
                    }
                )
                continue

            instance_state = ec2_state_by_id.get(ec2_instance_id)
            if instance_state is None:
                ghost_instances.append(
                    {
                        "container_instance_arn": container_instance_arn,
                        "ec2_instance_id": ec2_instance_id,
                        "reason": "EC2 instance does not exist",
                        "status": status,
                    }
                )
                continue

            if instance_state in [
                InstanceState.STOPPED,
                InstanceState.TERMINATED,
                InstanceState.SHUTTING_DOWN,
            ]:
                ghost_instances.append(
                    {
                        "container_instance_arn": container_instance_arn,
                        "ec2_instance_id": ec2_instance_id,
                        "reason": f"EC2 instance is {instance_state}",
                        "status": status,
                    }
                )
                continue

            # EC2 is alive. Agent connected → clear any disconnect tag.
            if agent_connected:
                reconnected_ec2_ids.append(ec2_instance_id)
                continue

            # Disconnected CI sharing its EC2 with a live CI is superseded; reap now.
            if ec2_instance_id in ec2_with_live_ci:
                ghost_instances.append(
                    {
                        "container_instance_arn": container_instance_arn,
                        "ec2_instance_id": ec2_instance_id,
                        "reason": "superseded by live container instance on same EC2",
                        "status": status,
                    }
                )
                continue

            # EC2 alive + agent disconnected, no live sibling — apply grace period.
            tags = ec2_tags_by_id.get(ec2_instance_id, {})
            first_seen = tags.get(_DISCONNECT_TAG_KEY)

            if not first_seen:
                now_str = datetime.now(timezone.utc).isoformat()
                print(
                    f"Agent disconnected on {ec2_instance_id}, "
                    f"starting grace period"
                )
                ec2.create_tags(
                    Resources=[ec2_instance_id],
                    Tags=[
                        {
                            "Key": _DISCONNECT_TAG_KEY,
                            "Value": now_str,
                        }
                    ],
                )
                continue

            try:
                first_seen_dt = datetime.fromisoformat(first_seen)
            except (ValueError, TypeError):
                first_seen_dt = datetime.now(timezone.utc)

            elapsed = (datetime.now(timezone.utc) - first_seen_dt).total_seconds()

            if elapsed < _AGENT_DISCONNECT_GRACE_SECONDS:
                print(
                    f"Agent disconnected on {ec2_instance_id} "
                    f"for {int(elapsed)}s, within grace period"
                )
                continue

            ghost_instances.append(
                {
                    "container_instance_arn": container_instance_arn,
                    "ec2_instance_id": ec2_instance_id,
                    "reason": (
                        f"ECS agent disconnected for "
                        f"{int(elapsed)}s (grace period "
                        f"{_AGENT_DISCONNECT_GRACE_SECONDS}s)"
                    ),
                    "status": status,
                }
            )

        # Clear disconnect tags on instances whose agents have reconnected
        if reconnected_ec2_ids:
            try:
                ec2.delete_tags(
                    Resources=reconnected_ec2_ids,
                    Tags=[{"Key": _DISCONNECT_TAG_KEY}],
                )
            except Exception as e:
                print(f"Warning: failed to clear disconnect tags: {str(e)}")

        if not ghost_instances:
            print("No ghost ECS registrations found")
            return

        print(f"Found {len(ghost_instances)} ghost ECS registrations to cleanup")

        # Deregister ghost container instances
        cleanup_count = 0
        for ghost in ghost_instances:
            try:
                print(
                    f"Deregistering ghost container instance "
                    f"{ghost['container_instance_arn']} "
                    f"(EC2: {ghost['ec2_instance_id']}, reason: {ghost['reason']})"
                )
                ecs.deregister_container_instance(
                    cluster=cluster_name,
                    containerInstance=ghost["container_instance_arn"],
                    force=True,
                )
                # Clean up the disconnect tag after successful deregistration
                if ghost["ec2_instance_id"]:
                    try:
                        ec2.delete_tags(
                            Resources=[ghost["ec2_instance_id"]],
                            Tags=[{"Key": _DISCONNECT_TAG_KEY}],
                        )
                    except Exception:
                        pass
                cleanup_count += 1
            except Exception as e:
                print(
                    f"Failed to deregister ghost container instance "
                    f"{ghost['container_instance_arn']}: {str(e)}"
                )

        print(f"Cleaned up {cleanup_count} ghost ECS registrations")

    except Exception as e:
        print(f"Error cleaning up ghost ECS registrations: {str(e)}")


# Shared by update_premium_service_desired_count and
# cleanup_orphaned_ec2_instances. Typical boot-to-ECS-register takes
# 1-2 minutes; 10 gives a comfortable multiple while capping how long
# a permanently-broken boot can inflate desiredCount or delay orphan
# cleanup to a single reconciliation cycle.
ORPHAN_GRACE_PERIOD_MINUTES = 10


def _list_premium_ecs_registered_ec2_ids(ecs, cluster_name) -> set:
    """EC2 instance IDs backing ACTIVE premium ECS container instances."""
    ci_response = ecs.list_container_instances(
        cluster=cluster_name,
        filter="attribute:tier == premium",
        status="ACTIVE",
    )
    ci_arns = ci_response.get("containerInstanceArns", [])
    if not ci_arns:
        return set()
    desc = ecs.describe_container_instances(
        cluster=cluster_name,
        containerInstances=ci_arns,
    )
    return {ci["ec2InstanceId"] for ci in desc.get("containerInstances", [])}


def _list_premium_ec2_instances_running(ec2) -> list:
    """Running EC2 instances tagged as premium for this environment.

    Single source of truth for the premium-EC2 tag/name filter so the
    desired-count and orphan-cleanup paths cannot drift apart — any
    instance one path sees, the other must see.
    """
    resp = ec2.describe_instances(
        Filters=[
            {
                "Name": "instance-state-name",
                "Values": [InstanceState.RUNNING],
            },
            {
                "Name": "tag:Tier",
                "Values": [
                    PremiumInstanceConfig.INSTANCE_IDENTIFIER,
                    PremiumInstanceConfig.INSTANCE_IDENTIFIER.capitalize(),
                ],
            },
            {
                "Name": "tag:Name",
                "Values": [PremiumInstanceConfig.get_instance_name_pattern()],
            },
        ]
    )
    return [i for r in resp["Reservations"] for i in r["Instances"]]


def cleanup_orphaned_ec2_instances():
    """Stop EC2 instances tagged Tier=premium that are running
    but not registered as ECS container instances.

    Orphaned instances inflate desiredCount and waste resources. The
    grace period avoids stopping instances that are still booting and
    haven't joined ECS yet; must stay symmetric with
    update_premium_service_desired_count.
    """
    try:
        cluster_name = get_required_env_var("CLUSTER_NAME")
        ecs: "ECSClient" = boto3.client("ecs")
        ec2: "EC2Client" = boto3.client("ec2")

        ecs_ec2_ids = _list_premium_ecs_registered_ec2_ids(ecs, cluster_name)
        instances = _list_premium_ec2_instances_running(ec2)

        now = datetime.now(timezone.utc)
        stopped_count = 0

        for instance in instances:
            iid = instance["InstanceId"]
            if iid in ecs_ec2_ids:
                continue

            launch_time = instance.get("LaunchTime")
            if launch_time:
                age_minutes = (now - launch_time).total_seconds() / 60
                if age_minutes < ORPHAN_GRACE_PERIOD_MINUTES:
                    print(
                        f"Orphan {iid} running "
                        f"{age_minutes:.0f}m, "
                        f"within grace period"
                    )
                    continue

            print(f"Stopping orphaned EC2 instance {iid}")
            ec2.stop_instances(InstanceIds=[iid])
            stopped_count += 1

            # Register as standby so terminate_aged_stopped_instances()
            # can find and terminate after PREMIUM_STOPPED_MAX_AGE_HOURS.
            try:
                store_user_assignment(
                    user_id=None,
                    instance_id=iid,
                    target_group_arn=PremiumAssignment.STANDBY,
                    rule_arn=PremiumAssignment.STANDBY,
                    instance_state=InstanceState.STOPPED,
                    is_shared=False,
                    is_standby=True,
                )
                print(f"Registered orphaned instance {iid} " f"as standby in database")
            except Exception as e:
                print(f"Failed to register standby for " f"{iid}: {str(e)}")

        print(f"Orphan cleanup: stopped {stopped_count} " f"instance(s)")

    except Exception as e:
        print(f"Error cleaning up orphaned EC2 instances: " f"{str(e)}")


def get_premium_system_status() -> Dict[str, Any]:
    """Get comprehensive status of premium system including standby pool"""
    try:
        # Get instance states
        all_instances = get_all_premium_instances_with_states()
        running_count = len(
            [i for i in all_instances if i["state"] == InstanceState.RUNNING]
        )
        launching_count = len(
            [
                i
                for i in all_instances
                if i["state"] in [InstanceState.PENDING, InstanceState.LAUNCHING]
            ]
        )

        # Get user counts
        total_premium_users = count_total_premium_users()
        active_users = count_active_premium_users()

        # Get standby pool status
        standby_instances = get_available_standby_instances()
        standby_count = len(standby_instances)
        standby_pool_counts = get_standby_pool_count()

        # Get capacity calculations
        max_capacity = get_dynamic_max_capacity()

        return {
            "instances": {
                "running": running_count,
                "launching": launching_count,
                "total": len(all_instances),
            },
            "users": {
                "total_premium": total_premium_users,
                "active": active_users,
            },
            "standby_pool": {
                "available": standby_count,
                "by_status": standby_pool_counts,
                "required_size": int(os.environ.get("PREMIUM_STANDBY_POOL_SIZE", "1")),
            },
            "capacity": {
                "max_capacity": max_capacity,
                "current_utilization": f"{active_users}/{max_capacity}",
            },
            "timestamp": time.time(),
        }

    except Exception as e:
        print(f"Error getting premium system status: {str(e)}")
        return {"error": str(e)}


def process_shared_instance_optimization() -> Dict[str, Any]:
    """
    Optimize shared instances by migrating users to available dedicated instances.
    Called during assignment operations to improve resource allocation.
    IMPORTANT: Only migrates users with no active workflows (active_workflow_count = 0)
    to prevent workflow interruption. Users with running workflows are automatically
    skipped and will be migrated on subsequent attempts after their workflows complete.

    Uses dynamic instance discovery by tags instead of hardcoded PREMIUM_INSTANCE_IDS,
    ensuring newly created/started instances are included in migration checks.
    """
    try:
        print(" Checking for shared instance optimization opportunities")

        # Get all premium instances dynamically by tags (not hardcoded list)
        # This ensures newly created/started instances are included
        all_instances = get_all_premium_instances_with_states()

        print(f"Discovered {len(all_instances)} total premium instances by tags")

        available_instances = []
        shared_instances = []

        # Check for users temporarily assigned to autoscaling pool
        autoscaling_users = get_assigned_users_for_instance(
            PremiumAssignment.AUTOSCALING_POOL
        )
        if autoscaling_users:
            print(
                f"Found {len(autoscaling_users)} users on autoscaling pool "
                f"needing migration"
            )
            shared_instances.append(
                (PremiumAssignment.AUTOSCALING_POOL, autoscaling_users)
            )

        for instance in all_instances:
            instance_id = instance["instance_id"]
            instance_state = instance["state"]

            print(f"Checking instance {instance_id} (state: {instance_state})")

            if instance_state == InstanceState.RUNNING:
                assigned_users = get_assigned_users_for_instance(instance_id)

                # Filter out standby assignments - they have no real user
                # Standby instances have is_standby=1 and user_id=NULL
                real_users = [
                    u
                    for u in assigned_users
                    if u.get("user_id") is not None and not u.get("is_standby")
                ]

                print(
                    f"Instance {instance_id}: {len(assigned_users)} total assignments, "
                    f"{len(real_users)} real users"
                )

                # Short timeout: don't block waiting for
                # unready instances during migration checks
                if not real_users and check_instance_readiness_with_retry(
                    instance_id,
                    max_wait_seconds=30,
                    retry_interval=10,
                ):
                    available_instances.append(instance_id)
                    print(f"Instance {instance_id} is available for migration")
                elif real_users:
                    # Check if this is a shared instance:
                    # 1. Multiple users on the instance, OR
                    # Check if any user has is_shared=1 flag
                    has_shared_flag = any(
                        user.get("is_shared", 0) == 1 for user in real_users
                    )
                    if len(real_users) > 1 or has_shared_flag:
                        shared_instances.append((instance_id, real_users))
                        print(
                            f"Instance {instance_id} marked for migration: "
                            f"{len(real_users)} users, "
                            f"has_shared_flag={has_shared_flag}"
                        )

        # If we have users needing migration but no running instances,
        # trigger scaling to start stopped instances
        if shared_instances and not available_instances:
            print(
                "Users need migration but no running instances available. "
                "Triggering scaling to start stopped instances..."
            )
            scaled = scale_premium_instances_if_needed()
            return {
                "migrations_performed": 0,
                "available_instances": 0,
                "shared_instances_found": len(shared_instances),
                "scaling_triggered": scaled,
                "message": "No running instances - triggered scaling",
            }

        # Only migrate if we have available instances
        if not available_instances or not shared_instances:
            return {
                "migrations_performed": 0,
                "available_instances": len(available_instances),
                "shared_instances_found": len(shared_instances),
                "message": "No optimization opportunities found",
            }

        # Check if we have enough instances for all users needing migration
        total_users_needing_migration = 0
        for instance_id, users in shared_instances:
            if instance_id == PremiumAssignment.AUTOSCALING_POOL:
                total_users_needing_migration += len(users)  # All autoscaling users
            else:
                total_users_needing_migration += len(users) - 1  # Shared, keep one

        print(
            f"Need to migrate {total_users_needing_migration} users, "
            f"have {len(available_instances)} available instances"
        )

        # If insufficient capacity, trigger scaling for additional instances
        if len(available_instances) < total_users_needing_migration:
            shortage = total_users_needing_migration - len(available_instances)
            print(
                f"Insufficient capacity: need {shortage} more instances. "
                f"Triggering scaling..."
            )
            scale_premium_instances_if_needed()

        migrations_performed = 0

        for instance_id, users in shared_instances:
            if not available_instances:
                break

            # Determine which users to migrate based on instance type
            if instance_id == PremiumAssignment.AUTOSCALING_POOL:
                users_to_migrate = users
                print(f"Migrating ALL {len(users)} users from autoscaling pool")
            elif len(users) == 1:
                users_to_migrate = users
                print(
                    f"Migrating single user {users[0].get('user_id')} "
                    f"incorrectly marked as shared"
                )
            else:
                # Multiple users - migrate those with is_shared=1, or all but first
                users_with_shared_flag = [
                    u for u in users if u.get("is_shared", 0) == 1
                ]
                if users_with_shared_flag:
                    users_to_migrate = users_with_shared_flag
                    print(
                        f"Migrating {len(users_to_migrate)} users with is_shared flag "
                        f"from {instance_id}"
                    )
                else:
                    users_to_migrate = users[1:]  # Keep first user, migrate others
                    print(
                        f"Migrating {len(users_to_migrate)} users from "
                        f"shared premium instance"
                    )

            for user_dict in users_to_migrate:
                user_id = user_dict.get("user_id")
                if not user_id:
                    print(f"Warning: Skipping user with missing user_id: {user_dict}")
                    continue

                if not available_instances:
                    break

                # Try instances until one succeeds (handles concurrent claims)
                migration_successful = False
                while available_instances and not migration_successful:
                    new_instance_id = available_instances.pop(0)

                    if migrate_user_to_dedicated_instance(user_id, new_instance_id):
                        migrations_performed += 1
                        migration_successful = True
                        print(
                            f"Optimized: Migrated user {user_id} to "
                            f"dedicated instance {new_instance_id}"
                        )
                    else:
                        print(f"Instance {new_instance_id} unavailable, trying next...")

                if not migration_successful:
                    print(f"Could not migrate user {user_id}: no available instances")

        print(
            f"Shared instance optimization complete: "
            f"{migrations_performed} users migrated"
        )

        return {
            "migrations_performed": migrations_performed,
            "shared_instances_found": len(shared_instances),
            "available_instances": len(available_instances),
            "message": f"Optimized {migrations_performed} user assignments",
        }

    except Exception as e:
        print(f" Error during shared instance optimization: {str(e)}")
        return {"error": str(e), "migrations_performed": 0}


@with_transaction
def fix_incorrect_is_shared_flags(connection) -> Dict[str, Any]:
    """
    Fix users incorrectly marked as is_shared=1 who are alone on their instance.

    This handles users who were migrated to dedicated instances but still have
    is_shared=1, which causes them to be flagged for migration in an infinite loop.
    """
    with connection.cursor() as cursor:
        # Find users with is_shared=1 who are the only active user on their instance
        cursor.execute(
            """
            SELECT pa.user_id, pa.instance_id
            FROM premium_user_assignments pa
            WHERE pa.is_shared = 1 AND pa.status = 'active' AND pa.is_standby = 0
              AND pa.instance_id != %s
              AND (SELECT COUNT(*) FROM premium_user_assignments pa2
                   WHERE pa2.instance_id = pa.instance_id
                   AND pa2.status = 'active' AND pa2.is_standby = 0) = 1
        """,
            (PremiumAssignment.AUTOSCALING_POOL,),
        )
        users_to_fix = cursor.fetchall()

        fixed_users = []
        for user in users_to_fix:
            cursor.execute(
                "UPDATE premium_user_assignments SET is_shared = 0 WHERE user_id = %s",
                (user["user_id"],),
            )
            fixed_users.append(
                {"user_id": user["user_id"], "instance_id": user["instance_id"]}
            )
            print(
                f"Fixed is_shared flag for user {user['user_id']} "
                f"on instance {user['instance_id']}"
            )

        return {"fixed_count": len(users_to_fix), "fixed_users": fixed_users}


@with_transaction
def update_user_activity_timestamp(connection, user_id: int) -> bool:
    """Update activity timestamp for a user with proper transaction isolation.

    Also restores a row from `pending_release` back to `active` so an explicit
    heartbeat from one tab heals a soft-release triggered by another tab's
    close. Mirrors the user_activity_middleware behaviour. The instance_state
    guard preserves the dead-EC2 escape valve.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE premium_user_assignments
            SET last_activity = CURRENT_TIMESTAMP,
                status = CASE
                    WHEN status = %s THEN %s
                    ELSE status
                END
            WHERE user_id = %s
            AND status IN (%s, %s)
            AND is_standby = 0
            AND (
                status = %s
                OR instance_state IS NULL
                OR instance_state NOT IN
                    ('terminated','shutting-down','stopped','stopping')
            )
            """,
            (
                PremiumAssignment.PENDING_RELEASE,
                PremiumAssignment.ACTIVE,
                user_id,
                PremiumAssignment.ACTIVE,
                PremiumAssignment.PENDING_RELEASE,
                PremiumAssignment.ACTIVE,
            ),
        )
        return cursor.rowcount > 0


def handle_activity_update(user_id: int) -> Dict[str, Any]:
    """Handle heartbeat activity update for a premium user"""
    try:
        print(f" Processing activity update for user {user_id}")

        # Update the user's activity timestamp using transaction-safe function
        success = update_user_activity_timestamp(user_id)

        if success:
            return {
                "statusCode": 200,
                "body": json.dumps(
                    {
                        "message": f"Activity updated for user {user_id}",
                        "user_id": user_id,
                        "timestamp": time.time(),
                    }
                ),
            }
        else:
            # User might not have an active assignment - not necessarily an error
            return {
                "statusCode": 200,
                "body": json.dumps(
                    {
                        "message": f"No active assignment found for user {user_id}",
                        "user_id": user_id,
                        "updated": False,
                    }
                ),
            }

    except Exception as e:
        print(f"Error handling activity update for user {user_id}: {str(e)}")
        return {
            "statusCode": 200,  # Don't fail heartbeats
            "body": json.dumps(
                {
                    "message": f"Activity update completed with "
                    f"warnings for user {user_id}",
                    "user_id": user_id,
                    "error": str(e),
                }
            ),
        }


# cleanup_stale_assignments function moved to premium_cleanup Lambda


def update_user_activity(user_id: int) -> bool:
    """Update last_activity timestamp for a user's assignment"""
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE premium_user_assignments
                    SET last_activity = CURRENT_TIMESTAMP
                    WHERE user_id = %s AND is_standby = 0
                """,
                    (user_id,),
                )

                connection.commit()
                if cursor.rowcount > 0:
                    print(f" Updated activity timestamp for user {user_id}")
                    return True
                else:
                    print(f" No assignment found to update activity for user {user_id}")
                    return False

    except Exception as e:
        print(f" Error updating activity for user {user_id}: {str(e)}")
        return False
