"""Shared fixtures and path setup for Lambda infrastructure tests."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Compute project root from conftest location:
# conftest is at studio/tests/infrastructure/conftest.py
# project root is 3 levels up
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Support both local dev and Docker layouts:
#   Local: PROJECT_ROOT/infrastructure/terraform/<package>
#   Docker: PROJECT_ROOT/terraform/<package>
TERRAFORM_DIR = PROJECT_ROOT / "infrastructure" / "terraform"
if not TERRAFORM_DIR.exists():
    TERRAFORM_DIR = PROJECT_ROOT / "terraform"

_PACKAGE_NAMES = [
    "premium_manager_package",
    "premium_cleanup_package",
    "free_manager_package",
    "free_cleanup_package",
    "common_user_manager_package",
    "dev_scheduler_package",
]

# Add Lambda package directories to sys.path so imports work
_LAMBDA_PATHS = [TERRAFORM_DIR / name for name in _PACKAGE_NAMES]

# Single-file Lambdas live directly in terraform/ rather than in a package dir.
_LAMBDA_PATHS.append(TERRAFORM_DIR)

# aws_constants lives at infrastructure/aws_constants.py
_LAMBDA_PATHS.append(PROJECT_ROOT / "infrastructure")

# Operator scripts (e.g. check_ecs_image_drift) are importable modules, not a
# package. Same dual layout as TERRAFORM_DIR above: infrastructure/scripts
# locally, scripts/ in the Docker image.
SCRIPTS_DIR = PROJECT_ROOT / "infrastructure" / "scripts"
if not SCRIPTS_DIR.exists():
    SCRIPTS_DIR = PROJECT_ROOT / "scripts"
_LAMBDA_PATHS.append(SCRIPTS_DIR)

for p in _LAMBDA_PATHS:
    p_str = str(p)
    if p_str not in sys.path:
        sys.path.insert(0, p_str)

# Ensure the real aws_constants module is loaded from the paths above.
# Other test files may install a limited mock into sys.modules;
# remove it first so we get the real module.
sys.modules.pop("aws_constants", None)
import aws_constants  # noqa: E402, F401


class MockRow:
    """Mock database row that supports both dict and index access."""

    def __init__(self, data):
        self.data = data

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.data.values())[key]
        return self.data.get(key)

    def get(self, key, default=None):
        return self.data.get(key, default)


# Base mock environment variables shared across premium test files
MOCK_ENV_VARS_BASE = {
    "RDS_HOST": "test-db.example.com:3306",
    "RDS_USER": "test_user",
    "RDS_PASSWORD": "test_pass",
    "RDS_DATABASE": "test_db",
    "VPC_ID": "vpc-test123",
    "ALB_LISTENER_ARN": (
        "arn:aws:elasticloadbalancing:region:account:" "listener/test"
    ),
    "ROUTING_SECRET_KEY": "test-secret-key-12345",
}

MOCK_ENV_VARS_PREMIUM = {
    **MOCK_ENV_VARS_BASE,
    "ENV_PREFIX": "test",
    "AUTOSCALING_TARGET_GROUP_ARN": (
        "arn:aws:elasticloadbalancing:region:account:" "targetgroup/asg"
    ),
    "CLUSTER_NAME": "test-cluster",
    "PREMIUM_SERVICE_NAME": "subscr-optinist-premium-service",
    "PREMIUM_INSTANCE_IDS": "i-test1,i-test2,i-test3",
    "PREMIUM_STANDBY_POOL_SIZE": "2",
    "PREMIUM_IDLE_TIMEOUT_HOURS": "3",
    "PREMIUM_EXTRA_CAPACITY": "1",
    "ABSOLUTE_MAX": "10",
}

MOCK_ENV_VARS_FREE = {
    **MOCK_ENV_VARS_BASE,
    "ENV_PREFIX": "test",
    "CLUSTER_NAME": "test-cluster",
    "FREE_SERVICE_NAME": "subscr-optinist-cloud-service",
    "ASG_NAME": "test-free-asg",
    "FREE_USER_THRESHOLD": "5",
    "FREE_IDLE_THRESHOLD_MINUTES": "10",
    "MAX_FREE_INSTANCES": "10",
}

MOCK_ENV_VARS_COMMON = {
    **MOCK_ENV_VARS_BASE,
    "ENV_PREFIX": "test",
    "AUTOSCALING_TARGET_GROUP_ARN": (
        "arn:aws:elasticloadbalancing:region:account:" "targetgroup/asg"
    ),
    "CLUSTER_NAME": "test-cluster",
    "FREE_IDLE_TIMEOUT_HOURS": "2",
    "PREMIUM_IDLE_TIMEOUT_HOURS": "2",
}


@pytest.fixture
def mock_env_vars_premium():
    return {**MOCK_ENV_VARS_PREMIUM}


@pytest.fixture
def mock_env_vars_free():
    return {**MOCK_ENV_VARS_FREE}


@pytest.fixture
def mock_env_vars_common():
    return {**MOCK_ENV_VARS_COMMON}


def setup_db_mock(fetchone_values=None, fetchall_values=None):
    """
    Create a properly configured database mock.

    Args:
        fetchone_values: List of values for fetchone() calls
        fetchall_values: List of values for fetchall() calls

    Returns:
        A mock connection object
    """
    mock_cursor = MagicMock()
    mock_cursor.rowcount = 1

    if fetchone_values is not None:
        mock_cursor.fetchone.side_effect = fetchone_values
    else:
        mock_cursor.fetchone.side_effect = lambda: None

    if fetchall_values is not None:
        mock_cursor.fetchall.side_effect = fetchall_values
    else:
        mock_cursor.fetchall.side_effect = lambda: []

    mock_connection = MagicMock()
    mock_connection.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connection.__enter__.return_value = mock_connection
    mock_connection.__exit__.return_value = None

    return mock_connection
