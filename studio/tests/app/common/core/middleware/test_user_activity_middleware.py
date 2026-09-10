"""
Unit Tests for UserActivityMiddleware

Tests the combined activity tracking middleware for both free and premium users.

WHAT IT TESTS:
1. Middleware correctly identifies free vs premium users
2. Activity updates are cached (60-second TTL)
3. Free users: updates free_user_assignments table
4. Premium users: updates premium_user_assignments table
5. Skips health checks, auth endpoints, system-internal paths
6. Standalone mode bypass
7. Error handling (graceful degradation)
8. Missing/invalid JWT handling

HOW TO RUN:
  cd studio/
  pytest tests/app/common/core/middleware/test_user_activity_middleware.py -v

REQUIREMENTS:
- pytest
- pytest-asyncio
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Test data
TEST_UID = "test_user_12345"
TEST_USER_ID = 42
TEST_JWT_TOKEN = "mock_firebase_jwt_token"
TIER_FREE = "free"
TIER_PREMIUM = "premium"


class TestMiddlewarePathSkipping:
    """Test that middleware skips appropriate paths"""

    @pytest.mark.asyncio
    async def test_skips_health_endpoint(self):
        """Middleware should skip /health endpoint"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            UserActivityMiddleware,
        )
        from studio.app.common.core.mode import MODE

        mock_app = AsyncMock()
        middleware = UserActivityMiddleware(app=mock_app)

        scope = {
            "type": "http",
            "path": "/health",
            "headers": [],
        }

        async def mock_receive():
            return {"type": "http.request"}

        async def mock_send(message):
            pass

        with patch.object(MODE, "IS_STANDALONE", False):
            await middleware(scope, mock_receive, mock_send)

        # App should be called directly without any user lookup
        mock_app.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_system_internal_paths(self):
        """Middleware should skip /system-internal/* paths"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            UserActivityMiddleware,
        )
        from studio.app.common.core.mode import MODE

        mock_app = AsyncMock()
        middleware = UserActivityMiddleware(app=mock_app)

        scope = {
            "type": "http",
            "path": "/system-internal/sync-experiments/1",
            "headers": [],
        }

        async def mock_receive():
            return {"type": "http.request"}

        async def mock_send(message):
            pass

        with patch.object(MODE, "IS_STANDALONE", False):
            await middleware(scope, mock_receive, mock_send)

        mock_app.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_standalone_mode(self):
        """Middleware should skip in standalone mode"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            UserActivityMiddleware,
        )
        from studio.app.common.core.mode import MODE

        mock_app = AsyncMock()
        middleware = UserActivityMiddleware(app=mock_app)

        scope = {
            "type": "http",
            "path": "/api/test",
            "headers": [(b"authorization", b"Bearer " + TEST_JWT_TOKEN.encode())],
        }

        async def mock_receive():
            return {"type": "http.request"}

        async def mock_send(message):
            pass

        with patch.object(MODE, "IS_STANDALONE", True):
            await middleware(scope, mock_receive, mock_send)

        mock_app.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_missing_auth_header(self):
        """Middleware should skip requests without Authorization header"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            UserActivityMiddleware,
        )
        from studio.app.common.core.mode import MODE

        mock_app = AsyncMock()
        middleware = UserActivityMiddleware(app=mock_app)

        scope = {
            "type": "http",
            "path": "/api/test",
            "headers": [],
        }

        async def mock_receive():
            return {"type": "http.request"}

        async def mock_send(message):
            pass

        with patch.object(MODE, "IS_STANDALONE", False):
            await middleware(scope, mock_receive, mock_send)

        mock_app.assert_called_once()


class TestCacheThrottling:
    """Test activity update caching/throttling"""

    def test_should_update_activity_first_time(self):
        """First activity update for a user should return True"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _free_activity_cache,
            _should_update_activity,
        )

        # Clear cache
        _free_activity_cache.clear()

        # First time should return True
        result = _should_update_activity(TEST_USER_ID, TIER_FREE)
        assert result is True

    def test_should_not_update_activity_within_ttl(self):
        """Activity update within TTL should return False"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _free_activity_cache,
            _should_update_activity,
            _update_cache_after_commit,
        )

        # Clear and set cache
        _free_activity_cache.clear()
        _update_cache_after_commit(TEST_USER_ID, TIER_FREE)

        # Immediately after should return False
        result = _should_update_activity(TEST_USER_ID, TIER_FREE)
        assert result is False

    def test_should_update_activity_after_ttl(self):
        """Activity update after TTL should return True"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _CACHE_TTL_SECONDS,
            _cache_lock,
            _free_activity_cache,
            _should_update_activity,
        )

        # Clear cache and set old timestamp
        _free_activity_cache.clear()
        with _cache_lock:
            _free_activity_cache[TEST_USER_ID] = time.time() - _CACHE_TTL_SECONDS - 1

        # After TTL should return True
        result = _should_update_activity(TEST_USER_ID, TIER_FREE)
        assert result is True

    def test_free_and_premium_caches_are_separate(self):
        """Free and premium users should have separate caches"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _free_activity_cache,
            _premium_activity_cache,
            _should_update_activity,
            _update_cache_after_commit,
        )

        # Clear both caches
        _free_activity_cache.clear()
        _premium_activity_cache.clear()

        # Update free cache
        _update_cache_after_commit(TEST_USER_ID, TIER_FREE)

        # Free should be cached, premium should not
        assert _should_update_activity(TEST_USER_ID, TIER_FREE) is False
        assert _should_update_activity(TEST_USER_ID, TIER_PREMIUM) is True


class TestUserTierDetection:
    """Test user tier detection"""

    def setup_method(self):
        """Clear tier cache before each test to ensure isolation"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            invalidate_user_tier_cache,
        )

        invalidate_user_tier_cache(TEST_UID)

    def test_get_user_id_and_tier_free_user(self):
        """Test detecting free tier user"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _get_user_id_and_tier,
        )
        from studio.app.common.core.subscription.constants import SubscriptionPlanIds

        mock_user = MagicMock()
        mock_user.id = TEST_USER_ID

        mock_plan = MagicMock()
        mock_plan.id = SubscriptionPlanIds.FREE

        mock_subscription = MagicMock()

        # Patch at the actual module location (imports are inside the function)
        with patch("studio.app.common.db.database.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.first.return_value = (
                mock_user
            )
            mock_get_db.return_value = iter([mock_db])

            with patch(
                "studio.app.common.core.subscription.subscription_service."
                "SubscriptionService.get_user_subscription"
            ) as mock_get_sub:
                mock_get_sub.return_value = (mock_subscription, mock_plan)

                user_id, tier = _get_user_id_and_tier(TEST_UID)

                assert user_id == TEST_USER_ID
                assert tier == TIER_FREE

    def test_get_user_id_and_tier_premium_user(self):
        """Test detecting premium tier user"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _get_user_id_and_tier,
        )
        from studio.app.common.core.subscription.constants import SubscriptionPlanIds

        mock_user = MagicMock()
        mock_user.id = TEST_USER_ID

        mock_plan = MagicMock()
        mock_plan.id = SubscriptionPlanIds.PREMIUM

        mock_subscription = MagicMock()

        with patch("studio.app.common.db.database.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.first.return_value = (
                mock_user
            )
            mock_get_db.return_value = iter([mock_db])

            with patch(
                "studio.app.common.core.subscription.subscription_service."
                "SubscriptionService.get_user_subscription"
            ) as mock_get_sub:
                mock_get_sub.return_value = (mock_subscription, mock_plan)

                user_id, tier = _get_user_id_and_tier(TEST_UID)

                assert user_id == TEST_USER_ID
                assert tier == TIER_PREMIUM

    def test_get_user_id_and_tier_user_not_found(self):
        """Test handling user not found"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _get_user_id_and_tier,
        )

        with patch("studio.app.common.db.database.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.first.return_value = None
            mock_get_db.return_value = iter([mock_db])

            user_id, tier = _get_user_id_and_tier(TEST_UID)

            assert user_id is None
            assert tier is None


class TestBackwardsCompatibility:
    """Test backwards compatibility alias"""

    def test_free_user_activity_middleware_alias(self):
        """FreeUserActivityMiddleware should be alias for UserActivityMiddleware"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            FreeUserActivityMiddleware,
            UserActivityMiddleware,
        )

        assert FreeUserActivityMiddleware is UserActivityMiddleware

    def test_import_from_init(self):
        """Both classes should be importable from __init__"""
        from studio.app.common.core.middleware import (
            FreeUserActivityMiddleware,
            UserActivityMiddleware,
        )

        assert FreeUserActivityMiddleware is UserActivityMiddleware


