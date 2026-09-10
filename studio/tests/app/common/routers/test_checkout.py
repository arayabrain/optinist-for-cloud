# flake8: noqa: E402
import os

# Set environment variables before other imports
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake_key_for_testing"

from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from studio.app.common.core.subscription.constants import SubscriptionPlanIds
from studio.app.common.core.subscription.subscription_service import SubscriptionService


class TestWebhookData:
    """The ``parent.subscription_details`` fallback for ``subscription_id``.

    Every other renewal test sends a top-level ``subscription``, so the fallback
    branch is only reachable from here. These cases drive
    ``handle_subscription_payment_succeeded`` rather than re-implementing the
    extraction: the previous version asserted its own dict comprehension and
    omitted the ``type`` discriminator production actually requires.
    """

    @staticmethod
    def _invoice(parent):
        return {
            "id": "in_test123",
            "customer": "cus_test123",
            "parent": parent,
            "status": "paid",
            "amount_paid": 2999,
            "billing_reason": "subscription_cycle",
        }

    def test_subscription_id_is_read_from_parent_when_absent_at_top_level(self):
        from studio.app.common.core.subscription.webhook_service import WebhookService

        db = Mock()
        db.query.return_value.filter.return_value.first.return_value = None

        result = WebhookService.handle_subscription_payment_succeeded(
            db,
            self._invoice(
                {
                    "type": "subscription_details",
                    "subscription_details": {"subscription": "sub_test123"},
                }
            ),
        )

        assert result["reason"] == "missing_user_account"

    def test_parent_without_the_type_discriminator_is_rejected(self):
        from studio.app.common.core.subscription.webhook_service import WebhookService

        db = Mock()
        db.query.return_value.filter.return_value.first.return_value = None

        with pytest.raises(HTTPException) as exc:
            WebhookService.handle_subscription_payment_succeeded(
                db,
                self._invoice(
                    {"subscription_details": {"subscription": "sub_test123"}}
                ),
            )

        assert exc.value.status_code == 400


class TestCheckoutRoutes:
    """Route-level assertions for the checkout and webhook endpoints.

    These three cases were previously ``@pytest.mark.integration`` and driven
    with live ``requests`` calls against ``SubscriptionService.get_base_url()``,
    behind a ``check_api_running`` fixture that called ``pytest.skip()`` unless
    a server answered ``/docs``. No lane runs a server alongside pytest, so
    they were the routers lane's three skips, while the coverage map credited the
    signature check. None of the three needs a server: the
    assertions are about our own routing and error mapping, so they run against
    ``TestClient`` with Stripe patched at the boundary.
    """

    @pytest.fixture
    def client(self):
        """TestClient with the DB dependency stubbed.

        The webhook rejects an unsigned body before touching the session, but
        FastAPI resolves ``get_db`` before entering the handler, and the test
        container has no MySQL.
        """
        from fastapi.testclient import TestClient

        from studio.__main_unit__ import app
        from studio.app.common.db.database import get_db

        app.dependency_overrides[get_db] = lambda: Mock()
        try:
            with TestClient(app) as c:
                yield c
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_get_subscription_plans(self, client):
        """The plans route is mounted and returns a list, not a 500.

        ``get_active_plans`` returning nothing is a legitimate state (an
        unseeded environment); the route logs a warning and must still answer
        200 with an empty list, because the SPA renders the plan cards from it.
        """
        with patch.object(SubscriptionService, "get_active_plans", return_value=[]):
            response = client.get("/api/subsc/mgmts/plans")

        assert response.status_code == 200
        assert response.json() == []

    def test_checkout_session_validation_rejects_an_unknown_session(self, client):
        """A session id Stripe does not recognise maps to 400, not 500.

        ``InvalidRequestError`` is a ``StripeError``, so it must be caught by
        the 400 branch rather than falling through to the generic 500 handler -
        the SPA's ``/subscription/thanks`` page distinguishes the two.
        """
        import stripe

        with patch(
            "stripe.checkout.Session.retrieve",
            side_effect=stripe.error.InvalidRequestError(
                "No such checkout session", param="session_id"
            ),
        ):
            response = client.post(
                "/api/subsc/checkout/validate-checkout-session",
                json={"session_id": "cs_test_fake_session"},
            )

        assert response.status_code == 400

    @staticmethod
    def _post_unverified(client, headers=None):
        """POST a plausible ``checkout.session.completed`` with the dispatcher
        spied on rather than mocked out of existence.

        The status alone is not the whole assertion. The invariant that matters
        is that an unverified event never reaches the dispatcher at all - that
        dispatcher is what writes the premium row and the 200GB quota - and a
        status check cannot distinguish "rejected before dispatch" from
        "dispatched and then failed with a 4xx".
        """
        from studio.app.common.core.subscription.webhook_service import WebhookService

        with patch.object(WebhookService, "dispatch_webhook_event") as dispatch:
            dispatch.return_value = None
            response = client.post(
                "/api/subsc/webhooks/stripe",
                json={
                    "type": "checkout.session.completed",
                    "data": {"object": {"id": "cs_test"}},
                },
                headers=headers or {},
            )
        return response, dispatch

    def test_webhook_requires_signature(self, client):
        """An unsigned webhook body is rejected and not processed.

        The only thing standing between the public webhook route and a forged
        ``checkout.session.completed`` - which grants premium and a 200GB quota -
        is ``construct_event``'s signature check.
        """
        response, dispatch = self._post_unverified(client)

        assert response.status_code == 400
        dispatch.assert_not_called()

    def test_webhook_rejects_a_forged_signature(self, client):
        """A syntactically valid but wrong signature is rejected too.

        A missing header and a wrong signature take different branches inside
        ``construct_event`` (``ValueError`` vs ``SignatureVerificationError``),
        so the missing-header case alone would still pass if verification were
        degraded to a presence check.
        """
        response, dispatch = self._post_unverified(
            client, headers={"stripe-signature": "t=1,v1=" + "0" * 64}
        )

        assert response.status_code == 400
        dispatch.assert_not_called()

    def test_webhook_dispatches_a_verified_event(self, client):
        """The negative cases above must not be passing because the route is
        broken for every input. With verification satisfied, the event reaches
        the dispatcher and the route answers 200."""
        from studio.app.common.core.subscription.webhook_service import WebhookService

        event = {
            "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_test"}},
        }

        with patch("stripe.Webhook.construct_event", return_value=event), patch.object(
            WebhookService, "dispatch_webhook_event"
        ) as dispatch:
            dispatch.return_value = None
            response = client.post(
                "/api/subsc/webhooks/stripe",
                json=event,
                headers={"stripe-signature": "t=1,v1=" + "0" * 64},
            )

        assert response.status_code == 200
        assert response.json()["processed"] == "checkout.session.completed"
        assert dispatch.call_args.args[1] == "checkout.session.completed"


