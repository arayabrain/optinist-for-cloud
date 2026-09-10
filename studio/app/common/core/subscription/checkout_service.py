from datetime import datetime
from typing import Any, Dict, Optional

import stripe
from dateutil.relativedelta import relativedelta
from fastapi import HTTPException
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from studio.app.common.core.logger import AppLogger
from studio.app.common.core.subscription.constants import (
    PAYMENT_METHOD_TYPE_CARD,
    PAYMENT_METHOD_TYPE_LINK,
    STRIPE_PROVIDER_NAME,
    StorageQuota,
    StripeSubscriptionStatus,
    SubscriptionActiveStatus,
    SubscriptionPeriods,
    SyncStatus,
)
from studio.app.common.core.subscription.subscription_service import SubscriptionService
from studio.app.common.core.utils.datetime_utils import datetime_from_timestamp
from studio.app.common.models.subscription import (
    SubscriptionPlans,
    SubscriptionProvider,
    SubscriptionUserAccount,
    SubscriptionUserPurchase,
    UserStorageUsage,
    UserSubscription,
)
from studio.app.common.models.user import User
from studio.app.common.schemas.subscriptions import (
    CreateCheckoutSessionRequest,
    CreateCheckoutSessionResponse,
)

logger = AppLogger.get_logger()


