"""
Unit tests for auth dependencies, specifically for public outputs bucket resolution.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from studio.app.common.core.auth.auth_dependencies import (
    _enrich_user_with_subscription_status,
    _get_user_remote_bucket_name,
    get_current_user_for_dataview_outputs,
    get_current_user_with_dataview_outputs_check,
    get_outputs_remote_bucket_name,
)
from studio.app.common.core.subscription.constants import (
    PlanName,
    SubscriptionPlanIds,
    SubscriptionStatus,
)
from studio.app.common.core.utils.datetime_utils import get_current_datetime


def create_mock_request(url_path: str, query_params: dict = None):
    """Create a mock request with properly configured state for caching tests."""
    mock_req = MagicMock()
    mock_req.url.path = url_path
    mock_req.query_params = query_params or {}
    # Use SimpleNamespace for state so hasattr() works correctly
    mock_req.state = SimpleNamespace()
    return mock_req


class TestGetCurrentUserForDataviewOutputs:
    """Test get_current_user_for_dataview_outputs dependency"""

    @pytest.mark.asyncio
    async def test_returns_none_for_public_request_without_credentials(self):
        """Public requests without credentials should return None"""
        mock_req = MagicMock()
        mock_res = MagicMock()
        mock_db = MagicMock()

        with patch(
            "studio.app.common.core.auth.auth_dependencies.DataviewService."
            "is_dataview_public_outputs_request",
            return_value=True,
        ):
            result = await get_current_user_for_dataview_outputs(
                req=mock_req,
                res=mock_res,
                ex_token=None,
                credential=None,
                db=mock_db,
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_user_for_authenticated_public_request(self):
        """Public requests with valid credentials should return user"""
        mock_req = MagicMock()
        mock_res = MagicMock()
        mock_db = MagicMock()
        mock_credential = MagicMock()
        mock_user = MagicMock()

        with patch(
            "studio.app.common.core.auth.auth_dependencies.DataviewService."
            "is_dataview_public_outputs_request",
            return_value=True,
        ):
            with patch(
                "studio.app.common.core.auth.auth_dependencies.get_current_user",
                new_callable=AsyncMock,
                return_value=mock_user,
            ):
                result = await get_current_user_for_dataview_outputs(
                    req=mock_req,
                    res=mock_res,
                    ex_token=None,
                    credential=mock_credential,
                    db=mock_db,
                )

        assert result == mock_user

    @pytest.mark.asyncio
    async def test_returns_none_for_invalid_credentials_on_public_request(self):
        """Public requests with invalid credentials should return None (not raise)"""
        from fastapi import HTTPException

        mock_req = MagicMock()
        mock_res = MagicMock()
        mock_db = MagicMock()
        mock_credential = MagicMock()

        with patch(
            "studio.app.common.core.auth.auth_dependencies.DataviewService."
            "is_dataview_public_outputs_request",
            return_value=True,
        ):
            with patch(
                "studio.app.common.core.auth.auth_dependencies.get_current_user",
                new_callable=AsyncMock,
                side_effect=HTTPException(status_code=401, detail="Invalid token"),
            ):
                result = await get_current_user_for_dataview_outputs(
                    req=mock_req,
                    res=mock_res,
                    ex_token=None,
                    credential=mock_credential,
                    db=mock_db,
                )

        # Should return None, not raise
        assert result is None

    @pytest.mark.asyncio
    async def test_requires_auth_for_non_public_request(self):
        """Non-public requests should require authentication"""
        mock_req = MagicMock()
        mock_res = MagicMock()
        mock_db = MagicMock()
        mock_user = MagicMock()

        with patch(
            "studio.app.common.core.auth.auth_dependencies.DataviewService."
            "is_dataview_public_outputs_request",
            return_value=False,
        ):
            with patch(
                "studio.app.common.core.auth.auth_dependencies.get_current_user",
                new_callable=AsyncMock,
                return_value=mock_user,
            ):
                result = await get_current_user_for_dataview_outputs(
                    req=mock_req,
                    res=mock_res,
                    ex_token="valid_token",
                    credential=None,
                    db=mock_db,
                )

        assert result == mock_user


class TestGetOutputsRemoteBucketName:
    """Test get_outputs_remote_bucket_name dependency"""

    @pytest.mark.asyncio
    async def test_returns_user_bucket_when_no_workspace_id(self):
        """Authenticated user should get their own bucket
        when workspace can't be determined"""
        mock_req = create_mock_request(
            "/api/visualizations/image/some/path/without/workspace"
        )
        mock_db = MagicMock()
        mock_user = MagicMock()
        mock_user.remote_bucket_name = "user-bucket-123"

        with patch("studio.app.dir_path.DIRPATH") as mock_dirpath:
            mock_dirpath.OUTPUT_DIR = "/app/studio_data/output"
            result = await get_outputs_remote_bucket_name(
                req=mock_req,
                current_user=mock_user,
                db=mock_db,
            )

        assert result == "user-bucket-123"

    @pytest.mark.asyncio
    async def test_returns_workspace_owner_bucket_for_authenticated_user(self):
        """Authenticated user with workspace access should
        get workspace owner's bucket"""
        from studio.app.common.core.experiment.experiment import ExptOutputPathIds

        mock_req = create_mock_request(
            "/api/visualizations/image//app/studio_data/output/123/abc123/file.json"
        )
        mock_db = MagicMock()
        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.remote_bucket_name = "user-bucket-123"

        mock_workspace = MagicMock()
        mock_workspace.user = MagicMock()
        mock_workspace.user.remote_bucket_name = "owner-bucket-456"

        mock_path_ids = MagicMock()
        mock_path_ids.workspace_id = "123"
        mock_path_ids.unique_id = "abc123"

        with patch.object(
            ExptOutputPathIds, "from_request_url", return_value=mock_path_ids
        ):
            # Mock the join query chain for authenticated users
            mock_query = mock_db.query.return_value
            mock_query.join.return_value.filter.return_value.first.return_value = (
                mock_workspace
            )

            result = await get_outputs_remote_bucket_name(
                req=mock_req,
                current_user=mock_user,
                db=mock_db,
            )

        # Should use workspace owner's bucket, not the current user's bucket
        assert result == "owner-bucket-456"

    @pytest.mark.asyncio
    async def test_returns_user_bucket_when_no_workspace_access_and_not_published(self):
        """Authenticated user without workspace access and non-published data
        should get their own bucket"""
        from studio.app.common.core.experiment.experiment import ExptOutputPathIds

        mock_req = create_mock_request(
            "/api/visualizations/image//app/studio_data/output/999/abc123/file.json"
        )
        mock_db = MagicMock()
        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.remote_bucket_name = "user-bucket-123"

        mock_path_ids = MagicMock()
        mock_path_ids.workspace_id = "999"
        mock_path_ids.unique_id = "abc123"

        with patch.object(
            ExptOutputPathIds, "from_request_url", return_value=mock_path_ids
        ):
            # Mock the join query chain - user has no access, returns None
            mock_query = mock_db.query.return_value
            mock_query.join.return_value.filter.return_value.first.return_value = None
            # Mock the published data check - data is NOT published
            mock_query.filter.return_value.first.return_value = None

            result = await get_outputs_remote_bucket_name(
                req=mock_req,
                current_user=mock_user,
                db=mock_db,
            )

        # Should fall back to user's own bucket since data is not published
        assert result == "user-bucket-123"

    @pytest.mark.asyncio
    async def test_returns_owner_bucket_when_no_workspace_access_but_published(self):
        """Authenticated user without workspace access but with published data
        should get workspace owner's bucket"""
        from studio.app.common.core.experiment.experiment import ExptOutputPathIds

        mock_req = create_mock_request(
            "/api/visualizations/image//app/studio_data/output/999/abc123/file.json"
        )
        mock_db = MagicMock()
        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.remote_bucket_name = "user-bucket-123"

        mock_workspace = MagicMock()
        mock_workspace.user = MagicMock()
        mock_workspace.user.remote_bucket_name = "owner-bucket-published"

        mock_published_record = MagicMock()

        mock_path_ids = MagicMock()
        mock_path_ids.workspace_id = "999"
        mock_path_ids.unique_id = "abc123"

        with patch.object(
            ExptOutputPathIds, "from_request_url", return_value=mock_path_ids
        ):
            # Setup query mock to return different results for different calls
            # First call: workspace with join (user has no access) -> None
            # Second call: ExperimentRecord (published check) ->
            # mock_published_record
            # Third call: workspace without join (after publish check) ->
            # mock_workspace
            mock_query = mock_db.query.return_value
            mock_query.join.return_value.filter.return_value.first.return_value = None
            # Both ExperimentRecord and Workspace queries use .filter().first()
            mock_query.filter.return_value.first.side_effect = [
                mock_published_record,  # Published record check
                mock_workspace,  # Workspace lookup
            ]

            result = await get_outputs_remote_bucket_name(
                req=mock_req,
                current_user=mock_user,
                db=mock_db,
            )

        # Should use workspace owner's bucket since data is published
        assert result == "owner-bucket-published"

    @pytest.mark.asyncio
    async def test_returns_workspace_owner_bucket_for_public_request(self):
        """Public request should get workspace owner's bucket"""
        from studio.app.common.core.experiment.experiment import ExptOutputPathIds

        mock_req = create_mock_request(
            "/api/visualizations/image//app/studio_data/output/123/abc123/file.json"
        )
        mock_db = MagicMock()

        mock_workspace = MagicMock()
        mock_workspace.user = MagicMock()
        mock_workspace.user.remote_bucket_name = "owner-bucket-456"

        mock_path_ids = MagicMock()
        mock_path_ids.workspace_id = "123"
        mock_path_ids.unique_id = "abc123"

        with patch.object(
            ExptOutputPathIds, "from_request_url", return_value=mock_path_ids
        ):
            mock_query = mock_db.query.return_value
            mock_query.filter.return_value.first.return_value = mock_workspace

            result = await get_outputs_remote_bucket_name(
                req=mock_req,
                current_user=None,
                db=mock_db,
            )

        assert result == "owner-bucket-456"

    @pytest.mark.asyncio
    async def test_returns_default_bucket_for_public_request_without_workspace(self):
        """Public request without workspace should fall back to default bucket"""
        import os

        from studio.app.common.core.experiment.experiment import ExptOutputPathIds
        from studio.app.common.core.storage.remote_storage_controller import (
            RemoteStorageType,
        )

        mock_req = create_mock_request("/api/visualizations/image/some/invalid/path")
        mock_db = MagicMock()

        # Create a mock ExptOutputPathIds with None values (invalid path)
        mock_ids = MagicMock()
        mock_ids.workspace_id = None
        mock_ids.unique_id = None

        with patch.object(ExptOutputPathIds, "from_request_url", return_value=mock_ids):
            with patch.dict(os.environ, {"S3_DEFAULT_BUCKET_NAME": "default-bucket"}):
                with patch(
                    "studio.app.common.core.auth.auth_dependencies."
                    "RemoteStorageType.get_activated_type",
                    return_value=RemoteStorageType.S3,
                ):
                    result = await get_outputs_remote_bucket_name(
                        req=mock_req,
                        current_user=None,
                        db=mock_db,
                    )

        assert result == "default-bucket"

    @pytest.mark.asyncio
    async def test_extracts_workspace_id_from_query_params(self):
        """Should extract workspace_id from query params as fallback"""
        from studio.app.common.core.experiment.experiment import ExptOutputPathIds

        mock_req = create_mock_request(
            "/api/visualizations/image/some/path", {"workspace_id": "456"}
        )
        mock_db = MagicMock()

        mock_workspace = MagicMock()
        mock_workspace.user = MagicMock()
        mock_workspace.user.remote_bucket_name = "owner-bucket-from-query"

        # Mock from_request_url to return empty IDs (path parsing fails)
        mock_ids = MagicMock()
        mock_ids.workspace_id = None
        mock_ids.unique_id = None

        with patch.object(ExptOutputPathIds, "from_request_url", return_value=mock_ids):
            mock_query = mock_db.query.return_value
            mock_query.filter.return_value.first.return_value = mock_workspace

            result = await get_outputs_remote_bucket_name(
                req=mock_req,
                current_user=None,
                db=mock_db,
            )

        assert result == "owner-bucket-from-query"

    @pytest.mark.asyncio
    async def test_extracts_unique_id_from_query_params_for_published_check(self):
        """Should extract unique_id from query params for published experiment check.

        This tests the scenario where an authenticated user accesses published
        dataview data they don't own. The unique_id must be extracted from query
        params to verify the data is published.
        """
        from studio.app.common.core.experiment.experiment import ExptOutputPathIds

        # URL path doesn't contain full DIRPATH.OUTPUT_DIR prefix
        # Both workspace_id and unique_id in query params
        mock_req = create_mock_request(
            "/api/visualizations/image/7/tutorial2/cell_roi.json",
            {"workspace_id": "7", "unique_id": "tutorial2"},
        )
        mock_db = MagicMock()

        mock_user = MagicMock()
        mock_user.id = 3
        mock_user.remote_bucket_name = "user-bucket-3"

        mock_workspace = MagicMock()
        mock_workspace.user = MagicMock()
        mock_workspace.user.remote_bucket_name = "owner-bucket-2"

        mock_published_record = MagicMock()

        # Mock from_request_url to return empty IDs (path parsing fails)
        mock_ids = MagicMock()
        mock_ids.workspace_id = None
        mock_ids.unique_id = None

        with patch.object(ExptOutputPathIds, "from_request_url", return_value=mock_ids):
            mock_query = mock_db.query.return_value
            # User doesn't have direct access to workspace
            mock_query.join.return_value.filter.return_value.first.return_value = None
            # Published record check succeeds, then workspace lookup
            mock_query.filter.return_value.first.side_effect = [
                mock_published_record,  # Published record check
                mock_workspace,  # Workspace lookup
            ]

            result = await get_outputs_remote_bucket_name(
                req=mock_req,
                current_user=mock_user,
                db=mock_db,
            )

        # Should use workspace owner's bucket, not the current user's bucket
        assert result == "owner-bucket-2"