class TestActivityCacheInvalidation:
    """Test activity cache invalidation on logout"""

    def test_invalidate_activity_cache_clears_free_cache(self):
        """invalidate_activity_cache should clear free user cache entry"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _free_activity_cache,
            _update_cache_after_commit,
            invalidate_activity_cache,
        )

        _free_activity_cache.clear()
        _update_cache_after_commit(TEST_USER_ID, TIER_FREE)
        assert TEST_USER_ID in _free_activity_cache

        invalidate_activity_cache(TEST_USER_ID)

        assert TEST_USER_ID not in _free_activity_cache

    def test_invalidate_activity_cache_clears_premium_cache(self):
        """invalidate_activity_cache should clear premium user cache entry"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _premium_activity_cache,
            _update_cache_after_commit,
            invalidate_activity_cache,
        )

        _premium_activity_cache.clear()
        _update_cache_after_commit(TEST_USER_ID, TIER_PREMIUM)
        assert TEST_USER_ID in _premium_activity_cache

        invalidate_activity_cache(TEST_USER_ID)

        assert TEST_USER_ID not in _premium_activity_cache

    def test_invalidate_activity_cache_clears_both_caches(self):
        """invalidate_activity_cache should clear both caches"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _free_activity_cache,
            _premium_activity_cache,
            _update_cache_after_commit,
            invalidate_activity_cache,
        )

        _free_activity_cache.clear()
        _premium_activity_cache.clear()
        _update_cache_after_commit(TEST_USER_ID, TIER_FREE)
        _update_cache_after_commit(TEST_USER_ID, TIER_PREMIUM)
        assert TEST_USER_ID in _free_activity_cache
        assert TEST_USER_ID in _premium_activity_cache

        invalidate_activity_cache(TEST_USER_ID)

        assert TEST_USER_ID not in _free_activity_cache
        assert TEST_USER_ID not in _premium_activity_cache

    def test_invalidate_nonexistent_user_does_not_raise(self):
        """invalidate_activity_cache should not raise for nonexistent user"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _free_activity_cache,
            _premium_activity_cache,
            invalidate_activity_cache,
        )

        _free_activity_cache.clear()
        _premium_activity_cache.clear()

        invalidate_activity_cache(999999)

    def test_rapid_relogin_gets_fresh_activity(self):
        """After cache invalidation, re-login should record fresh activity"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _free_activity_cache,
            _should_update_activity,
            _update_cache_after_commit,
            invalidate_activity_cache,
        )

        _free_activity_cache.clear()
        _update_cache_after_commit(TEST_USER_ID, TIER_FREE)

        assert _should_update_activity(TEST_USER_ID, TIER_FREE) is False

        invalidate_activity_cache(TEST_USER_ID)

        assert _should_update_activity(TEST_USER_ID, TIER_FREE) is True


class TestLoggedOutUserTracking:
    """Test background activity updates for logged out users"""

    def setup_method(self):
        """Clear logged out users tracking before each test"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _logged_out_users,
        )

        _logged_out_users.clear()

    def test_mark_user_logged_out_adds_to_tracking(self):
        """mark_user_logged_out should add user to tracking set"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _logged_out_users,
            mark_user_logged_out,
        )

        mark_user_logged_out(TEST_USER_ID)
        assert TEST_USER_ID in _logged_out_users

    def test_is_user_logged_out_returns_true_for_recent_logout(self):
        """is_user_logged_out should return True for recently logged out user"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            is_user_logged_out,
            mark_user_logged_out,
        )

        mark_user_logged_out(TEST_USER_ID)
        assert is_user_logged_out(TEST_USER_ID) is True

    def test_is_user_logged_out_returns_false_for_non_logged_out_user(self):
        """is_user_logged_out should return False for user not logged out"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            is_user_logged_out,
        )

        assert is_user_logged_out(TEST_USER_ID) is False

    def test_is_user_logged_out_returns_false_after_ttl_expires(self):
        """is_user_logged_out should return False after TTL expires"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _LOGGED_OUT_TTL_SECONDS,
            _logged_out_lock,
            _logged_out_users,
            is_user_logged_out,
        )

        with _logged_out_lock:
            _logged_out_users[TEST_USER_ID] = time.time() - _LOGGED_OUT_TTL_SECONDS - 1

        assert is_user_logged_out(TEST_USER_ID) is False
        assert TEST_USER_ID not in _logged_out_users

    def test_clear_logged_out_status_removes_tracking(self):
        """clear_logged_out_status should remove user from tracking"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _logged_out_users,
            clear_logged_out_status,
            mark_user_logged_out,
        )

        mark_user_logged_out(TEST_USER_ID)
        assert TEST_USER_ID in _logged_out_users

        clear_logged_out_status(TEST_USER_ID)
        assert TEST_USER_ID not in _logged_out_users

    def test_clear_logged_out_status_does_not_raise_for_nonexistent_user(self):
        """clear_logged_out_status should not raise for non-existent user"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            clear_logged_out_status,
        )

        clear_logged_out_status(999999)

    @pytest.mark.asyncio
    async def test_free_activity_update_skipped_for_logged_out_user(self):
        """Background activity update should be skipped for logged out user"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _update_free_user_activity_async,
            mark_user_logged_out,
        )

        mark_user_logged_out(TEST_USER_ID)

        with patch(
            "studio.app.common.core.middleware.user_activity_middleware."
            "_update_free_user_activity_sync"
        ) as mock_sync:
            await _update_free_user_activity_async(TEST_USER_ID)

            mock_sync.assert_not_called()

    @pytest.mark.asyncio
    async def test_premium_activity_update_skipped_for_logged_out_user(self):
        """Background premium activity update should be skipped"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _update_premium_user_activity_async,
            mark_user_logged_out,
        )

        mark_user_logged_out(TEST_USER_ID)

        with patch(
            "studio.app.common.core.middleware.user_activity_middleware."
            "_update_premium_user_activity_sync"
        ) as mock_sync:
            await _update_premium_user_activity_async(TEST_USER_ID)

            mock_sync.assert_not_called()

    def test_free_activity_sync_skipped_for_logged_out_user(self):
        """Sync DB update should be skipped for logged out user"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _update_free_user_activity_sync,
            mark_user_logged_out,
        )

        mark_user_logged_out(TEST_USER_ID)

        result = _update_free_user_activity_sync(TEST_USER_ID)
        assert result is False

    def test_premium_activity_sync_skipped_for_logged_out_user(self):
        """Sync premium DB update should be skipped for logged out user"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _update_premium_user_activity_sync,
            mark_user_logged_out,
        )

        mark_user_logged_out(TEST_USER_ID)

        result = _update_premium_user_activity_sync(TEST_USER_ID)
        assert result is False