class CheckoutService:
    """Service class for handling checkout operations"""

    @staticmethod
    def get_existing_subscription(
        db: Session, user_id: int
    ) -> Optional[UserSubscription]:
        """
        Check if user has an active subscription

        Args:
            db: Database session
            user_id: Internal user ID

        Returns:
            UserSubscription object if active subscription exists, else None
        """
        return (
            db.query(UserSubscription)
            .filter(
                UserSubscription.user_id == user_id,
                UserSubscription.expiration
                > SubscriptionService.get_current_datetime(),
            )
            .first()
        )

    @staticmethod
    def verify_stripe_session(session_id: str) -> Dict[str, Any]:
        """
        Verify and retrieve Stripe checkout session details

        Args:
            session_id: Stripe checkout session ID

        Returns:
            Dict containing session data including tax information

        Raises:
            stripe.error.StripeError: If session is invalid or API error occurs
        """
        try:
            session = stripe.checkout.Session.retrieve(
                session_id, expand=["total_details"]
            )

            # Extract tax information if available
            tax_amount = 0
            tax_details = None
            if hasattr(session, "total_details") and session.total_details:
                tax_amount = getattr(session.total_details, "amount_tax", 0)
                if hasattr(session.total_details, "breakdown"):
                    tax_details = getattr(
                        session.total_details.breakdown, "taxes", None
                    )

            return {
                "customer_id": session.customer,
                "payment_status": session.payment_status,
                "amount_total": session.amount_total,
                "amount_subtotal": getattr(session, "amount_subtotal", None),
                "amount_tax": tax_amount,
                "currency": session.currency,
                "metadata": session.metadata or {},
                "tax_details": tax_details,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe API error: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Error processing subscription_schedule.released webhook: "
                    f"{str(e)}"
                ),
            )

    @staticmethod
    def get_or_create_stripe_provider(db: Session) -> int:
        """
        Get existing Stripe provider or create new one

        Args:
            db: Database session

        Returns:
            Provider ID for Stripe
        """
        provider = (
            db.query(SubscriptionProvider)
            .filter(SubscriptionProvider.name == STRIPE_PROVIDER_NAME)
            .first()
        )

        if not provider:
            provider = SubscriptionProvider(name=STRIPE_PROVIDER_NAME)
            db.add(provider)
            db.commit()
            db.refresh(provider)

        return provider.id

    @staticmethod
    def get_subscription_plan(db: Session, plan_id: int) -> Optional[SubscriptionPlans]:
        """
        Get active subscription plan by ID

        Args:
            db: Database session
            plan_id: Subscription plan ID

        Returns:
            SubscriptionPlan object or None if not found
        """
        return (
            db.query(SubscriptionPlans)
            .filter(
                SubscriptionPlans.id == plan_id,
                SubscriptionPlans.status == SubscriptionActiveStatus.ACTIVE,
            )
            .first()
        )

    @staticmethod
    def calculate_expiration_date(billing_cycle: int = 1) -> datetime:
        """
        Calculate subscription expiration date based on billing cycle

        Args:
            billing_cycle: Billing cycle in months (default: 1)

        Returns:
            Expiration datetime
        """
        return SubscriptionService.get_current_datetime() + relativedelta(
            months=billing_cycle
        )

    @staticmethod
    def create_or_update_user_account(
        db: Session, user_id: int, provider_id: int, customer_id: str
    ) -> SubscriptionUserAccount:
        """
        Create or update user account with payment provider.

        Handles re-registration: if a user deletes their account and
        re-registers with the same email, the Stripe customer ID is reused.
        The old row (from the deleted user) is reassigned to the new user_id
        to satisfy the unique constraint on provider_customer_id.

        Args:
            db: Database session
            user_id: Internal user ID
            provider_id: Payment provider ID
            customer_id: Provider's customer ID

        Returns:
            SubscriptionUserAccount object
        """
        user_account = (
            db.query(SubscriptionUserAccount)
            .filter(
                SubscriptionUserAccount.user_id == user_id,
                SubscriptionUserAccount.provider_id == provider_id,
            )
            .first()
        )

        if user_account:
            user_account.provider_customer_id = customer_id
            user_account.updated_at = SubscriptionService.get_current_datetime()
            return user_account

        # Check if this Stripe customer ID already exists (e.g., from a
        # deleted user who re-registered with the same email). Reassign
        # the row to the new user instead of creating a duplicate.
        existing_by_customer = (
            db.query(SubscriptionUserAccount)
            .filter(
                SubscriptionUserAccount.provider_customer_id == customer_id,
            )
            .first()
        )

        if existing_by_customer:
            logger.info(
                f"Reassigning Stripe customer {customer_id} from "
                f"user {existing_by_customer.user_id} to user {user_id} "
                f"(re-registration)"
            )
            existing_by_customer.user_id = user_id
            existing_by_customer.provider_id = provider_id
            existing_by_customer.updated_at = SubscriptionService.get_current_datetime()
            return existing_by_customer

        user_account = SubscriptionUserAccount(
            user_id=user_id,
            provider_id=provider_id,
            provider_customer_id=customer_id,
        )
        db.add(user_account)
        return user_account

    @staticmethod
    def set_default_payment_method(session_id: str, customer_id: str):
        try:
            # Retrieve the full session from Stripe
            checkout_session = stripe.checkout.Session.retrieve(session_id)

            payment_method_id = None

            # For subscription mode checkouts, get payment method from subscription
            if checkout_session.get("mode") == "subscription" and checkout_session.get(
                "subscription"
            ):
                subscription_id = checkout_session["subscription"]

                # Retrieve the subscription to get the payment method
                subscription = stripe.Subscription.retrieve(subscription_id)

                if subscription.default_payment_method:
                    payment_method_id = subscription.default_payment_method
                elif len(subscription.items.data) > 0:
                    # Fallback: get from subscription items if available
                    payment_method_id = getattr(
                        subscription.items.data[0], "default_payment_method", None
                    )

            # For payment mode checkouts, get from payment_intent
            elif checkout_session.get("payment_intent"):
                payment_intent = stripe.PaymentIntent.retrieve(
                    checkout_session["payment_intent"], expand=["payment_method"]
                )
                if payment_intent.payment_method:
                    payment_method_id = payment_intent.payment_method.id

            # Alternative approach: Get the most recent payment method for the customer
            if not payment_method_id:
                # Get payment methods attached to this customer (try card first,
                # then link)
                for pm_type in [PAYMENT_METHOD_TYPE_CARD, PAYMENT_METHOD_TYPE_LINK]:
                    payment_methods = stripe.PaymentMethod.list(
                        customer=customer_id, type=pm_type, limit=1
                    )
                    if payment_methods.data:
                        payment_method_id = payment_methods.data[0].id
                        logger.info(
                            f"Using most recent {pm_type} payment method "
                            f"{payment_method_id} for customer {customer_id}"
                        )
                        break

            if payment_method_id:
                try:
                    stripe.PaymentMethod.attach(
                        payment_method_id,
                        customer=customer_id,
                    )
                except stripe.error.InvalidRequestError as e:
                    if "already been attached" in str(e):
                        logger.info(
                            f"Payment method {payment_method_id} already attached "
                            f"to customer {customer_id}"
                        )
                    else:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Failed to attach payment method: {str(e)}",
                        )

                # Update customer's default payment method
                stripe.Customer.modify(
                    customer_id,
                    invoice_settings={"default_payment_method": payment_method_id},
                )

                logger.info(
                    f"Webhook: Set payment method {payment_method_id} as default "
                    f"for customer {customer_id}"
                )
            else:
                logger.warning(
                    f"Webhook: Could not find any payment method for session "
                    f"{session_id} and customer {customer_id}"
                )

        except stripe.error.StripeError as e:
            logger.error(
                f"Webhook: Failed to set default payment method for customer "
                f"{customer_id}: {str(e)}"
            )
            # Don't fail the entire webhook for this - continue processing
        except Exception as e:
            logger.error(
                f"Webhook: Unexpected error setting default payment method: {str(e)}"
            )

    @staticmethod
    def create_or_update_subscription(
        db: Session, user_id: int, plan_id: int, expiration_date: datetime
    ) -> int:
        """
        Create new subscription or update existing one.

        Uses SELECT FOR UPDATE to prevent race conditions from concurrent
        webhook calls creating duplicate records.

        Args:
            db: Database session
            user_id: Internal user ID
            plan_id: Subscription plan ID
            expiration_date: When subscription expires

        Returns:
            Subscription user ID
        """
        # Use with_for_update() to lock the row and prevent race conditions
        existing_subscription = (
            db.query(UserSubscription)
            .filter(
                UserSubscription.user_id == user_id,
            )
            .with_for_update()
            .first()
        )

        if existing_subscription:
            return CheckoutService._apply_subscription_update(
                existing_subscription, plan_id, expiration_date
            )

        # Create new subscription. ``subscription_users.user_id`` is unique,
        # and SELECT ... FOR UPDATE does not lock a row that doesn't exist
        # yet — so concurrent webhook deliveries (e.g. ``checkout.session.
        # completed`` racing ``customer.subscription.created``) can both
        # reach this branch and one will hit IntegrityError. Guard the
        # insert with a SAVEPOINT and, on conflict, fall back to selecting
        # + updating the row that the other delivery just created. This
        # makes the upsert idempotent and avoids a spurious 500 -> Stripe
        # retry.
        try:
            with db.begin_nested():
                new_subscription = UserSubscription(
                    plan_id=plan_id,
                    user_id=user_id,
                    expiration=expiration_date,
                    sync_status=SyncStatus.SYNCED,
                )
                db.add(new_subscription)
                db.flush()  # Get ID without committing
            return new_subscription.id
        except IntegrityError:
            logger.warning(
                "Concurrent insert detected for subscription_users "
                f"user_id={user_id}; falling back to update"
            )
            existing_subscription = (
                db.query(UserSubscription)
                .filter(UserSubscription.user_id == user_id)
                .with_for_update()
                .first()
            )
            if existing_subscription is None:
                # The conflict implies a row exists; if we still can't find
                # it, surface the original error rather than swallow it.
                raise
            return CheckoutService._apply_subscription_update(
                existing_subscription, plan_id, expiration_date
            )

    @staticmethod
    def _apply_subscription_update(
        subscription: UserSubscription, plan_id: int, expiration_date: datetime
    ) -> int:
        """Apply the standard field updates to an existing subscription row."""
        subscription.plan_id = plan_id
        subscription.sync_status = SyncStatus.SYNCED
        subscription.expiration = expiration_date
        subscription.scheduled_downgrade = False
        subscription.updated_at = SubscriptionService.get_current_datetime()
        return subscription.id

    @staticmethod
    def get_subscription_account(
        db: Session, user_id: int
    ) -> Optional[SubscriptionUserAccount]:
        """
        Retrieve subscription user account by user ID

        Args:
            db: Database session
            user_id: Internal user ID
            provider_id: Payment provider ID

        Returns:
            SubscriptionUserAccount object or None if not found
        """
        return (
            db.query(SubscriptionUserAccount)
            .filter(
                SubscriptionUserAccount.user_id == user_id,
            )
            .first()
        )

    @staticmethod
    def has_stripe_purchase_history(customer_id: str) -> bool:
        """
        Check if a Stripe customer has any previous purchase history

        Args:
            customer_id: Stripe customer ID

        Returns:
            True if customer has any successful payments, False otherwise
        """
        try:
            # Check for any successful charges
            charges = stripe.Charge.list(customer=customer_id, limit=1)
            if charges.data:
                logger.debug(
                    f"Found {len(charges.data)} charge(s) for customer {customer_id}"
                )
                return True

            # Check for any subscriptions (past or present)
            subscriptions = stripe.Subscription.list(customer=customer_id, limit=1)
            if subscriptions.data:
                logger.debug(
                    f"Found {len(subscriptions.data)} subscription(s) "
                    f"for customer {customer_id}"
                )
                return True

            # Check for any invoices that were paid
            invoices = stripe.Invoice.list(customer=customer_id, status="paid", limit=1)
            if invoices.data:
                logger.debug(
                    f"Found {len(invoices.data)} paid invoice(s) "
                    f"for customer {customer_id}"
                )
                return True

            logger.info(
                f"No purchase history found in Stripe for customer {customer_id}"
            )
            return False

        except stripe.error.StripeError as e:
            logger.error(
                f"Stripe API error while checking purchase history for "
                f"customer {customer_id}: {str(e)}"
            )
            # In case of error, assume they have purchase history to be safe
            # (we don't want to give a trial if we can't verify)
            return True

    @staticmethod
    def record_purchase(
        db: Session, plan_id: int, user_id: int
    ) -> SubscriptionUserPurchase:
        """
        Record a new purchase in purchase history

        Args:
            db: Database session
            plan_id: Subscription plan ID
            user_id: Internal user ID

        Returns:
            SubscriptionUserPurchase object
        """
        purchase = SubscriptionUserPurchase(plan_id=plan_id, user_id=user_id)
        db.add(purchase)
        db.flush()  # Get ID without committing
        return purchase

    @staticmethod
    def recover_existing_stripe_subscription(
        db: Session, user_id: int, customer_id: str, plan_id: int
    ) -> bool:
        """
        Check if the user already has an active/trialing subscription in Stripe
        that was not synced to the database (e.g., server was down when webhook
        was sent). If found, sync it to the database to prevent duplicate
        subscriptions.

        Args:
            db: Database session
            user_id: Internal user ID
            customer_id: Stripe customer ID
            plan_id: Subscription plan ID being requested

        Returns:
            True if an existing subscription was recovered, False otherwise
        """
        try:
            # Check for active or trialing subscriptions in Stripe
            for status in [
                StripeSubscriptionStatus.ACTIVE,
                StripeSubscriptionStatus.TRIAL,
            ]:
                stripe_subscriptions = stripe.Subscription.list(
                    customer=customer_id, status=status, limit=1
                )
                if stripe_subscriptions.data:
                    stripe_sub = stripe_subscriptions.data[0]
                    logger.info(
                        f"Found existing Stripe subscription {stripe_sub.id} "
                        f"(status={status}) for user {user_id}. "
                        f"Recovering missed webhook."
                    )

                    # Determine expiration from Stripe subscription
                    trial_end = getattr(stripe_sub, "trial_end", None)
                    current_period_end = getattr(stripe_sub, "current_period_end", None)

                    if trial_end:
                        expiration_date = datetime_from_timestamp(trial_end)
                    elif current_period_end:
                        expiration_date = datetime_from_timestamp(current_period_end)
                    else:
                        logger.warning(
                            f"No expiration data in Stripe subscription "
                            f"{stripe_sub.id}, skipping recovery"
                        )
                        return False

                    # Determine the plan from stripe subscription metadata
                    sub_metadata = getattr(stripe_sub, "metadata", {}) or {}
                    recovered_plan_id = sub_metadata.get("plan_id")
                    if recovered_plan_id:
                        recovered_plan_id = int(recovered_plan_id)
                    else:
                        recovered_plan_id = plan_id

                    # Sync to database: create/update subscription
                    CheckoutService.create_or_update_subscription(
                        db, user_id, recovered_plan_id, expiration_date
                    )

                    # Ensure provider and account are linked
                    provider_id = CheckoutService.get_or_create_stripe_provider(db)
                    CheckoutService.create_or_update_user_account(
                        db, user_id, provider_id, customer_id
                    )

                    # Record purchase if not already recorded
                    existing_purchase = (
                        db.query(SubscriptionUserPurchase)
                        .filter(
                            SubscriptionUserPurchase.user_id == user_id,
                            SubscriptionUserPurchase.plan_id == recovered_plan_id,
                        )
                        .first()
                    )
                    if not existing_purchase:
                        CheckoutService.record_purchase(db, recovered_plan_id, user_id)

                    # Update storage quota
                    storage_quota_bytes = StorageQuota.bytes_for_plan(recovered_plan_id)
                    db.execute(
                        mysql_insert(UserStorageUsage)
                        .values(
                            user_id=user_id,
                            storage_usage_bytes=0,
                            storage_quota_bytes=storage_quota_bytes,
                        )
                        .on_duplicate_key_update(
                            storage_quota_bytes=storage_quota_bytes
                        )
                    )

                    db.commit()

                    # Set default payment method from the recovered subscription
                    try:
                        payment_method_id = None

                        # Try getting payment method from the subscription
                        if stripe_sub.default_payment_method:
                            payment_method_id = stripe_sub.default_payment_method
                        else:
                            # Fallback: get the most recent payment method
                            # attached to the customer
                            for pm_type in [
                                PAYMENT_METHOD_TYPE_CARD,
                                PAYMENT_METHOD_TYPE_LINK,
                            ]:
                                payment_methods = stripe.PaymentMethod.list(
                                    customer=customer_id, type=pm_type, limit=1
                                )
                                if payment_methods.data:
                                    payment_method_id = payment_methods.data[0].id
                                    break

                        if payment_method_id:
                            stripe.Customer.modify(
                                customer_id,
                                invoice_settings={
                                    "default_payment_method": payment_method_id
                                },
                            )
                            logger.info(
                                f"Recovery: Set payment method "
                                f"{payment_method_id} as default "
                                f"for customer {customer_id}"
                            )
                        else:
                            logger.warning(
                                f"Recovery: No payment method found for "
                                f"customer {customer_id}"
                            )
                    except Exception as e:
                        logger.warning(
                            f"Recovery: Failed to set default payment "
                            f"method for customer {customer_id}: {str(e)}"
                        )
                        # Non-critical, continue with recovery

                    # Invalidate cache
                    try:
                        from studio.app.common.core.middleware.secure_routing_middleware import (  # noqa: E501
                            invalidate_user_tier_cache,
                        )

                        user_record = db.query(User).filter(User.id == user_id).first()
                        if user_record:
                            invalidate_user_tier_cache(user_record.uid)
                    except Exception:
                        pass  # Cache invalidation is best-effort

                    logger.info(
                        f"Successfully recovered Stripe subscription "
                        f"{stripe_sub.id} for user {user_id}. "
                        f"Expiration: {expiration_date}"
                    )
                    return True

            return False

        except stripe.error.StripeError as e:
            logger.error(
                f"Stripe API error during subscription recovery for "
                f"user {user_id}: {str(e)}"
            )
            return False
        except Exception as e:
            logger.error(
                f"Error during subscription recovery for " f"user {user_id}: {str(e)}"
            )
            db.rollback()
            return False

    @staticmethod
    async def handle_checkout_session(
        db: Session, request: CreateCheckoutSessionRequest, user: User
    ) -> CreateCheckoutSessionResponse:
        try:
            # Get subscription plan from database using plan_id as string
            plan = SubscriptionService.get_plan_by_id(db, int(request.plan_id))

            if not plan:
                raise HTTPException(
                    status_code=404, detail="Subscription plan not found"
                )

            # Validate that the plan has Stripe product and price IDs
            if not plan.stripe_price_id:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Subscription plan '{plan.name}' is not configured "
                        f"with a Stripe price ID"
                    ),
                )

            logger.info(
                f"Request details - plan: {plan.name}, "
                f"stripe_product_id: {plan.stripe_product_id}, "
                f"stripe_price_id: {plan.stripe_price_id}"
            )

            # Create Stripe checkout session using the price ID from the database
            try:
                logger.debug("Initializing Stripe")

                # Unified customer lookup: DB first, then Stripe API, then
                # create. Prevents duplicate Stripe customers.
                from studio.app.common.core.subscription.stripe_service import (
                    get_or_create_stripe_customer,
                )

                stripe_customer = await get_or_create_stripe_customer(db, user)
                customer_id = stripe_customer.id

                # Check if user already has an active subscription in Stripe
                # that wasn't synced to DB (e.g., server was down during
                # webhook). If found, recover it to prevent duplicate
                # subscriptions.
                existing_db_subscription = CheckoutService.get_existing_subscription(
                    db, user.id
                )
                if not existing_db_subscription:
                    recovered = CheckoutService.recover_existing_stripe_subscription(
                        db,
                        user.id,
                        customer_id,
                        int(request.plan_id),
                    )
                    if recovered:
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "You are already a premium user. "
                                "Due to a temporary system issue, your "
                                "subscription was not reflected in your "
                                "account. It has now been restored."
                            ),
                        )

                # Check if user has any previous purchase history
                # Check both database and Stripe to ensure we don't miss any purchases
                previous_purchase = SubscriptionService.get_user_subscription_purchase(
                    db, user.id
                )
                has_db_purchase = previous_purchase is not None
                has_stripe_purchase = CheckoutService.has_stripe_purchase_history(
                    customer_id
                )

                # User is first-time only if they have no purchase in either system
                is_first_time_user = not (has_db_purchase or has_stripe_purchase)

                # Prepare subscription parameters
                stripe_callback_url = SubscriptionService.get_base_url()
                subscription_params = {
                    "payment_method_types": [
                        PAYMENT_METHOD_TYPE_CARD,
                        PAYMENT_METHOD_TYPE_LINK,
                    ],
                    "line_items": [
                        {
                            "price": plan.stripe_price_id,
                            "quantity": 1,
                        }
                    ],
                    "mode": "subscription",
                    "success_url": (
                        f"{stripe_callback_url}/subscription/thanks"
                        "?session_id={CHECKOUT_SESSION_ID}"
                    ),
                    "cancel_url": f"{stripe_callback_url}/subscription",
                    "customer": customer_id,
                    "client_reference_id": str(user.id),
                    "metadata": {
                        "user_id": str(user.id),
                        "plan_id": request.plan_id,
                        "plan_name": plan.name,
                    },
                    # Enable automatic tax calculation
                    "automatic_tax": {"enabled": True},
                    # Collect customer address for tax calculation and save it
                    "billing_address_collection": "required",
                    "customer_update": {"address": "auto"},
                }

                # Add trial period for first-time users
                if is_first_time_user:
                    subscription_params["subscription_data"] = {
                        "trial_period_days": SubscriptionPeriods.TRIAL_PERIOD_DAYS,
                        "metadata": {
                            "is_trial": "true",
                            "trial_days": str(SubscriptionPeriods.TRIAL_PERIOD_DAYS),
                        },
                    }
                    # Require payment method during trial so it can be charged later
                    subscription_params["payment_method_collection"] = "always"
                    logger.debug(
                        f"Adding {SubscriptionPeriods.TRIAL_PERIOD_DAYS}-day trial "
                        f"for first-time user {user.id}"
                    )

                checkout_session = stripe.checkout.Session.create(**subscription_params)

                return CreateCheckoutSessionResponse(
                    checkout_url=checkout_session.url, session_id=checkout_session.id
                )

            except stripe.error.StripeError as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to create checkout session: {str(e)}",
                )

        except HTTPException:
            # Re-raise HTTPExceptions as-is (e.g., 404 plan not found, 400
            # missing Stripe price, 409 subscription recovery) so they are not
            # caught by the generic Exception handler below and wrapped in a 500.
            raise
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Internal server error: {str(e)}"
            )