class TestGetUserRemoteBucketName:
    """Test _get_user_remote_bucket_name helper"""

    def test_returns_user_bucket_when_available(self):
        """Should return user's bucket name when user has one"""
        mock_user = MagicMock()
        mock_user.remote_bucket_name = "my-bucket"

        result = _get_user_remote_bucket_name(mock_user)

        assert result == "my-bucket"

    def test_returns_default_bucket_when_user_has_no_bucket(self):
        """Should return default bucket when user has no custom bucket"""
        import os

        mock_user = MagicMock()
        mock_user.remote_bucket_name = None

        with patch.dict(os.environ, {"S3_DEFAULT_BUCKET_NAME": "default-bucket"}):
            with patch(
                "studio.app.common.core.auth.auth_dependencies."
                "RemoteStorageType.get_activated_type"
            ) as mock_storage_type:
                from studio.app.common.core.storage.remote_storage_controller import (
                    RemoteStorageType,
                )

                mock_storage_type.return_value = RemoteStorageType.S3

                result = _get_user_remote_bucket_name(mock_user)

        assert result == "default-bucket"

    def test_returns_default_bucket_when_no_user(self):
        """Should return default bucket when no user provided"""
        import os

        with patch.dict(os.environ, {"S3_DEFAULT_BUCKET_NAME": "default-bucket"}):
            with patch(
                "studio.app.common.core.auth.auth_dependencies."
                "RemoteStorageType.get_activated_type"
            ) as mock_storage_type:
                from studio.app.common.core.storage.remote_storage_controller import (
                    RemoteStorageType,
                )

                mock_storage_type.return_value = RemoteStorageType.S3

                result = _get_user_remote_bucket_name(None)

        assert result == "default-bucket"