class TestClearFreeUserLoggedOutAt:
    """Test clearing logged_out_at on re-login"""

    def test_clear_logged_out_at_clears_timestamp(self):
        """clear_free_user_logged_out_at should execute UPDATE and commit"""
        from unittest.mock import MagicMock, patch

        from studio.app.common.core.middleware.user_activity_middleware import (
            clear_free_user_logged_out_at,
        )

        mock_session = MagicMock()
        mock_session.execute.return_value.rowcount = 1

        with patch(
            "studio.app.common.core.middleware."
            "user_activity_middleware.session_scope"
        ) as mock_session_scope:
            mock_session_scope.return_value.__enter__.return_value = mock_session

            result = clear_free_user_logged_out_at(TEST_USER_ID)

            assert result is True
            mock_session.execute.assert_called_once()
            mock_session.commit.assert_called_once()

    def test_clear_logged_out_at_updates_last_activity(self):
        """The UPDATE has to clear ``logged_out_at`` and bump ``last_activity``.

        This case was a copy of the one above and asserted nothing about either
        column: without the ``last_activity`` bump the stale-assignment sweep
        still sees the returning user as idle and reclaims their instance.
        """
        from unittest.mock import MagicMock, patch

        from sqlalchemy.dialects import mysql

        from studio.app.common.core.middleware.user_activity_middleware import (
            clear_free_user_logged_out_at,
        )

        mock_session = MagicMock()
        mock_session.execute.return_value.rowcount = 1

        with patch(
            "studio.app.common.core.middleware."
            "user_activity_middleware.session_scope"
        ) as mock_session_scope:
            mock_session_scope.return_value.__enter__.return_value = mock_session

            result = clear_free_user_logged_out_at(TEST_USER_ID)

        assert result is True
        compiled = mock_session.execute.call_args.args[0].compile(
            dialect=mysql.dialect()
        )
        sql = " ".join(str(compiled).split())
        assert sql.startswith("UPDATE free_user_assignments SET ")
        assert "logged_out_at=%s" in sql
        assert "last_activity=%s" in sql
        assert "free_user_assignments.logged_out_at IS NOT NULL" in sql
        assert compiled.params["logged_out_at"] is None
        assert compiled.params["last_activity"] is not None
        assert compiled.params["user_id_1"] == TEST_USER_ID

    def test_clear_logged_out_at_returns_true_if_no_assignment(self):
        """Should return True even if no rows matched (no assignment)"""
        from unittest.mock import MagicMock, patch

        from studio.app.common.core.middleware.user_activity_middleware import (
            clear_free_user_logged_out_at,
        )

        mock_session = MagicMock()
        mock_session.execute.return_value.rowcount = 0

        with patch(
            "studio.app.common.core.middleware."
            "user_activity_middleware.session_scope"
        ) as mock_session_scope:
            mock_session_scope.return_value.__enter__.return_value = mock_session

            result = clear_free_user_logged_out_at(TEST_USER_ID)

            assert result is True
            mock_session.commit.assert_called_once()

    def test_clear_logged_out_at_returns_true_if_already_null(self):
        """Should return True if already None
        (WHERE logged_out_at IS NOT NULL won't match)"""
        from unittest.mock import MagicMock, patch

        from studio.app.common.core.middleware.user_activity_middleware import (
            clear_free_user_logged_out_at,
        )

        mock_session = MagicMock()
        mock_session.execute.return_value.rowcount = 0

        with patch(
            "studio.app.common.core.middleware."
            "user_activity_middleware.session_scope"
        ) as mock_session_scope:
            mock_session_scope.return_value.__enter__.return_value = mock_session

            result = clear_free_user_logged_out_at(TEST_USER_ID)

            assert result is True
            mock_session.commit.assert_called_once()

    def test_clear_logged_out_at_returns_false_on_exception(self):
        """Should return False on DB error"""
        from unittest.mock import patch

        from studio.app.common.core.middleware.user_activity_middleware import (
            clear_free_user_logged_out_at,
        )

        with patch(
            "studio.app.common.core.middleware."
            "user_activity_middleware.session_scope"
        ) as mock_session_scope:
            mock_session_scope.return_value.__enter__.side_effect = Exception(
                "DB connection failed"
            )

            result = clear_free_user_logged_out_at(TEST_USER_ID)

            assert result is False

    def test_clear_logged_out_at_prevents_cleanup_after_relogin(self):
        """Clearing logged_out_at should execute UPDATE setting logged_out_at=None"""
        from unittest.mock import MagicMock, patch

        from studio.app.common.core.middleware.user_activity_middleware import (
            clear_free_user_logged_out_at,
        )

        mock_session = MagicMock()
        mock_session.execute.return_value.rowcount = 1

        with patch(
            "studio.app.common.core.middleware."
            "user_activity_middleware.session_scope"
        ) as mock_session_scope:
            mock_session_scope.return_value.__enter__.return_value = mock_session

            result = clear_free_user_logged_out_at(TEST_USER_ID)

            assert result is True
            mock_session.execute.assert_called_once()
            mock_session.commit.assert_called_once()


