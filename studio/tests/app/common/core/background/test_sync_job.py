"""
Unit tests for published experiment sync job.

Tests S3 validation, proactive download trigger, and startup sync.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from studio.app.common.core.background.sync_job import PublishedExperimentSyncJob


class TestStartupSync:
    """Test one-time startup sync for API containers"""

    @pytest.mark.asyncio
    async def test_downloads_missing_experiments(self):
        """Test startup sync downloads experiments missing locally"""
        published = [
            ("ws1", "uid1", 1, "bucket1"),
            ("ws2", "uid2", 2, "bucket1"),
        ]

        mock_s3 = MagicMock()
        mock_s3.download_experiment = AsyncMock(return_value=True)

        with patch.object(
            PublishedExperimentSyncJob,
            "_get_all_published_experiments",
            return_value=published,
        ):
            with patch("os.path.exists", return_value=False):
                with patch(
                    "studio.app.common.core.background" ".sync_job.S3StorageController",
                    return_value=mock_s3,
                ):
                    await PublishedExperimentSyncJob.run_startup_sync()

        # 2 experiments x 2 phases = 4 download calls
        assert mock_s3.download_experiment.call_count == 4

    @pytest.mark.asyncio
    async def test_skips_locally_present_experiments(self):
        """Test startup sync skips experiments already on disk"""
        published = [
            ("ws1", "uid1", 1, "bucket1"),
        ]

        mock_s3 = MagicMock()
        mock_s3.download_experiment = AsyncMock(return_value=True)

        with patch.object(
            PublishedExperimentSyncJob,
            "_get_all_published_experiments",
            return_value=published,
        ):
            # Both yaml files exist locally
            with patch("os.path.exists", return_value=True):
                with patch(
                    "studio.app.common.core.background" ".sync_job.S3StorageController",
                    return_value=mock_s3,
                ):
                    await PublishedExperimentSyncJob.run_startup_sync()

        assert mock_s3.download_experiment.call_count == 0

    @pytest.mark.asyncio
    async def test_handles_empty_published_list(self):
        """Test startup sync handles no published experiments"""
        with patch.object(
            PublishedExperimentSyncJob,
            "_get_all_published_experiments",
            return_value=[],
        ):
            # Should not raise
            await PublishedExperimentSyncJob.run_startup_sync()


class TestProactiveDownloadTrigger:
    """Tests for _trigger_proactive_download ALB call"""

    @pytest.mark.asyncio
    async def test_trigger_success(self):
        """Successful POST returns True"""
        env = {
            "ALB_DNS_NAME": "test-alb.example.com",
            "INTERNAL_API_SECRET": "secret-123",
        }
        with patch.dict(os.environ, env):
            with patch("requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_post.return_value = mock_resp

                result = await PublishedExperimentSyncJob._trigger_proactive_download(
                    "ws1", "uid1", "bucket1"
                )

                assert result is True
                mock_post.assert_called_once()
                call_url = mock_post.call_args[0][0]
                assert "sync-experiment/ws1/uid1" in call_url
                params = mock_post.call_args[1]["params"]
                assert params["bucket_name"] == "bucket1"

    @pytest.mark.asyncio
    async def test_trigger_passes_has_thumbnails(self):
        """has_thumbnails is passed to ALB request"""
        env = {
            "ALB_DNS_NAME": "test-alb.example.com",
            "INTERNAL_API_SECRET": "secret-123",
        }
        with patch.dict(os.environ, env):
            with patch("requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_post.return_value = mock_resp

                await PublishedExperimentSyncJob._trigger_proactive_download(
                    "ws1",
                    "uid1",
                    "bucket1",
                    has_thumbnails=False,
                )

                params = mock_post.call_args[1]["params"]
                assert params["has_thumbnails"] == "false"

    @pytest.mark.asyncio
    async def test_trigger_missing_config(self):
        """Missing ALB_DNS_NAME returns False"""
        env = {"ALB_DNS_NAME": "", "INTERNAL_API_SECRET": ""}
        with patch.dict(os.environ, env, clear=False):
            result = await PublishedExperimentSyncJob._trigger_proactive_download(
                "ws1", "uid1", "bucket1"
            )

            assert result is False

    @pytest.mark.asyncio
    async def test_trigger_uses_internal_base_url(self):
        """INTERNAL_API_BASE_URL, when set, overrides the scheme/port.

        Dev has no HTTPS:443 listener, so the base URL is injected as
        http://{dns}:8080 (see issue #719). Verify the request targets it.
        """
        env = {
            "ALB_DNS_NAME": "test-alb.example.com",
            "INTERNAL_API_SECRET": "secret-123",
            "INTERNAL_API_BASE_URL": "http://test-alb.example.com:8080",
        }
        with patch.dict(os.environ, env):
            with patch("requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_post.return_value = mock_resp

                result = await PublishedExperimentSyncJob._trigger_proactive_download(
                    "ws1", "uid1", "bucket1"
                )

                assert result is True
                call_url = mock_post.call_args[0][0]
                assert call_url == (
                    "http://test-alb.example.com:8080"
                    "/system-internal/sync-experiment/ws1/uid1"
                )

    @pytest.mark.asyncio
    async def test_trigger_falls_back_to_https(self):
        """Without INTERNAL_API_BASE_URL, fall back to https://{alb_dns}.

        Preserves the pre-#719 prod behavior so the code change is safe to
        roll out before the env var reaches every environment.
        """
        env = {
            "ALB_DNS_NAME": "test-alb.example.com",
            "INTERNAL_API_SECRET": "secret-123",
        }
        # clear=True guarantees INTERNAL_API_BASE_URL is absent even if the
        # host environment happens to define it.
        with patch.dict(os.environ, env, clear=True):
            with patch("requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_post.return_value = mock_resp

                result = await PublishedExperimentSyncJob._trigger_proactive_download(
                    "ws1", "uid1", "bucket1"
                )

                assert result is True
                call_url = mock_post.call_args[0][0]
                assert call_url == (
                    "https://test-alb.example.com"
                    "/system-internal/sync-experiment/ws1/uid1"
                )

    @pytest.mark.asyncio
    async def test_trigger_connection_error(self):
        """ConnectionError returns False"""
        import requests

        env = {
            "ALB_DNS_NAME": "test-alb.example.com",
            "INTERNAL_API_SECRET": "secret",
        }
        with patch.dict(os.environ, env):
            with patch("requests.post") as mock_post:
                mock_post.side_effect = requests.exceptions.ConnectionError(
                    "Network error"
                )

                result = await PublishedExperimentSyncJob._trigger_proactive_download(
                    "ws1", "uid1", "bucket1"
                )

                assert result is False

    @pytest.mark.asyncio
    async def test_trigger_api_error(self):
        """500 response returns False"""
        env = {
            "ALB_DNS_NAME": "test-alb.example.com",
            "INTERNAL_API_SECRET": "secret",
        }
        with patch.dict(os.environ, env):
            with patch("requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.status_code = 500
                mock_post.return_value = mock_resp

                result = await PublishedExperimentSyncJob._trigger_proactive_download(
                    "ws1", "uid1", "bucket1"
                )

                assert result is False

    @pytest.mark.asyncio
    async def test_trigger_timeout(self):
        """Timeout returns False"""
        import requests

        env = {
            "ALB_DNS_NAME": "test-alb.example.com",
            "INTERNAL_API_SECRET": "secret",
        }
        with patch.dict(os.environ, env):
            with patch("requests.post") as mock_post:
                mock_post.side_effect = requests.exceptions.Timeout("Request timed out")

                result = await PublishedExperimentSyncJob._trigger_proactive_download(
                    "ws1", "uid1", "bucket1"
                )

                assert result is False

    @pytest.mark.asyncio
    async def test_trigger_missing_only_alb(self):
        """ALB set but no secret returns False"""
        env = {
            "ALB_DNS_NAME": "test-alb.example.com",
            "INTERNAL_API_SECRET": "",
        }
        with patch.dict(os.environ, env, clear=False):
            result = await PublishedExperimentSyncJob._trigger_proactive_download(
                "ws1", "uid1", "bucket1"
            )

            assert result is False

    @pytest.mark.asyncio
    async def test_trigger_missing_only_secret(self):
        """Secret set but no ALB returns False"""
        env = {
            "ALB_DNS_NAME": "",
            "INTERNAL_API_SECRET": "secret",
        }
        with patch.dict(os.environ, env, clear=False):
            result = await PublishedExperimentSyncJob._trigger_proactive_download(
                "ws1", "uid1", "bucket1"
            )

            assert result is False


class TestValidateExperiment:
    """Tests for _validate_experiment with proactive trigger"""

    @pytest.mark.asyncio
    async def test_validate_success_triggers_download(self):
        """Successful validation triggers proactive download"""
        mock_s3 = MagicMock()
        mock_s3.validate_experiment_in_s3 = AsyncMock(
            return_value={
                "valid": True,
                "has_thumbnails": True,
            }
        )

        with patch.object(
            PublishedExperimentSyncJob,
            "_mark_sync_complete",
        ), patch.object(
            PublishedExperimentSyncJob,
            "_clear_retry_count",
        ), patch.object(
            PublishedExperimentSyncJob,
            "_trigger_proactive_download",
            new_callable=AsyncMock,
        ) as mock_trigger:
            result = await PublishedExperimentSyncJob._validate_experiment(
                mock_s3, "ws1", "uid1", 1, "bucket1"
            )

            assert result is True
            mock_trigger.assert_called_once_with(
                "ws1",
                "uid1",
                "bucket1",
                has_thumbnails=True,
            )

    @pytest.mark.asyncio
    async def test_validate_passes_has_thumbnails_false(self):
        """has_thumbnails=False is forwarded to trigger"""
        mock_s3 = MagicMock()
        mock_s3.validate_experiment_in_s3 = AsyncMock(
            return_value={
                "valid": True,
                "has_thumbnails": False,
            }
        )

        with patch.object(
            PublishedExperimentSyncJob,
            "_mark_sync_complete",
        ), patch.object(
            PublishedExperimentSyncJob,
            "_clear_retry_count",
        ), patch.object(
            PublishedExperimentSyncJob,
            "_trigger_proactive_download",
            new_callable=AsyncMock,
        ) as mock_trigger:
            result = await PublishedExperimentSyncJob._validate_experiment(
                mock_s3, "ws1", "uid1", 1, "bucket1"
            )

            assert result is True
            mock_trigger.assert_called_once_with(
                "ws1",
                "uid1",
                "bucket1",
                has_thumbnails=False,
            )

    @pytest.mark.asyncio
    async def test_validate_failure_no_trigger(self):
        """Failed validation does not trigger download"""
        mock_s3 = MagicMock()
        mock_s3.validate_experiment_in_s3 = AsyncMock(
            return_value={
                "valid": False,
                "error": "Missing files",
            }
        )

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch.object(
                PublishedExperimentSyncJob,
                "_mark_sync_error",
            ),
            patch.object(
                PublishedExperimentSyncJob,
                "_increment_retry_count",
            ),
            patch.object(
                PublishedExperimentSyncJob,
                "_check_persistent_failure",
            ),
            patch.object(
                PublishedExperimentSyncJob,
                "_trigger_proactive_download",
                new_callable=AsyncMock,
            ) as mock_trigger,
        ):
            result = await PublishedExperimentSyncJob._validate_experiment(
                mock_s3, "ws1", "uid1", 1, "bucket1"
            )

            assert result is False
            mock_trigger.assert_not_called()

    @pytest.mark.asyncio
    async def test_validate_retry_then_succeed(self):
        """Fail twice then succeed on third attempt"""
        mock_s3 = MagicMock()
        mock_s3.validate_experiment_in_s3 = AsyncMock(
            side_effect=[
                {"valid": False, "error": "Transient error"},
                {"valid": False, "error": "Transient error"},
                {"valid": True, "has_thumbnails": True},
            ]
        )

        with (
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch.object(
                PublishedExperimentSyncJob,
                "_mark_sync_complete",
            ) as mock_complete,
            patch.object(
                PublishedExperimentSyncJob,
                "_clear_retry_count",
            ),
            patch.object(
                PublishedExperimentSyncJob,
                "_trigger_proactive_download",
                new_callable=AsyncMock,
            ),
        ):
            result = await PublishedExperimentSyncJob._validate_experiment(
                mock_s3, "ws1", "uid1", 1, "bucket1"
            )

            assert result is True
            assert mock_s3.validate_experiment_in_s3.call_count == 3
            # 2 retries: 2^0=1, 2^1=2
            assert mock_sleep.call_count == 2
            mock_sleep.assert_any_call(1)
            mock_sleep.assert_any_call(2)
            mock_complete.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_validate_generic_exception_retries(self):
        """Generic exception (not SyncRetryError) also retries"""
        mock_s3 = MagicMock()
        mock_s3.validate_experiment_in_s3 = AsyncMock(
            side_effect=[
                RuntimeError("S3 timeout"),
                {"valid": True, "has_thumbnails": False},
            ]
        )

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch.object(
                PublishedExperimentSyncJob,
                "_mark_sync_complete",
            ),
            patch.object(
                PublishedExperimentSyncJob,
                "_clear_retry_count",
            ),
            patch.object(
                PublishedExperimentSyncJob,
                "_trigger_proactive_download",
                new_callable=AsyncMock,
            ),
        ):
            result = await PublishedExperimentSyncJob._validate_experiment(
                mock_s3, "ws1", "uid1", 1, "bucket1"
            )

            assert result is True
            assert mock_s3.validate_experiment_in_s3.call_count == 2

    @pytest.mark.asyncio
    async def test_validate_all_retries_exhausted(self):
        """All 3 retries fail -> marks error + persistent failure"""
        mock_s3 = MagicMock()
        mock_s3.validate_experiment_in_s3 = AsyncMock(
            return_value={
                "valid": False,
                "error": "Missing",
            }
        )

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch.object(
                PublishedExperimentSyncJob,
                "_mark_sync_error",
            ) as mock_error,
            patch.object(
                PublishedExperimentSyncJob,
                "_increment_retry_count",
            ) as mock_inc,
            patch.object(
                PublishedExperimentSyncJob,
                "_check_persistent_failure",
            ) as mock_persist,
        ):
            result = await PublishedExperimentSyncJob._validate_experiment(
                mock_s3, "ws1", "uid1", 1, "bucket1"
            )

            assert result is False
            assert mock_s3.validate_experiment_in_s3.call_count == 3
            mock_error.assert_called_once_with(1)
            mock_inc.assert_called_once_with(1)
            mock_persist.assert_called_once_with(1, "ws1", "uid1")

    @pytest.mark.asyncio
    async def test_validate_outer_exception(self):
        """Exception outside retry loop -> marks error"""
        original_range = range

        def selective_range(*args, **kwargs):
            if args == (3,):
                raise RuntimeError("outer")
            return original_range(*args, **kwargs)

        with (
            patch.object(
                PublishedExperimentSyncJob,
                "_mark_sync_error",
            ) as mock_error,
            patch.object(
                PublishedExperimentSyncJob,
                "_increment_retry_count",
            ) as mock_inc,
            patch(
                "studio.app.common.core.background.sync_job.logger",
            ),
            patch(
                "builtins.range",
                side_effect=selective_range,
            ),
        ):
            bad_s3 = MagicMock()
            bad_s3.validate_experiment_in_s3 = AsyncMock(
                side_effect=RuntimeError("broken")
            )
            result = await PublishedExperimentSyncJob._validate_experiment(
                bad_s3, "ws1", "uid1", 1, "bucket1"
            )

            assert result is False
            mock_error.assert_called_once_with(1)
            mock_inc.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_validate_default_empty_bucket_name(self):
        """Default bucket_name='' is passed to trigger"""
        mock_s3 = MagicMock()
        mock_s3.validate_experiment_in_s3 = AsyncMock(
            return_value={
                "valid": True,
                "has_thumbnails": True,
            }
        )

        with patch.object(
            PublishedExperimentSyncJob,
            "_mark_sync_complete",
        ), patch.object(
            PublishedExperimentSyncJob,
            "_clear_retry_count",
        ), patch.object(
            PublishedExperimentSyncJob,
            "_trigger_proactive_download",
            new_callable=AsyncMock,
        ) as mock_trigger:
            result = await PublishedExperimentSyncJob._validate_experiment(
                mock_s3, "ws1", "uid1", 1
            )

            assert result is True
            mock_trigger.assert_called_once_with(
                "ws1",
                "uid1",
                "",
                has_thumbnails=True,
            )


class TestRetryCount:
    """Tests for module-level retry count tracking"""

    def setup_method(self):
        """Reset retry counts before each test."""
        from studio.app.common.core.background import sync_job

        sync_job._retry_counts.clear()

    def test_increment_retry_count(self):
        """Incrementing tracks count per experiment"""
        cls = PublishedExperimentSyncJob
        cls._increment_retry_count(42)
        cls._increment_retry_count(42)
        cls._increment_retry_count(99)

        from studio.app.common.core.background import sync_job

        assert sync_job._retry_counts[42] == 2
        assert sync_job._retry_counts[99] == 1

    def test_clear_retry_count(self):
        """Clearing removes entry entirely"""
        cls = PublishedExperimentSyncJob
        cls._increment_retry_count(42)
        cls._clear_retry_count(42)

        from studio.app.common.core.background import sync_job

        assert 42 not in sync_job._retry_counts

    def test_persistent_failure_below_threshold(self):
        """Below MAX_PERSISTENT_RETRIES: no metric"""
        from studio.app.common.core.background import sync_job

        cls = PublishedExperimentSyncJob
        for _ in range(sync_job.MAX_PERSISTENT_RETRIES - 1):
            cls._increment_retry_count(42)

        with patch("boto3.client") as mock_boto:
            cls._check_persistent_failure(42, "ws1", "uid1")
            mock_boto.assert_not_called()

    def test_persistent_failure_at_threshold(self):
        """At MAX_PERSISTENT_RETRIES: publishes metric"""
        from studio.app.common.core.background import sync_job

        cls = PublishedExperimentSyncJob
        for _ in range(sync_job.MAX_PERSISTENT_RETRIES):
            cls._increment_retry_count(42)

        with patch("boto3.client") as mock_boto:
            mock_cw = MagicMock()
            mock_boto.return_value = mock_cw
            cls._check_persistent_failure(42, "ws1", "uid1")
            mock_cw.put_metric_data.assert_called_once()


class TestValidationLogicMetrics:
    """Tests that _run_validation_logic always publishes metrics"""

    @pytest.mark.asyncio
    async def test_publishes_zero_metrics_when_no_pending(self):
        """Metrics are published even when no experiments are pending"""
        with patch.object(
            PublishedExperimentSyncJob,
            "_get_pending_experiments",
            return_value=[],
        ), patch.object(
            PublishedExperimentSyncJob,
            "_publish_metrics",
        ) as mock_publish:
            await PublishedExperimentSyncJob._run_validation_logic()

            mock_publish.assert_called_once_with(0, 0)

    @pytest.mark.asyncio
    async def test_publishes_correct_counts_after_validation(self):
        """Metrics reflect actual sync/error counts from validation"""
        pending = [
            ("ws1", "uid1", 1, "bucket1"),
            ("ws2", "uid2", 2, "bucket1"),
            ("ws3", "uid3", 3, "bucket1"),
        ]

        mock_s3 = MagicMock()

        async def mock_validate(s3, ws, uid, eid, bucket):
            return eid != 2  # exp 2 fails, others succeed

        with patch.object(
            PublishedExperimentSyncJob,
            "_get_pending_experiments",
            return_value=pending,
        ), patch.object(
            PublishedExperimentSyncJob,
            "_get_s3_controller",
            return_value=mock_s3,
        ), patch.object(
            PublishedExperimentSyncJob,
            "_validate_experiment",
            side_effect=mock_validate,
        ), patch.object(
            PublishedExperimentSyncJob,
            "_publish_metrics",
        ) as mock_publish:
            await PublishedExperimentSyncJob._run_validation_logic()

            mock_publish.assert_called_once_with(2, 1)

    @pytest.mark.asyncio
    async def test_counts_gather_exceptions_as_errors(self):
        """Bare exceptions from asyncio.gather are counted as errors"""
        pending = [
            ("ws1", "uid1", 1, "bucket1"),
            ("ws2", "uid2", 2, "bucket1"),
        ]

        async def mock_validate(s3, ws, uid, eid, bucket):
            if eid == 1:
                return True
            raise RuntimeError("unexpected")

        with patch.object(
            PublishedExperimentSyncJob,
            "_get_pending_experiments",
            return_value=pending,
        ), patch.object(
            PublishedExperimentSyncJob,
            "_get_s3_controller",
            return_value=MagicMock(),
        ), patch.object(
            PublishedExperimentSyncJob,
            "_validate_experiment",
            side_effect=mock_validate,
        ), patch.object(
            PublishedExperimentSyncJob,
            "_mark_sync_error",
        ), patch.object(
            PublishedExperimentSyncJob,
            "_publish_metrics",
        ) as mock_publish, patch(
            "studio.app.common.core.background.sync_job.logger",
        ):
            await PublishedExperimentSyncJob._run_validation_logic()

            mock_publish.assert_called_once_with(1, 1)

    @pytest.mark.asyncio
    async def test_publishes_metrics_when_all_fail(self):
        """Metrics published correctly when every experiment fails"""
        pending = [
            ("ws1", "uid1", 1, "bucket1"),
            ("ws2", "uid2", 2, "bucket1"),
        ]

        async def mock_validate(s3, ws, uid, eid, bucket):
            return False

        with patch.object(
            PublishedExperimentSyncJob,
            "_get_pending_experiments",
            return_value=pending,
        ), patch.object(
            PublishedExperimentSyncJob,
            "_get_s3_controller",
            return_value=MagicMock(),
        ), patch.object(
            PublishedExperimentSyncJob,
            "_validate_experiment",
            side_effect=mock_validate,
        ), patch.object(
            PublishedExperimentSyncJob,
            "_publish_metrics",
        ) as mock_publish:
            await PublishedExperimentSyncJob._run_validation_logic()

            mock_publish.assert_called_once_with(0, 2)


class TestPublishMetrics:
    """Tests for _publish_metrics CloudWatch publishing"""

    def test_publishes_count_and_percent_metrics(self):
        """Publishes ExperimentsSynced, SyncErrors, and SyncErrorRate"""
        with patch("boto3.client") as mock_boto:
            mock_cw = MagicMock()
            mock_boto.return_value = mock_cw

            PublishedExperimentSyncJob._publish_metrics(5, 2)

            assert mock_cw.put_metric_data.call_count == 2

            # First call: Count metrics
            count_call = mock_cw.put_metric_data.call_args_list[0]
            count_data = count_call[1]["MetricData"]
            assert len(count_data) == 2
            assert count_data[0]["MetricName"] == "ExperimentsSynced"
            assert count_data[0]["Value"] == 5
            assert count_data[0]["Unit"] == "Count"
            assert count_data[1]["MetricName"] == "SyncErrors"
            assert count_data[1]["Value"] == 2
            assert count_data[1]["Unit"] == "Count"

            # Second call: Percent metric
            pct_call = mock_cw.put_metric_data.call_args_list[1]
            pct_data = pct_call[1]["MetricData"]
            assert len(pct_data) == 1
            assert pct_data[0]["MetricName"] == "SyncErrorRate"
            assert pct_data[0]["Unit"] == "Percent"
            assert abs(pct_data[0]["Value"] - 28.571) < 0.1

    def test_publishes_zero_counts(self):
        """Zero counts publish 0 values and 0.0 error rate"""
        with patch("boto3.client") as mock_boto:
            mock_cw = MagicMock()
            mock_boto.return_value = mock_cw

            PublishedExperimentSyncJob._publish_metrics(0, 0)

            assert mock_cw.put_metric_data.call_count == 2
            pct_call = mock_cw.put_metric_data.call_args_list[1]
            pct_data = pct_call[1]["MetricData"]
            assert pct_data[0]["Value"] == 0.0

    def test_error_rate_is_float(self):
        """SyncErrorRate value is always a float"""
        with patch("boto3.client") as mock_boto:
            mock_cw = MagicMock()
            mock_boto.return_value = mock_cw

            PublishedExperimentSyncJob._publish_metrics(10, 0)

            pct_call = mock_cw.put_metric_data.call_args_list[1]
            pct_data = pct_call[1]["MetricData"]
            assert isinstance(pct_data[0]["Value"], float)

    def test_hundred_percent_error_rate(self):
        """100% error rate when all experiments fail"""
        with patch("boto3.client") as mock_boto:
            mock_cw = MagicMock()
            mock_boto.return_value = mock_cw

            PublishedExperimentSyncJob._publish_metrics(0, 5)

            pct_call = mock_cw.put_metric_data.call_args_list[1]
            pct_data = pct_call[1]["MetricData"]
            assert pct_data[0]["Value"] == 100.0
            assert isinstance(pct_data[0]["Value"], float)

    def test_cloudwatch_error_is_logged_with_traceback(self):
        """CloudWatch errors are logged at error level with traceback"""
        with patch("boto3.client") as mock_boto:
            mock_cw = MagicMock()
            mock_cw.put_metric_data.side_effect = Exception("CloudWatch error")
            mock_boto.return_value = mock_cw

            with patch(
                "studio.app.common.core.background.sync_job.logger"
            ) as mock_logger:
                PublishedExperimentSyncJob._publish_metrics(1, 0)

                mock_logger.error.assert_called_once()
                call_kwargs = mock_logger.error.call_args[1]
                assert call_kwargs.get("exc_info") is True

    def test_does_not_raise_on_cloudwatch_error(self):
        """_publish_metrics swallows exceptions, never propagates"""
        with patch("boto3.client") as mock_boto, patch(
            "studio.app.common.core.background.sync_job.logger",
        ):
            mock_cw = MagicMock()
            mock_cw.put_metric_data.side_effect = Exception("CloudWatch down")
            mock_boto.return_value = mock_cw

            # Should not raise
            PublishedExperimentSyncJob._publish_metrics(1, 0)

    def test_count_metrics_published_when_percent_fails(self):
        """Count metrics succeed even if Percent call fails"""
        with patch("boto3.client") as mock_boto:
            mock_cw = MagicMock()
            # First call (Count) succeeds, second call (Percent) fails
            mock_cw.put_metric_data.side_effect = [
                None,
                Exception("Invalid unit"),
            ]
            mock_boto.return_value = mock_cw

            with patch("studio.app.common.core.background.sync_job.logger"):
                PublishedExperimentSyncJob._publish_metrics(5, 1)

            # First call completed successfully with Count metrics
            first_call = mock_cw.put_metric_data.call_args_list[0]
            count_data = first_call[1]["MetricData"]
            assert count_data[0]["MetricName"] == "ExperimentsSynced"
            assert count_data[0]["Value"] == 5
            assert count_data[1]["MetricName"] == "SyncErrors"
            assert count_data[1]["Value"] == 1

    def test_uses_consistent_timestamp(self):
        """All metrics in a call share the same timestamp"""
        with patch("boto3.client") as mock_boto:
            mock_cw = MagicMock()
            mock_boto.return_value = mock_cw

            PublishedExperimentSyncJob._publish_metrics(3, 1)

            count_call = mock_cw.put_metric_data.call_args_list[0]
            count_data = count_call[1]["MetricData"]
            pct_call = mock_cw.put_metric_data.call_args_list[1]
            pct_data = pct_call[1]["MetricData"]

            ts = count_data[0]["Timestamp"]
            assert count_data[1]["Timestamp"] == ts
            assert pct_data[0]["Timestamp"] == ts

    def test_uses_correct_namespace(self):
        """All calls use the env-scoped OptiNiSt/BackgroundJobs namespace"""
        with patch.dict("os.environ", {"ENV_PREFIX": "test"}), patch(
            "boto3.client"
        ) as mock_boto:
            mock_cw = MagicMock()
            mock_boto.return_value = mock_cw

            PublishedExperimentSyncJob._publish_metrics(1, 0)

            for call in mock_cw.put_metric_data.call_args_list:
                assert call[1]["Namespace"] == "OptiNiSt/BackgroundJobs/test"