class TestOutputsAuthReleasesConnection:
    """Outputs auth deps must return the pooled DB connection before the route
    streams the file, so a slow download doesn't pin a connection."""

    _DS = "studio.app.common.core.auth.auth_dependencies.DataviewService."

    @pytest.mark.asyncio
    async def test_bucket_name_closes_db(self):
        mock_req = create_mock_request("/outputs/image/some/path/without/workspace")
        mock_db = MagicMock()
        mock_user = MagicMock()
        mock_user.remote_bucket_name = "user-bucket-123"

        with patch("studio.app.dir_path.DIRPATH") as mock_dirpath:
            mock_dirpath.OUTPUT_DIR = "/app/studio_data/output"
            await get_outputs_remote_bucket_name(
                req=mock_req, current_user=mock_user, db=mock_db
            )

        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_public_allowed_returns_and_closes_db(self):
        mock_db = MagicMock()
        with patch(
            self._DS + "is_dataview_public_outputs_request", return_value=True
        ), patch(
            self._DS + "validate_dataview_public_outputs_request", return_value=True
        ):
            result = await get_current_user_with_dataview_outputs_check(
                req=MagicMock(),
                res=MagicMock(),
                ex_token=None,
                credential=None,
                db=mock_db,
            )

        assert result is None
        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_public_denied_raises_403_and_closes_db(self):
        from fastapi import HTTPException

        mock_db = MagicMock()
        with patch(
            self._DS + "is_dataview_public_outputs_request", return_value=True
        ), patch(
            self._DS + "validate_dataview_public_outputs_request", return_value=False
        ):
            with pytest.raises(HTTPException) as exc:
                await get_current_user_with_dataview_outputs_check(
                    req=MagicMock(),
                    res=MagicMock(),
                    ex_token=None,
                    credential=None,
                    db=mock_db,
                )

        assert exc.value.status_code == 403
        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_authenticated_returns_user_and_closes_db(self):
        mock_db = MagicMock()
        mock_user = MagicMock()
        with patch(
            self._DS + "is_dataview_public_outputs_request", return_value=False
        ), patch(
            "studio.app.common.core.auth.auth_dependencies.get_current_user",
            new_callable=AsyncMock,
            return_value=mock_user,
        ):
            result = await get_current_user_with_dataview_outputs_check(
                req=MagicMock(),
                res=MagicMock(),
                ex_token="valid_token",
                credential=None,
                db=mock_db,
            )

        assert result == mock_user
        mock_db.close.assert_called_once()