class TestHeartbeatFailureTracking:
    """Heartbeat failure tracking for grace period"""

    def setup_method(self):
        """Clear logged out state to avoid leaking from other tests"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _logged_out_users,
        )

        _logged_out_users.clear()

    def test_increment_heartbeat_failures(self):
        """increment_heartbeat_failures should increment counter"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            increment_heartbeat_failures,
        )

        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_row = (3,)

        mock_session = MagicMock()
        mock_session.execute.side_effect = [
            mock_result,
            MagicMock(fetchone=lambda: mock_row),
        ]

        with patch(
            "studio.app.common.core.middleware."
            "user_activity_middleware.session_scope"
        ) as mock_session_scope:
            mock_session_scope.return_value.__enter__.return_value = mock_session

            count = increment_heartbeat_failures(TEST_USER_ID)

            assert count == 3
            mock_session.commit.assert_called_once()

    def test_returns_zero_if_no_assignment(self):
        """Should return 0 if no active assignment"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            increment_heartbeat_failures,
        )

        mock_result = MagicMock()
        mock_result.rowcount = 0

        mock_session = MagicMock()
        mock_session.execute.return_value = mock_result

        with patch(
            "studio.app.common.core.middleware."
            "user_activity_middleware.session_scope"
        ) as mock_session_scope:
            mock_session_scope.return_value.__enter__.return_value = mock_session

            count = increment_heartbeat_failures(TEST_USER_ID)

            assert count == 0

    def test_returns_negative_on_error(self):
        """Should return -1 on DB error"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            increment_heartbeat_failures,
        )

        with patch(
            "studio.app.common.core.middleware."
            "user_activity_middleware.session_scope"
        ) as mock_session_scope:
            mock_session_scope.return_value.__enter__.side_effect = Exception(
                "DB connection failed"
            )

            count = increment_heartbeat_failures(TEST_USER_ID)

            assert count == -1

    def test_premium_sync_resets_heartbeat_failures(self):
        """Successful heartbeat should reset heartbeat_failures"""
        from studio.app.common.core.middleware.user_activity_middleware import (
            _logged_out_users,
            _update_premium_user_activity_sync,
        )

        # Clear logged-out state from earlier tests
        _logged_out_users.pop(TEST_USER_ID, None)

        mock_result = MagicMock()
        mock_result.rowcount = 1

        mock_session = MagicMock()
        mock_session.execute.return_value = mock_result

        mw_path = "studio.app.common.core.middleware." "user_activity_middleware"
        with patch(f"{mw_path}.session_scope") as mock_session_scope:
            mock_session_scope.return_value.__enter__.return_value = mock_session

            result = _update_premium_user_activity_sync(TEST_USER_ID)

            assert result is True
            call_args = mock_session.execute.call_args
            sql_text = str(call_args[0][0])
            assert "heartbeat_failures = 0" in sql_text


