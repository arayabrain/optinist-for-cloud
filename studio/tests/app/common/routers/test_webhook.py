from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session

from studio.__main_unit__ import app
from studio.app.common.core.subscription.checkout_service import CheckoutService
from studio.app.common.core.subscription.constants import (
    SubscriptionPlanIds,
    SyncStatus,
)
from studio.app.common.core.subscription.subscription_service import SubscriptionService
from studio.app.common.core.subscription.webhook_service import WebhookService
from studio.app.common.core.utils.datetime_utils import (
    datetime_from_timestamp,
    get_current_datetime,
)
from studio.app.common.db.database import get_db


class TestInvoicePaymentSucceeded:
    """Test suite for invoice.payment_succeeded webhook handler"""

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session"""
        db = Mock(spec=Session)
        # Setup query chain to return mocked objects properly
        db.query = Mock()
        db.add = Mock()
        db.flush = Mock()
        db.commit = Mock()
        db.rollback = Mock()
        return db

    @pytest.fixture
    def mock_user_account(self):
        """Create a mock user account"""
        account = Mock()
        account.user_id = "user_123"
        account.provider_customer_id = "cus_test123"
        return account

    @pytest.fixture
    def mock_subscription(self):
        """Create a mock active subscription"""
        subscription = Mock()
        subscription.id = "sub_123"
        subscription.user_id = "user_123"
        subscription.plan_id = "plan_123"
        # IMPORTANT: Set to real datetime, not Mock
        subscription.expiration = get_current_datetime() + timedelta(days=5)
        subscription.updated_at = None
        return subscription

    @pytest.fixture
    def mock_plan(self):
        """Create a mock subscription plan"""
        plan = Mock()
        plan.id = "plan_123"
        plan.name = "Premium Plan"
        plan.billing_cycle = (
            "monthly"  # This should match BILLING_CYCLE.MONTHLY from your code
        )
        return plan

    @pytest.fixture
    def mock_user(self):
        """Create a mock user for cache invalidation"""
        user = Mock()
        user.id = "user_123"
        user.uid = "user_uid_123"
        return user

    @pytest.fixture
    def invoice_data_subscription_cycle(self):
        """Create mock invoice data for subscription renewal.

        The period end sits beyond the fixture subscription's seeded
        expiration (now + 5 days): a renewal extends, and the handler
        refuses a period end that does not advance the stored value.
        """
        period_start = int((get_current_datetime() + timedelta(days=29)).timestamp())
        period_end = int((get_current_datetime() + timedelta(days=30)).timestamp())
        return {
            "id": "in_test123",
            "customer": "cus_test123",
            "subscription": "sub_stripe123",  # Added subscription at top level
            "parent": {"subscription_details": {"subscription": "sub_stripe123"}},
            "status": "paid",
            "amount_paid": 2999,  # $29.99 in cents
            "billing_reason": "subscription_cycle",
            "period_start": period_start,
            "period_end": period_end,
            "lines": {
                "object": "list",
                "data": [
                    {
                        "id": "il_test123",
                        "period": {"end": period_end, "start": period_start},
                    }
                ],
            },
        }

    @pytest.fixture
    def invoice_data_initial_payment(self):
        """Create mock invoice data for initial subscription payment"""
        return {
            "id": "in_test456",
            "customer": "cus_test123",
            "subscription": "sub_stripe123",  # Added subscription at top level
            "parent": {"subscription_details": {"subscription": "sub_stripe123"}},
            "status": "paid",
            "amount_paid": 2999,
            "billing_reason": "subscription_create",  # This should be skipped
        }

    def test_successful_subscription_renewal_monthly(
        self,
        mock_db,
        mock_user_account,
        mock_subscription,
        mock_plan,
        mock_user,
        invoice_data_subscription_cycle,
    ):
        """Test successful monthly subscription renewal"""
        # Setup the query chain to return different results
        mock_db.query.side_effect = [
            # 1st query: UserAccount by customer_id
            Mock(
                filter=Mock(
                    return_value=Mock(first=Mock(return_value=mock_user_account))
                )
            ),
            # 2nd query: UserSubscription by user_id
            Mock(
                filter=Mock(
                    return_value=Mock(
                        order_by=Mock(
                            return_value=Mock(
                                first=Mock(return_value=mock_subscription)
                            )
                        )
                    )
                )
            ),
            # 3rd query: User for cache invalidation
            Mock(filter=Mock(return_value=Mock(first=Mock(return_value=mock_user)))),
        ]

        with patch.object(
            CheckoutService, "get_subscription_plan", return_value=mock_plan
        ), patch.object(
            SubscriptionService,
            "get_current_datetime",
            return_value=get_current_datetime(),
        ):
            # Execute
            result = WebhookService.handle_subscription_payment_succeeded(
                mock_db, invoice_data_subscription_cycle
            )

            # Assertions
            assert result["success"] is True
            assert result["user_id"] == "user_123"
            assert result["webhook_processed"] is True
            assert "new_expiration" in result
            assert "old_expiration" in result
            assert result["amount_paid"] == 29.99

            # Verify database interactions
            mock_db.add.assert_called_once()  # Purchase record added
            mock_db.flush.assert_called_once()
            mock_db.commit.assert_called_once()

            # Verify subscription was updated
            assert mock_subscription.expiration is not None
            assert mock_subscription.updated_at is not None

    @pytest.mark.parametrize("period_end_days_out", [30, 60])
    def test_renewal_writes_the_invoice_period_end_and_leaves_the_plan_alone(
        self,
        period_end_days_out,
        mock_db,
        mock_user_account,
        mock_subscription,
        mock_plan,
        mock_user,
        invoice_data_subscription_cycle,
    ):
        """The renewal's only job is to move ``expiration`` to the invoice's
        period end.

        The fixture pre-seeds ``expiration`` five days out, so
        ``expiration is not None`` holds even if the write is deleted. Two
        distinct period ends make the seeded value unable to satisfy either run.
        """
        period_end = int(
            (get_current_datetime() + timedelta(days=period_end_days_out)).timestamp()
        )
        invoice_data_subscription_cycle["lines"]["data"][0]["period"][
            "end"
        ] = period_end
        invoice_data_subscription_cycle["period_end"] = period_end
        seeded_expiration = mock_subscription.expiration

        mock_db.query.side_effect = [
            Mock(
                filter=Mock(
                    return_value=Mock(first=Mock(return_value=mock_user_account))
                )
            ),
            Mock(
                filter=Mock(
                    return_value=Mock(
                        order_by=Mock(
                            return_value=Mock(
                                first=Mock(return_value=mock_subscription)
                            )
                        )
                    )
                )
            ),
            Mock(filter=Mock(return_value=Mock(first=Mock(return_value=mock_user)))),
        ]

        with patch.object(
            CheckoutService, "get_subscription_plan", return_value=mock_plan
        ), patch.object(
            SubscriptionService,
            "get_current_datetime",
            return_value=get_current_datetime(),
        ):
            result = WebhookService.handle_subscription_payment_succeeded(
                mock_db, invoice_data_subscription_cycle
            )

        expected = datetime_from_timestamp(period_end)
        assert mock_subscription.expiration == expected
        assert mock_subscription.expiration != seeded_expiration
        assert result["new_expiration"] == expected.isoformat()
        assert result["old_expiration"] == seeded_expiration.isoformat()
        assert mock_subscription.plan_id == "plan_123"

    @pytest.mark.parametrize("stored_is_naive", [False, True])
    def test_a_stale_invoice_event_never_rewinds_the_expiration(
        self,
        stored_is_naive,
        mock_db,
        mock_user_account,
        mock_subscription,
        mock_plan,
        mock_user,
        invoice_data_subscription_cycle,
    ):
        """Stripe delivers invoice events out of period order (retries, late
        settlements): on 2026-08-24 a redelivered three-day-old invoice event
        arrived 33s after the fresh renewal and rewound the stored expiration
        behind ``now``, turning the account expired. An older period end must
        be skipped - expiration untouched and no duplicate purchase row.

        Parametrized over the stored value's awareness: MySQL hands back a
        naive datetime while the invoice's period end is UTC-aware, and
        comparing the two without normalising raises.
        """
        if stored_is_naive:
            mock_subscription.expiration = mock_subscription.expiration.replace(
                tzinfo=None
            )
        stale_end = int((get_current_datetime() - timedelta(days=3)).timestamp())
        invoice_data_subscription_cycle["lines"]["data"][0]["period"]["end"] = stale_end
        invoice_data_subscription_cycle["period_end"] = stale_end
        seeded_expiration = mock_subscription.expiration

        mock_db.query.side_effect = [
            Mock(
                filter=Mock(
                    return_value=Mock(first=Mock(return_value=mock_user_account))
                )
            ),
            Mock(
                filter=Mock(
                    return_value=Mock(
                        order_by=Mock(
                            return_value=Mock(
                                first=Mock(return_value=mock_subscription)
                            )
                        )
                    )
                )
            ),
        ]

        with patch.object(
            CheckoutService, "get_subscription_plan", return_value=mock_plan
        ), patch.object(
            SubscriptionService,
            "get_current_datetime",
            return_value=get_current_datetime(),
        ):
            result = WebhookService.handle_subscription_payment_succeeded(
                mock_db, invoice_data_subscription_cycle
            )

        assert result["skipped"] is True
        assert result["reason"] == "stale_period_end"
        assert mock_subscription.expiration == seeded_expiration
        mock_db.add.assert_not_called()
        mock_db.commit.assert_not_called()

    def test_skip_initial_payment(self, mock_db, invoice_data_initial_payment):
        """Test that initial subscription payments are skipped"""
        # Execute
        result = WebhookService.handle_subscription_payment_succeeded(
            mock_db, invoice_data_initial_payment
        )

        # Assertions
        assert result["success"] is True
        assert result["skipped"] is True
        assert "billing_reason" in result["message"]

        # Verify no database changes were made
        mock_db.add.assert_not_called()
        mock_db.commit.assert_not_called()

    def test_missing_customer_id(self, mock_db):
        """Test error handling for missing customer_id"""
        invoice_data = {
            "id": "in_test789",
            "subscription": "sub_stripe123",  # Added subscription at top level
            "parent": {"subscription_details": {"subscription": "sub_stripe123"}},
            "status": "paid",
            "amount_paid": 2999,
            "billing_reason": "subscription_cycle",
            # Missing customer field
        }

        with pytest.raises(Exception):  # Should raise HTTPException
            WebhookService.handle_subscription_payment_succeeded(mock_db, invoice_data)

        # Verify rollback was called
        mock_db.rollback.assert_called()

    def test_payment_not_completed(self, mock_db, mock_user_account):
        """Test error handling for incomplete payment"""
        invoice_data = {
            "id": "in_test789",
            "customer": "cus_test123",
            "subscription": "sub_stripe123",  # Added subscription at top level
            "parent": {"subscription_details": {"subscription": "sub_stripe123"}},
            "status": "open",  # Not paid
            "amount_paid": 2999,
            "billing_reason": "subscription_cycle",
        }

        mock_db.query.return_value.filter.return_value.first.return_value = (
            mock_user_account
        )

        with pytest.raises(Exception):  # Should raise HTTPException
            WebhookService.handle_subscription_payment_succeeded(mock_db, invoice_data)

        mock_db.rollback.assert_called()

    def test_user_not_found(self, mock_db, invoice_data_subscription_cycle):
        """Test handling when user is not found"""
        # Mock user not found
        mock_db.query.return_value.filter.return_value.first.return_value = None

        # Should return success response instead of raising exception
        result = WebhookService.handle_subscription_payment_succeeded(
            mock_db, invoice_data_subscription_cycle
        )

        # Verify it returns success with skipped flag
        assert result["success"] is True
        assert result["skipped"] is True
        assert result["reason"] == "missing_user_account"
        assert result["webhook_processed"] is True

    def test_subscription_not_found(
        self, mock_db, mock_user_account, invoice_data_subscription_cycle
    ):
        """Test error handling when active subscription is not found"""
        # Mock user found but no active subscription
        mock_db.query.side_effect = [
            Mock(
                filter=Mock(
                    return_value=Mock(first=Mock(return_value=mock_user_account))
                )
            ),
            Mock(
                filter=Mock(
                    return_value=Mock(
                        order_by=Mock(return_value=Mock(first=Mock(return_value=None)))
                    )
                )
            ),
        ]

        with pytest.raises(Exception):  # Should raise HTTPException
            WebhookService.handle_subscription_payment_succeeded(
                mock_db, invoice_data_subscription_cycle
            )

        mock_db.rollback.assert_called()

    def test_plan_not_found(
        self,
        mock_db,
        mock_user_account,
        mock_subscription,
        invoice_data_subscription_cycle,
    ):
        """Test error handling when subscription plan is not found"""
        mock_db.query.side_effect = [
            Mock(
                filter=Mock(
                    return_value=Mock(first=Mock(return_value=mock_user_account))
                )
            ),
            Mock(
                filter=Mock(
                    return_value=Mock(
                        order_by=Mock(
                            return_value=Mock(
                                first=Mock(return_value=mock_subscription)
                            )
                        )
                    )
                )
            ),
        ]

        with patch.object(CheckoutService, "get_subscription_plan", return_value=None):
            with pytest.raises(Exception):  # Should raise HTTPException
                WebhookService.handle_subscription_payment_succeeded(
                    mock_db, invoice_data_subscription_cycle
                )

            mock_db.rollback.assert_called()

    def test_database_commit_failure(
        self,
        mock_db,
        mock_user_account,
        mock_subscription,
        mock_plan,
        invoice_data_subscription_cycle,
    ):
        """Test error handling when database commit fails"""
        mock_db.query.side_effect = [
            Mock(
                filter=Mock(
                    return_value=Mock(first=Mock(return_value=mock_user_account))
                )
            ),
            Mock(
                filter=Mock(
                    return_value=Mock(
                        order_by=Mock(
                            return_value=Mock(
                                first=Mock(return_value=mock_subscription)
                            )
                        )
                    )
                )
            ),
        ]
        mock_db.commit.side_effect = Exception("Database error")

        with patch.object(
            CheckoutService, "get_subscription_plan", return_value=mock_plan
        ), patch.object(
            SubscriptionService,
            "get_current_datetime",
            return_value=get_current_datetime(),
        ):
            with pytest.raises(Exception):
                WebhookService.handle_subscription_payment_succeeded(
                    mock_db, invoice_data_subscription_cycle
                )

            mock_db.rollback.assert_called()


class TestSubscriptionLookbackWindow:
    """Extended lookback window for trial-to-paid conversion."""

    def test_subscription_lookback_constant_is_30_days(self):
        """RECENT_SUBSCRIPTION_WINDOW_DAYS should be 30 days for extended lookback"""
        from studio.app.common.core.subscription.constants import (
            RECENT_SUBSCRIPTION_WINDOW_DAYS,
        )

        assert RECENT_SUBSCRIPTION_WINDOW_DAYS == 30


class TestPaymentFailureTracking:
    """Payment failure tracking."""

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session"""
        db = Mock(spec=Session)
        db.query = Mock()
        db.commit = Mock()
        return db

    @pytest.fixture
    def mock_user_account(self):
        """Create a mock user account"""
        account = Mock()
        account.user_id = 123
        account.provider_customer_id = "cus_test123"
        return account

    @pytest.fixture
    def mock_subscription(self):
        """Create a mock subscription"""
        subscription = Mock()
        subscription.id = 1
        subscription.user_id = 123
        subscription.sync_status = None
        subscription.expiration = get_current_datetime() + timedelta(days=30)
        return subscription

    @pytest.fixture
    def mock_user(self):
        """Create a mock user"""
        user = Mock()
        user.id = 123
        user.uid = "user_uid_123"
        return user

    def test_payment_failed_sets_sync_status_failed(
        self, mock_db, mock_user_account, mock_subscription, mock_user
    ):
        """Payment failure should set sync_status to FAILED"""
        invoice_data = {
            "id": "in_test123",
            "customer": "cus_test123",
        }

        mock_db.query.side_effect = [
            Mock(
                filter=Mock(
                    return_value=Mock(first=Mock(return_value=mock_user_account))
                )
            ),
            Mock(
                filter=Mock(
                    return_value=Mock(first=Mock(return_value=mock_subscription))
                )
            ),
            Mock(filter=Mock(return_value=Mock(first=Mock(return_value=mock_user)))),
        ]

        with patch(
            "studio.app.common.core.subscription.webhook_service."
            "invalidate_user_tier_cache"
        ):
            WebhookService.handle_payment_failed(mock_db, invoice_data)

        assert mock_subscription.sync_status == SyncStatus.FAILED


