"""Tests that DELETE /api/subsc/mgmts/cancel preserves inner error status and detail.

Regression coverage for the handler collapsing every inner HTTPException into a
generic "404 Subscription not found", which hid whether the failure was a missing
subscription, a missing cancelable Stripe subscription, or a server-side failure.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import stripe
from fastapi import HTTPException
from fastapi.testclient import TestClient

from studio.__main_unit__ import app
from studio.app.common.core.auth.auth_dependencies import get_current_user
from studio.app.common.core.logger import AppLogger
from studio.app.common.db.database import get_db
from studio.app.common.schemas.subscriptions import CancelSubscriptionResponse

CANCEL_URL = "/api/subsc/mgmts/cancel"
LOGGER_NAME = AppLogger.LOGGER_NAME

HANDLE_CANCEL = (
    "studio.app.common.core.subscription.stripe_service"
    ".StripeService.handle_cancel_user_subscription"
)
GET_USER_SUBSCRIPTION = (
    "studio.app.common.core.subscription.subscription_service"
    ".SubscriptionService.get_user_subscription"
)
GET_OR_CREATE_CUSTOMER = (
    "studio.app.common.core.subscription.stripe_service.get_or_create_stripe_customer"
)
STRIPE_SUBSCRIPTION_LIST = (
    "studio.app.common.core.subscription.stripe_service.stripe.Subscription.list"
)


@pytest.fixture
def mock_user():
    user = MagicMock()
    user.id = 1
    user.email = "test@example.com"
    return user


@pytest.fixture
def mock_db():
    return MagicMock()


@pytest.fixture(autouse=True)
def cleanup_overrides():
    original_overrides = app.dependency_overrides.copy()
    yield
    app.dependency_overrides.clear()
    app.dependency_overrides.update(original_overrides)


@pytest.fixture
def cancel_client(cleanup_overrides, mock_user, mock_db):
    app.dependency_overrides[get_current_user] = lambda: mock_user
    app.dependency_overrides[get_db] = lambda: mock_db
    return TestClient(app)


class TestCancelSubscriptionErrorDetail:
    def test_missing_subscription_row_keeps_service_detail(self, cancel_client):
        """Real service chain: no UserSubscription row surfaces its own detail."""
        with patch(GET_USER_SUBSCRIPTION, return_value=None):
            response = cancel_client.delete(CANCEL_URL)

        assert response.status_code == 404
        assert response.json()["detail"] == "No active subscription found to cancel"

    def test_no_cancelable_stripe_sub_keeps_service_detail(self, cancel_client):
        """Real service chain: nothing cancelable in Stripe stays distinguishable."""
        with (
            patch(GET_USER_SUBSCRIPTION, return_value=(MagicMock(), MagicMock())),
            patch(
                GET_OR_CREATE_CUSTOMER,
                new_callable=AsyncMock,
                return_value=MagicMock(id="cus_test"),
            ),
            patch(STRIPE_SUBSCRIPTION_LIST, return_value=MagicMock(data=[])),
        ):
            response = cancel_client.delete(CANCEL_URL)

        assert response.status_code == 404
        assert (
            response.json()["detail"] == "No active or trial Stripe subscription found"
        )

    def test_server_error_is_not_downgraded_to_404(self, cancel_client):
        """A 500 from deeper in the chain must not be reported as a 404."""
        with patch(
            HANDLE_CANCEL,
            new_callable=AsyncMock,
            side_effect=HTTPException(
                status_code=500, detail="Failed to update scheduled downgrade"
            ),
        ):
            response = cancel_client.delete(CANCEL_URL)

        assert response.status_code == 500
        assert response.json()["detail"] == "Failed to update scheduled downgrade"

    def test_stripe_error_still_returns_400(self, cancel_client):
        with patch(
            HANDLE_CANCEL,
            new_callable=AsyncMock,
            side_effect=stripe.error.StripeError("card declined"),
        ):
            response = cancel_client.delete(CANCEL_URL)

        assert response.status_code == 400
        assert "Payment processing error" in response.json()["detail"]

    def test_unexpected_error_still_returns_500(self, cancel_client):
        with patch(
            HANDLE_CANCEL,
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            response = cancel_client.delete(CANCEL_URL)

        assert response.status_code == 500
        assert response.json()["detail"] == "Failed to cancel subscription: boom"

    def test_successful_cancel_is_unaffected(self, cancel_client):
        with patch(
            HANDLE_CANCEL,
            new_callable=AsyncMock,
            return_value=CancelSubscriptionResponse(
                success=True,
                message="Subscription will be cancelled on 2026-08-30.",
                cancellation_date="2026-08-30",
                access_until="2026-08-30 00:00:00",
            ),
        ):
            response = cancel_client.delete(CANCEL_URL)

        assert response.status_code == 200
        assert response.json()["success"] is True
        assert response.json()["cancellation_date"] == "2026-08-30"


class TestCancelSubscriptionLogging:
    """The endpoint used to discard the inner error without logging it."""

    def _cancel_failing_with(self, cancel_client, caplog, exc):
        with patch(HANDLE_CANCEL, new_callable=AsyncMock, side_effect=exc):
            with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
                cancel_client.delete(CANCEL_URL)
        return [
            r for r in caplog.records if "Cancel subscription failed" in r.getMessage()
        ]

    def test_client_error_logged_as_warning_with_status_and_detail(
        self, cancel_client, caplog
    ):
        records = self._cancel_failing_with(
            cancel_client,
            caplog,
            HTTPException(status_code=404, detail="No active subscription found"),
        )

        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert "for user 1:" in records[0].getMessage()
        assert "404 No active subscription found" in records[0].getMessage()

    def test_server_error_logged_as_error(self, cancel_client, caplog):
        """A 5xx must page as ERROR, not hide at WARNING with the 4xx traffic."""
        records = self._cancel_failing_with(
            cancel_client,
            caplog,
            HTTPException(
                status_code=500, detail="Failed to update scheduled downgrade"
            ),
        )

        assert len(records) == 1
        assert records[0].levelno == logging.ERROR
        assert "500 Failed to update scheduled downgrade" in records[0].getMessage()


class TestCancelSubscriptionWithoutUser:
    """Standalone mode resolves get_current_user to None (see __main_unit__)."""

    def test_missing_user_still_returns_a_json_error(self, cleanup_overrides, mock_db):
        """The except arms must not crash on user.id while handling the failure."""
        app.dependency_overrides[get_current_user] = lambda: None
        app.dependency_overrides[get_db] = lambda: mock_db

        response = TestClient(app).delete(CANCEL_URL)

        assert response.status_code == 500
        assert "detail" in response.json()