class TestPremiumActivityRestoresPendingRelease:
    """SQL widening — heartbeat restores `terminating` rows back to `active`.

    These assertions guard the multi-tab close fix. The unit harness
    mocks `session_scope`, so semantic SQL behaviour (which rows actually
    match) is not exercised — that's covered by integration tests. Here we
    assert the SQL shape itself, since the shape encodes the behaviour.
    """

    def setup_method(self):
        from studio.app.common.core.middleware.user_activity_middleware import (
            _logged_out_users,
        )

        _logged_out_users.clear()

    def _execute_sync_update(self, rowcount=1):
        """Run _update_premium_user_activity_sync under a mocked session.

        Returns (result, sql_text). The mocked rowcount drives the boolean
        return; sql_text is the raw textual SQL passed to session.execute.
        """
        from studio.app.common.core.middleware.user_activity_middleware import (
            _update_premium_user_activity_sync,
        )

        mock_result = MagicMock()
        mock_result.rowcount = rowcount

        mock_session = MagicMock()
        mock_session.execute.return_value = mock_result

        mw_path = "studio.app.common.core.middleware.user_activity_middleware"
        with patch(f"{mw_path}.session_scope") as mock_session_scope:
            mock_session_scope.return_value.__enter__.return_value = mock_session

            result = _update_premium_user_activity_sync(TEST_USER_ID)
            sql_text = str(mock_session.execute.call_args[0][0])
            return result, sql_text

    def test_update_matches_active_and_terminating(self):
        """Filter widened from `status='active'` to IN ('active','terminating')."""
        _, sql_text = self._execute_sync_update()
        assert "status IN ('active', 'terminating')" in sql_text

    def test_update_writes_active_to_restore_pending_release(self):
        """SET clause writes status='active' so terminating rows flip back."""
        _, sql_text = self._execute_sync_update()
        # The SET-clause assignment, not the WHERE-clause comparison.
        assert "SET last_activity = :now" in sql_text
        assert "status = 'active'" in sql_text

    def test_update_guards_against_known_dead_instance_states(self):
        """instance_state guard prevents restoring known-dead rows
        (preserves the existing dead-EC2 escape valve)."""
        _, sql_text = self._execute_sync_update()
        assert "instance_state NOT IN" in sql_text
        for dead_state in ("terminated", "shutting-down", "stopped", "stopping"):
            assert dead_state in sql_text

    def test_update_allows_active_rows_unconditionally(self):
        """OR-branch on `status = 'active'` skips the instance_state guard
        for already-active rows (frontend handles instance-unreachable)."""
        _, sql_text = self._execute_sync_update()
        # The full OR-branch — the guard is gated only on terminating rows.
        assert "OR instance_state IS NULL" in sql_text

    def test_returns_true_when_row_matched(self):
        """rowcount > 0 → returns True (row was updated or restored)."""
        result, _ = self._execute_sync_update(rowcount=1)
        assert result is True

    def test_returns_false_when_no_row_matched(self):
        """rowcount == 0 → returns False (no row, or escape valve blocked)."""
        result, _ = self._execute_sync_update(rowcount=0)
        assert result is False