class TestWebhookCacheInvalidation:
    """User tier cache invalidation in webhook handlers."""

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session"""
        db = Mock(spec=Session)
        db.query = Mock()
        db.add = Mock()
        db.flush = Mock()
        db.commit = Mock()
        db.rollback = Mock()
        return db

    @pytest.fixture
    def mock_user(self):
        """Create a mock user"""
        user = Mock()
        user.id = 123
        user.uid = "user_uid_123"
        user.email = "test@example.com"
        return user

    @pytest.fixture
    def mock_user_account(self):
        """Create a mock user account"""
        account = Mock()
        account.user_id = 123
        account.provider_customer_id = "cus_test123"
        return account

    @pytest.fixture
    def mock_subscription(self):
        """Create a mock active subscription"""
        subscription = Mock()
        subscription.id = 1
        subscription.user_id = 123
        subscription.plan_id = 1
        subscription.expiration = get_current_datetime() + timedelta(days=5)
        subscription.sync_status = None
        subscription.updated_at = None
        return subscription

    def test_payment_failed_invalidates_cache(
        self, mock_db, mock_user_account, mock_subscription, mock_user
    ):
        """Test that handle_payment_failed invalidates user tier cache"""
        invoice_data = {
            "id": "in_test123",
            "customer": "cus_test123",
        }

        # Setup query chain
        mock_db.query.side_effect = [
            # First query: find user account
            Mock(
                filter=Mock(
                    return_value=Mock(first=Mock(return_value=mock_user_account))
                )
            ),
            # Second query: find subscription
            Mock(
                filter=Mock(
                    return_value=Mock(first=Mock(return_value=mock_subscription))
                )
            ),
            # Third query: find user for cache invalidation
            Mock(filter=Mock(return_value=Mock(first=Mock(return_value=mock_user)))),
        ]

        with patch(
            "studio.app.common.core.subscription.webhook_service."
            "invalidate_user_tier_cache"
        ) as mock_invalidate:
            WebhookService.handle_payment_failed(mock_db, invoice_data)

            # Verify cache invalidation was called with user UID
            mock_invalidate.assert_called_once_with(mock_user.uid)

    def test_payment_failed_no_cache_invalidation_when_no_subscription(
        self, mock_db, mock_user_account
    ):
        """Test that cache is not invalidated when no subscription found"""
        invoice_data = {
            "id": "in_test123",
            "customer": "cus_test123",
        }

        # Setup query chain - no subscription found
        mock_db.query.side_effect = [
            Mock(
                filter=Mock(
                    return_value=Mock(first=Mock(return_value=mock_user_account))
                )
            ),
            Mock(filter=Mock(return_value=Mock(first=Mock(return_value=None)))),
        ]

        with patch(
            "studio.app.common.core.subscription.webhook_service."
            "invalidate_user_tier_cache"
        ) as mock_invalidate:
            WebhookService.handle_payment_failed(mock_db, invoice_data)

            # Verify cache invalidation was NOT called
            mock_invalidate.assert_not_called()

    def test_subscription_renewal_invalidates_cache(
        self, mock_db, mock_user_account, mock_subscription, mock_user
    ):
        """Test that handle_subscription_payment_succeeded invalidates cache"""
        mock_plan = Mock()
        mock_plan.id = 1

        # Beyond the fixture's seeded expiration, or the stale-period guard
        # skips the renewal before the cache is ever touched
        future_end = int((get_current_datetime() + timedelta(days=30)).timestamp())
        invoice_data = {
            "id": "in_test123",
            "customer": "cus_test123",
            "subscription": "sub_stripe123",
            "status": "paid",
            "amount_paid": 2999,
            "billing_reason": "subscription_cycle",
            "lines": {"data": [{"period": {"end": future_end}}]},
        }

        # Setup query chain
        mock_db.query.side_effect = [
            # Find user account
            Mock(
                filter=Mock(
                    return_value=Mock(first=Mock(return_value=mock_user_account))
                )
            ),
            # Find subscription
            Mock(
                filter=Mock(
                    return_value=Mock(
                        order_by=Mock(
                            return_value=Mock(
                                first=Mock(return_value=mock_subscription)
                            )
                        )
                    )
                )
            ),
            # Find user for cache invalidation
            Mock(filter=Mock(return_value=Mock(first=Mock(return_value=mock_user)))),
        ]

        with patch.object(
            CheckoutService, "get_subscription_plan", return_value=mock_plan
        ), patch.object(
            SubscriptionService,
            "get_current_datetime",
            return_value=get_current_datetime(),
        ), patch(
            "studio.app.common.core.subscription.webhook_service."
            "invalidate_user_tier_cache"
        ) as mock_invalidate:
            result = WebhookService.handle_subscription_payment_succeeded(
                mock_db, invoice_data
            )

            assert result["success"] is True
            # Verify cache invalidation was called
            mock_invalidate.assert_called_once_with(mock_user.uid)


class TestStorageQuotaBytesForPlan:
    """Unit tests for StorageQuota.bytes_for_plan mapping"""

    def test_premium_plan_returns_premium_quota(self):
        from studio.app.common.core.subscription.constants import (
            StorageQuota,
            StorageSize,
            SubscriptionPlanIds,
        )

        result = StorageQuota.bytes_for_plan(SubscriptionPlanIds.PREMIUM)
        assert result == StorageQuota.PREMIUM * StorageSize.GB

    def test_free_plan_returns_free_quota(self):
        from studio.app.common.core.subscription.constants import (
            StorageQuota,
            StorageSize,
            SubscriptionPlanIds,
        )

        result = StorageQuota.bytes_for_plan(SubscriptionPlanIds.FREE)
        assert result == StorageQuota.FREE * StorageSize.GB

    def test_unknown_plan_falls_back_to_free_quota(self):
        from studio.app.common.core.subscription.constants import (
            StorageQuota,
            StorageSize,
        )

        result = StorageQuota.bytes_for_plan(999)
        assert result == StorageQuota.FREE * StorageSize.GB


class TestCheckoutStorageQuotaUpdate:
    """Test that handle_checkout_completed updates storage quota correctly"""

    @pytest.fixture
    def mock_db(self):
        db = Mock(spec=Session)
        db.query = Mock()
        db.add = Mock()
        db.execute = Mock()
        db.commit = Mock()
        db.rollback = Mock()
        return db

    @pytest.fixture
    def session_data(self):
        """Minimal session data to reach step 10"""
        return {
            "id": "cs_test_session",
            "customer": "cus_test123",
            "payment_status": "paid",
            "metadata": {"user_id": "42", "plan_id": "2"},
            "subscription": "sub_stripe_123",
        }

    @pytest.fixture
    def mock_user(self):
        user = Mock()
        user.id = 42
        user.uid = "uid_42"
        return user

    def _setup_checkout_mocks(self, mock_db, mock_user):
        """Patch CheckoutService, stripe, and cache so we reach step 10"""
        # Step 1: duplicate check — no existing purchase
        mock_query_purchase = Mock()
        mock_query_purchase.join.return_value.filter.return_value.first.return_value = (
            None
        )
        # Step 12: user lookup for cache invalidation
        mock_query_user = Mock()
        mock_query_user.filter.return_value.first.return_value = mock_user

        mock_db.query.side_effect = [mock_query_purchase, mock_query_user]

        mock_purchase = Mock()
        mock_purchase.id = 1

        mock_stripe_sub = {
            "current_period_end": 1735689600,
            "trial_end": None,
            "current_period_start": 1733097600,
        }

        patches = {
            "plan": patch.object(
                CheckoutService,
                "get_subscription_plan",
                return_value=Mock(id=2),
            ),
            "provider": patch.object(
                CheckoutService,
                "get_or_create_stripe_provider",
                return_value=1,
            ),
            "account": patch.object(CheckoutService, "create_or_update_user_account"),
            "payment": patch.object(CheckoutService, "set_default_payment_method"),
            "subscription": patch.object(
                CheckoutService,
                "create_or_update_subscription",
                return_value=1,
            ),
            "purchase": patch.object(
                CheckoutService,
                "record_purchase",
                return_value=mock_purchase,
            ),
            "stripe": patch(
                "studio.app.common.core.subscription.webhook_service."
                "stripe.Subscription.retrieve",
                return_value=mock_stripe_sub,
            ),
            "cache": patch(
                "studio.app.common.core.subscription.webhook_service."
                "invalidate_user_tier_cache",
            ),
            "datetime": patch.object(
                SubscriptionService,
                "get_current_datetime",
                return_value=get_current_datetime(),
            ),
        }
        return patches

    def _run_checkout(self, mock_db, session_data, mock_user):
        patches = self._setup_checkout_mocks(mock_db, mock_user)
        with (
            patches["plan"],
            patches["provider"],
            patches["account"],
            patches["payment"],
            patches["subscription"],
            patches["purchase"],
            patches["stripe"],
            patches["cache"],
            patches["datetime"],
        ):
            return WebhookService.handle_checkout_completed(mock_db, session_data)

    def test_storage_quota_written_as_single_upsert(
        self, mock_db, session_data, mock_user
    ):
        """Quota write is one statement, whether or not the row already exists."""
        from studio.app.common.core.subscription.constants import (
            StorageQuota,
            SubscriptionPlanIds,
        )

        result = self._run_checkout(mock_db, session_data, mock_user)

        assert result["success"] is True
        mock_db.execute.assert_called_once()
        # No separate INSERT path to get out of sync with the UPDATE path.
        mock_db.add.assert_not_called()
        mock_db.commit.assert_called_once()

        stmt = mock_db.execute.call_args[0][0]
        params = stmt.compile().params
        assert params["user_id"] == 42
        assert params["storage_usage_bytes"] == 0
        assert params["storage_quota_bytes"] == StorageQuota.bytes_for_plan(
            SubscriptionPlanIds.PREMIUM
        )

    def test_quota_write_is_idempotent_for_an_existing_row(
        self, mock_db, session_data, mock_user
    ):
        """
        Re-upgrading a user whose quota already equals the target must not
        attempt a fresh INSERT. MySQL reports 0 affected rows both for "no
        such row" and for "row matched but value unchanged", so a rowcount
        check cannot tell them apart and raises a duplicate-key error here.
        """
        from sqlalchemy.dialects import mysql

        self._run_checkout(mock_db, session_data, mock_user)

        sql = str(
            mock_db.execute.call_args[0][0].compile(dialect=mysql.dialect())
        ).upper()
        assert "INSERT INTO USER_STORAGE_USAGE" in sql
        assert "ON DUPLICATE KEY UPDATE" in sql
        assert "ROWCOUNT" not in sql


class TestCustomerSubscriptionDeleted:
    """Tests for the release-on-delete webhook path (issue #629, P3).

    Verifies that customer.subscription.deleted now releases the dangling
    premium compute assignment via release_premium_user (in addition to the
    existing DB-row expiration in handle_subscription_cancelled).
    """

    @pytest.fixture
    def mock_db(self):
        db = Mock(spec=Session)
        db.query = Mock()
        db.add = Mock()
        db.flush = Mock()
        db.commit = Mock()
        db.rollback = Mock()
        return db

    @pytest.fixture
    def mock_user_account(self):
        account = Mock()
        account.user_id = 42
        return account

    @pytest.fixture
    def mock_user(self):
        user = Mock()
        user.id = 42
        user.uid = "user_uid_42"
        return user

    @pytest.fixture
    def subscription_event(self):
        """A customer.subscription.deleted event payload."""
        return {
            "id": "sub_stripe123",
            "customer": "cus_test123",
            "status": "canceled",
        }

    @pytest.mark.asyncio
    async def test_release_premium_assignment_on_delete(
        self, mock_db, mock_user_account, mock_user, subscription_event
    ):
        """The delete path hard-releases with the short webhook timeout."""
        from studio.app.common.core.premium.premium_assignment_service import (
            WEBHOOK_RELEASE_TIMEOUT_SECONDS,
            premium_assignment_service,
        )

        # Single JOIN query: SubscriptionUserAccount + User by customer_id
        mock_db.query.return_value = Mock(
            join=Mock(
                return_value=Mock(
                    filter=Mock(
                        return_value=Mock(
                            first=Mock(return_value=(mock_user_account, mock_user))
                        )
                    )
                )
            )
        )
        with patch.object(
            premium_assignment_service,
            "release_premium_user",
            new=AsyncMock(return_value={"success": True, "message": "released"}),
        ) as mock_release:
            await WebhookService._release_premium_assignment(
                mock_db, subscription_event
            )

        mock_release.assert_awaited_once_with(
            user_id=42,
            user_uid="user_uid_42",
            hard=True,
            timeout=WEBHOOK_RELEASE_TIMEOUT_SECONDS,
        )

    @pytest.mark.asyncio
    async def test_release_premium_assignment_continues_on_timeout(
        self, mock_db, mock_user_account, mock_user, subscription_event
    ):
        """A fail-open (timed-out) release must not raise; webhook still acks."""
        from studio.app.common.core.premium.premium_assignment_service import (
            premium_assignment_service,
        )

        # Single JOIN query: SubscriptionUserAccount + User by customer_id
        mock_db.query.return_value = Mock(
            join=Mock(
                return_value=Mock(
                    filter=Mock(
                        return_value=Mock(
                            first=Mock(return_value=(mock_user_account, mock_user))
                        )
                    )
                )
            )
        )
        timed_out = {
            "success": False,
            "timed_out": True,
            "message": "Release timed out",
        }
        with patch.object(
            premium_assignment_service,
            "release_premium_user",
            new=AsyncMock(return_value=timed_out),
        ) as mock_release:
            # Must not raise even though release reports failure.
            await WebhookService._release_premium_assignment(
                mock_db, subscription_event
            )
        mock_release.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_release_premium_assignment_no_account_is_noop(
        self, mock_db, subscription_event
    ):
        """No local account -> nothing to release, no error."""
        from studio.app.common.core.premium.premium_assignment_service import (
            premium_assignment_service,
        )

        # JOIN query returns None -> no account/user found
        mock_db.query.return_value = Mock(
            join=Mock(
                return_value=Mock(
                    filter=Mock(return_value=Mock(first=Mock(return_value=None)))
                )
            )
        )
        with patch.object(
            premium_assignment_service, "release_premium_user", new=AsyncMock()
        ) as mock_release:
            await WebhookService._release_premium_assignment(
                mock_db, subscription_event
            )

        mock_release.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_release_failure_does_not_raise(
        self, mock_db, mock_user_account, mock_user, subscription_event
    ):
        """A release exception is swallowed so the webhook still acks."""
        from studio.app.common.core.premium.premium_assignment_service import (
            premium_assignment_service,
        )

        # Single JOIN query: SubscriptionUserAccount + User by customer_id
        mock_db.query.return_value = Mock(
            join=Mock(
                return_value=Mock(
                    filter=Mock(
                        return_value=Mock(
                            first=Mock(return_value=(mock_user_account, mock_user))
                        )
                    )
                )
            )
        )
        with patch.object(
            premium_assignment_service,
            "release_premium_user",
            new=AsyncMock(side_effect=Exception("Lambda boom")),
        ):
            # Must not raise.
            await WebhookService._release_premium_assignment(
                mock_db, subscription_event
            )

    @pytest.mark.asyncio
    async def test_dispatch_delete_mirrors_and_releases(
        self, mock_db, subscription_event
    ):
        """dispatch routes delete to the cancel handler AND the release helper."""
        with patch.object(
            WebhookService, "handle_subscription_cancelled"
        ) as mock_cancel, patch.object(
            WebhookService, "_release_premium_assignment", new=AsyncMock()
        ) as mock_release:
            result = await WebhookService.dispatch_webhook_event(
                mock_db, "customer.subscription.deleted", subscription_event
            )

        assert result["success"] is True
        mock_cancel.assert_called_once_with(mock_db, subscription_event)
        mock_release.assert_awaited_once_with(mock_db, subscription_event)


class TestSubscriptionLifecycleWebhooks:
    """Tests for customer.subscription.created handling (issue #629, P1).

    Verifies that the new `created` webhook activates a subscription
    end-to-end so users are no longer stuck on "Activation Pending" when
    `checkout.session.completed` can't complete the activation on its own.
    """

    @pytest.fixture
    def mock_db(self):
        db = Mock(spec=Session)
        db.query = Mock()
        db.add = Mock()
        db.flush = Mock()
        db.commit = Mock()
        db.rollback = Mock()
        db.execute = Mock()
        return db

    @pytest.fixture
    def mock_user_account(self):
        account = Mock()
        account.user_id = 42
        account.provider_customer_id = "cus_test123"
        return account

    @pytest.fixture
    def mock_user(self):
        user = Mock()
        user.id = 42
        user.uid = "user_uid_42"
        return user

    @pytest.fixture
    def subscription_event(self):
        """A customer.subscription.created event payload (event['data']['object'])."""
        return {
            "id": "sub_stripe123",
            "customer": "cus_test123",
            "status": "active",
            "current_period_end": 2000000000,  # far-future unix timestamp
            "metadata": {"plan_id": str(SubscriptionPlanIds.PREMIUM)},
        }

    def _setup_query_chain(self, mock_db, account, user):
        """Sequential query results: account, storage usage, then user (cache)."""
        mock_storage = Mock()
        mock_storage.storage_quota_bytes = 214748364800
        mock_db.query.side_effect = [
            # 1. SubscriptionUserAccount by customer_id
            Mock(filter=Mock(return_value=Mock(first=Mock(return_value=account)))),
            # 2. UserStorageUsage by user_id (storage quota sync)
            Mock(filter=Mock(return_value=Mock(first=Mock(return_value=mock_storage)))),
            # 3. User for cache invalidation
            Mock(filter=Mock(return_value=Mock(first=Mock(return_value=user)))),
        ]

    def test_subscription_created_activates_subscription(
        self, mock_db, mock_user_account, mock_user, subscription_event
    ):
        """`created` upserts subscription_users and invalidates the tier cache."""
        self._setup_query_chain(mock_db, mock_user_account, mock_user)

        with patch.object(
            CheckoutService, "create_or_update_subscription", return_value=99
        ) as mock_upsert, patch(
            "studio.app.common.core.subscription.webhook_service."
            "invalidate_user_tier_cache"
        ) as mock_invalidate:
            result = WebhookService.handle_subscription_created(
                mock_db, subscription_event
            )

        assert result["success"] is True
        assert result["user_id"] == 42
        assert result["plan_id"] == SubscriptionPlanIds.PREMIUM
        mock_upsert.assert_called_once()
        upsert_args = mock_upsert.call_args[0]
        assert upsert_args[1] == 42  # user_id
        assert upsert_args[2] == SubscriptionPlanIds.PREMIUM
        mock_db.commit.assert_called_once()
        mock_invalidate.assert_called_once_with("user_uid_42")

    @pytest.mark.asyncio
    async def test_dispatch_created_routes_to_handler(
        self, mock_db, subscription_event
    ):
        """dispatch routes `created` to handle_subscription_created."""
        with patch.object(
            WebhookService,
            "handle_subscription_created",
            return_value={"success": True},
        ) as mock_created:
            result = await WebhookService.dispatch_webhook_event(
                mock_db, "customer.subscription.created", subscription_event
            )

        assert result["success"] is True
        mock_created.assert_called_once_with(mock_db, subscription_event)

    def test_subscription_event_acknowledged_when_no_customer_id(
        self, mock_db, subscription_event
    ):
        """Missing customer ID is acknowledged without a DB write."""
        subscription_event.pop("customer")
        with patch.object(
            CheckoutService, "create_or_update_subscription"
        ) as mock_upsert:
            result = WebhookService.handle_subscription_created(
                mock_db, subscription_event
            )

        assert result["success"] is True
        assert result["skipped"] is True
        assert result["reason"] == "missing_customer_id"
        mock_upsert.assert_not_called()
        mock_db.commit.assert_not_called()

    def test_subscription_event_acknowledged_when_no_account(
        self, mock_db, subscription_event
    ):
        """Unknown customer is acknowledged without a DB write (no Stripe retries)."""
        mock_db.query.side_effect = [
            Mock(filter=Mock(return_value=Mock(first=Mock(return_value=None)))),
        ]
        with patch.object(
            CheckoutService, "create_or_update_subscription"
        ) as mock_upsert:
            result = WebhookService.handle_subscription_created(
                mock_db, subscription_event
            )

        assert result["success"] is True
        assert result["skipped"] is True
        assert result["reason"] == "missing_user_account"
        mock_upsert.assert_not_called()
        mock_db.commit.assert_not_called()

    def test_subscription_event_acknowledged_when_no_period_end(
        self, mock_db, mock_user_account, subscription_event
    ):
        """Missing expiration is acknowledged without a DB write."""
        subscription_event.pop("current_period_end")
        subscription_event.pop("trial_end", None)
        mock_db.query.side_effect = [
            Mock(
                filter=Mock(
                    return_value=Mock(first=Mock(return_value=mock_user_account))
                )
            ),
        ]
        with patch.object(
            CheckoutService, "create_or_update_subscription"
        ) as mock_upsert:
            result = WebhookService.handle_subscription_created(
                mock_db, subscription_event
            )

        assert result["success"] is True
        assert result["skipped"] is True
        assert result["reason"] == "missing_expiration"
        mock_upsert.assert_not_called()
        mock_db.commit.assert_not_called()

    # --- P2: handle_subscription_updated ---

    @pytest.mark.asyncio
    async def test_dispatch_updated_routes_to_handler(
        self, mock_db, subscription_event
    ):
        """dispatch routes `updated` to handle_subscription_updated."""
        with patch.object(
            WebhookService,
            "handle_subscription_updated",
            return_value={"success": True},
        ) as mock_updated:
            result = await WebhookService.dispatch_webhook_event(
                mock_db, "customer.subscription.updated", subscription_event
            )
        assert result["success"] is True
        mock_updated.assert_called_once_with(mock_db, subscription_event)

    def test_subscription_updated_mirrors_scheduled_downgrade(
        self, mock_db, mock_user_account, mock_user, subscription_event
    ):
        """`updated` with cancel_at_period_end=True flips scheduled_downgrade."""
        subscription_event["cancel_at_period_end"] = True

        mock_subscription = Mock()
        mock_subscription.scheduled_downgrade = False
        mock_subscription.updated_at = None

        mock_storage = Mock()
        mock_storage.storage_quota_bytes = 214748364800

        # Query side_effect: account, subscription re-query (cancel path),
        # storage usage, then user (cache invalidation).
        mock_db.query.side_effect = [
            Mock(
                filter=Mock(
                    return_value=Mock(first=Mock(return_value=mock_user_account))
                )
            ),
            Mock(
                filter=Mock(
                    return_value=Mock(first=Mock(return_value=mock_subscription))
                )
            ),
            Mock(filter=Mock(return_value=Mock(first=Mock(return_value=mock_storage)))),
            Mock(filter=Mock(return_value=Mock(first=Mock(return_value=mock_user)))),
        ]

        with patch.object(
            CheckoutService, "create_or_update_subscription", return_value=99
        ), patch(
            "studio.app.common.core.subscription.webhook_service."
            "invalidate_user_tier_cache"
        ):
            result = WebhookService.handle_subscription_updated(
                mock_db, subscription_event
            )

        assert result["success"] is True
        assert result["scheduled_downgrade"] is True
        assert mock_subscription.scheduled_downgrade is True
        assert mock_subscription.updated_at is not None

    def test_subscription_updated_resets_scheduled_downgrade_when_uncancelled(
        self, mock_db, mock_user_account, mock_user, subscription_event
    ):
        """`updated` with cancel_at_period_end=False resets scheduled_downgrade.

        When a user un-cancels in Stripe, the upsert in Step 4
        (_apply_subscription_update) unconditionally sets
        scheduled_downgrade=False, so the local state is corrected.
        """
        subscription_event["cancel_at_period_end"] = False
        # cancel_at_period_end=False -> helper skips the subscription
        # re-query, so the query chain is just account + user.
        self._setup_query_chain(mock_db, mock_user_account, mock_user)

        with patch.object(
            CheckoutService, "create_or_update_subscription", return_value=99
        ) as mock_upsert, patch(
            "studio.app.common.core.subscription.webhook_service."
            "invalidate_user_tier_cache"
        ):
            result = WebhookService.handle_subscription_updated(
                mock_db, subscription_event
            )

        assert result["success"] is True
        assert result["scheduled_downgrade"] is False
        mock_upsert.assert_called_once()

    def test_updated_past_due_still_marked_synced(
        self, mock_db, mock_user_account, mock_user, subscription_event
    ):
        """A past_due subscription stays SYNCED after mirroring."""
        subscription_event["status"] = "past_due"
        self._setup_query_chain(mock_db, mock_user_account, mock_user)

        with patch.object(
            CheckoutService, "create_or_update_subscription", return_value=99
        ) as mock_upsert, patch(
            "studio.app.common.core.subscription.webhook_service."
            "invalidate_user_tier_cache"
        ):
            result = WebhookService.handle_subscription_updated(
                mock_db, subscription_event
            )

        assert result["success"] is True
        mock_upsert.assert_called_once()

    def test_subscription_updated_error_logs_traceback(
        self, mock_db, mock_user_account, subscription_event
    ):
        """Errors in handle_subscription_updated log full traceback."""
        from fastapi import HTTPException

        mock_db.query.side_effect = [
            # 1. SubscriptionUserAccount lookup succeeds
            Mock(
                filter=Mock(
                    return_value=Mock(first=Mock(return_value=mock_user_account))
                )
            ),
        ]

        with patch.object(
            CheckoutService,
            "create_or_update_subscription",
            side_effect=RuntimeError("db write failed"),
        ), pytest.raises(HTTPException) as exc_info:
            WebhookService.handle_subscription_updated(mock_db, subscription_event)

        assert exc_info.value.status_code == 500
        assert "subscription.updated" in exc_info.value.detail

    # --- Concurrency ---

    def test_concurrent_storage_insert_falls_back_to_update(
        self, mock_db, mock_user_account, mock_user, subscription_event
    ):
        """Duplicate storage insert (race with checkout) falls back."""
        from sqlalchemy.exc import IntegrityError

        # Account lookup, storage (None), user for cache invalidation
        mock_db.query.side_effect = [
            # 1. SubscriptionUserAccount by customer_id
            Mock(
                filter=Mock(
                    return_value=Mock(first=Mock(return_value=mock_user_account))
                )
            ),
            # 2. UserStorageUsage -> None (triggers INSERT path)
            Mock(filter=Mock(return_value=Mock(first=Mock(return_value=None)))),
            # 3. User for cache invalidation
            Mock(filter=Mock(return_value=Mock(first=Mock(return_value=mock_user)))),
        ]

        # begin_nested() SAVEPOINT; flush raises IntegrityError
        nested_cm = Mock()
        nested_cm.__enter__ = Mock(return_value=None)
        nested_cm.__exit__ = Mock(return_value=False)
        mock_db.begin_nested.return_value = nested_cm
        mock_db.flush.side_effect = IntegrityError(
            "Duplicate entry", params=None, orig=Exception()
        )

        with patch.object(
            CheckoutService, "create_or_update_subscription", return_value=99
        ), patch(
            "studio.app.common.core.subscription.webhook_service."
            "invalidate_user_tier_cache"
        ):
            result = WebhookService.handle_subscription_created(
                mock_db, subscription_event
            )

        assert result["success"] is True
        assert result["user_id"] == 42
        # SAVEPOINT used, NOT full rollback
        mock_db.begin_nested.assert_called_once()
        mock_db.rollback.assert_not_called()
        # execute() called once for fallback UPDATE
        mock_db.execute.assert_called_once()


class TestWebhookErrorDetailPassthrough:
    """
    Webhook handlers used to collapse every inner HTTPException into
    400 "Invalid webhook data", at up to four nesting levels, without
    logging any of them. The originating status and detail must now
    survive to the caller, and be logged exactly once at the dispatch
    boundary.
    """

    @pytest.fixture
    def mock_db(self):
        db = Mock(spec=Session)
        db.query = Mock()
        db.add = Mock()
        db.commit = Mock()
        db.rollback = Mock()
        db.execute = Mock()
        return db

    @pytest.mark.asyncio
    async def test_inner_detail_survives_dispatch(self, mock_db):
        """A handler's own 404 is not rewritten into 400 Invalid webhook data."""
        with patch.object(
            WebhookService,
            "handle_checkout_completed",
            side_effect=HTTPException(
                status_code=404, detail="Subscription plan not found: 3"
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                await WebhookService.dispatch_webhook_event(
                    mock_db, "checkout.session.completed", {}
                )

        assert exc.value.status_code == 404
        assert exc.value.detail == "Subscription plan not found: 3"

    @pytest.mark.asyncio
    async def test_server_error_is_not_downgraded_to_400(self, mock_db):
        """A 5xx must not be relabelled as a client-side 400."""
        with patch.object(
            WebhookService,
            "handle_checkout_completed",
            side_effect=HTTPException(
                status_code=500, detail="Failed to update subscription"
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                await WebhookService.dispatch_webhook_event(
                    mock_db, "checkout.session.completed", {}
                )

        assert exc.value.status_code == 500
        assert exc.value.detail == "Failed to update subscription"

    @pytest.mark.asyncio
    async def test_client_error_logged_once_as_warning(self, mock_db, caplog):
        """4xx logs at WARNING with the event type, status and detail."""
        with patch.object(
            WebhookService,
            "handle_checkout_completed",
            side_effect=HTTPException(status_code=404, detail="Nope"),
        ):
            with caplog.at_level("WARNING"):
                with pytest.raises(HTTPException):
                    await WebhookService.dispatch_webhook_event(
                        mock_db, "checkout.session.completed", {}
                    )

        matches = [r for r in caplog.records if "Webhook checkout" in r.getMessage()]
        assert len(matches) == 1
        assert matches[0].levelname == "WARNING"
        assert "404" in matches[0].getMessage()
        assert "Nope" in matches[0].getMessage()

    @pytest.mark.asyncio
    async def test_server_error_logged_as_error(self, mock_db, caplog):
        """5xx must page at ERROR, not hide at WARNING with the 4xx traffic."""
        with patch.object(
            WebhookService,
            "handle_checkout_completed",
            side_effect=HTTPException(status_code=500, detail="Boom"),
        ):
            with caplog.at_level("WARNING"):
                with pytest.raises(HTTPException):
                    await WebhookService.dispatch_webhook_event(
                        mock_db, "checkout.session.completed", {}
                    )

        matches = [r for r in caplog.records if "Webhook checkout" in r.getMessage()]
        assert len(matches) == 1
        assert matches[0].levelname == "ERROR"

    @pytest.mark.asyncio
    async def test_unexpected_error_still_becomes_500(self, mock_db):
        """The generic arm is untouched: non-HTTP errors still surface as 500."""
        with patch.object(
            WebhookService,
            "handle_checkout_completed",
            side_effect=RuntimeError("kaboom"),
        ):
            with pytest.raises(HTTPException) as exc:
                await WebhookService.dispatch_webhook_event(
                    mock_db, "checkout.session.completed", {}
                )

        assert exc.value.status_code == 500
        assert "kaboom" in exc.value.detail

    @pytest.mark.asyncio
    async def test_success_path_logs_no_failure(self, mock_db, caplog):
        """A healthy event must not emit a failure line."""
        with patch.object(
            WebhookService,
            "handle_checkout_completed",
            return_value={"success": True},
        ):
            with caplog.at_level("WARNING"):
                result = await WebhookService.dispatch_webhook_event(
                    mock_db, "checkout.session.completed", {}
                )

        assert result["success"] is True
        assert not [r for r in caplog.records if "failed" in r.getMessage()]


class TestWebhookRouteErrorStatusPassthrough:
    """
    The route arm used to rewrite every inner HTTPException to 400. It now
    keeps the inner status and masks the detail, so a handler's 500 stays out
    of the malformed-request bucket without naming which check failed. The
    other tests in this file call dispatch_webhook_event directly, so only an
    HTTP-level request covers what Stripe actually receives.
    """

    WEBHOOK_URL = "/api/subsc/webhooks/stripe"
    CONSTRUCT_EVENT = (
        "studio.app.common.routers.subscriptions.stripe.Webhook.construct_event"
    )

    @pytest.fixture
    def webhook_client(self):
        original_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_db] = lambda: Mock(spec=Session)
        yield TestClient(app)
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original_overrides)

    def _post(self, client, dispatch_exc):
        with (
            patch.object(
                WebhookService, "get_webhook_secret", return_value="whsec_test"
            ),
            patch(
                self.CONSTRUCT_EVENT,
                return_value={
                    "type": "checkout.session.completed",
                    "data": {"object": {}},
                },
            ),
            patch.object(
                WebhookService,
                "dispatch_webhook_event",
                new_callable=AsyncMock,
                side_effect=dispatch_exc,
            ),
        ):
            return client.post(
                self.WEBHOOK_URL,
                content=b"{}",
                headers={"stripe-signature": "t=1,v1=sig"},
            )

    def test_inner_status_survives_the_route(self, webhook_client):
        response = self._post(
            webhook_client,
            HTTPException(status_code=404, detail="Subscription plan not found: 3"),
        )

        assert response.status_code == 404
        # Masked on purpose: the plan id must not reach Stripe's delivery log
        assert response.json()["detail"] == "Webhook processing failed"

    def test_server_error_keeps_its_status_at_the_route(self, webhook_client):
        response = self._post(
            webhook_client,
            HTTPException(status_code=500, detail="Failed to update subscription"),
        )

        assert response.status_code == 500
        assert response.json()["detail"] == "Webhook processing failed"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestInvoiceFinalizedPaymentOutcomes:
    """invoice.finalized separates outcomes that are not our failure.

    One `except stripe.error.StripeError` used to log all three at ERROR, which
    made an already-settled invoice and a customer's declined card look like a
    broken integration and left ERROR-based alerting unusable.
    """

    INVOICE = {
        "id": "in_test_finalized",
        "status": "open",
        "default_payment_method": "pm_card_visa",
        "amount_due": 2000,
    }

    def test_already_paid_invoice_is_success_not_failure(self):
        """Stripe's auto-collection can win the race; the end state is what counts."""
        import stripe

        error = stripe.error.InvalidRequestError("Invoice is already paid", None)
        with patch(
            "studio.app.common.core.subscription.subscription_service."
            "SubscriptionService._ensure_stripe_initialized"
        ), patch("stripe.Invoice.pay", side_effect=error), patch(
            "stripe.Invoice.retrieve", return_value={"status": "paid"}
        ):
            result = WebhookService.handle_invoice_finalized(dict(self.INVOICE))

        assert result["success"] is True
        assert result["new_status"] == "paid"
        assert "payment_failed" not in result

    def test_declined_card_is_reported_without_claiming_our_fault(self):
        import stripe

        error = stripe.error.CardError("Your card was declined.", None, "card_declined")
        with patch(
            "studio.app.common.core.subscription.subscription_service."
            "SubscriptionService._ensure_stripe_initialized"
        ), patch("stripe.Invoice.pay", side_effect=error):
            result = WebhookService.handle_invoice_finalized(dict(self.INVOICE))

        assert result["success"] is False
        assert result["payment_failed"] is True
        assert result["card_declined"] is True

    def test_a_genuine_stripe_failure_still_fails(self):
        """The read-back must not turn every refused pay into a success."""
        import stripe

        error = stripe.error.APIConnectionError("connection reset")
        with patch(
            "studio.app.common.core.subscription.subscription_service."
            "SubscriptionService._ensure_stripe_initialized"
        ), patch("stripe.Invoice.pay", side_effect=error), patch(
            "stripe.Invoice.retrieve", return_value={"status": "open"}
        ):
            result = WebhookService.handle_invoice_finalized(dict(self.INVOICE))

        assert result["success"] is False
        assert result["payment_failed"] is True

    def test_a_failed_read_back_does_not_escalate_to_a_500(self):
        """A read-back that itself fails must leave the outcome a plain failure."""
        import stripe

        error = stripe.error.InvalidRequestError("Invoice is already paid", None)
        with patch(
            "studio.app.common.core.subscription.subscription_service."
            "SubscriptionService._ensure_stripe_initialized"
        ), patch("stripe.Invoice.pay", side_effect=error), patch(
            "stripe.Invoice.retrieve",
            side_effect=stripe.error.RateLimitError("slow down"),
        ):
            result = WebhookService.handle_invoice_finalized(dict(self.INVOICE))

        assert result["success"] is False
        assert result["payment_failed"] is True
