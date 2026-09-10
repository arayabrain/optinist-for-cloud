"""
Integration tests for dataview publish endpoint with optimistic locking.

Tests concurrent publish/unpublish operations.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
import yaml
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.dialects import mysql

from studio.app.common.routers.dataview import publish_dataview_records
from studio.app.common.schemas.dataview import LocalSyncStatus

# A minimal experiment.yaml that passes PublishValidator (all required fields,
# success == SUCCESS).
_VALID_CONFIG = {
    "workspace_id": "1",
    "unique_id": "uid",
    "name": "exp",
    "started_at": "2025-01-01 00:00:00",
    "finished_at": "2025-01-01 00:01:00",
    "success": "success",
    "hasNWB": True,
    "function": {},
    "nwb": {"session_description": "optinist"},
    "snakemake": {"use_conda": True},
}

# Parses (read() succeeds) but fails validation: nwb/snakemake are required but
# read via .get(), so their absence does not raise.
_INCOMPLETE_CONFIG = {
    "workspace_id": "1",
    "unique_id": "uid",
    "name": "exp",
    "started_at": "2025-01-01 00:00:00",
    "success": "success",
    "hasNWB": True,
    "function": {},
}


def _config_path(workspace_id, unique_id):
    from studio.app.common.routers.dataview import ExptConfigReader

    return ExptConfigReader.get_config_yaml_path(workspace_id, unique_id)


def _write_config(path, config):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(config, f)


def _can_publish(workspace_id, unique_id):
    from studio.app.common.routers.dataview import _local_config_can_publish

    return _local_config_can_publish(workspace_id, unique_id)


def _compiled_updates(mock_db):
    """Return ``[(sql, params), ...]`` for every UPDATE the endpoint executed."""
    out = []
    for call in mock_db.execute.call_args_list:
        compiled = call.args[0].compile(dialect=mysql.dialect())
        out.append((" ".join(str(compiled).split()), compiled.params))
    return out


class TestPublishDataviewRecords:
    """Test publish endpoint with optimistic locking"""

    @pytest.mark.asyncio
    async def test_publish_success(self):
        """Test successful publish operation"""
        mock_db = MagicMock()
        mock_user = MagicMock()
        mock_user.id = 123
        mock_user.remote_bucket_name = "test-bucket"
        mock_record = MagicMock()
        mock_record.id = 1
        mock_record.workspace_id = "1"
        mock_record.uid = "test_uid"
        mock_record.publish_status = 0
        mock_record.local_sync_status = LocalSyncStatus.synced.value
        mock_record.version = 0

        # Setup mock result for execute() with rowcount=1 (success)
        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_db.execute.return_value = mock_result

        # Mock validation result
        mock_validation = MagicMock()
        mock_validation.can_publish = True

        with patch(
            "studio.app.common.routers.dataview.DataviewService."
            "find_user_owned_dataview_record",
            return_value=mock_record,
        ), patch(
            "studio.app.common.routers.dataview._validate_experiment_exists_in_s3",
            return_value=(True, None),
        ), patch(
            "studio.app.common.routers.dataview.PublishValidator.validate",
            return_value=mock_validation,
        ):
            from studio.app.common.routers.dataview import PublishFlags

            result = await publish_dataview_records(
                id=1, flag=PublishFlags.on, db=mock_db, current_user=mock_user
            )

        assert result is True
        mock_db.commit.assert_called_once()

        # The three written values. Writing ``synced`` instead of ``pending``
        # keeps the record out of the sync job's retry set, which
        # ``execute.called`` cannot see.
        sql, params = _compiled_updates(mock_db)[0]
        assert sql.startswith("UPDATE experiment_records SET")
        assert params["publish_status"] == 1
        assert params["local_sync_status"] == LocalSyncStatus.pending.value
        assert "version=(experiment_records.version + " in sql
        assert params["version_1"] == 1

    @pytest.mark.asyncio
    async def test_unpublish_success(self):
        mock_db = MagicMock()
        mock_user = MagicMock()
        mock_user.id = 123
        mock_record = MagicMock()
        mock_record.id = 1
        mock_record.local_sync_status = LocalSyncStatus.synced.value
        mock_record.version = 0
        """Test successful unpublish operation"""
        mock_record.publish_status = 1

        # Setup mock result for execute() with rowcount=1 (success)
        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_db.execute.return_value = mock_result

        with patch(
            "studio.app.common.routers.dataview.DataviewService."
            "find_user_owned_dataview_record",
            return_value=mock_record,
        ):
            from studio.app.common.routers.dataview import PublishFlags

            result = await publish_dataview_records(
                id=1, flag=PublishFlags.off, db=mock_db, current_user=mock_user
            )

        assert result is True
        mock_db.commit.assert_called_once()

        sql, params = _compiled_updates(mock_db)[0]
        assert sql.startswith("UPDATE experiment_records SET")
        assert params["publish_status"] == 0
        assert params["local_sync_status"] == LocalSyncStatus.synced.value

    @pytest.mark.asyncio
    async def test_publish_no_change_needed(self):
        """Test publish when already published"""
        mock_db = MagicMock()
        mock_user = MagicMock()
        mock_user.id = 123
        mock_user.remote_bucket_name = "test-bucket"
        mock_record = MagicMock()
        mock_record.id = 1
        mock_record.workspace_id = "1"
        mock_record.uid = "test_uid"
        mock_record.version = 0
        mock_record.publish_status = 1

        # Mock validation result
        mock_validation = MagicMock()
        mock_validation.can_publish = True

        with patch(
            "studio.app.common.routers.dataview.DataviewService."
            "find_user_owned_dataview_record",
            return_value=mock_record,
        ), patch(
            "studio.app.common.routers.dataview.PublishValidator.validate",
            return_value=mock_validation,
        ):
            from studio.app.common.routers.dataview import PublishFlags

            result = await publish_dataview_records(
                id=1, flag=PublishFlags.on, db=mock_db, current_user=mock_user
            )

        assert result is True
        # ``version`` was asserted before, but production never mutates the
        # attribute: it writes ``version + 1`` in SQL. The observable claim is
        # that no statement is issued at all.
        mock_db.execute.assert_not_called()
        mock_db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_publish_record_not_found(self):
        mock_db = MagicMock()
        mock_user = MagicMock()
        mock_user.id = 123
        """Test publish when record not found"""
        with patch(
            "studio.app.common.routers.dataview.DataviewService."
            "find_user_owned_dataview_record",
            return_value=None,
        ):
            from studio.app.common.routers.dataview import PublishFlags

            with pytest.raises(HTTPException) as exc_info:
                await publish_dataview_records(
                    id=1, flag=PublishFlags.on, db=mock_db, current_user=mock_user
                )

            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_publish_concurrent_modification_retry(self):
        """A conflicted attempt is retried and the next success is reported.

        "Version increments exactly once" is not pinnable here: production
        re-reads the record on every attempt, and a ``MagicMock`` re-read hands
        back the same stale ``version``, so the retry's WHERE clause is a state
        production cannot reach. What is pinnable is the retry ladder, below.
        """
        mock_db = MagicMock()
        mock_user = MagicMock()
        mock_user.id = 123
        mock_user.remote_bucket_name = "test-bucket"
        mock_record = MagicMock()
        mock_record.id = 1
        mock_record.workspace_id = "1"
        mock_record.uid = "test_uid"
        mock_record.publish_status = 0
        mock_record.local_sync_status = LocalSyncStatus.synced.value
        mock_record.version = 0

        call_count = 0

        def mock_execute(stmt):
            nonlocal call_count
            call_count += 1
            mock_result = MagicMock()
            if call_count == 1:
                # First attempt: version conflict (rowcount=0)
                mock_result.rowcount = 0
            else:
                # Second attempt: success (rowcount=1)
                mock_result.rowcount = 1
            return mock_result

        mock_db.execute.side_effect = mock_execute

        # Mock validation result
        mock_validation = MagicMock()
        mock_validation.can_publish = True

        with patch(
            "studio.app.common.routers.dataview.DataviewService."
            "find_user_owned_dataview_record",
            return_value=mock_record,
        ), patch(
            "studio.app.common.routers.dataview._validate_experiment_exists_in_s3",
            return_value=(True, None),
        ), patch(
            "studio.app.common.routers.dataview.PublishValidator.validate",
            return_value=mock_validation,
        ):
            from studio.app.common.routers.dataview import PublishFlags

            result = await publish_dataview_records(
                id=1, flag=PublishFlags.on, db=mock_db, current_user=mock_user
            )

        assert result is True
        assert call_count == 2, "a conflict must be retried, not surfaced"
        assert mock_db.commit.call_count == 2

    @pytest.mark.asyncio
    async def test_the_update_is_guarded_by_the_version_it_read(self):
        """The single-record endpoint's optimistic lock.

        ``TestPublishToggleIsLastWriteWins`` drives the bulk endpoint, which
        carries no version predicate at all, so nothing pinned that a rapid
        second toggle on one record cannot overwrite the first. Without
        ``version == current_version`` in the WHERE, two concurrent toggles both
        report success and the loser's ``local_sync_status`` silently wins.
        """
        mock_db = MagicMock()
        mock_user = MagicMock()
        mock_user.id = 123
        mock_user.remote_bucket_name = "test-bucket"
        mock_record = MagicMock()
        mock_record.id = 7
        mock_record.workspace_id = "1"
        mock_record.uid = "test_uid"
        mock_record.publish_status = 0
        mock_record.local_sync_status = LocalSyncStatus.synced.value
        mock_record.version = 4

        mock_db.execute.return_value.rowcount = 1
        mock_validation = MagicMock()
        mock_validation.can_publish = True

        with patch(
            "studio.app.common.routers.dataview.DataviewService."
            "find_user_owned_dataview_record",
            return_value=mock_record,
        ), patch(
            "studio.app.common.routers.dataview._validate_experiment_exists_in_s3",
            return_value=(True, None),
        ), patch(
            "studio.app.common.routers.dataview.PublishValidator.validate",
            return_value=mock_validation,
        ):
            from studio.app.common.routers.dataview import PublishFlags

            await publish_dataview_records(
                id=7, flag=PublishFlags.on, db=mock_db, current_user=mock_user
            )

        sql, params = _compiled_updates(mock_db)[0]
        assert "WHERE experiment_records.id = " in sql
        assert "AND experiment_records.version = " in sql
        assert params["id_1"] == 7
        assert params["version_2"] == 4

    @pytest.mark.asyncio
    async def test_publish_concurrent_modification_max_retries(self):
        """Test failure after max retries on concurrent modification"""
        mock_db = MagicMock()
        mock_user = MagicMock()
        mock_user.id = 123
        mock_user.remote_bucket_name = "test-bucket"
        mock_record = MagicMock()
        mock_record.id = 1
        mock_record.workspace_id = "1"
        mock_record.uid = "test_uid"
        mock_record.publish_status = 0
        mock_record.local_sync_status = LocalSyncStatus.synced.value
        mock_record.version = 0

        # Always return rowcount=0 to simulate persistent version conflicts
        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_db.execute.return_value = mock_result

        # Mock validation result
        mock_validation = MagicMock()
        mock_validation.can_publish = True

        with patch(
            "studio.app.common.routers.dataview.DataviewService."
            "find_user_owned_dataview_record",
            return_value=mock_record,
        ), patch(
            "studio.app.common.routers.dataview._validate_experiment_exists_in_s3",
            return_value=(True, None),
        ), patch(
            "studio.app.common.routers.dataview.PublishValidator.validate",
            return_value=mock_validation,
        ):
            from studio.app.common.routers.dataview import PublishFlags

            with pytest.raises(HTTPException) as exc_info:
                await publish_dataview_records(
                    id=1, flag=PublishFlags.on, db=mock_db, current_user=mock_user
                )

            assert exc_info.value.status_code == 409
            assert "Concurrent modification" in exc_info.value.detail
            # Exactly three attempts: a smaller ladder surfaces a transient
            # conflict as a user-visible 409, a larger one holds the request open.
            assert mock_db.execute.call_count == 3


class TestPublicDataviewReproduceWorkflow:
    """Test public dataview reproduce endpoint"""

    @pytest.mark.asyncio
    async def test_reproduce_pending_sync_returns_202(self):
        """Test 202 response when experiment is pending sync and data not available"""
        from unittest.mock import AsyncMock

        from studio.app.common.routers.dataview import public_reproduce_experiment

        mock_record = MagicMock()
        mock_record.local_sync_status = LocalSyncStatus.pending.value

        mock_remote_controller = MagicMock()
        mock_remote_controller.download_experiment = AsyncMock(return_value=True)

        mock_remote_reader = AsyncMock()
        mock_remote_reader.__aenter__ = AsyncMock(return_value=mock_remote_controller)
        mock_remote_reader.__aexit__ = AsyncMock(return_value=False)

        mock_validation = MagicMock()
        mock_validation.is_displayable = False
        mock_validation.reason = "Data not available"

        with patch(
            "studio.app.common.routers.dataview.DataviewService."
            "find_dataview_record",
            return_value=mock_record,
        ):
            with patch(
                "studio.app.common.routers.dataview.RemoteSyncStatusFileUtil."
                "check_sync_status_unsynced",
                return_value=False,
            ):
                with patch.dict(
                    "os.environ", {"S3_DEFAULT_BUCKET_NAME": "test-bucket"}
                ):
                    with patch(
                        "studio.app.common.routers.dataview.RemoteStorageReader",
                        return_value=mock_remote_reader,
                    ):
                        with patch(
                            "studio.app.common.routers.dataview.PublishValidator."
                            "validate_for_display",
                            return_value=mock_validation,
                        ):
                            response = await public_reproduce_experiment(
                                workspace_id="1", unique_id="exp123", db=MagicMock()
                            )

        assert response.status_code == 202
        assert "pending_sync" in response.body.decode()

    @pytest.mark.asyncio
    async def test_reproduce_sync_error_returns_503(self):
        """Test 503 response when experiment has sync error and data not available"""
        from unittest.mock import AsyncMock

        from studio.app.common.routers.dataview import public_reproduce_experiment

        mock_record = MagicMock()
        mock_record.local_sync_status = LocalSyncStatus.error.value

        mock_remote_controller = MagicMock()
        mock_remote_controller.download_experiment = AsyncMock(return_value=True)

        mock_remote_reader = AsyncMock()
        mock_remote_reader.__aenter__ = AsyncMock(return_value=mock_remote_controller)
        mock_remote_reader.__aexit__ = AsyncMock(return_value=False)

        mock_validation = MagicMock()
        mock_validation.is_displayable = False
        mock_validation.reason = "Data not available"

        with patch(
            "studio.app.common.routers.dataview.DataviewService."
            "find_dataview_record",
            return_value=mock_record,
        ):
            with patch(
                "studio.app.common.routers.dataview.RemoteSyncStatusFileUtil."
                "check_sync_status_unsynced",
                return_value=False,
            ):
                with patch.dict(
                    "os.environ", {"S3_DEFAULT_BUCKET_NAME": "test-bucket"}
                ):
                    with patch(
                        "studio.app.common.routers.dataview.RemoteStorageReader",
                        return_value=mock_remote_reader,
                    ):
                        with patch(
                            "studio.app.common.routers.dataview.PublishValidator."
                            "validate_for_display",
                            return_value=mock_validation,
                        ):
                            response = await public_reproduce_experiment(
                                workspace_id="1", unique_id="exp123", db=MagicMock()
                            )

        assert response.status_code == 503
        assert "data_error" in response.body.decode()

    @pytest.mark.asyncio
    async def test_reproduce_downloads_from_s3_if_missing(self):
        """Test S3 download when experiment not on local EBS"""
        from unittest.mock import AsyncMock

        from studio.app.common.routers.dataview import public_reproduce_experiment

        mock_record = MagicMock()
        mock_record.local_sync_status = LocalSyncStatus.synced.value

        mock_remote_controller = MagicMock()
        # Use AsyncMock for async method
        mock_remote_controller.download_experiment = AsyncMock(return_value=True)

        mock_remote_reader = AsyncMock()
        mock_remote_reader.__aenter__ = AsyncMock(return_value=mock_remote_controller)
        mock_remote_reader.__aexit__ = AsyncMock(return_value=False)

        mock_validation = MagicMock()
        mock_validation.is_displayable = True

        with patch(
            "studio.app.common.routers.dataview.DataviewService."
            "find_dataview_record",
            return_value=mock_record,
        ):
            with patch(
                "studio.app.common.routers.dataview.RemoteSyncStatusFileUtil."
                "check_sync_status_unsynced",
                return_value=True,
            ):
                with patch(
                    "studio.app.common.routers.dataview."
                    "RemoteStorageController.is_available",
                    return_value=True,
                ):
                    with patch.dict(
                        "os.environ", {"S3_DEFAULT_BUCKET_NAME": "test-bucket"}
                    ):
                        with patch(
                            "studio.app.common.routers.dataview.RemoteStorageReader",
                            return_value=mock_remote_reader,
                        ):
                            with patch(
                                "studio.app.common.routers.dataview.PublishValidator."
                                "validate_for_display",
                                return_value=mock_validation,
                            ):
                                with patch(
                                    "studio.app.common.routers.dataview."
                                    "reproduce_experiment"
                                ):
                                    await public_reproduce_experiment(
                                        workspace_id="1",
                                        unique_id="exp123",
                                        db=MagicMock(),
                                    )

        mock_remote_controller.download_experiment.assert_called_once()

    @pytest.mark.asyncio
    async def test_reproduce_auto_updates_sync_status_when_data_available(self):
        """Test sync status auto-updates to synced when data is available"""
        from unittest.mock import AsyncMock

        from studio.app.common.routers.dataview import public_reproduce_experiment

        mock_record = MagicMock()
        mock_record.local_sync_status = LocalSyncStatus.error.value

        mock_remote_controller = MagicMock()
        mock_remote_controller.download_experiment = AsyncMock(return_value=True)

        mock_remote_reader = AsyncMock()
        mock_remote_reader.__aenter__ = AsyncMock(return_value=mock_remote_controller)
        mock_remote_reader.__aexit__ = AsyncMock(return_value=False)

        mock_validation = MagicMock()
        mock_validation.is_displayable = True

        mock_db = MagicMock()
        mock_db.execute.return_value.rowcount = 1

        with patch(
            "studio.app.common.routers.dataview.DataviewService."
            "find_dataview_record",
            return_value=mock_record,
        ), patch("os.path.exists", return_value=True), patch.dict(
            "os.environ", {"S3_DEFAULT_BUCKET_NAME": "test-bucket"}
        ), patch(
            "studio.app.common.routers.dataview.PublishValidator."
            "validate_for_display",
            return_value=mock_validation,
        ), patch(
            "studio.app.common.routers.dataview.reproduce_experiment"
        ):
            with patch(
                "studio.app.common.routers.dataview.RemoteSyncStatusFileUtil."
                "check_sync_status_unsynced",
                return_value=False,
            ):
                with patch.dict(
                    "os.environ", {"S3_DEFAULT_BUCKET_NAME": "test-bucket"}
                ):
                    with patch(
                        "studio.app.common.routers.dataview.RemoteStorageReader",
                        return_value=mock_remote_reader,
                    ):
                        with patch(
                            "studio.app.common.routers.dataview.PublishValidator."
                            "validate_for_display",
                            return_value=mock_validation,
                        ):
                            with patch(
                                "studio.app.common.routers.dataview."
                                "reproduce_experiment",
                                return_value=JSONResponse(
                                    status_code=200, content={"ok": True}
                                ),
                            ):
                                response = await public_reproduce_experiment(
                                    workspace_id="1", unique_id="exp123", db=mock_db
                                )

        assert response.status_code == 200
        mock_db.execute.assert_called_once()
        mock_db.commit.assert_called_once()
        # Which status: the sibling ``..._demotes_to_error`` pins error, this one
        # has to pin synced, or a promotion writing ``error`` would pass here.
        params = mock_db.execute.call_args[0][0].compile().params
        assert params["local_sync_status"] == LocalSyncStatus.synced.value
        assert params["version_1"] == 1

    @pytest.mark.asyncio
    async def test_reproduce_synced_but_missing_in_s3_demotes_to_error(self):
        """A synced row whose files are gone from S3 is demoted to error."""
        from unittest.mock import AsyncMock

        from studio.app.common.routers.dataview import public_reproduce_experiment

        mock_record = MagicMock()
        mock_record.id = 1
        mock_record.local_sync_status = LocalSyncStatus.synced.value

        mock_validation = MagicMock()
        mock_validation.is_displayable = False
        mock_validation.reason = "Data not available"

        mock_db = MagicMock()

        with patch(
            "studio.app.common.routers.dataview.DataviewService."
            "find_dataview_record",
            return_value=mock_record,
        ), patch(
            "studio.app.common.routers.dataview.RemoteSyncStatusFileUtil."
            "check_sync_status_unsynced",
            return_value=False,
        ), patch(
            "studio.app.common.routers.dataview.RemoteStorageController."
            "is_available",
            return_value=True,
        ), patch(
            "studio.app.common.routers.dataview."
            "_resolve_workspace_remote_bucket_name",
            return_value="test-bucket",
        ), patch(
            "studio.app.common.routers.dataview._validate_experiment_exists_in_s3",
            new=AsyncMock(return_value=(False, "No data found in S3")),
        ), patch(
            "studio.app.common.routers.dataview.PublishValidator."
            "validate_for_display",
            return_value=mock_validation,
        ):
            response = await public_reproduce_experiment(
                workspace_id="1", unique_id="exp123", db=mock_db
            )

        assert response.status_code == 503
        mock_db.execute.assert_called_once()
        mock_db.commit.assert_called_once()
        # Pin that the statement demotes synced -> error (SET target and guard)
        params = mock_db.execute.call_args[0][0].compile().params
        assert params["local_sync_status"] == LocalSyncStatus.error.value
        assert LocalSyncStatus.synced.value in params.values()

    @pytest.mark.asyncio
    async def test_reproduce_synced_present_in_s3_does_not_demote(self):
        """A synced row still present in S3 is not demoted (transient local miss)."""
        from unittest.mock import AsyncMock

        from studio.app.common.routers.dataview import public_reproduce_experiment

        mock_record = MagicMock()
        mock_record.id = 1
        mock_record.local_sync_status = LocalSyncStatus.synced.value

        mock_validation = MagicMock()
        mock_validation.is_displayable = False
        mock_validation.reason = "Data not available"

        mock_db = MagicMock()

        with patch(
            "studio.app.common.routers.dataview.DataviewService."
            "find_dataview_record",
            return_value=mock_record,
        ), patch(
            "studio.app.common.routers.dataview.RemoteSyncStatusFileUtil."
            "check_sync_status_unsynced",
            return_value=False,
        ), patch(
            "studio.app.common.routers.dataview.RemoteStorageController."
            "is_available",
            return_value=True,
        ), patch(
            "studio.app.common.routers.dataview."
            "_resolve_workspace_remote_bucket_name",
            return_value="test-bucket",
        ), patch(
            "studio.app.common.routers.dataview._validate_experiment_exists_in_s3",
            new=AsyncMock(return_value=(True, None)),
        ), patch(
            "studio.app.common.routers.dataview.PublishValidator."
            "validate_for_display",
            return_value=mock_validation,
        ):
            response = await public_reproduce_experiment(
                workspace_id="1", unique_id="exp123", db=mock_db
            )

        assert response.status_code == 503
        mock_db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_publish_blocks_when_s3_check_unverifiable(self):
        """Publish must block (400) when the S3 check is unverifiable (None)."""
        from unittest.mock import AsyncMock

        from studio.app.common.core.storage.remote_storage_controller import (
            RemoteStorageType,
        )
        from studio.app.common.routers.dataview import PublishFlags

        mock_db = MagicMock()
        mock_user = MagicMock()
        mock_user.id = 123
        mock_user.remote_bucket_name = "test-bucket"
        mock_record = MagicMock()
        mock_record.id = 1
        mock_record.workspace_id = "1"
        mock_record.uid = "test_uid"
        mock_record.publish_status = 0
        mock_record.local_sync_status = LocalSyncStatus.synced.value
        mock_record.version = 0

        mock_validation = MagicMock()
        mock_validation.can_publish = True

        with patch(
            "studio.app.common.routers.dataview.DataviewService."
            "find_user_owned_dataview_record",
            return_value=mock_record,
        ), patch(
            "studio.app.common.routers.dataview.RemoteStorageType.get_activated_type",
            return_value=RemoteStorageType.S3,
        ), patch(
            "studio.app.common.routers.dataview._validate_experiment_exists_in_s3",
            new=AsyncMock(return_value=(None, "Could not verify S3 data")),
        ), patch(
            "studio.app.common.routers.dataview.PublishValidator.validate",
            return_value=mock_validation,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await publish_dataview_records(
                    id=1, flag=PublishFlags.on, db=mock_db, current_user=mock_user
                )

        assert exc_info.value.status_code == 400
        mock_db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_reproduce_synced_s3_check_fails_does_not_demote(self):
        """A transient S3 check failure (None) must not demote a synced row."""
        from unittest.mock import AsyncMock

        from studio.app.common.routers.dataview import public_reproduce_experiment

        mock_record = MagicMock()
        mock_record.id = 1
        mock_record.local_sync_status = LocalSyncStatus.synced.value

        mock_validation = MagicMock()
        mock_validation.is_displayable = False
        mock_validation.reason = "Data not available"

        mock_db = MagicMock()

        with patch(
            "studio.app.common.routers.dataview.DataviewService."
            "find_dataview_record",
            return_value=mock_record,
        ), patch(
            "studio.app.common.routers.dataview.RemoteSyncStatusFileUtil."
            "check_sync_status_unsynced",
            return_value=False,
        ), patch(
            "studio.app.common.routers.dataview.RemoteStorageController."
            "is_available",
            return_value=True,
        ), patch(
            "studio.app.common.routers.dataview."
            "_resolve_workspace_remote_bucket_name",
            return_value="test-bucket",
        ), patch(
            "studio.app.common.routers.dataview._validate_experiment_exists_in_s3",
            new=AsyncMock(return_value=(None, "Could not verify S3 data")),
        ), patch(
            "studio.app.common.routers.dataview.PublishValidator."
            "validate_for_display",
            return_value=mock_validation,
        ):
            response = await public_reproduce_experiment(
                workspace_id="1", unique_id="exp123", db=mock_db
            )

        assert response.status_code == 503
        mock_db.execute.assert_not_called()


class TestValidateExperimentExistsInS3:
    """Exercise the real tri-state S3 existence check against a mocked client."""

    @staticmethod
    def _s3_client_ctx(*, key_count=None, raise_exc=None):
        from unittest.mock import AsyncMock

        client = MagicMock()
        if raise_exc is not None:
            client.list_objects_v2 = AsyncMock(side_effect=raise_exc)
        else:
            client.list_objects_v2 = AsyncMock(return_value={"KeyCount": key_count})
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=client)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    async def _run(self, ctx):
        from studio.app.common.core.storage.s3_storage_controller import (
            S3StorageController,
        )
        from studio.app.common.routers.dataview import _validate_experiment_exists_in_s3

        with patch(
            "studio.app.common.routers.dataview.RemoteStorageController.is_available",
            return_value=True,
        ), patch.object(
            S3StorageController,
            "_S3StorageController__get_s3_client",
            return_value=ctx,
        ):
            return await _validate_experiment_exists_in_s3("1", "exp123", "test-bucket")

    @pytest.mark.asyncio
    async def test_present_returns_true(self):
        exists, error = await self._run(self._s3_client_ctx(key_count=3))
        assert exists is True
        assert error is None

    @pytest.mark.asyncio
    async def test_confirmed_absent_returns_false(self):
        exists, error = await self._run(self._s3_client_ctx(key_count=0))
        assert exists is False
        assert error is not None

    @pytest.mark.asyncio
    async def test_transient_error_returns_none(self):
        exists, error = await self._run(
            self._s3_client_ctx(raise_exc=Exception("throttled"))
        )
        assert exists is None
        assert error is not None


class TestSyncExperimentConfigForPublish:
    """Behaviour of the publish pre-sync helper on a real filesystem.

    Only the S3 download is faked; validation runs against the real
    PublishValidator and real files, so the tests pin the repaired behaviour
    (not just that the code calls a mock).
    """

    class _FakeReader:
        """Stands in for RemoteStorageReader.

        Records constructor args, and on ``download_experiment_meta`` writes
        ``s3_config`` to the local config path (``None`` => S3 has nothing).
        """

        def __init__(self, s3_config):
            self._s3_config = s3_config
            self.calls = []

        def __call__(self, bucket, workspace_id, unique_id, sync_mode=None):
            self.calls.append((bucket, workspace_id, unique_id, sync_mode))
            outer = self

            class _Ctx:
                async def __aenter__(self_ctx):
                    return self_ctx

                async def __aexit__(self_ctx, *exc):
                    return False

                async def download_experiment_meta(self_ctx, ws, uid):
                    if outer._s3_config is not None:
                        _write_config(_config_path(ws, uid), outer._s3_config)
                    return True

            return _Ctx()

    async def _run(self, tmp_path, monkeypatch, *, local, s3, bucket="test-bucket"):
        """Seed ``local`` config (or None), fake S3 to yield ``s3``, run the
        helper, and return (fake_reader, config_path)."""
        from studio.app.common.routers.dataview import (
            _sync_experiment_config_for_publish,
        )
        from studio.app.dir_path import DIRPATH

        monkeypatch.setattr(DIRPATH, "OUTPUT_DIR", str(tmp_path))
        config_path = _config_path("1", "uid")
        if local is not None:
            _write_config(config_path, local)

        fake = self._FakeReader(s3)
        with patch(
            "studio.app.common.routers.dataview.RemoteStorageController."
            "is_available",
            return_value=True,
        ), patch("studio.app.common.routers.dataview.RemoteStorageReader", fake):
            await _sync_experiment_config_for_publish("1", "uid", bucket)
        return fake, config_path

    @pytest.mark.asyncio
    async def test_absent_local_valid_in_s3_becomes_publishable(
        self, tmp_path, monkeypatch
    ):
        """Probe A: no local config, valid in S3 -> downloaded and publishable."""
        fake, config_path = await self._run(
            tmp_path, monkeypatch, local=None, s3=_VALID_CONFIG
        )
        assert os.path.exists(config_path)
        assert _can_publish("1", "uid") is True

    @pytest.mark.asyncio
    async def test_stub_local_valid_in_s3_is_repaired(self, tmp_path, monkeypatch):
        """Probe B: `{}` stub, valid in S3 -> repaired, backup discarded."""
        fake, config_path = await self._run(
            tmp_path, monkeypatch, local={}, s3=_VALID_CONFIG
        )
        assert _can_publish("1", "uid") is True
        assert not os.path.exists(f"{config_path}.bak")

    @pytest.mark.asyncio
    async def test_incomplete_local_valid_in_s3_is_repaired(
        self, tmp_path, monkeypatch
    ):
        """Probe C: parses but missing required fields -> re-synced from S3.

        This is the case the previous read()-based skip did NOT repair.
        """
        # Sanity: the seeded local config is not publishable to begin with.
        from studio.app.dir_path import DIRPATH

        monkeypatch.setattr(DIRPATH, "OUTPUT_DIR", str(tmp_path))
        _write_config(_config_path("1", "uid"), _INCOMPLETE_CONFIG)
        assert _can_publish("1", "uid") is False

        await self._run(
            tmp_path, monkeypatch, local=_INCOMPLETE_CONFIG, s3=_VALID_CONFIG
        )
        assert _can_publish("1", "uid") is True

    @pytest.mark.asyncio
    async def test_stub_local_absent_in_s3_preserves_original(
        self, tmp_path, monkeypatch
    ):
        """Probe E: `{}` stub, absent in S3 -> file is NOT destroyed."""
        fake, config_path = await self._run(tmp_path, monkeypatch, local={}, s3=None)
        # The only local copy must survive (restored from backup).
        assert os.path.exists(config_path)
        with open(config_path) as f:
            assert yaml.safe_load(f) == {}
        assert not os.path.exists(f"{config_path}.bak")
        assert _can_publish("1", "uid") is False

    @pytest.mark.asyncio
    async def test_valid_local_skips_download(self, tmp_path, monkeypatch):
        """A valid local config is left untouched (no S3 call)."""
        fake, _ = await self._run(
            tmp_path, monkeypatch, local=_VALID_CONFIG, s3=_VALID_CONFIG
        )
        assert fake.calls == []

    @pytest.mark.asyncio
    async def test_download_uses_metadata_only_and_correct_args(
        self, tmp_path, monkeypatch
    ):
        """The reader is constructed with (bucket, ws, uid, METADATA_ONLY)."""
        from studio.app.common.core.storage.remote_storage_controller import (
            RemoteExperimentSyncMode,
        )

        fake, _ = await self._run(
            tmp_path, monkeypatch, local=None, s3=_VALID_CONFIG, bucket="b1"
        )
        assert fake.calls == [
            ("b1", "1", "uid", RemoteExperimentSyncMode.METADATA_ONLY)
        ]

    @pytest.mark.asyncio
    async def test_noop_without_bucket(self, tmp_path, monkeypatch):
        """No bucket -> return early, do not construct the reader."""
        fake, _ = await self._run(
            tmp_path, monkeypatch, local=None, s3=_VALID_CONFIG, bucket=""
        )
        assert fake.calls == []


class TestMultiplePublishDataviewRecords:
    """Bulk publish: pre-sync + all-or-nothing validation."""

    @staticmethod
    def _make_record(record_id):
        record = MagicMock()
        record.id = record_id
        record.workspace_id = "1"
        record.uid = f"uid{record_id}"
        record.name = f"exp{record_id}"
        return record

    @pytest.mark.asyncio
    async def test_bulk_publish_success_presyncs_each_record(self):
        """All valid: pre-sync runs per record and the service is invoked."""
        from unittest.mock import AsyncMock

        from studio.app.common.routers.dataview import (
            PublishFlags,
            multiple_publish_dataview_records,
        )

        mock_db = MagicMock()
        mock_user = MagicMock()
        mock_user.id = 123
        mock_user.remote_bucket_name = "test-bucket"

        records = {1: self._make_record(1), 2: self._make_record(2)}

        mock_validation = MagicMock()
        mock_validation.can_publish = True

        with patch(
            "studio.app.common.routers.dataview.DataviewService."
            "find_user_owned_dataview_record",
            side_effect=lambda db, rid, uid: records.get(rid),
        ), patch(
            "studio.app.common.routers.dataview." "_sync_experiment_config_for_publish",
            new=AsyncMock(),
        ) as mock_sync, patch(
            "studio.app.common.routers.dataview.RemoteStorageController."
            "is_available",
            return_value=True,
        ), patch(
            "studio.app.common.routers.dataview._resolve_workspace_remote_bucket_name",
            return_value="test-bucket",
        ), patch(
            "studio.app.common.routers.dataview.PublishValidator.validate",
            return_value=mock_validation,
        ), patch(
            "studio.app.common.routers.dataview.DataviewService."
            "multiple_publish_dataview_records"
        ) as mock_service:
            result = await multiple_publish_dataview_records(
                ids=[1, 2], flag=PublishFlags.on, db=mock_db, current_user=mock_user
            )

        assert result is True
        assert mock_sync.await_count == 2
        mock_service.assert_called_once()

    @pytest.mark.asyncio
    async def test_bulk_publish_blocks_when_a_record_invalid(self):
        """One invalid record blocks the whole batch with 400; no publish."""
        from unittest.mock import AsyncMock

        from studio.app.common.routers.dataview import (
            PublishFlags,
            multiple_publish_dataview_records,
        )

        mock_db = MagicMock()
        mock_user = MagicMock()
        mock_user.id = 123
        mock_user.remote_bucket_name = "test-bucket"

        records = {1: self._make_record(1), 2: self._make_record(2)}

        def validate(workspace_id, unique_id, **kwargs):
            result = MagicMock()
            result.can_publish = unique_id != "uid2"
            result.reason = None if result.can_publish else "corrupted"
            return result

        with patch(
            "studio.app.common.routers.dataview.DataviewService."
            "find_user_owned_dataview_record",
            side_effect=lambda db, rid, uid: records.get(rid),
        ), patch(
            "studio.app.common.routers.dataview." "_sync_experiment_config_for_publish",
            new=AsyncMock(),
        ), patch(
            "studio.app.common.routers.dataview.PublishValidator.validate",
            side_effect=validate,
        ), patch(
            "studio.app.common.routers.dataview.DataviewService."
            "multiple_publish_dataview_records"
        ) as mock_service:
            with pytest.raises(HTTPException) as exc_info:
                await multiple_publish_dataview_records(
                    ids=[1, 2],
                    flag=PublishFlags.on,
                    db=mock_db,
                    current_user=mock_user,
                )

        assert exc_info.value.status_code == 400
        mock_service.assert_not_called()


class TestSinglePublishPreSync:
    """Single publish wires the pre-sync helper before validation."""

    @pytest.mark.asyncio
    async def test_single_publish_presyncs_before_validate(self):
        """publish_dataview_records calls the sync helper before validating."""
        from unittest.mock import AsyncMock

        from studio.app.common.routers.dataview import (
            PublishFlags,
            publish_dataview_records,
        )

        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_db.execute.return_value = mock_result

        mock_user = MagicMock()
        mock_user.id = 123
        mock_user.remote_bucket_name = "test-bucket"

        mock_record = MagicMock()
        mock_record.id = 1
        mock_record.workspace_id = "1"
        mock_record.uid = "test_uid"
        mock_record.publish_status = 0
        mock_record.local_sync_status = LocalSyncStatus.synced.value
        mock_record.version = 0

        mock_validation = MagicMock()
        mock_validation.can_publish = True

        order = []

        async def sync_side(*args, **kwargs):
            order.append("sync")

        def validate_side(*args, **kwargs):
            order.append("validate")
            return mock_validation

        with patch(
            "studio.app.common.routers.dataview.DataviewService."
            "find_user_owned_dataview_record",
            return_value=mock_record,
        ), patch(
            "studio.app.common.routers.dataview." "_sync_experiment_config_for_publish",
            new=AsyncMock(side_effect=sync_side),
        ) as mock_sync, patch(
            "studio.app.common.routers.dataview.RemoteStorageController."
            "is_available",
            return_value=True,
        ), patch(
            "studio.app.common.routers.dataview._resolve_workspace_remote_bucket_name",
            return_value="test-bucket",
        ), patch(
            "studio.app.common.routers.dataview.PublishValidator.validate",
            side_effect=validate_side,
        ), patch(
            "studio.app.common.routers.dataview._validate_experiment_exists_in_s3",
            new=AsyncMock(return_value=(True, None)),
        ):
            result = await publish_dataview_records(
                id=1, flag=PublishFlags.on, db=mock_db, current_user=mock_user
            )

        assert result is True
        mock_sync.assert_awaited_once_with("1", "test_uid", "test-bucket")
        # Sync must be ordered before validation.
        assert order == ["sync", "validate"]