class TestFreeUserActivityInstanceIdWrite:
    """instance_id is refreshed to the serving instance on UPDATE and set on
    INSERT, so it tracks where the user is currently active.

    (Reworking instance_id write ownership to a single authoritative writer is
    deferred to a separate follow-up issue.)
    """

    def _run_sync_update(self, rowcount):
        from unittest.mock import MagicMock, patch

        from studio.app.common.core.middleware.user_activity_middleware import (
            _update_free_user_activity_sync,
        )

        mock_session = MagicMock()
        exec_result = MagicMock()
        exec_result.rowcount = rowcount
        exec_result.scalar.return_value = 1  # open usage session already exists
        # rowcount==0 path does an existence SELECT; None → treat as a new user
        # and take the INSERT branch (a returned row would mean "logged out").
        exec_result.first.return_value = None
        mock_session.execute.return_value = exec_result

        with patch(
            "studio.app.common.core.middleware.user_activity_middleware."
            "is_user_logged_out",
            return_value=False,
        ), patch(
            "studio.app.common.core.middleware.user_activity_middleware."
            "_get_instance_id",
            return_value="i-current",
        ), patch(
            "studio.app.common.core.middleware."
            "user_activity_middleware.session_scope"
        ) as mock_scope:
            mock_scope.return_value.__enter__.return_value = mock_session
            _update_free_user_activity_sync(TEST_USER_ID)

        return mock_session

    def test_update_refreshes_instance_id(self):
        """Existing-row UPDATE sets both last_activity and instance_id."""
        mock_session = self._run_sync_update(rowcount=1)

        update_stmt = mock_session.execute.call_args_list[0][0][0]
        compiled = str(update_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "last_activity" in compiled
        assert "instance_id" in compiled

    def test_insert_sets_instance_id(self):
        """New-row INSERT path sets instance_id via session.add()."""
        mock_session = self._run_sync_update(rowcount=0)

        added = [c.args[0] for c in mock_session.add.call_args_list]
        assignments = [a for a in added if hasattr(a, "instance_id")]
        assert assignments, "expected a FreeUserAssignment to be added"
        assert any(a.instance_id == "i-current" for a in assignments)

    def test_logged_out_user_is_not_resurrected(self):
        """The logged_out_at IS NULL guard makes the UPDATE match nothing; with
        an existing (logged-out) row present, the write must NOT INSERT/resurrect
        — it returns without adding anything. Cross-process, no reliance on the
        short-lived in-memory logout map."""
        from unittest.mock import MagicMock, patch

        from studio.app.common.core.middleware.user_activity_middleware import (
            _update_free_user_activity_sync,
        )

        mock_session = MagicMock()
        update_result = MagicMock()
        update_result.rowcount = 0  # guarded UPDATE matched no row (logged out)
        select_result = MagicMock()
        select_result.first.return_value = (TEST_USER_ID,)  # assignment exists
        mock_session.execute.side_effect = [update_result, select_result]

        with patch(
            "studio.app.common.core.middleware.user_activity_middleware."
            "is_user_logged_out",
            return_value=False,  # in-memory map already expired (10s TTL)
        ), patch(
            "studio.app.common.core.middleware.user_activity_middleware."
            "_get_instance_id",
            return_value="i-current",
        ), patch(
            "studio.app.common.core.middleware."
            "user_activity_middleware.session_scope"
        ) as mock_scope:
            mock_scope.return_value.__enter__.return_value = mock_session
            result = _update_free_user_activity_sync(TEST_USER_ID)

        assert result is False
        mock_session.add.assert_not_called()  # no INSERT / no usage-log resurrect