class TestWebhookStatusReportsWhoseFaultItWas:
    """The webhook's status must say whether a failure was ours or the caller's.

    Two layers used to replace every inner ``HTTPException`` with a hardcoded
    400 - the route, and ``dispatch_webhook_event`` one level deeper. Both were
    written to stop the response naming which verification failed, and at the
    time every raise they saw was already a 400, so the status was unchanged for
    those. What neither accounted for is the 500s ``WebhookService`` raises, such
    as ``HTTPException(500, "Error retrieving subscription from Stripe: ...")``
    when the Stripe call inside ``handle_checkout_completed`` fails.

    This is *not* about redelivery. Stripe treats every non-2xx alike, retrying
    with exponential backoff for up to three days in live mode, so the old 400
    did not abandon the delivery. What it did was report our own outage as a
    malformed request: the failure stayed out of the 5xx alarm, and the delivery
    log pointed whoever read it at Stripe's payload instead of at our stack
    trace.

    The detail-suppression intent is preserved - the body is still the generic
    string - so these tests assert the status, which is the part that carries the
    diagnosis.
    """

    @staticmethod
    def _post_verified_but_failing(client, error):
        """A correctly signed event whose *handler* raises ``error``.

        Patched at ``handle_checkout_completed``, deliberately not at
        ``dispatch_webhook_event``: the dispatcher had its own
        ``except HTTPException -> 400`` flattener, so patching it would replace
        the code under test and the assertions below would pass whether or not
        the status actually survives to the client.
        """
        from studio.app.common.core.subscription.webhook_service import WebhookService

        event = {
            "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_test"}},
        }

        with patch("stripe.Webhook.construct_event", return_value=event), patch.object(
            WebhookService, "handle_checkout_completed", side_effect=error
        ):
            return client.post(
                "/api/subsc/webhooks/stripe",
                json=event,
                headers={"stripe-signature": "t=1,v1=" + "0" * 64},
            )

    def test_an_internal_failure_during_dispatch_is_5xx(self, client):
        """Our own half, using the real error ``WebhookService`` raises when the
        Stripe subscription lookup fails mid-processing."""
        response = self._post_verified_but_failing(
            client,
            HTTPException(
                status_code=500,
                detail="Error retrieving subscription from Stripe: connection reset",
            ),
        )

        assert response.status_code >= 500, (
            f"an internal failure answered {response.status_code}, reporting our "
            "own outage as the caller's fault and keeping it out of the 5xx alarm"
        )

    def test_an_unexpected_exception_during_dispatch_is_5xx(self, client):
        """A bug that raises something other than HTTPException must also be
        retryable rather than reported as the caller's fault."""
        response = self._post_verified_but_failing(
            client, RuntimeError("unhandled bug in the handler")
        )

        assert response.status_code >= 500

    def test_a_caller_side_problem_stays_4xx(self, client):
        """The converse direction. An unpaid session is a fact about the event,
        not a fault of ours, so it must not be promoted to a 5xx and page
        somebody. Preserving the status has to work both ways or it is just a
        different constant."""
        response = self._post_verified_but_failing(
            client,
            HTTPException(
                status_code=400, detail="Payment not completed. Status: unpaid"
            ),
        )

        assert response.status_code == 400

    def test_a_lookup_race_is_not_reported_as_a_client_error(self, client):
        """``checkout.session.completed`` can arrive before the user row is
        visible. Stripe will retry regardless, so what this pins is the reported
        status: today the race surfaces as 404, and this records that rather than
        leaving it to be discovered from a delivery log."""
        response = self._post_verified_but_failing(
            client,
            HTTPException(status_code=404, detail="No active user found for customer"),
        )

        assert response.status_code == 404, (
            "a not-yet-visible user currently surfaces as 404; if this ever "
            "needs to be retryable it must be mapped to 5xx at the raise site"
        )

    def test_the_inner_detail_is_not_leaked(self, client):
        """The intent of the original change is preserved: the response body must
        not name which check failed or echo an internal error string."""
        response = self._post_verified_but_failing(
            client,
            HTTPException(
                status_code=500,
                detail="Error retrieving subscription from Stripe: connection reset",
            ),
        )

        assert response.json()["detail"] == "Webhook processing failed"

    def test_the_signature_rejection_detail_is_not_leaked(self, client):
        """Same for the 400 path: "Invalid signature" and "Invalid payload" tell a
        forger which check they failed."""
        response, _ = TestCheckoutRoutes._post_unverified(client)

        assert response.json()["detail"] == "Webhook processing failed"


