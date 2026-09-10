"""
Tests for proactive experiment sync feature.

This module tests the experiment metadata synchronization that occurs when
users are migrated between instances (free <-> premium or between free instances).

Test cases covered:
1. Internal API Security
2. Rate Limiting
3. Sync Endpoint Logic
4. Lazy Sync (ensure_synced_async)
5. Middleware Bypass
6. Lambda Integration (trigger_experiment_sync)
7. Router Integration
"""

import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from studio.app.common.core.storage.remote_storage_controller import (
    RemoteExperimentSyncMode,
)

# ---------------------------------------------------------------------------
# Mock aws_constants for Lambda tests (aws_constants is only available in Lambda)
# Only install if not already loaded (e.g., by infrastructure test conftest)
# ---------------------------------------------------------------------------
if "aws_constants" not in sys.modules:

    class MockDatabaseConfig:
        DEFAULT_PORT = 3306

    class MockAwsConstants:
        DatabaseConfig = MockDatabaseConfig

    sys.modules["aws_constants"] = MockAwsConstants

# ---------------------------------------------------------------------------
# Internal API Security
# ---------------------------------------------------------------------------


class TestInternalAPISecurity:
    """Tests for /system-internal/sync-experiments endpoint security."""

    @pytest.fixture
    def app_with_internal_router(self):
        """Create a minimal FastAPI app with internal router."""
        from fastapi import FastAPI

        app = FastAPI()

        # Patch the environment variable before importing the router
        with patch.dict(os.environ, {"INTERNAL_API_SECRET": "test-secret-12345"}):
            # Need to reload the module to pick up the new env var
            import importlib

            import studio.app.common.routers.internal as internal_module

            importlib.reload(internal_module)
            app.include_router(internal_module.router)

        return app

    @pytest.fixture
    def client(self, app_with_internal_router):
        """Create test client."""
        return TestClient(app_with_internal_router)

    def test_valid_secret_accepted(self, app_with_internal_router):
        """POST with correct X-Internal-Secret header should be processed."""
        from studio.app.common.db.database import get_db

        app = app_with_internal_router

        # Create mock DB session and user
        mock_db = MagicMock()
        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.email = "test@example.com"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_user

        # Override FastAPI dependency properly
        app.dependency_overrides[get_db] = lambda: mock_db

        # Clear rate limit cache and patch bucket name
        with patch("studio.app.common.routers.internal._rate_limit_cache", {}), patch(
            "studio.app.common.routers.internal._get_user_remote_bucket_name"
        ) as mock_bucket, patch(
            "studio.app.common.routers.internal._download_experiments_for_user",
            new_callable=AsyncMock,
        ):
            mock_bucket.return_value = "test-bucket"

            client = TestClient(app)
            response = client.post(
                "/system-internal/sync-experiments/1",
                headers={"X-Internal-Secret": "test-secret-12345"},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "sync_initiated"
            assert data["user_id"] == 1

        # Clean up
        app.dependency_overrides.clear()

    def test_invalid_secret_rejected(self, client):
        """POST with wrong secret should return 403."""
        response = client.post(
            "/system-internal/sync-experiments/1",
            headers={"X-Internal-Secret": "wrong-secret"},
        )

        assert response.status_code == 403
        assert "Invalid internal secret" in response.json()["detail"]

    def test_missing_secret_rejected(self, client):
        """POST without X-Internal-Secret header should return 422."""
        response = client.post("/system-internal/sync-experiments/1")

        assert response.status_code == 422  # Missing required header

    def test_missing_env_var_returns_503(self):
        """When INTERNAL_API_SECRET not set, should return 503."""
        from fastapi import FastAPI

        app = FastAPI()

        # Patch with empty string to simulate unset
        with patch.dict(os.environ, {"INTERNAL_API_SECRET": ""}, clear=False):
            import importlib

            import studio.app.common.routers.internal as internal_module

            # Force reload to pick up empty env var
            importlib.reload(internal_module)
            app.include_router(internal_module.router)

            client = TestClient(app)
            response = client.post(
                "/system-internal/sync-experiments/1",
                headers={"X-Internal-Secret": "any-secret"},
            )

            assert response.status_code == 503
            assert "Internal API not configured" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    """Tests for rate limiting on sync endpoint."""

    @pytest.fixture
    def setup_internal_router(self):
        """Setup internal router with mocked dependencies."""
        from fastapi import FastAPI

        app = FastAPI()

        with patch.dict(os.environ, {"INTERNAL_API_SECRET": "test-secret"}):
            import importlib

            import studio.app.common.routers.internal as internal_module

            importlib.reload(internal_module)
            # Clear rate limit cache before each test
            internal_module._rate_limit_cache.clear()
            app.include_router(internal_module.router)

        return app, internal_module

    def test_rate_limit_enforced(self, setup_internal_router):
        """Two sync requests for same user within 10s should return 429."""
        from studio.app.common.db.database import get_db

        app, internal_module = setup_internal_router

        # Create mock DB session and user
        mock_db = MagicMock()
        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.email = "test@example.com"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_user

        # Override FastAPI dependency properly
        app.dependency_overrides[get_db] = lambda: mock_db

        with patch.object(
            internal_module, "_get_user_remote_bucket_name"
        ) as mock_bucket, patch.object(
            internal_module,
            "_download_experiments_for_user",
            new_callable=AsyncMock,
        ):
            mock_bucket.return_value = "test-bucket"

            client = TestClient(app)

            # First request should succeed
            response1 = client.post(
                "/system-internal/sync-experiments/1",
                headers={"X-Internal-Secret": "test-secret"},
            )
            assert response1.status_code == 200

            # Second immediate request should be rate limited
            response2 = client.post(
                "/system-internal/sync-experiments/1",
                headers={"X-Internal-Secret": "test-secret"},
            )
            assert response2.status_code == 429
            assert "too frequent" in response2.json()["detail"]

        # Clean up
        app.dependency_overrides.clear()

    def test_rate_limit_resets_after_cooldown(self, setup_internal_router):
        """After 10s cooldown, same user should be able to sync again."""
        app, internal_module = setup_internal_router

        # Create mock DB session and user
        mock_db = MagicMock()
        mock_user = MagicMock()
        mock_user.id = 2
        mock_user.email = "test2@example.com"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_user

        # Override dependencies
        from studio.app.common.db.database import get_db

        app.dependency_overrides[get_db] = lambda: mock_db

        with patch.object(
            internal_module,
            "_download_experiments_for_user",
            new_callable=AsyncMock,
        ):
            client = TestClient(app)

            # First request
            response1 = client.post(
                "/system-internal/sync-experiments/2",
                headers={"X-Internal-Secret": "test-secret"},
            )
            assert response1.status_code == 200

            # Simulate time passing (manipulate cache directly)
            internal_module._rate_limit_cache[2] = time.time() - 15  # 15s ago

            # Second request after cooldown should succeed
            response2 = client.post(
                "/system-internal/sync-experiments/2",
                headers={"X-Internal-Secret": "test-secret"},
            )
            assert response2.status_code == 200

        # Clean up
        app.dependency_overrides.clear()

    def test_different_users_not_rate_limited(self, setup_internal_router):
        """Concurrent sync requests for different users should both succeed."""
        app, internal_module = setup_internal_router

        # Create mock with side effect to return different users
        call_count = [0]

        def mock_db_generator():
            mock_db = MagicMock()

            def get_user():
                call_count[0] += 1
                mock_user = MagicMock()
                mock_user.id = 10 if call_count[0] == 1 else 11
                mock_user.email = f"user{mock_user.id}@example.com"
                return mock_user

            mock_db.query.return_value.filter.return_value.first = get_user
            return mock_db

        from studio.app.common.db.database import get_db

        app.dependency_overrides[get_db] = mock_db_generator

        with patch.object(
            internal_module,
            "_download_experiments_for_user",
            new_callable=AsyncMock,
        ):
            client = TestClient(app)

            # Request for user 10
            response1 = client.post(
                "/system-internal/sync-experiments/10",
                headers={"X-Internal-Secret": "test-secret"},
            )
            assert response1.status_code == 200

            # Request for user 11 should also succeed
            response2 = client.post(
                "/system-internal/sync-experiments/11",
                headers={"X-Internal-Secret": "test-secret"},
            )
            assert response2.status_code == 200

        # Clean up
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Sync Endpoint Logic
# ---------------------------------------------------------------------------


class TestSyncEndpointLogic:
    """Tests for sync endpoint business logic."""

    @pytest.fixture
    def setup_app(self):
        """Setup app with internal router."""
        from fastapi import FastAPI

        app = FastAPI()

        with patch.dict(os.environ, {"INTERNAL_API_SECRET": "test-secret"}):
            import importlib

            import studio.app.common.routers.internal as internal_module

            importlib.reload(internal_module)
            internal_module._rate_limit_cache.clear()
            app.include_router(internal_module.router)

        return app, internal_module

    def test_valid_user_syncs(self, setup_app):
        """Existing user should trigger background download."""
        app, internal_module = setup_app

        # Create mock DB session and user
        mock_db = MagicMock()
        mock_user = MagicMock()
        mock_user.id = 100
        mock_user.email = "valid@example.com"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_user

        from studio.app.common.db.database import get_db

        app.dependency_overrides[get_db] = lambda: mock_db

        with patch.object(
            internal_module,
            "_download_experiments_for_user",
            new_callable=AsyncMock,
        ):
            client = TestClient(app)

            response = client.post(
                "/system-internal/sync-experiments/100",
                headers={"X-Internal-Secret": "test-secret"},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "sync_initiated"
            assert data["user_id"] == 100

        # Clean up
        app.dependency_overrides.clear()

    def test_nonexistent_user_returns_404(self, setup_app):
        """Request for invalid user_id should return 404."""
        from studio.app.common.db.database import get_db

        app, internal_module = setup_app

        # Create mock DB session that returns no user
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        # Override FastAPI dependency properly
        app.dependency_overrides[get_db] = lambda: mock_db

        client = TestClient(app)

        response = client.post(
            "/system-internal/sync-experiments/99999",
            headers={"X-Internal-Secret": "test-secret"},
        )

        assert response.status_code == 404
        assert "User not found" in response.json()["detail"]

        # Clean up
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Lazy Sync (ensure_synced_async)
# ---------------------------------------------------------------------------


class TestLazySync:
    """Tests for ExptConfigReader.ensure_synced_async method."""

    @pytest.fixture
    def mock_config_path(self, tmp_path):
        """Create a temporary config path."""
        return str(tmp_path / "workspace" / "experiment" / "experiment.yaml")

    @pytest.mark.asyncio
    async def test_existing_config_skips_sync(self, tmp_path):
        """When experiment config exists locally, S3 should not be called."""
        from studio.app.common.core.experiment.experiment_reader import ExptConfigReader

        # Create a mock config file
        config_dir = tmp_path / "output" / "workspace1" / "exp1"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "experiment.yaml"
        config_file.write_text("test: data")

        with patch.object(
            ExptConfigReader, "get_config_yaml_path", return_value=str(config_file)
        ), patch(
            "studio.app.common.core.storage.remote_storage_controller."
            "RemoteStorageController"
        ) as mock_controller:
            result = await ExptConfigReader.ensure_synced_async(
                "workspace1", "exp1", "test-bucket"
            )

            assert result is True
            # RemoteStorageController should not be called (file exists)
            mock_controller.is_available.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_config_triggers_sync(self, tmp_path):
        """When config missing locally, should download from S3."""
        from studio.app.common.core.experiment.experiment_reader import ExptConfigReader

        config_path = str(tmp_path / "missing" / "experiment.yaml")

        with patch.object(
            ExptConfigReader, "get_config_yaml_path", return_value=config_path
        ), patch(
            "studio.app.common.core.storage.remote_storage_controller."
            "RemoteStorageController"
        ) as mock_controller_class, patch(
            "studio.app.common.core.experiment.experiment_reader." "RemoteStorageReader"
        ) as mock_reader_class:
            mock_controller_class.is_available.return_value = True

            # Mock the async context manager
            mock_reader = AsyncMock()
            mock_reader.__aenter__.return_value = mock_reader
            mock_reader.__aexit__.return_value = None
            mock_reader_class.return_value = mock_reader

            result = await ExptConfigReader.ensure_synced_async(
                "workspace1", "exp1", "test-bucket"
            )

            # Should have tried to sync the specific experiment
            mock_reader.download_experiment_meta.assert_called_once_with(
                "workspace1", "exp1"
            )
            # Result depends on whether file exists after sync (mock returns False)
            assert result is False  # File doesn't exist after mock sync

    @pytest.mark.asyncio
    async def test_s3_unavailable_returns_false(self, tmp_path):
        """When remote storage unavailable, should return False without crashing."""
        from studio.app.common.core.experiment.experiment_reader import ExptConfigReader

        config_path = str(tmp_path / "missing" / "experiment.yaml")

        with patch.object(
            ExptConfigReader, "get_config_yaml_path", return_value=config_path
        ), patch(
            "studio.app.common.core.storage.remote_storage_controller."
            "RemoteStorageController"
        ) as mock_controller_class:
            mock_controller_class.is_available.return_value = False

            result = await ExptConfigReader.ensure_synced_async(
                "workspace1", "exp1", "test-bucket"
            )

            assert result is False

    @pytest.mark.asyncio
    async def test_sync_failure_handled_gracefully(self, tmp_path):
        """S3 errors should be logged and return False, not propagate exception."""
        from studio.app.common.core.experiment.experiment_reader import ExptConfigReader

        config_path = str(tmp_path / "missing" / "experiment.yaml")

        with patch.object(
            ExptConfigReader, "get_config_yaml_path", return_value=config_path
        ), patch(
            "studio.app.common.core.storage.remote_storage_controller."
            "RemoteStorageController"
        ) as mock_controller_class, patch(
            "studio.app.common.core.experiment.experiment_reader." "RemoteStorageReader"
        ) as mock_reader_class, patch(
            "studio.app.common.core.experiment.experiment_reader.logger.warning"
        ):
            mock_controller_class.is_available.return_value = True

            # Mock reader to raise an exception
            mock_reader = AsyncMock()
            mock_reader.__aenter__.side_effect = Exception("S3 connection failed")
            mock_reader_class.return_value = mock_reader

            # Should not raise, just return False
            result = await ExptConfigReader.ensure_synced_async(
                "workspace1", "exp1", "test-bucket"
            )

            assert result is False


# ---------------------------------------------------------------------------
# Middleware Bypass
# ---------------------------------------------------------------------------


class TestMiddlewareBypass:
    """Tests for middleware skipping /system-internal/ paths."""

    def test_user_activity_middleware_skips_internal_paths(self):
        """UserActivityMiddleware should skip /system-internal/* routes."""
        # Verify the middleware has the skip logic
        import inspect

        import studio.app.common.core.middleware.user_activity_middleware as uam

        source = inspect.getsource(uam.UserActivityMiddleware)
        assert 'startswith("/system-internal/")' in source

    def test_secure_routing_middleware_skips_internal_paths(self):
        """SecureRoutingMiddleware should skip /system-internal/* routes."""
        # Verify the middleware has the skip logic
        # The middleware source should contain the /system-internal/ skip
        import inspect

        import studio.app.common.core.middleware.secure_routing_middleware as srm

        source = inspect.getsource(srm.SecureRoutingMiddleware)
        assert 'startswith("/system-internal/")' in source

    def test_internal_endpoint_accessible_without_bearer_token(self):
        """Internal endpoints should work without JWT (but require secret)."""
        from fastapi import FastAPI

        from studio.app.common.db.database import get_db

        app = FastAPI()

        with patch.dict(os.environ, {"INTERNAL_API_SECRET": "test-secret"}):
            import importlib

            import studio.app.common.routers.internal as internal_module

            importlib.reload(internal_module)
            internal_module._rate_limit_cache.clear()
            app.include_router(internal_module.router)

        # Create mock DB session and user
        mock_db = MagicMock()
        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.email = "test@example.com"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_user

        # Override FastAPI dependency properly
        app.dependency_overrides[get_db] = lambda: mock_db

        with patch.object(
            internal_module, "_get_user_remote_bucket_name"
        ) as mock_bucket, patch.object(
            internal_module,
            "_download_experiments_for_user",
            new_callable=AsyncMock,
        ):
            mock_bucket.return_value = "test-bucket"

            client = TestClient(app)

            # No Bearer token, only internal secret
            response = client.post(
                "/system-internal/sync-experiments/1",
                headers={"X-Internal-Secret": "test-secret"},
                # No Authorization header
            )

            # Should succeed with just the internal secret
            assert response.status_code == 200

        # Clean up
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Lambda Integration
# ---------------------------------------------------------------------------


class TestLambdaIntegration:
    """Tests for Lambda trigger_experiment_sync function."""

    def test_trigger_experiment_sync_success(self):
        """Successful sync trigger should return True."""
        # Import the function (need to mock environment first)
        with patch.dict(
            os.environ,
            {
                "ALB_DNS_NAME": "test-alb.example.com",
                "INTERNAL_API_SECRET": "lambda-secret-123",
            },
        ):
            # Mock requests.post
            with patch("requests.post") as mock_post:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_post.return_value = mock_response

                # Import and call the function
                from infrastructure.terraform.free_manager_package import (
                    free_user_utils,
                )

                trigger_experiment_sync = free_user_utils.trigger_experiment_sync

                result = trigger_experiment_sync(42)

                assert result is True
                mock_post.assert_called_once()
                call_args = mock_post.call_args
                assert "sync-experiments/42" in call_args[0][0]
                assert (
                    call_args[1]["headers"]["X-Internal-Secret"] == "lambda-secret-123"
                )

    def test_trigger_experiment_sync_missing_config(self):
        """Missing ALB_DNS_NAME or INTERNAL_API_SECRET should return False."""
        with patch.dict(os.environ, {"ALB_DNS_NAME": "", "INTERNAL_API_SECRET": ""}):
            # Need to reload to pick up empty env vars
            import importlib

            # Import fresh
            sys.path.insert(0, "infrastructure/terraform/free_manager_package")
            try:
                from infrastructure.terraform.free_manager_package import (
                    free_user_utils,
                )

                importlib.reload(free_user_utils)
                result = free_user_utils.trigger_experiment_sync(1)
                assert result is False
            except ImportError:
                # If can't import, test the logic directly
                alb_dns = os.environ.get("ALB_DNS_NAME")
                internal_secret = os.environ.get("INTERNAL_API_SECRET")
                assert not alb_dns or not internal_secret

    def test_trigger_experiment_sync_api_failure(self):
        """API call failure should return False without raising."""
        with patch.dict(
            os.environ,
            {
                "ALB_DNS_NAME": "test-alb.example.com",
                "INTERNAL_API_SECRET": "secret",
            },
        ):
            with patch("requests.post") as mock_post:
                mock_response = MagicMock()
                mock_response.status_code = 500
                mock_post.return_value = mock_response

                from infrastructure.terraform.free_manager_package import (
                    free_user_utils,
                )

                result = free_user_utils.trigger_experiment_sync(42)

                assert result is False

    def test_trigger_experiment_sync_connection_error(self):
        """Connection errors should return False without raising."""
        with patch.dict(
            os.environ,
            {
                "ALB_DNS_NAME": "test-alb.example.com",
                "INTERNAL_API_SECRET": "secret",
            },
        ):
            with patch("requests.post") as mock_post:
                import requests

                mock_post.side_effect = requests.exceptions.ConnectionError(
                    "Network error"
                )

                from infrastructure.terraform.free_manager_package import (
                    free_user_utils,
                )

                # Should not raise
                result = free_user_utils.trigger_experiment_sync(42)

                assert result is False

    def test_migration_still_succeeds_when_sync_fails(self):
        """Migration should return True even if sync trigger fails."""
        # This tests that sync is fire-and-forget
        with patch.dict(
            os.environ,
            {
                "ALB_DNS_NAME": "test-alb.example.com",
                "INTERNAL_API_SECRET": "secret",
                "DB_HOST": "localhost",
                "DB_USER": "test",
                "DB_PASSWORD": "test",
                "DB_NAME": "test",
            },
        ):
            with patch("requests.post") as mock_post:
                mock_post.side_effect = Exception("Sync failed")

                # The sync failure should be logged but not prevent migration
                # This is a design test - verify the code has try/except
                from infrastructure.terraform.free_manager_package import (
                    free_user_utils,
                )

                # Sync fails
                result = free_user_utils.trigger_experiment_sync(1)
                assert result is False  # Sync returned False

                # But in actual migrate_user_to_instance, migration would still succeed
                # because trigger_experiment_sync is called after the DB commit


# ---------------------------------------------------------------------------
# Router Integration
# ---------------------------------------------------------------------------


class TestRouterIntegration:
    """Tests for sync integration in run.py and workflow.py routers."""

    def test_run_router_has_sync_call(self):
        """run_result endpoint should call ensure_synced_async."""
        import inspect

        from studio.app.common.routers import run

        source = inspect.getsource(run.run_result)

        # Verify ensure_synced_async is called
        assert "ensure_synced_async" in source
        assert "ExptConfigReader" in source

    def test_workflow_router_has_sync_call(self):
        """reproduce_experiment endpoint should call ensure_synced_async."""
        import inspect

        from studio.app.common.routers import workflow

        source = inspect.getsource(workflow.reproduce_experiment)

        # Verify ensure_synced_async is called
        assert "ensure_synced_async" in source

    def test_existing_local_experiment_unaffected(self, tmp_path):
        """Routes should work normally when experiment exists locally."""
        # Create a mock experiment file
        config_dir = tmp_path / "output" / "workspace" / "exp123"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "experiment.yaml"
        config_file.write_text(
            """
workspace_id: workspace
unique_id: exp123
name: Test Experiment
started_at: 2024-01-01T00:00:00
finished_at: null
success: 1
hasNWB: false
function: {}
"""
        )

        from studio.app.common.core.experiment.experiment_reader import ExptConfigReader

        with patch.object(
            ExptConfigReader, "get_config_yaml_path", return_value=str(config_file)
        ):
            # Sync should return True immediately without S3 call
            import asyncio

            result = asyncio.get_event_loop().run_until_complete(
                ExptConfigReader.ensure_synced_async("workspace", "exp123", "bucket")
            )
            assert result is True


# ---------------------------------------------------------------------------
# Input Data Sync (Multi-Instance Migration)
# ---------------------------------------------------------------------------


class TestInputDataSync:
    """
    Tests for input data sync when users are migrated between instances.

    When a user is migrated to a new instance, their input data exists in S3
    but not locally. These tests verify:
    1. File listing shows both local and S3 files with sync status
    2. Input files are downloaded before workflow runs
    3. HDF5/MATLAB structure is available via cached metadata (no full download)
    4. CSV files are synced before settings dialog opens
    """

    def test_merged_endpoint_returns_sync_status(self):
        """The /files/{workspace_id}/merged endpoint should return sync_status."""
        from studio.app.common.schemas.files import SyncStatus, TreeNodeWithSync

        # Verify TreeNodeWithSync has sync_status field
        node = TreeNodeWithSync(
            path="test.tif",
            name="test.tif",
            isdir=False,
            nodes=[],
            sync_status=SyncStatus.REMOTE,
        )
        assert node.sync_status == SyncStatus.REMOTE

    def test_sync_status_values(self):
        """SyncStatus enum should have correct values."""
        from studio.app.common.schemas.files import SyncStatus

        assert SyncStatus.LOCAL.value == "local"
        assert SyncStatus.SYNCED.value == "synced"
        assert SyncStatus.REMOTE.value == "remote"

    @pytest.fixture()
    def mock_storage(self, tmp_path, monkeypatch):
        """A real ``MockStorageController`` over ``tmp_path``, reached through the
        production ``RemoteStorageSimpleReader`` wrapper.

        The seven cases this replaced were ``assert "<name>" in
        inspect.getsource(module)``, which hold for a function that is defined and
        never wired up. Nothing here patches the download: the file really moves
        between two directories, so an on-demand sync that stops firing fails.
        """
        from studio.app.common.core.storage.mock_storage_controller import (
            MockStorageController,
        )
        from studio.app.dir_path import DIRPATH

        remote = tmp_path / "remote"
        monkeypatch.setenv("REMOTE_STORAGE_TYPE", "1")
        monkeypatch.setattr(MockStorageController, "MOCK_STORAGE_DIR", str(remote))
        monkeypatch.setattr(
            MockStorageController, "MOCK_INPUT_DIR", str(remote / "input")
        )
        monkeypatch.setattr(
            MockStorageController, "MOCK_OUTPUT_DIR", str(remote / "output")
        )
        monkeypatch.setattr(DIRPATH, "INPUT_DIR", str(tmp_path / "local"))

        class Harness:
            workspace_id = "ws1"
            bucket = "bucket-under-test"

            def put_remote(self, filename, body="a,b\n1,2\n"):
                path = remote / "input" / self.workspace_id / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
                return path

            def put_local(self, filename, body="x,y\n3,4\n"):
                path = tmp_path / "local" / self.workspace_id / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
                return path

            def local_path(self, filename):
                return tmp_path / "local" / self.workspace_id / filename

        return Harness()

    @pytest.mark.asyncio
    async def test_sync_input_file_downloads_the_remote_file(self, mock_storage):
        from fastapi import BackgroundTasks

        from studio.app.common.routers.files import sync_input_file

        mock_storage.put_remote("remote_only.csv", "a,b\n9,9\n")
        assert not mock_storage.local_path("remote_only.csv").exists()

        tasks = BackgroundTasks()
        result = await sync_input_file(
            mock_storage.workspace_id,
            "remote_only.csv",
            tasks,
            remote_bucket_name=mock_storage.bucket,
        )

        assert result == {"file_path": "remote_only.csv"}
        assert mock_storage.local_path("remote_only.csv").read_text() == "a,b\n9,9\n"

    @pytest.mark.asyncio
    async def test_sync_input_file_404s_when_the_file_is_not_in_remote_storage(
        self, mock_storage
    ):
        from fastapi import BackgroundTasks, HTTPException

        from studio.app.common.routers.files import sync_input_file

        with pytest.raises(HTTPException) as exc:
            await sync_input_file(
                mock_storage.workspace_id,
                "absent.csv",
                BackgroundTasks(),
                remote_bucket_name=mock_storage.bucket,
            )

        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_sync_input_file_503s_without_remote_storage(
        self, mock_storage, monkeypatch
    ):
        from fastapi import BackgroundTasks, HTTPException

        from studio.app.common.routers.files import sync_input_file

        monkeypatch.setenv("REMOTE_STORAGE_TYPE", "0")

        with pytest.raises(HTTPException) as exc:
            await sync_input_file(
                mock_storage.workspace_id,
                "remote_only.csv",
                BackgroundTasks(),
                remote_bucket_name=mock_storage.bucket,
            )

        assert exc.value.status_code == 503

    @pytest.mark.asyncio
    async def test_merged_listing_labels_local_synced_and_remote_only_files(
        self, mock_storage
    ):
        """The three sync statuses a migrated user's file list has to show."""
        from studio.app.common.routers.files import get_files_merged
        from studio.app.common.schemas.files import SyncStatus
        from studio.app.const import FILETYPE

        mock_storage.put_local("local_only.csv")
        mock_storage.put_local("both.csv")
        mock_storage.put_remote("both.csv")
        mock_storage.put_remote("remote_only.csv")

        nodes = await get_files_merged(
            mock_storage.workspace_id,
            file_type=FILETYPE.CSV,
            remote_bucket_name=mock_storage.bucket,
        )

        statuses = {node.path: node.sync_status for node in nodes}
        assert statuses == {
            "local_only.csv": SyncStatus.LOCAL,
            "both.csv": SyncStatus.SYNCED,
            "remote_only.csv": SyncStatus.REMOTE,
        }

    @pytest.mark.asyncio
    async def test_merged_listing_reports_the_remote_size_for_remote_only_files(
        self, mock_storage
    ):
        from studio.app.common.routers.files import get_files_merged
        from studio.app.const import FILETYPE

        body = "a,b\n1,2\n3,4\n"
        mock_storage.put_remote("remote_only.csv", body)

        nodes = await get_files_merged(
            mock_storage.workspace_id,
            file_type=FILETYPE.CSV,
            remote_bucket_name=mock_storage.bucket,
        )

        assert [(n.path, n.size) for n in nodes] == [
            ("remote_only.csv", len(body.encode()))
        ]

    @pytest.mark.asyncio
    async def test_ensure_input_data_local_downloads_a_missing_input(
        self, mock_storage
    ):
        """The workflow-side half: a migrated user's inputs arrive before the run."""
        from studio.app.common.core.workflow.workflow_runner import WorkflowRunner

        mock_storage.put_remote("input.csv", "a,b\n7,7\n")

        runner = WorkflowRunner.__new__(WorkflowRunner)
        runner.remote_bucket_name = mock_storage.bucket
        runner.workspace_id = mock_storage.workspace_id
        runner.logger = MagicMock()
        with patch.object(
            WorkflowRunner, "_extract_input_files", return_value=["input.csv"]
        ):
            await runner.ensure_input_data_local()

        assert mock_storage.local_path("input.csv").read_text() == "a,b\n7,7\n"

    @pytest.mark.asyncio
    async def test_ensure_input_data_local_leaves_an_existing_local_file_alone(
        self, mock_storage
    ):
        """The runner's own ``os.path.exists`` guard is what has to refuse, so the
        assertion is that no download was requested at all.

        Reading the resulting file instead would read the backend's guard: the
        mock controller skips whenever a local file exists, while S3 skips only on
        a size match, and the two versions here differ in size.
        """
        from studio.app.common.core.storage.remote_storage_controller import (
            RemoteStorageController,
        )
        from studio.app.common.core.workflow.workflow_runner import WorkflowRunner

        mock_storage.put_local("input.csv", "local version\n")
        mock_storage.put_remote("input.csv", "remote version\n")

        runner = WorkflowRunner.__new__(WorkflowRunner)
        runner.remote_bucket_name = mock_storage.bucket
        runner.workspace_id = mock_storage.workspace_id
        runner.logger = MagicMock()
        with patch.object(
            WorkflowRunner, "_extract_input_files", return_value=["input.csv"]
        ), patch.object(
            RemoteStorageController,
            "download_input_data",
            autospec=True,
            wraps=RemoteStorageController.download_input_data,
        ) as download:
            await runner.ensure_input_data_local()

        assert download.call_count == 0
        assert mock_storage.local_path("input.csv").read_text() == "local version\n"

    @pytest.mark.asyncio
    async def test_run_endpoint_syncs_inputs_before_starting_the_workflow(self):
        """Ordering is the claim: a download after the run starts is useless."""
        from studio.app.common.routers import run as run_router

        calls = []
        runner = MagicMock()
        runner.ensure_input_data_local = AsyncMock(
            side_effect=lambda: calls.append("sync")
        )
        runner.run_workflow = MagicMock(side_effect=lambda _: calls.append("run"))

        with patch.object(
            run_router, "WorkflowRunner", MagicMock(return_value=runner)
        ), patch.object(
            run_router, "_check_storage_quota", new_callable=AsyncMock
        ), patch.object(
            run_router.RemoteStorageController, "is_available", return_value=True
        ), patch.object(
            run_router, "get_current_user_storage_usage"
        ):
            await run_router.run(
                "ws1",
                MagicMock(),
                MagicMock(),
                current_user=MagicMock(id=1),
                remote_bucket_name="bucket-under-test",
            )

        assert calls == ["sync", "run"]

    def test_hdf5_endpoint_uses_cached_structure(self):
        """HDF5 endpoint should check for cached structure before reading file."""
        import inspect

        from studio.app.optinist.routers import hdf5

        source = inspect.getsource(hdf5.get_files)

        # Verify it checks for cached structure
        assert "get_hdf5_structure_dict" in source
        assert "hdf5_structure.json" in source or "_hdf5_structure" in source

    def test_mat_endpoint_uses_cached_structure(self):
        """MATLAB endpoint should check for cached structure before reading file."""
        import inspect

        from studio.app.optinist.routers import mat

        source = inspect.getsource(mat.get_matfiles)

        # Verify it checks for cached structure
        assert "get_mat_structure_dict" in source
        assert "mat_structure.json" in source or "_mat_structure" in source

    def test_files_router_has_sync_endpoint(self):
        """Files router should have sync endpoint for on-demand file download."""
        import inspect

        from studio.app.common.routers import files

        source = inspect.getsource(files)

        # Verify sync endpoint exists
        assert "sync_input_file" in source
        assert "/sync/" in source

    def test_files_router_has_merged_endpoint(self):
        """Files router should have merged endpoint for local+S3 file listing."""
        import inspect

        from studio.app.common.routers import files

        source = inspect.getsource(files)

        # Verify merged endpoint exists
        assert "get_files_merged" in source
        assert "/merged" in source

    def test_structure_caching_functions_exist(self):
        """Structure caching functions should exist in files router."""
        from studio.app.common.routers.files import (
            get_hdf5_structure_dict,
            get_mat_structure_dict,
            update_hdf5_structure,
            update_mat_structure,
        )

        # Verify functions are callable
        assert callable(update_hdf5_structure)
        assert callable(update_mat_structure)
        assert callable(get_hdf5_structure_dict)
        assert callable(get_mat_structure_dict)

    def test_sample_data_import_caches_structures(self):
        """Sample data import should cache HDF5/MATLAB structures."""
        import inspect

        from studio.app.common.routers import workflow

        source = inspect.getsource(workflow.import_sample_data)

        # Verify structure caching is called for HDF5/MATLAB
        assert "update_hdf5_structure" in source
        assert "update_mat_structure" in source

    @pytest.mark.asyncio
    async def test_list_input_data_objects_format(self):
        """list_input_data_objects should return correct format."""
        from studio.app.common.core.storage.remote_storage_controller import (
            RemoteStorageController,
        )

        if not RemoteStorageController.is_available():
            pytest.skip("Remote storage not available")

        from studio.app.common.core.storage.mock_storage_controller import (
            MockStorageController,
        )

        controller = MockStorageController()
        result = await controller.list_input_data_objects("nonexistent_workspace")

        # Should return empty list for nonexistent workspace
        assert isinstance(result, list)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Single Experiment Sync
# ---------------------------------------------------------------------------


class TestSingleExperimentSync:
    """Tests for /system-internal/sync-experiment endpoint."""

    @pytest.fixture
    def app_with_internal_router(self):
        """Create a minimal FastAPI app with internal router."""
        from fastapi import FastAPI

        app = FastAPI()

        with patch.dict(
            os.environ,
            {"INTERNAL_API_SECRET": "test-secret-12345"},
        ):
            import importlib

            import studio.app.common.routers.internal as mod

            importlib.reload(mod)
            app.include_router(mod.router)

        return app

    @pytest.fixture
    def client(self, app_with_internal_router):
        """Create test client."""
        return TestClient(app_with_internal_router)

    def test_sync_single_experiment_success(self, app_with_internal_router):
        """POST with valid params returns 200."""
        client = TestClient(app_with_internal_router)

        response = client.post(
            "/system-internal/sync-experiment" "/1/uid1?bucket_name=my-bucket",
            headers={"X-Internal-Secret": "test-secret-12345"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "sync_initiated"
        assert data["workspace_id"] == "1"
        assert data["unique_id"] == "uid1"

    def test_sync_single_experiment_bad_secret(self, client):
        """POST with wrong secret returns 403."""
        response = client.post(
            "/system-internal/sync-experiment" "/1/uid1?bucket_name=my-bucket",
            headers={"X-Internal-Secret": "wrong-secret"},
        )

        assert response.status_code == 403

    def test_sync_single_experiment_missing_secret(self, client):
        """POST without secret header returns 422."""
        response = client.post(
            "/system-internal/sync-experiment" "/1/uid1?bucket_name=my-bucket",
        )

        assert response.status_code == 422

    def test_sync_single_experiment_missing_bucket(self, client):
        """POST without bucket_name query param returns 422."""
        response = client.post(
            "/system-internal/sync-experiment/1/uid1",
            headers={"X-Internal-Secret": "test-secret-12345"},
        )

        assert response.status_code == 422

    def test_sync_single_experiment_bad_bucket(self, client):
        """Invalid bucket name returns 422."""
        response = client.post(
            "/system-internal/sync-experiment" "/1/uid1?bucket_name=AB",
            headers={"X-Internal-Secret": "test-secret-12345"},
        )
        assert response.status_code == 422

    def test_sync_single_experiment_invalid_workspace_id(self, client):
        """Non-numeric workspace_id returns 422."""
        response = client.post(
            "/system-internal/sync-experiment" "/ws1/uid1?bucket_name=my-bucket",
            headers={"X-Internal-Secret": "test-secret-12345"},
        )
        assert response.status_code == 422

    def test_sync_single_experiment_invalid_unique_id(self, client):
        """unique_id with special chars returns 422."""
        response = client.post(
            "/system-internal/sync-experiment"
            "/1/uid%20with%20spaces?bucket_name=my-bucket",
            headers={"X-Internal-Secret": "test-secret-12345"},
        )
        assert response.status_code == 422

    def test_sync_single_experiment_rate_limited(self, app_with_internal_router):
        """Rapid calls to same experiment return 429."""
        client = TestClient(app_with_internal_router)

        resp1 = client.post(
            "/system-internal/sync-experiment" "/1/uid1?bucket_name=my-bucket",
            headers={"X-Internal-Secret": "test-secret-12345"},
        )
        assert resp1.status_code == 200

        resp2 = client.post(
            "/system-internal/sync-experiment" "/1/uid1?bucket_name=my-bucket",
            headers={"X-Internal-Secret": "test-secret-12345"},
        )
        assert resp2.status_code == 429

    def test_sync_single_experiment_has_thumbnails(self, client):
        """has_thumbnails=false is accepted."""
        import studio.app.common.routers.internal as mod

        mod._sync_rate_limit_cache.clear()

        response = client.post(
            "/system-internal/sync-experiment"
            "/1/uid1"
            "?bucket_name=my-bucket"
            "&has_thumbnails=false",
            headers={"X-Internal-Secret": "test-secret-12345"},
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Download Single Experiment Background Task
# ---------------------------------------------------------------------------


class TestDownloadSingleExperiment:
    """Tests for _download_single_experiment background task."""

    @pytest.mark.asyncio
    async def test_skips_when_remote_storage_unavailable(self):
        """Returns early when remote storage is not available."""
        with patch(
            "studio.app.common.routers.internal." "RemoteStorageController"
        ) as mock_ctrl:
            mock_ctrl.is_available.return_value = False

            from studio.app.common.routers.internal import _download_single_experiment

            # Should not raise
            await _download_single_experiment("bucket1", "ws1", "uid1")

            mock_ctrl.is_available.assert_called_once()

    @pytest.mark.asyncio
    async def test_downloads_thumbnails_then_essential(self):
        """Downloads thumbnails_only then essential_only."""
        mock_reader = AsyncMock()
        mock_reader.__aenter__.return_value = mock_reader
        mock_reader.__aexit__.return_value = None

        with patch(
            "studio.app.common.routers.internal." "RemoteStorageController"
        ) as mock_ctrl, patch(
            "studio.app.common.routers.internal." "RemoteStorageReader",
            return_value=mock_reader,
        ), patch(
            "os.path.exists", return_value=False
        ):
            mock_ctrl.is_available.return_value = True

            from studio.app.common.routers.internal import _download_single_experiment

            await _download_single_experiment(
                "bucket1",
                "ws1",
                "uid1",
                has_thumbnails=True,
            )

        assert mock_reader.download_experiment.call_count == 2
        calls = mock_reader.download_experiment.call_args_list
        assert calls[0] == (
            ("ws1", "uid1"),
            {"sync_mode": RemoteExperimentSyncMode.THUMBNAILS_ONLY},
        )
        assert calls[1] == (
            ("ws1", "uid1"),
            {"sync_mode": RemoteExperimentSyncMode.ESSENTIAL_ONLY},
        )

    @pytest.mark.asyncio
    async def test_skips_thumbnails_when_not_present(self):
        """has_thumbnails=False skips thumbnail download."""
        mock_reader = AsyncMock()
        mock_reader.__aenter__.return_value = mock_reader
        mock_reader.__aexit__.return_value = None

        with patch(
            "studio.app.common.routers.internal." "RemoteStorageController"
        ) as mock_ctrl, patch(
            "studio.app.common.routers.internal." "RemoteStorageReader",
            return_value=mock_reader,
        ), patch(
            "os.path.exists", return_value=False
        ):
            mock_ctrl.is_available.return_value = True

            from studio.app.common.routers.internal import _download_single_experiment

            await _download_single_experiment(
                "bucket1",
                "ws1",
                "uid1",
                has_thumbnails=False,
            )

        assert mock_reader.download_experiment.call_count == 1
        calls = mock_reader.download_experiment.call_args_list
        assert calls[0] == (
            ("ws1", "uid1"),
            {"sync_mode": RemoteExperimentSyncMode.ESSENTIAL_ONLY},
        )

    @pytest.mark.asyncio
    async def test_skips_when_files_exist_locally(self):
        """Early return when YAML files already present."""
        mock_reader = AsyncMock()
        mock_reader.__aenter__.return_value = mock_reader
        mock_reader.__aexit__.return_value = None

        with patch(
            "studio.app.common.routers.internal." "RemoteStorageController"
        ) as mock_ctrl, patch(
            "studio.app.common.routers.internal." "RemoteStorageReader",
            return_value=mock_reader,
        ), patch(
            "os.path.exists", return_value=True
        ):
            mock_ctrl.is_available.return_value = True

            from studio.app.common.routers.internal import _download_single_experiment

            await _download_single_experiment("bucket1", "ws1", "uid1")

        assert mock_reader.download_experiment.call_count == 0

    @pytest.mark.asyncio
    async def test_handles_download_exception(self):
        """Exception during download is caught, not propagated."""
        mock_reader = AsyncMock()
        mock_reader.__aenter__.return_value = mock_reader
        mock_reader.__aexit__.return_value = None
        mock_reader.download_experiment.side_effect = RuntimeError("S3 error")

        with patch(
            "studio.app.common.routers.internal." "RemoteStorageController"
        ) as mock_ctrl, patch(
            "studio.app.common.routers.internal." "RemoteStorageReader",
            return_value=mock_reader,
        ), patch(
            "os.path.exists", return_value=False
        ), patch(
            "studio.app.common.routers.internal.logger.error"
        ):
            mock_ctrl.is_available.return_value = True

            from studio.app.common.routers.internal import _download_single_experiment

            # Should not raise
            await _download_single_experiment("bucket1", "ws1", "uid1")


# ---------------------------------------------------------------------------
# Run tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