class TestEnrichUserWithSubscriptionStatus:
    """The /users/me enrichment, at the two points it used to get wrong.

    `subscription_expiration` was set by crud_users but not here, so the same
    account reported an expiration on /api/subsc/mgmts and null on /users/me;
    and the status came from a truncated day count, so a paying user under 24
    hours from renewal read as being in the grace period.
    """

    @staticmethod
    def _enrich(expiration):
        user = SimpleNamespace(__dict__={})
        _enrich_user_with_subscription_status(
            user, expiration, SubscriptionPlanIds.PREMIUM, PlanName.PREMIUM.value
        )
        return user.__dict__

    def test_users_me_carries_the_expiration(self):
        expiration = get_current_datetime() + timedelta(days=5)
        assert self._enrich(expiration)["subscription_expiration"] == expiration

    def test_paying_user_inside_the_final_day_is_still_premium(self):
        fields = self._enrich(get_current_datetime() + timedelta(hours=23))
        assert fields["subscription_status"] == SubscriptionStatus.PREMIUM.value
        assert fields["subscription_days_remaining"] == 1

    def test_free_user_carries_a_null_expiration_rather_than_omitting_it(self):
        user = SimpleNamespace(__dict__={})
        _enrich_user_with_subscription_status(user, None, None, None)
        assert user.__dict__["subscription_expiration"] is None
        assert user.__dict__["subscription_status"] == SubscriptionStatus.FREE.value
