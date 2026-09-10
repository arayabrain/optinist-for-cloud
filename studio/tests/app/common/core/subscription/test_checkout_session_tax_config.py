"""Stripe checkout-session configuration, tax inputs, and the purchase record.

Before this file, a grep for ``tax`` / ``automatic_tax`` / ``billing_address``
across ``studio/tests`` returned nothing.
Stripe's tax engine is not ours to assert - but every input we hand it is, and
that is where our own bugs live. Tax silently disabled is a compliance and
revenue problem that no other test in this repository would catch.

What is asserted here:

- The session is created with ``automatic_tax`` enabled and
  ``billing_address_collection`` required. Stripe cannot compute tax without an
  address, so the two are one contract.
- The checkout path never calls a product- or price-mutating Stripe API. The
  catalog is terraform's, seeded by ``seed_subscription_plans.py``; a checkout
  that creates prices would silently fork it.
- Every field ``SUBSCRIPTION_PLANS_CONFIG`` declares is a field the seed writes
  to ``subscription_plans``, in both directions. Field names only: the live
  tfvars are gitignored, so comparing a deployed price against Stripe cannot be
  done from CI.
- The webhook records the purchase against the ``user_id`` and ``plan_id`` from
  the session metadata, converted from Stripe's strings, and the same handler
  run over a database really leaves that row behind.
- A trial is offered to a first-time buyer and to nobody else, with a payment
  method collected so the conversion at the end of it has something to charge.
- The session-verification helper reads ``total_details`` / ``amount_tax``.
  That helper currently has no caller, so these cases pin the extraction against
  rot rather than proving a tax figure reaches anything.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
import stripe

from studio.app.common.core.subscription.checkout_service import CheckoutService
from studio.app.common.core.subscription.constants import SubscriptionPeriods
from studio.app.common.core.subscription.webhook_service import WebhookService

# studio/tests/app/common/core/subscription/<this file> -> 7 levels to the root.
PROJECT_ROOT = Path(__file__).resolve().parents[6]
TERRAFORM_DIR = PROJECT_ROOT / "infrastructure" / "terraform"
MAIN_TF = TERRAFORM_DIR / "main.tf"
SEED_SCRIPT = PROJECT_ROOT / "infrastructure" / "scripts" / "seed_subscription_plans.py"


def _read_source(path: Path) -> str:
    """Read a tracked config file, failing loudly if the layout moved.

    A missing file must not silently turn the comparison below into a no-op.
    """
    assert path.exists(), f"expected to read {path}, which does not exist"
    return path.read_text()


# Stripe APIs that mutate the product catalog. The checkout path must only ever
# reference the price id terraform seeded, never create or edit one.
CATALOG_MUTATING_APIS = [
    ("Product", "create"),
    ("Product", "modify"),
    ("Price", "create"),
    ("Price", "modify"),
]


@pytest.fixture
def created_session(request):
    """Run ``handle_checkout_session`` and return the kwargs it passed to
    ``stripe.checkout.Session.create``, plus the patched stripe module.

    Everything outside the session-creation call is stubbed at the boundary:
    the plan comes from the DB, the customer from ``get_or_create_stripe_customer``.
    The subject under test is the parameter dict.

    Parametrise indirectly with ``True`` for a caller who has never bought
    anything, which is the only state the trial branch fires in.
    """
    first_time = getattr(request, "param", False)
    plan = Mock(
        id=2,
        name="Premium",
        stripe_price_id="price_seeded_by_terraform",
        stripe_product_id="prod_seeded_by_terraform",
    )
    user = Mock(id=42, email="user@example.com")
    checkout_request = Mock(plan_id="2")

    with patch(
        "studio.app.common.core.subscription.checkout_service.SubscriptionService"
    ) as subscription_service, patch(
        "studio.app.common.core.subscription.stripe_service."
        "get_or_create_stripe_customer"
    ) as get_customer, patch.object(
        CheckoutService,
        "get_existing_subscription",
        return_value=None if first_time else Mock(),
    ), patch.object(
        CheckoutService, "recover_existing_stripe_subscription", return_value=None
    ), patch.object(
        CheckoutService, "has_stripe_purchase_history", return_value=not first_time
    ), patch(
        "studio.app.common.core.subscription.checkout_service.stripe"
    ) as mock_stripe:
        subscription_service.get_plan_by_id.return_value = plan
        subscription_service.get_base_url.return_value = "https://optinist.example"
        subscription_service.get_user_subscription_purchase.return_value = (
            None if first_time else Mock()
        )

        async def _customer(db, user):
            return Mock(id="cus_existing")

        get_customer.side_effect = _customer
        mock_stripe.checkout.Session.create.return_value = Mock(
            url="https://checkout.stripe.com/c/pay/cs_test", id="cs_test"
        )
        # The real module's exception classes, so ``except stripe.error.StripeError``
        # still behaves.
        mock_stripe.error = stripe.error

        yield _CreatedSession(mock_stripe, checkout_request, user)


class _CreatedSession:
    """Lazily invokes the checkout path so a test can assert before or after."""

    def __init__(self, mock_stripe, request, user):
        self.stripe = mock_stripe
        self._request = request
        self._user = user

    async def params(self):
        await CheckoutService.handle_checkout_session(
            MagicMock(), self._request, self._user
        )
        return self.stripe.checkout.Session.create.call_args.kwargs


class TestCheckoutSessionTaxConfiguration:
    """The two tax inputs we control on the checkout session."""

    @pytest.mark.asyncio
    async def test_automatic_tax_is_enabled(self, created_session):
        """Without this, Stripe charges the list price with no tax and
        every invoice is under-collected."""
        params = await created_session.params()

        assert params["automatic_tax"] == {"enabled": True}

    @pytest.mark.asyncio
    async def test_billing_address_collection_is_required(self, created_session):
        """``automatic_tax`` needs a jurisdiction; with the address
        optional, Stripe falls back to no tax for customers who skip it, so this
        is not cosmetic - it is the other half of enabling automatic tax."""
        params = await created_session.params()

        assert params["billing_address_collection"] == "required"

    @pytest.mark.asyncio
    async def test_the_collected_address_is_saved_back_to_the_customer(
        self, created_session
    ):
        """Renewals are charged off the stored customer, not the checkout
        session, so an address collected once and not persisted leaves every
        subsequent invoice untaxed."""
        params = await created_session.params()

        assert params["customer_update"] == {"address": "auto"}

    @pytest.mark.asyncio
    async def test_the_session_is_a_subscription_against_the_seeded_price(
        self, created_session
    ):
        """Guards the premise of the three assertions above: they only mean
        something if this really is the subscription-mode session for the plan's
        terraform-seeded price."""
        params = await created_session.params()

        assert params["mode"] == "subscription"
        assert params["line_items"] == [
            {"price": "price_seeded_by_terraform", "quantity": 1}
        ]


class TestCheckoutDoesNotMutateTheStripeCatalog:
    """The checkout path never creates or edits a product or price.

    The catalog is owned by ``SUBSCRIPTION_PLANS_CONFIG`` and seeded into
    ``subscription_plans``. A checkout that creates its own price would diverge
    from the seeded catalog and bill an amount no tfvars file records.
    """

    @pytest.mark.asyncio
    async def test_no_product_or_price_mutating_call_is_made(self, created_session):
        await created_session.params()

        for resource, method in CATALOG_MUTATING_APIS:
            call = getattr(getattr(created_session.stripe, resource), method)
            assert not call.called, (
                f"checkout called stripe.{resource}.{method}; the product "
                f"catalog is terraform's, seeded by seed_subscription_plans.py"
            )

    @pytest.mark.asyncio
    async def test_the_session_references_the_price_id_from_the_database(
        self, created_session
    ):
        """The positive counterpart: the price is looked up, not minted."""
        params = await created_session.params()

        assert params["line_items"][0]["price"] == "price_seeded_by_terraform"


class TestOnlyAFirstTimeUserGetsATrial:
    """The trial branch of ``handle_checkout_session``.

    Nothing in the suite reached ``subscription_data`` before this: every other
    case here stubs the caller into a returning customer. Both directions matter
    and for opposite reasons: no trial for a first-time user is the feature
    missing, a trial for a returning one is a free month given away on every
    repeat purchase.
    """

    @pytest.mark.parametrize("created_session", [True], indirect=True)
    @pytest.mark.asyncio
    async def test_a_first_time_user_is_given_the_trial_period(self, created_session):
        params = await created_session.params()

        assert params["subscription_data"]["trial_period_days"] == (
            SubscriptionPeriods.TRIAL_PERIOD_DAYS
        )
        assert SubscriptionPeriods.TRIAL_PERIOD_DAYS == 30

    @pytest.mark.parametrize("created_session", [True], indirect=True)
    @pytest.mark.asyncio
    async def test_the_trial_still_collects_a_payment_method(self, created_session):
        """Stripe skips card collection on a trial unless it is asked for, and
        then the conversion at the end of the trial has nothing to charge. This
        parameter is what makes the auto-conversion to paid possible at all."""
        params = await created_session.params()

        assert params["payment_method_collection"] == "always"

    @pytest.mark.parametrize("created_session", [True], indirect=True)
    @pytest.mark.asyncio
    async def test_the_subscription_is_marked_as_a_trial(self, created_session):
        params = await created_session.params()

        assert params["subscription_data"]["metadata"] == {
            "is_trial": "true",
            "trial_days": str(SubscriptionPeriods.TRIAL_PERIOD_DAYS),
        }

    @pytest.mark.asyncio
    async def test_a_returning_customer_is_given_no_trial(self, created_session):
        params = await created_session.params()

        assert "subscription_data" not in params
        assert "payment_method_collection" not in params


class TestPlanConfigAgreesWithTheSeededRows:
    """``seed_subscription_plans.py`` maps every field the terraform
    ``subscription_plans`` variable declares, in both directions.

    This is a *field-name* invariant, not a value one: it catches a terraform
    schema change the seed silently drops, which is what would desync a plan's
    price from Stripe. It does not compare any tfvars value, DB row or Stripe
    object - the live tfvars are gitignored, so that comparison stays manual.
    """

    # Declared by the terraform variable but deliberately not written to
    # subscription_plans by the seed script.
    UNMAPPED_BY_DESIGN = {
        "id",  # passed positionally as the primary key, not through plan_fields
        "storage_quota_gb",  # quota comes from StorageQuota, not this table
    }

    @staticmethod
    def _terraform_plan_fields():
        """Top-level attribute names of the ``subscription_plans`` object type.

        Only the outermost level: ``features`` is itself
        ``map(list(object({text, isPremium})))``, and those inner names are not
        columns of ``subscription_plans``.
        """
        block = re.search(
            r'variable\s+"subscription_plans"\s*\{.*?^\}',
            _read_source(MAIN_TF),
            re.DOTALL | re.MULTILINE,
        )
        assert block, 'variable "subscription_plans" not found in main.tf'
        object_body = re.search(
            r"list\(object\(\{(.*)\n\s*\}\)\)", block.group(), re.DOTALL
        )
        assert object_body, "subscription_plans is no longer a list(object({...}))"

        lines = object_body.group(1).split("\n")
        indents = [
            len(line) - len(line.lstrip())
            for line in lines
            if re.match(r"^\s*\w+\s*=\s*\S", line)
        ]
        assert indents, "no attributes parsed from the subscription_plans object type"
        top_level = min(indents)

        return {
            match.group(1)
            for line in lines
            if (match := re.match(r"^\s*(\w+)\s*=\s*\S", line))
            and len(line) - len(line.lstrip()) == top_level
        }

    @staticmethod
    def _seed_mapped_fields():
        mapping = re.search(
            r"plan_fields\s*=\s*\{(.*?)\n\s*\}", _read_source(SEED_SCRIPT), re.DOTALL
        )
        assert mapping, "plan_fields mapping not found in seed_subscription_plans.py"
        return set(re.findall(r'"(\w+)":', mapping.group(1)))

    def test_every_configured_field_is_written_by_the_seed(self):
        declared = self._terraform_plan_fields()
        mapped = self._seed_mapped_fields()

        dropped = declared - mapped - self.UNMAPPED_BY_DESIGN
        assert not dropped, (
            f"SUBSCRIPTION_PLANS_CONFIG declares {sorted(dropped)} but "
            f"seed_subscription_plans.py never writes them, so the seeded "
            f"subscription_plans rows cannot agree with the config"
        )

    def test_the_seed_writes_nothing_the_config_does_not_declare(self):
        """The other direction: a mapped field with no config source silently
        seeds a default and diverges from what the tfvars say."""
        mapped = self._seed_mapped_fields()
        declared = self._terraform_plan_fields()

        invented = mapped - declared
        assert not invented, (
            f"seed_subscription_plans.py writes {sorted(invented)}, which "
            f"SUBSCRIPTION_PLANS_CONFIG does not declare"
        )


class TestWebhookRecordsThePurchase:
    """The purchase row's ``user_id`` and ``plan_id``.

    Stripe sends metadata as strings. ``subscription_user_purchase`` has integer
    foreign keys, so the conversion is load-bearing: a string that slipped
    through would either violate the constraint or record the purchase against
    the wrong row.
    """

    SESSION = {
        "id": "cs_test_completed",
        "customer": "cus_test",
        "payment_status": "paid",
        "subscription": "sub_test",
        "metadata": {"user_id": "42", "plan_id": "2", "plan_name": "Premium"},
    }

    def _run_webhook(self):
        db = MagicMock()
        # No prior purchase, so the duplicate-processing short circuit is skipped.
        db.query.return_value.join.return_value.filter.return_value.first.return_value = (  # noqa: E501
            None
        )

        with patch.object(
            CheckoutService, "get_subscription_plan", return_value=Mock(id=2)
        ), patch.object(
            CheckoutService, "get_or_create_stripe_provider", return_value=1
        ), patch.object(
            CheckoutService, "create_or_update_user_account"
        ), patch.object(
            CheckoutService, "set_default_payment_method"
        ), patch.object(
            CheckoutService, "create_or_update_subscription", return_value=99
        ), patch.object(
            CheckoutService, "record_purchase", return_value=Mock(id=7)
        ) as record_purchase, patch(
            "studio.app.common.core.subscription.webhook_service.stripe"
        ) as mock_stripe, patch(
            "studio.app.common.core.subscription.webhook_service."
            "invalidate_user_tier_cache"
        ):
            mock_stripe.error = stripe.error
            mock_stripe.Subscription.retrieve.return_value = Mock(
                current_period_end=2_000_000_000, trial_end=None
            )
            result = WebhookService.handle_checkout_completed(db, self.SESSION)

        return record_purchase, result

    def test_purchase_is_recorded_against_the_session_user_and_plan(self):
        record_purchase, _ = self._run_webhook()

        record_purchase.assert_called_once()
        _, plan_id, user_id = record_purchase.call_args.args
        assert user_id == 42
        assert plan_id == 2

    def test_metadata_strings_are_converted_to_integers(self):
        """Not ``"42"`` and ``"2"``. The foreign keys are integers, and a string
        user_id would silently mis-key the row on a permissive driver."""
        record_purchase, _ = self._run_webhook()

        _, plan_id, user_id = record_purchase.call_args.args
        assert isinstance(user_id, int) and not isinstance(user_id, bool)
        assert isinstance(plan_id, int) and not isinstance(plan_id, bool)

    def test_the_recorded_purchase_id_is_returned_to_stripe(self):
        _, result = self._run_webhook()

        assert result["purchase_id"] == 7
        assert result["success"] is True

    def test_missing_metadata_is_rejected_before_anything_is_written(self):
        """A session with no metadata must not produce a purchase row keyed on
        ``None``."""
        from fastapi import HTTPException

        db = MagicMock()
        with patch.object(CheckoutService, "record_purchase") as record_purchase:
            with pytest.raises(HTTPException) as excinfo:
                WebhookService.handle_checkout_completed(
                    db, {**self.SESSION, "metadata": {}}
                )

        assert excinfo.value.status_code == 400
        record_purchase.assert_not_called()

    def test_non_numeric_metadata_is_rejected(self):
        from fastapi import HTTPException

        db = MagicMock()
        with patch.object(CheckoutService, "record_purchase") as record_purchase:
            with pytest.raises(HTTPException) as excinfo:
                WebhookService.handle_checkout_completed(
                    db,
                    {**self.SESSION, "metadata": {"user_id": "abc", "plan_id": "2"}},
                )

        assert excinfo.value.status_code == 400
        record_purchase.assert_not_called()


class TestSessionVerificationReadsTax:
    """``total_details`` / ``amount_tax`` are read from the session.

    ``verify_stripe_session`` is the only code in the repository that reads tax
    off a Stripe session, and it currently has **no caller** - see the note in
    ``SYSTEM_TEST_COVERAGE.md`` against this row. These cases pin the extraction
    so it cannot rot before it is wired up; that the tax figure reaches a user or
    a database column is not asserted, because today it does not.
    """

    @staticmethod
    def _session(**total_details):
        session = Mock(
            customer="cus_test",
            payment_status="paid",
            amount_total=2200,
            amount_subtotal=2000,
            currency="jpy",
            metadata={},
        )
        session.total_details = Mock(**total_details) if total_details else None
        return session

    def test_amount_tax_is_read_from_total_details(self):
        session = self._session(amount_tax=200, breakdown=Mock(taxes=[{"amount": 200}]))

        with patch("stripe.checkout.Session.retrieve", return_value=session):
            result = CheckoutService.verify_stripe_session("cs_test")

        assert result["amount_tax"] == 200
        assert result["tax_details"] == [{"amount": 200}]

    def test_total_details_is_expanded_in_the_retrieve_call(self):
        """Stripe omits ``total_details`` unless it is expanded, so without this
        the tax read would silently see nothing."""
        with patch(
            "stripe.checkout.Session.retrieve", return_value=self._session()
        ) as retrieve:
            CheckoutService.verify_stripe_session("cs_test")

        assert retrieve.call_args.kwargs["expand"] == ["total_details"]

    def test_a_session_with_no_tax_reports_zero_not_none(self):
        """The subtotal / total / tax triple is arithmetic downstream; ``None``
        would make it raise rather than read as an untaxed sale."""
        with patch("stripe.checkout.Session.retrieve", return_value=self._session()):
            result = CheckoutService.verify_stripe_session("cs_test")

        assert result["amount_tax"] == 0
        assert result["tax_details"] is None

    def test_the_amounts_are_carried_through_unmodified(self):
        session = self._session(amount_tax=200, breakdown=Mock(taxes=[]))

        with patch("stripe.checkout.Session.retrieve", return_value=session):
            result = CheckoutService.verify_stripe_session("cs_test")

        assert result["amount_total"] == 2200
        assert result["amount_subtotal"] == 2000
        assert result["currency"] == "jpy"


class TestPurchaseRowSurvivesTheDatabase:
    """The insert, against a real database.

    ``TestWebhookRecordsThePurchase`` patches ``record_purchase`` out entirely,
    so no row is ever written and "no constraint or foreign-key errors" is
    unobservable from it. These cases run the real insert.
    """

    PLAN_PREMIUM = 2
    USER_ID = 42

    @pytest.fixture()
    def db(self):
        from studio.app.common.models.subscription import SubscriptionUserPurchase
        from studio.tests.app.common.sqlite_harness import sqlite_session

        with sqlite_session([SubscriptionUserPurchase.__table__]) as session:
            yield session

    def test_the_purchase_row_is_inserted_with_integer_keys(self, db):
        from studio.app.common.models.subscription import SubscriptionUserPurchase

        purchase = CheckoutService.record_purchase(db, self.PLAN_PREMIUM, self.USER_ID)
        db.commit()
        db.expire_all()

        row = db.query(SubscriptionUserPurchase).one()
        assert row.id == purchase.id
        assert row.user_id == self.USER_ID
        assert row.plan_id == self.PLAN_PREMIUM
        assert isinstance(row.user_id, int) and isinstance(row.plan_id, int)
        assert row.created_at is not None
        assert row.updated_at is not None

    def test_a_null_key_is_refused_by_the_column_constraint(self, db):
        """The other direction: the NOT NULL constraints the row's "no
        constraint errors" criterion depends on are really enforced, so the
        positive case above is not passing on a permissive schema."""
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            CheckoutService.record_purchase(db, None, self.USER_ID)

    def test_two_purchases_for_one_user_are_both_kept(self, db):
        """Purchase history is append-only: a renewal must not collide with the
        original purchase."""
        from studio.app.common.models.subscription import SubscriptionUserPurchase

        first = CheckoutService.record_purchase(db, self.PLAN_PREMIUM, self.USER_ID)
        second = CheckoutService.record_purchase(db, self.PLAN_PREMIUM, self.USER_ID)
        db.commit()

        assert first.id != second.id
        assert db.query(SubscriptionUserPurchase).count() == 2


class TestTheWebhookItselfWritesThePurchaseRow:
    """The whole ``checkout.session.completed`` handler, over a database.

    ``TestWebhookRecordsThePurchase`` above patches ``record_purchase`` out, so it
    proves the arguments and nothing about the row; the class above it calls
    ``record_purchase`` directly, so it proves the row and nothing about the
    handler reaching it. Only Stripe is stubbed here, and every write the handler
    performs lands in the same session it is handed.
    """

    PLAN_PREMIUM = 2
    STRIPE_CUSTOMER = "cus_row_917"
    PERIOD_END = 1_795_000_000

    @pytest.fixture()
    def db(self):
        from studio.app.common.models import User as UserModel
        from studio.app.common.models.subscription import (
            SubscriptionPlans,
            SubscriptionProvider,
            SubscriptionUserAccount,
            SubscriptionUserPurchase,
            UserStorageUsage,
            UserSubscription,
        )
        from studio.tests.app.common.sqlite_harness import sqlite_session

        with sqlite_session(
            [
                UserModel.__table__,
                SubscriptionPlans.__table__,
                SubscriptionProvider.__table__,
                SubscriptionUserAccount.__table__,
                SubscriptionUserPurchase.__table__,
                UserSubscription.__table__,
                UserStorageUsage.__table__,
            ]
        ) as session:
            session.add(
                SubscriptionPlans(
                    id=self.PLAN_PREMIUM,
                    name="Premium",
                    price=20,
                    billing_cycle=1,
                    features={},
                    status=True,
                    currency=1,
                )
            )
            session.commit()
            yield session

    @pytest.fixture()
    def user_id(self, db):
        from studio.app.common.models import User as UserModel

        user = UserModel(
            organization_id=1,
            uid="uid-row-917",
            name="Buyer",
            email="buyer@example.com",
            attributes={},
            active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id

    def _run_webhook(self, db, user_id):
        session_data = {
            "id": "cs_row_917",
            "customer": self.STRIPE_CUSTOMER,
            "payment_status": "paid",
            "subscription": "sub_row_917",
            "metadata": {"user_id": str(user_id), "plan_id": str(self.PLAN_PREMIUM)},
        }

        with patch.object(CheckoutService, "set_default_payment_method"), patch(
            "studio.app.common.core.subscription.webhook_service."
            "stripe.Subscription.retrieve",
            return_value={
                "current_period_end": self.PERIOD_END,
                "trial_end": None,
                "current_period_start": self.PERIOD_END - 86_400,
            },
        ):
            return WebhookService.handle_checkout_completed(db, session_data)

    def test_one_purchase_row_is_written_for_the_session_user(self, db, user_id):
        from studio.app.common.models.subscription import SubscriptionUserPurchase

        result = self._run_webhook(db, user_id)
        db.expire_all()

        assert result["success"] is True
        row = db.query(SubscriptionUserPurchase).one()
        assert row.user_id == user_id
        assert row.plan_id == self.PLAN_PREMIUM
        assert row.created_at is not None
        assert result["purchase_id"] == row.id

    def test_the_purchase_the_subscription_and_the_account_all_land(self, db, user_id):
        """The handler commits once, at the end. A row set that is only
        partially present is the "constraint or foreign-key error" the sibling
        row describes, seen from the other side.
        """
        from studio.app.common.core.subscription.constants import STRIPE_PROVIDER_NAME
        from studio.app.common.models.subscription import (
            SubscriptionProvider,
            SubscriptionUserAccount,
            SubscriptionUserPurchase,
            UserSubscription,
        )

        # Creating the provider row commits on its own, so seed it and the
        # handler's own commit is the only one left to count.
        db.add(SubscriptionProvider(name=STRIPE_PROVIDER_NAME))
        db.commit()

        with patch.object(db, "commit", wraps=db.commit) as commit:
            self._run_webhook(db, user_id)
        db.expire_all()

        # Autoflush makes the rows below visible inside the open transaction, so
        # the commit is observable only as a call.
        assert commit.call_count == 1
        assert db.query(SubscriptionUserPurchase).count() == 1
        subscription = db.query(UserSubscription).one()
        assert subscription.plan_id == self.PLAN_PREMIUM
        assert int(subscription.expiration.timestamp()) == self.PERIOD_END
        account = db.query(SubscriptionUserAccount).one()
        assert account.user_id == user_id
        assert account.provider_customer_id == self.STRIPE_CUSTOMER

    def test_an_unpaid_session_writes_no_purchase_row(self, db, user_id):
        """The negative control for the count above: it has to be able to be 0."""
        from fastapi import HTTPException

        from studio.app.common.models.subscription import SubscriptionUserPurchase

        with patch.object(CheckoutService, "set_default_payment_method"):
            with pytest.raises(HTTPException):
                WebhookService.handle_checkout_completed(
                    db,
                    {
                        "id": "cs_row_917_unpaid",
                        "customer": self.STRIPE_CUSTOMER,
                        "payment_status": "unpaid",
                        "subscription": "sub_row_917",
                        "metadata": {
                            "user_id": str(user_id),
                            "plan_id": str(self.PLAN_PREMIUM),
                        },
                    },
                )

        assert db.query(SubscriptionUserPurchase).count() == 0


class TestSeededPlanValuesMatchTheConfig:
    """The value half of the plan-config comparison.

    ``TestPlanConfigAgreesWithTheSeededRows`` compares *field-name* sets, so a
    seeded price of 200 where the config says 20 passes it. These cases run the
    seed script's own ``main()`` against an in-memory database and compare the
    written values. The Stripe-side comparison stays manual: the live tfvars are
    gitignored.
    """

    CONFIG = [
        {
            "id": 1,
            "name": "Free",
            "price": 0,
            "billing_cycle": 1,
            "currency": 1,
            "status": 1,
            "stripe_product_id": "prod_free",
            "stripe_price_id": "price_free",
        },
        {
            "id": 2,
            "name": "Premium",
            "price": 20,
            "billing_cycle": 1,
            "currency": 1,
            "status": 1,
            "stripe_product_id": "prod_premium",
            "stripe_price_id": "price_premium",
        },
    ]

    @staticmethod
    def _seed_module():
        import importlib.util

        spec = importlib.util.spec_from_file_location("seed_plans", SEED_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @pytest.fixture()
    def seeded(self):
        """Run ``main()`` with its engine redirected at the harness session."""
        import json as json_module

        from studio.app.common.models.subscription import SubscriptionPlans
        from studio.tests.app.common.sqlite_harness import sqlite_session

        module = self._seed_module()
        with sqlite_session([SubscriptionPlans.__table__]) as session:
            with patch.dict(
                "os.environ",
                {"SUBSCRIPTION_PLANS_CONFIG": json_module.dumps(self.CONFIG)},
            ), patch.object(module, "create_engine"), patch.object(
                module, "sessionmaker", return_value=lambda: session
            ), patch.object(
                module, "get_database_url", return_value="sqlite://"
            ), patch(
                "studio.app.common.db.config.get_ssl_creator", return_value=None
            ), patch.object(
                session, "close"
            ):
                module.main()

            session.expire_all()
            yield session

    def test_every_configured_value_is_written_verbatim(self, seeded):
        from studio.app.common.models.subscription import SubscriptionPlans

        rows = {row.id: row for row in seeded.query(SubscriptionPlans).all()}

        assert set(rows) == {plan["id"] for plan in self.CONFIG}
        for plan in self.CONFIG:
            row = rows[plan["id"]]
            assert row.name == plan["name"]
            assert row.price == plan["price"]
            assert row.billing_cycle == plan["billing_cycle"]
            assert row.currency == plan["currency"]
            assert row.status is bool(plan["status"])
            assert row.stripe_product_id == plan["stripe_product_id"]
            assert row.stripe_price_id == plan["stripe_price_id"]

    def test_the_premium_price_is_not_defaulted(self, seeded):
        """The specific failure the field-name test cannot see."""
        from studio.app.common.models.subscription import SubscriptionPlans

        premium = seeded.query(SubscriptionPlans).filter_by(id=2).one()

        assert premium.price == 20, "the seeded premium price diverged from the config"

    def test_no_seeded_plan_is_missing_a_stripe_id(self, seeded):
        """Seeded plans carry their Stripe ids, against the seeded harness
        rather than the deployed RDS.

        Both columns are nullable, so a dropped mapping seeds NULL rather than
        failing, and the first symptom is a checkout session created with no
        price.
        """
        from studio.app.common.models.subscription import SubscriptionPlans

        rows = seeded.query(SubscriptionPlans).all()

        assert len(rows) == len(self.CONFIG)
        assert [
            (row.id, row.stripe_product_id, row.stripe_price_id)
            for row in rows
            if row.stripe_product_id is None or row.stripe_price_id is None
        ] == []