class TestCreateOrUpdateSubscriptionConcurrency:
    """create_or_update_subscription idempotency under concurrent inserts.

    Guards the race in issue #629 where checkout.session.completed and
    customer.subscription.created can both reach the INSERT branch and one
    hits the unique constraint on subscription_users.user_id. The handler
    catches IntegrityError and falls back to selecting + updating the row
    the racing delivery created.
    """

    @staticmethod
    def _mock_db(existing_first, existing_after_conflict):
        """db whose SELECT...FOR UPDATE returns existing_first, then
        existing_after_conflict on the post-conflict re-select."""
        from unittest.mock import MagicMock

        db = Mock()
        first = Mock(side_effect=[existing_first, existing_after_conflict])
        db.query.return_value.filter.return_value.with_for_update.return_value.first = (
            first
        )
        # begin_nested() as a context manager that does NOT suppress exceptions.
        cm = MagicMock()
        cm.__enter__.return_value = cm
        cm.__exit__.return_value = False
        db.begin_nested.return_value = cm
        db.add = Mock()
        return db

    def test_concurrent_insert_falls_back_to_update(self):
        """A unique-constraint conflict on insert re-selects and updates."""
        from studio.app.common.core.subscription.checkout_service import CheckoutService

        existing_row = Mock()
        existing_row.id = 555
        db = self._mock_db(existing_first=None, existing_after_conflict=existing_row)
        # The insert flush raises a unique-violation IntegrityError.
        db.flush = Mock(
            side_effect=IntegrityError("INSERT", {}, Exception("duplicate user_id"))
        )

        expiration = datetime.now() + timedelta(days=30)
        with patch.object(
            SubscriptionService, "get_current_datetime", return_value=datetime.now()
        ):
            result_id = CheckoutService.create_or_update_subscription(
                db,
                user_id=42,
                plan_id=SubscriptionPlanIds.PREMIUM,
                expiration_date=expiration,
            )

        # Fell back to updating the row the racing delivery created.
        assert result_id == 555
        assert existing_row.plan_id == SubscriptionPlanIds.PREMIUM
        assert existing_row.expiration == expiration
        assert existing_row.scheduled_downgrade is False

    def test_other_integrity_error_is_not_swallowed(self):
        """If no row exists even after the conflict, the error is re-raised."""
        from studio.app.common.core.subscription.checkout_service import CheckoutService

        db = self._mock_db(existing_first=None, existing_after_conflict=None)
        db.flush = Mock(
            side_effect=IntegrityError("INSERT", {}, Exception("some other constraint"))
        )

        expiration = datetime.now() + timedelta(days=30)
        with pytest.raises(IntegrityError):
            CheckoutService.create_or_update_subscription(
                db,
                user_id=42,
                plan_id=SubscriptionPlanIds.PREMIUM,
                expiration_date=expiration,
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
