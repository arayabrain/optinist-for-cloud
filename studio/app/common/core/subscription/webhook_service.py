import os
from datetime import timedelta
from typing import Any, Dict

import stripe
from dateutil.relativedelta import relativedelta
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from studio.app.common.core.logger import AppLogger
from studio.app.common.core.middleware.secure_routing_middleware import (
    invalidate_user_tier_cache,
)
from studio.app.common.core.subscription.checkout_service import CheckoutService
from studio.app.common.core.subscription.constants import (
    DUPLICATE_PURCHASE_WINDOW_MINUTES,
    RECENT_SUBSCRIPTION_WINDOW_DAYS,
    CancellationReason,
    InvoiceStatus,
    PaymentStatus,
    StorageQuota,
    StorageSize,
    StripeWebhookEvent,
    SubscriptionCurrencyType,
    SubscriptionPlanIds,
    SyncStatus,
)
from studio.app.common.core.subscription.subscription_service import SubscriptionService
from studio.app.common.core.utils.datetime_utils import (
    datetime_from_timestamp,
    ensure_utc,
)
from studio.app.common.models.subscription import (
    SubscriptionCancellation,
    SubscriptionPlans,
    SubscriptionUserAccount,
    SubscriptionUserPurchase,
    UserStorageUsage,
    UserSubscription,
)
from studio.app.common.models.user import User

logger = AppLogger.get_logger()


class WebhookService:
    """Service class for handling Stripe webhooks"""

    _stripe_initialized = False

    @classmethod
    def _ensure_stripe_initialized(cls):
        """Lazy initialization of Stripe API key"""
        if not cls._stripe_initialized:
            try:
                stripe.api_key = SubscriptionService.get_stripe_key()
                cls._stripe_initialized = True
            except ValueError as e:
                logger.warning(f"Stripe not initialized: {e}")
                # Don't raise here - allow module to load for tests

    @staticmethod
    def handle_checkout_completed(
        db: Session, session_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Handle checkout.session.completed webhook

        Args:
            db: Database session
            session_data: Webhook session data from Stripe

        Returns:
            Dict with processing results

        Raises:
            HTTPException: If validation fails or processing errors occur
        """
        try:
            session_id = session_data.get("id")
            logger.info(f"Webhook: Processing checkout session completed: {session_id}")

            # Extract data from webhook payload
            customer_id = session_data.get("customer")
            payment_status = session_data.get("payment_status")

            # Get metadata from the session (should contain user_id and plan_id)
            metadata = session_data.get("metadata", {})
            user_id = metadata.get("user_id")
            plan_id = metadata.get("plan_id")

            # Validate required data
            if not user_id or not plan_id:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Missing user_id or plan_id in session metadata: "
                        f"{session_id}"
                    ),
                )

            # Convert to integers
            try:
                user_id = int(user_id)
                plan_id = int(plan_id)
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Invalid user_id or plan_id format in session metadata: "
                        f"{session_id}"
                    ),
                )

            # 1. CHECK FOR DUPLICATE PROCESSING FIRST
            # Check if this session has already been processed
            existing_purchase = (
                db.query(SubscriptionUserPurchase)
                .join(
                    UserSubscription,
                    SubscriptionUserPurchase.user_id == UserSubscription.user_id,
                )
                .filter(
                    SubscriptionUserPurchase.user_id == user_id,
                    SubscriptionUserPurchase.plan_id == plan_id,
                    SubscriptionUserPurchase.created_at
                    > SubscriptionService.get_current_datetime()
                    - timedelta(minutes=DUPLICATE_PURCHASE_WINDOW_MINUTES),
                )
                .first()
            )

            if existing_purchase:
                # Find the corresponding subscription
                existing_subscription = (
                    db.query(UserSubscription)
                    .filter(
                        UserSubscription.user_id == user_id,
                        UserSubscription.plan_id == plan_id,
                        UserSubscription.expiration
                        > SubscriptionService.get_current_datetime(),
                    )
                    .first()
                )

                # Only treat as duplicate if there's an ACTIVE subscription
                # If subscription is expired/cancelled, allow new purchase to proceed
                if existing_subscription:
                    logger.info(
                        f"Webhook: Duplicate processing detected for user {user_id}, "
                        f"session {session_id}"
                    )
                    return {
                        "success": True,
                        "subscription_user_id": existing_subscription.id,
                        "purchase_id": existing_purchase.id,
                        "expiration_date": existing_subscription.expiration,
                        "message": "Subscription already processed successfully",
                        "webhook_processed": True,
                    }
                else:
                    logger.info(
                        f"Webhook: Found recent purchase but no active subscription "
                        f"for user {user_id}. Proceeding with new subscription."
                    )

            # 2. Verify payment status from webhook data
            if payment_status != PaymentStatus.PAID:
                raise HTTPException(
                    status_code=400,
                    detail=f"Payment not completed. Status: {payment_status}",
                )

            # 3. Get subscription plan
            plan = CheckoutService.get_subscription_plan(db, plan_id)
            if not plan:
                raise HTTPException(
                    status_code=404, detail=f"Subscription plan not found: {plan_id}"
                )

            # 4. Get or create Stripe provider
            stripe_provider_id = CheckoutService.get_or_create_stripe_provider(db)

            # 5. Create or update user account
            CheckoutService.create_or_update_user_account(
                db, user_id, stripe_provider_id, customer_id
            )

            # 6. SET DEFAULT PAYMENT METHOD
            CheckoutService.set_default_payment_method(session_id, customer_id)

            # 7. Get expiration date from Stripe subscription
            stripe_subscription_id = session_data.get("subscription")
            if not stripe_subscription_id:
                raise HTTPException(
                    status_code=400,
                    detail=f"No subscription ID found in session: {session_id}",
                )

            # Retrieve subscription from Stripe to get the current_period_end
            try:
                stripe_subscription = stripe.Subscription.retrieve(
                    stripe_subscription_id
                )

                # Log the full subscription object for debugging
                logger.info(
                    f"Webhook: Retrieved subscription {stripe_subscription_id}, "
                    f"status: {getattr(stripe_subscription, 'status', 'unknown')}"
                )

                # Access current_period_end - use getattr to handle both objects
                # and dicts
                current_period_end = getattr(
                    stripe_subscription, "current_period_end", None
                )
                if current_period_end is None and isinstance(stripe_subscription, dict):
                    current_period_end = stripe_subscription.get("current_period_end")

                # Check for trial end as well (in case of trial subscriptions)
                trial_end = getattr(stripe_subscription, "trial_end", None)
                if trial_end is None and isinstance(stripe_subscription, dict):
                    trial_end = stripe_subscription.get("trial_end")

                # Also check current_period_start for fallback calculation
                current_period_start = getattr(
                    stripe_subscription, "current_period_start", None
                )
                if current_period_start is None and isinstance(
                    stripe_subscription, dict
                ):
                    current_period_start = stripe_subscription.get(
                        "current_period_start"
                    )

                # Debug logging to see raw values
                logger.info(
                    f"Webhook: Raw subscription data - "
                    f"current_period_end: {current_period_end}, "
                    f"trial_end: {trial_end}, "
                    f"current_period_start: {current_period_start}, "
                    f"created: {getattr(stripe_subscription, 'created', None)}"
                )

                logger.info(
                    f"Webhook: Stripe subscription type: {type(stripe_subscription)}, "
                    f"current_period_end: {current_period_end}, trial_end: {trial_end},"
                    f"current_period_start: {current_period_start}"
                )

                # For trial subscriptions, use trial_end.
                # Otherwise use current_period_end
                expiration_timestamp = None
                if trial_end:
                    # Subscription is in trial period
                    expiration_timestamp = trial_end
                    logger.info(
                        f"Webhook: Subscription is in trial period, "
                        f"using trial_end: {trial_end}"
                    )
                elif current_period_end:
                    # Regular subscription
                    expiration_timestamp = current_period_end
                    logger.info(
                        f"Webhook: Regular subscription, "
                        f"using current_period_end: {current_period_end}"
                    )
                elif current_period_start:
                    # Fallback: calculate expiration as 1 month from start
                    # This handles edge case where subscription is just created
                    start_date = datetime_from_timestamp(current_period_start)
                    expiration_date = start_date + relativedelta(months=1)
                    expiration_timestamp = int(expiration_date.timestamp())
                    logger.warning(
                        f"Webhook: current_period_end not available, "
                        f"calculated from current_period_start: {expiration_date}"
                    )
                else:
                    # Final fallback: Try to get expiration from the latest invoice
                    logger.warning(
                        "Webhook: No period data found in subscription, "
                        "trying to get from latest invoice"
                    )
                    try:
                        latest_invoice_id = getattr(
                            stripe_subscription, "latest_invoice", None
                        )
                        if latest_invoice_id:
                            invoice = stripe.Invoice.retrieve(latest_invoice_id)
                            lines = invoice.get("lines", {}).get("data", [])
                            if lines:
                                period_end = lines[0].get("period", {}).get("end")
                                if period_end:
                                    expiration_timestamp = period_end
                                    logger.info(
                                        f"Webhook: Got expiration from invoice: "
                                        f"{datetime_from_timestamp(period_end)}"
                                    )
                                else:
                                    raise HTTPException(
                                        status_code=400,
                                        detail=(
                                            f"No period end found in invoice lines for "
                                            f"subscription: {stripe_subscription_id}"
                                        ),
                                    )
                            else:
                                raise HTTPException(
                                    status_code=400,
                                    detail=(
                                        f"No invoice lines found for subscription: "
                                        f"{stripe_subscription_id}"
                                    ),
                                )
                        else:
                            raise HTTPException(
                                status_code=400,
                                detail=(
                                    f"No expiration date found in subscription: "
                                    f"{stripe_subscription_id}"
                                ),
                            )
                    except stripe.error.StripeError as e:
                        logger.error(f"Webhook: Error retrieving invoice: {str(e)}")
                        raise HTTPException(
                            status_code=500,
                            detail=f"Error retrieving invoice: {str(e)}",
                        )

                # Convert Unix timestamp to datetime
                expiration_date = datetime_from_timestamp(expiration_timestamp)
                logger.info(
                    f"Webhook: Using expiration date from Stripe: {expiration_date} "
                    f"(Unix timestamp: {expiration_timestamp})"
                )

            except stripe.error.StripeError as e:
                logger.error(
                    f"Webhook: Stripe API error retrieving subscription: {str(e)}"
                )
                raise HTTPException(
                    status_code=500,
                    detail=f"Error retrieving subscription from Stripe: {str(e)}",
                )

            # 8. Create or update subscription
            subscription_user_id = CheckoutService.create_or_update_subscription(
                db, user_id, plan_id, expiration_date
            )

            # 9. Record purchase (optionally store session_id for reference)
            purchase = CheckoutService.record_purchase(db, plan_id, user_id)

            # 10. Update storage quota based on new subscription plan
            storage_quota_bytes = StorageQuota.bytes_for_plan(plan_id)
            db.execute(
                mysql_insert(UserStorageUsage)
                .values(
                    user_id=user_id,
                    storage_usage_bytes=0,
                    storage_quota_bytes=storage_quota_bytes,
                )
                .on_duplicate_key_update(storage_quota_bytes=storage_quota_bytes)
            )

            # 11. Commit all changes atomically
            db.commit()
            logger.info(
                f"Webhook: Updated storage quota for user {user_id} to "
                f"{storage_quota_bytes / StorageSize.GB:.0f}GB (plan_id={plan_id})"
            )

            # 12. Invalidate tier cache for immediate routing update
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                invalidate_user_tier_cache(user.uid)
                logger.info("Invalidated tier cache after premium upgrade for user")

            logger.info(
                f"Webhook: Successfully processed checkout for user {user_id}, "
                f"plan {plan_id}, session {session_id}"
            )

            return {
                "success": True,
                "subscription_user_id": subscription_user_id,
                "purchase_id": purchase.id,
                "expiration_date": expiration_date,
                "message": "Subscription activated successfully via webhook",
                "webhook_processed": True,
                "session_id": session_id,
            }

        except HTTPException as e:
            logger.error(
                f"Webhook: HTTPException processing checkout for session "
                f"{session_id}: {e.detail}"
            )
            db.rollback()
            raise  # Re-raise the original HTTPException with its details
        except Exception as e:
            logger.error(
                f"Webhook: Error processing checkout success for session "
                f"{session_id}: {str(e)}"
            )
            db.rollback()
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Error processing checkout success for session "
                    f"{session_id}: {str(e)}"
                ),
            )

    @staticmethod
    def handle_payment_failed(db: Session, invoice_data: Dict[str, Any]) -> None:
        """
        Handle invoice.payment_failed webhook

        Args:
            db: Database session
            invoice_data: Webhook invoice data
        """
        customer_id = invoice_data.get("customer")
        logger.warning(f"Webhook: Payment failed for customer: {customer_id}")

        # Find user account by customer ID
        user_account = (
            db.query(SubscriptionUserAccount)
            .filter(SubscriptionUserAccount.provider_customer_id == customer_id)
            .first()
        )

        if user_account:
            # Find active subscription and mark as failed
            subscription = (
                db.query(UserSubscription)
                .filter(
                    UserSubscription.user_id == user_account.user_id,
                    UserSubscription.expiration
                    > SubscriptionService.get_current_datetime(),
                )
                .first()
            )

            if subscription:
                subscription.sync_status = SyncStatus.FAILED
                subscription.updated_at = SubscriptionService.get_current_datetime()
                db.commit()
                logger.info(
                    "Marked subscription as failed for user %s",
                    user_account.user_id,
                )

                # Invalidate cache so user sees payment failure warning immediately
                user = db.query(User).filter(User.id == user_account.user_id).first()
                if user:
                    invalidate_user_tier_cache(user.uid)
                    logger.info("Invalidated tier cache after payment failure")

    @staticmethod
    def handle_subscription_cancelled(
        db: Session, subscription_data: Dict[str, Any]
    ) -> None:
        """
        Handle customer.subscription.deleted webhook

        Args:
            db: Database session
            subscription_data: Webhook subscription data
        """
        customer_id = subscription_data.get("customer")
        logger.info(f"Webhook: Subscription cancelled for customer: {customer_id}")
        stripe_subscription_id = subscription_data.get("id")
        logger.info(f"Webhook: Subscription cancelled: {stripe_subscription_id}")

        # Find user account by customer ID
        user_account = (
            db.query(SubscriptionUserAccount)
            .filter(SubscriptionUserAccount.provider_customer_id == customer_id)
            .first()
        )

        logger.info(f"Webhook: Found user account: {user_account}")

        if user_account:
            # Find active subscription and expire it
            subscription = (
                db.query(UserSubscription)
                .filter(
                    UserSubscription.user_id == user_account.user_id,
                    UserSubscription.expiration
                    > SubscriptionService.get_current_datetime(),
                )
                .first()
            )

            if subscription:
                # Expire subscription immediately
                subscription.expiration = SubscriptionService.get_current_datetime()
                subscription.updated_at = SubscriptionService.get_current_datetime()

                # Remove any scheduled downgrade
                subscription.scheduled_downgrade = False

                # Find the most recent purchase for this user and plan
                purchase = (
                    db.query(SubscriptionUserPurchase)
                    .filter(
                        SubscriptionUserPurchase.user_id == user_account.user_id,
                        SubscriptionUserPurchase.plan_id == subscription.plan_id,
                    )
                    .order_by(SubscriptionUserPurchase.created_at.desc())
                    .first()
                )

                # Only record cancellation if we have a purchase record
                if purchase:
                    # Record cancellation
                    cancellation = SubscriptionCancellation(
                        cancelled_by_user_id=user_account.user_id,
                        purchases_id=purchase.id,
                        reason=CancellationReason.USER_REQUEST,
                        notes=(
                            f"Cancelled via Stripe webhook for subscription "
                            f"{stripe_subscription_id}"
                        ),
                    )
                    db.add(cancellation)
                    logger.info(
                        f"Recorded cancellation for user {user_account.user_id}, "
                        f"purchase {purchase.id}"
                    )
                else:
                    logger.warning(
                        f"No purchase record found for user {user_account.user_id}, "
                        f"plan {subscription.plan_id}. Skipping cancellation record."
                    )

                db.commit()

                # Update storage quota to free tier
                storage_quota_bytes = StorageQuota.FREE * StorageSize.GB
                storage_record = (
                    db.query(UserStorageUsage)
                    .filter(UserStorageUsage.user_id == user_account.user_id)
                    .first()
                )
                if storage_record:
                    storage_record.storage_quota_bytes = storage_quota_bytes
                    db.add(storage_record)
                else:
                    db.add(
                        UserStorageUsage(
                            user_id=user_account.user_id,
                            storage_usage_bytes=0,
                            storage_quota_bytes=storage_quota_bytes,
                        )
                    )
                db.commit()
                logger.info(
                    f"Webhook: Updated storage quota for user "
                    f"{user_account.user_id} to "
                    f"{storage_quota_bytes / StorageSize.GB:.0f}GB "
                    f"(cancelled)"
                )

                # Invalidate cache so next request reflects free tier immediately
                user = db.query(User).filter(User.id == user_account.user_id).first()
                if user:
                    invalidate_user_tier_cache(user.uid)
                    logger.info(
                        "Invalidated tier cache after subscription cancellation"
                    )

                logger.info(f"Cancelled subscription for user {user_account.user_id}")

    @classmethod
    def handle_subscription_schedule_released(cls, db: Session, data: dict):
        """
        Handle when a subscription schedule is released (plan change executed)
        Event: subscription_schedule.released
        """
        try:
            logger.info("Processing subscription_schedule.released webhook")

            # Ensure Stripe is initialized
            SubscriptionService._ensure_stripe_initialized()

            # Get the subscription schedule data
            subscription_id = data.get("subscription")
            customer_id = data.get("customer")

            if not subscription_id:
                raise HTTPException(
                    status_code=400,
                    detail="No subscription ID in schedule released event",
                )

            # Get the subscription details from Stripe
            subscription = stripe.Subscription.retrieve(subscription_id)
            current_period_end = subscription["items"]["data"][0]["current_period_end"]

            # Find user by customer_id in DB first (most reliable),
            # then fall back to email lookup from Stripe customer.
            from studio.app.common.models.user import User

            user = None
            user_account = (
                db.query(SubscriptionUserAccount)
                .filter(SubscriptionUserAccount.provider_customer_id == customer_id)
                .first()
            )
            if user_account:
                user = (
                    db.query(User)
                    .filter(
                        User.id == user_account.user_id,
                        User.active.is_(True),
                    )
                    .first()
                )

            if not user:
                # Fall back to email lookup from Stripe customer
                customer = stripe.Customer.retrieve(customer_id)
                customer_email = customer.get("email")

                if not customer_email:
                    raise HTTPException(
                        status_code=400,
                        detail=f"No email found for customer {customer_id}",
                    )

                # Filter active=True so a soft-deleted user (active=0) with
                # the same email doesn't shadow the new active user after a
                # re-registration (issue #629 P5).
                user = (
                    db.query(User)
                    .filter(
                        User.email == customer_email,
                        User.active.is_(True),
                    )
                    .first()
                )

            if not user:
                raise HTTPException(
                    status_code=404,
                    detail=f"No active user found for customer {customer_id}",
                )

            # Find the plan by matching the price or metadata
            new_plan_id = None

            # Try to get plan_id from subscription metadata first
            if "plan_id" in subscription.get("metadata", {}):
                new_plan_id = int(subscription["metadata"]["plan_id"])
            else:
                # Fallback: find plan by price and currency
                price = subscription["items"]["data"][0]["price"]
                plan = (
                    db.query(SubscriptionPlans)
                    .filter(
                        SubscriptionPlans.price == price["unit_amount"],
                        SubscriptionPlans.currency
                        == SubscriptionCurrencyType.get_currency_enum(
                            price["currency"]
                        ),
                    )
                    .first()
                )

                if plan:
                    new_plan_id = plan.id

            if not new_plan_id:
                raise HTTPException(
                    status_code=400,
                    detail=f"Could not determine new plan ID for user {user.id}",
                )

            # Update the user's subscription in database
            # Find the active subscription for this user
            user_subscription = (
                db.query(UserSubscription)
                .filter(
                    UserSubscription.user_id == user.id,
                    UserSubscription.expiration
                    > SubscriptionService.get_current_datetime(),
                )
                .first()
            )

            if user_subscription:
                # Update the subscription with new plan details
                user_subscription.plan_id = new_plan_id
                user_subscription.updated_at = (
                    SubscriptionService.get_current_datetime()
                )
                user_subscription.expiration = datetime_from_timestamp(
                    current_period_end
                )
            else:
                raise HTTPException(
                    status_code=404,
                    detail=f"No active subscription found for user {user.id}",
                )

            db.commit()

            # Update storage quota based on new plan
            storage_quota_bytes = (
                StorageQuota.PREMIUM * StorageSize.GB
                if new_plan_id == SubscriptionPlanIds.PREMIUM
                else StorageQuota.FREE * StorageSize.GB
            )
            storage_record = (
                db.query(UserStorageUsage)
                .filter(UserStorageUsage.user_id == user.id)
                .first()
            )
            if storage_record:
                storage_record.storage_quota_bytes = storage_quota_bytes
                db.add(storage_record)
            else:
                db.add(
                    UserStorageUsage(
                        user_id=user.id,
                        storage_usage_bytes=0,
                        storage_quota_bytes=storage_quota_bytes,
                    )
                )
            db.commit()
            logger.info(
                f"Webhook: Updated storage quota for user {user.id} to "
                f"{storage_quota_bytes / StorageSize.GB:.0f}GB "
                f"(plan_id={new_plan_id})"
            )

            # Invalidate cache for immediate tier change
            invalidate_user_tier_cache(user.uid)
            logger.info("Invalidated tier cache after plan change")

            logger.info(
                f"Successfully updated subscription for user {user.id} to plan "
                f"{new_plan_id} via webhook"
            )

        except HTTPException:
            # bare raise: the generic arm below would turn this into a 500
            db.rollback()
            raise
        except Exception as e:
            logger.error(
                f"Error processing subscription_schedule.released webhook: {str(e)}"
            )
            db.rollback()
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Error processing subscription_schedule.released webhook: "
                    f"{str(e)}"
                ),
            )

    @staticmethod
    def handle_subscription_payment_succeeded(
        db: Session, invoice_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Handle invoice.payment_succeeded webhook for subscription renewals

        Args:
            db: Database session
            invoice_data: Webhook invoice data from Stripe

        Returns:
            Dict with processing results

        Raises:
            HTTPException: If validation fails or processing errors occur
        """
        try:
            invoice_id = invoice_data.get("id")
            logger.info(
                f"Webhook: Processing subscription payment succeeded: {invoice_id}"
            )

            # Extract data from webhook payload
            customer_id = invoice_data.get("customer")
            subscription_id = invoice_data.get("subscription")

            # If subscription_id is None, check in parent.subscription_details
            if not subscription_id:
                parent = invoice_data.get("parent", {})
                if parent.get("type") == "subscription_details":
                    subscription_details = parent.get("subscription_details", {})
                    subscription_id = subscription_details.get("subscription")

            payment_status = invoice_data.get("status")
            amount_paid = invoice_data.get("amount_paid", 0)
            billing_reason = invoice_data.get("billing_reason")

            logger.info(
                f"Webhook: customer_id={customer_id}, subscription_id={subscription_id}"
            )
            logger.info(
                f"Webhook: payment_status={payment_status}, amount={amount_paid}, "
                f"billing_reason={billing_reason}"
            )

            # Only process subscription cycle payments (not initial payments)
            if billing_reason not in ["subscription_cycle", "subscription_update"]:
                logger.info(
                    f"Webhook: Skipping invoice - billing_reason: {billing_reason}"
                )
                return {
                    "success": True,
                    "message": f"Invoice skipped - billing_reason: {billing_reason}",
                    "webhook_processed": True,
                    "skipped": True,
                }

            # Validate required data
            if not customer_id or not subscription_id:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Missing customer_id or subscription_id in invoice: "
                        f"{invoice_id}"
                    ),
                )

            # Verify payment was successful
            if payment_status != PaymentStatus.PAID:
                raise HTTPException(
                    status_code=400,
                    detail=f"Payment not completed. Status: {payment_status}",
                )

            # 1. Find user by Stripe customer ID
            try:
                logger.debug(f"Webhook: Finding user by customer_id: {customer_id}")
                logger.debug(f"Webhook: Invoice ID: {invoice_id}")

                user_account = (
                    db.query(SubscriptionUserAccount)
                    .filter(SubscriptionUserAccount.provider_customer_id == customer_id)
                    .first()
                )

                logger.debug(f"Webhook: User account query result: {user_account}")

                if not user_account:
                    logger.warning(
                        f"Webhook: No user account found for customer_id: {customer_id}"
                    )
                    logger.warning(
                        "Webhook: This likely means the user hasn't completed initial "
                        "checkout or the customer_id wasn't stored correctly. "
                        "This could be from test webhooks, incomplete checkouts, "
                        "or deleted users. Acknowledging webhook to prevent retries."
                    )
                    # Return success to acknowledge webhook and prevent Stripe retries
                    # This is normal for test data, incomplete checkouts, etc.
                    return {
                        "success": True,
                        "message": f"User not found for customer_id: {customer_id}",
                        "webhook_processed": True,
                        "skipped": True,
                        "reason": "missing_user_account",
                    }

                user_id = user_account.user_id
                logger.debug(f"Webhook: Found user_id: {user_id}")

            except HTTPException as http_exc:
                logger.error(f"Webhook: HTTPException finding user: {http_exc.detail}")
                raise  # Re-raise the original exception
            except Exception as e:
                logger.error(f"Webhook: Error finding user: {str(e)}")
                raise HTTPException(
                    status_code=500, detail=f"Error finding user: {str(e)}"
                )

            # 2. Find active or recently expired subscription
            # (for trial to paid conversion, the trial might have just expired)
            try:
                logger.debug(f"Webhook: Finding subscription for user_id: {user_id}")
                current_time = SubscriptionService.get_current_datetime()
                logger.debug(f"Webhook: Current datetime: {current_time}")

                # First try to find active subscription
                user_subscription = (
                    db.query(UserSubscription)
                    .filter(
                        UserSubscription.user_id == user_id,
                        UserSubscription.expiration > current_time,
                    )
                    .order_by(UserSubscription.expiration.desc())
                    .first()
                )

                # If no active subscription, check for recently expired ones
                # (within extended lookback window) - handles trial-to-paid conversion
                if not user_subscription:
                    logger.debug(
                        "Webhook: No active subscription found, checking for "
                        f"subscription within {RECENT_SUBSCRIPTION_WINDOW_DAYS} days"
                    )
                    user_subscription = (
                        db.query(UserSubscription)
                        .filter(
                            UserSubscription.user_id == user_id,
                            UserSubscription.expiration
                            > current_time
                            - timedelta(days=RECENT_SUBSCRIPTION_WINDOW_DAYS),
                        )
                        .order_by(UserSubscription.expiration.desc())
                        .first()
                    )

                # Fallback: Look up any subscription by user regardless of date
                if not user_subscription:
                    logger.warning(
                        "Webhook: No subscription within extended window, "
                        "trying fallback (any subscription for user)"
                    )
                    user_subscription = (
                        db.query(UserSubscription)
                        .filter(
                            UserSubscription.user_id == user_id,
                            UserSubscription.plan_id != SubscriptionPlanIds.FREE,
                        )
                        .order_by(UserSubscription.expiration.desc())
                        .first()
                    )

                logger.debug(f"Webhook: Subscription query result: {user_subscription}")

                if not user_subscription:
                    logger.error(f"Webhook: No subscription found for user: {user_id}")
                    logger.error(
                        "Webhook: No subscription found at all for this user. "
                        "This means the subscription wasn't created during checkout."
                    )
                    raise HTTPException(
                        status_code=404,
                        detail=f"Subscription not found for user: {user_id}",
                    )

                plan_id = user_subscription.plan_id
                logger.debug(f"Webhook: Found subscription plan_id: {plan_id}")
                logger.debug(
                    f"Webhook: Current expiration: {user_subscription.expiration}"
                )

            except HTTPException:
                # bare raise: the generic arm below would turn this into a 500
                raise
            except Exception as e:
                logger.error(f"Webhook: Error finding subscription: {str(e)}")
                raise HTTPException(
                    status_code=500, detail=f"Error finding subscription: {str(e)}"
                )

            # 3. Get subscription plan details
            try:
                logger.debug(f"Webhook: Getting subscription plan: {plan_id}")
                plan = CheckoutService.get_subscription_plan(db, plan_id)
                if not plan:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Subscription plan not found: {plan_id}",
                    )

            except HTTPException:
                # bare raise: the generic arm below would turn this into a 500
                raise
            except Exception as e:
                logger.error(f"Webhook: Error getting subscription plan: {str(e)}")
                raise HTTPException(
                    status_code=500, detail=f"Error getting subscription plan: {str(e)}"
                )

            # 4. Get expiration date from invoice line items
            try:
                logger.debug("Webhook: Getting expiration date from invoice...")

                current_expiration = user_subscription.expiration

                # Get the period end from invoice line items
                lines = invoice_data.get("lines", {}).get("data", [])
                if not lines:
                    raise HTTPException(
                        status_code=400,
                        detail=f"No line items found in invoice: {invoice_id}",
                    )

                # Get the period end from the first line item
                period_end_timestamp = lines[0].get("period", {}).get("end")
                if not period_end_timestamp:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"No period end found in invoice line items: "
                            f"{invoice_id}"
                        ),
                    )

                # Convert Unix timestamp to datetime
                new_expiration = datetime_from_timestamp(period_end_timestamp)

                logger.debug(
                    f"Webhook: Using expiration date from invoice: {new_expiration} "
                    f"(Unix timestamp: {period_end_timestamp})"
                )
                logger.debug(
                    f"Webhook: Extending expiration from {current_expiration} "
                    f"to {new_expiration}"
                )

                # Stripe delivers invoice events out of period order (retries,
                # late settlements), so a renewal may only ever advance the
                # stored expiration - never rewind it to an older period's end.
                # ensure_utc on both sides: the stored value comes back naive
                # from MySQL while datetime_from_timestamp is UTC-aware, and
                # comparing the two raises.
                if current_expiration and new_expiration <= ensure_utc(
                    current_expiration
                ):
                    logger.warning(
                        f"Webhook: Skipping invoice {invoice_id} - its period end "
                        f"{new_expiration} does not advance the stored expiration "
                        f"{current_expiration} (out-of-order or redelivered event)"
                    )
                    return {
                        "success": True,
                        "message": (
                            f"Invoice skipped - period end {new_expiration} does "
                            f"not advance stored expiration {current_expiration}"
                        ),
                        "webhook_processed": True,
                        "skipped": True,
                        "reason": "stale_period_end",
                    }

            except HTTPException:
                raise
            except Exception as e:
                logger.error(
                    f"Webhook: Error getting expiration from invoice: {str(e)}"
                )
                raise HTTPException(
                    status_code=500,
                    detail=f"Error getting expiration from invoice: {str(e)}",
                )

            # 5. Update subscription expiration and reset payment failure tracking
            try:
                logger.debug("Webhook: Updating subscription expiration...")
                user_subscription.expiration = new_expiration
                user_subscription.updated_at = (
                    SubscriptionService.get_current_datetime()
                )

            except Exception as e:
                logger.error(f"Webhook: Error updating subscription: {str(e)}")
                raise HTTPException(
                    status_code=500, detail=f"Error updating subscription: {str(e)}"
                )

            # 6. Record the payment/purchase
            try:
                logger.debug("Webhook: Recording subscription renewal purchase...")

                # Create purchase record for the renewal
                purchase = SubscriptionUserPurchase(
                    user_id=user_id,
                    plan_id=plan_id,
                    created_at=SubscriptionService.get_current_datetime(),
                )

                db.add(purchase)
                db.flush()  # Get the purchase ID

                logger.debug(f"Webhook: Purchase recorded with ID: {purchase.id}")

            except Exception as e:
                logger.error(f"Webhook: Error recording purchase: {str(e)}")
                raise HTTPException(
                    status_code=500, detail=f"Error recording purchase: {str(e)}"
                )

            # 7. Commit all changes
            db.commit()

            # 8. Invalidate tier cache for immediate status update
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                invalidate_user_tier_cache(user.uid)
                logger.debug("Invalidated tier cache after subscription renewal")

            logger.info(
                f"Webhook: Successfully processed subscription renewal for user "
                f"{user_id}, plan {plan_id}, invoice {invoice_id}. "
                f"New expiration: {new_expiration}"
            )

            return {
                "success": True,
                "user_id": user_id,
                "subscription_id": user_subscription.id,
                "purchase_id": purchase.id,
                "old_expiration": current_expiration.isoformat(),
                "new_expiration": new_expiration.isoformat(),
                "amount_paid": amount_paid / 100,
                "message": "Subscription renewed successfully via webhook",
                "webhook_processed": True,
                "invoice_id": invoice_id,
            }

        except HTTPException:
            # bare raise: the generic arm below would turn this into a 500
            db.rollback()
            raise
        except Exception as e:
            logger.error(
                f"Webhook: Error processing subscription payment for invoice "
                f"{invoice_id}: {str(e)}"
            )
            db.rollback()
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Error processing subscription payment for invoice "
                    f"{invoice_id}: {str(e)}"
                ),
            )

    @staticmethod
    def handle_invoice_created(invoice_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle invoice.created webhook - finalize draft invoices immediately

        Args:
            invoice_data: Webhook invoice data from Stripe

        Returns:
            Dict with processing results

        Raises:
            HTTPException: If finalization fails
        """
        try:
            # Ensure Stripe is initialized
            SubscriptionService._ensure_stripe_initialized()

            invoice_id = invoice_data.get("id")
            invoice_status = invoice_data.get("status")

            logger.info(
                f"Webhook: Processing invoice.created event for invoice {invoice_id} "
                f"with status: {invoice_status}"
            )

            # Check if invoice is in draft status
            if invoice_status == InvoiceStatus.DRAFT:
                logger.debug(f"Webhook: Finalizing draft invoice {invoice_id}")

                # Ensure payment method is set before finalizing
                try:
                    # Get the customer and check for default payment method
                    customer_id = invoice_data.get("customer")
                    subscription_id = invoice_data.get("subscription")
                    default_pm = None

                    if customer_id:
                        # First, try to get payment method from the subscription
                        if subscription_id:
                            try:
                                logger.debug(
                                    f"Webhook: Retrieving subscription "
                                    f"{subscription_id} to get payment method"
                                )
                                subscription = stripe.Subscription.retrieve(
                                    subscription_id
                                )
                                default_pm = subscription.get("default_payment_method")
                                logger.debug(
                                    f"Webhook: Subscription retrieved. "
                                    f"default_payment_method = {default_pm}"
                                )
                                if default_pm:
                                    logger.debug(
                                        f"Webhook: Found payment method {default_pm} "
                                        f"from subscription {subscription_id}"
                                    )
                            except stripe.error.StripeError as sub_error:
                                logger.warning(
                                    f"Webhook: Could not retrieve subscription "
                                    f"{subscription_id}: {str(sub_error)}"
                                )

                        # If not found on subscription, check customer's invoice setting
                        if not default_pm:
                            customer = stripe.Customer.retrieve(customer_id)
                            default_pm = customer.get("invoice_settings", {}).get(
                                "default_payment_method"
                            )
                            if default_pm:
                                logger.debug(
                                    f"Webhook: Found payment method {default_pm} "
                                    f"from customer invoice settings"
                                )

                        # If we found a payment method but invoice doesn't have one,
                        # set it
                        if default_pm and not invoice_data.get(
                            "default_payment_method"
                        ):
                            logger.debug(
                                f"Webhook: Setting default payment method {default_pm} "
                                f"on invoice {invoice_id}"
                            )
                            updated_invoice = stripe.Invoice.modify(
                                invoice_id, default_payment_method=default_pm
                            )
                            logger.debug(
                                f"Webhook: Payment method successfully attached to "
                                f"invoice. Updated invoice default_payment_method: "
                                f"{updated_invoice.get('default_payment_method')}"
                            )
                        elif not default_pm:
                            logger.warning(
                                f"Webhook: No payment method found for customer "
                                f"{customer_id} or subscription {subscription_id}"
                            )
                        elif invoice_data.get("default_payment_method"):
                            logger.debug(
                                f"Webhook: Invoice {invoice_id} already has payment "
                                f"method {invoice_data.get('default_payment_method')}"
                            )

                except stripe.error.StripeError as e:
                    logger.warning(
                        f"Webhook: Could not set payment method on invoice "
                        f"{invoice_id}: {str(e)}"
                    )
                    # Continue anyway - finalization might still work

                # Finalize the invoice
                try:
                    finalized_invoice = stripe.Invoice.finalize_invoice(
                        invoice_id,
                        auto_advance=True,  # Enable automatic payment attempts
                    )
                    logger.debug(
                        f"Webhook: Successfully finalized invoice {invoice_id}. "
                        f"New status: {finalized_invoice.get('status')}, "
                        f"auto_advance: {finalized_invoice.get('auto_advance')}"
                    )

                    # After finalizing, attempt to pay the invoice immediately
                    # Only if it has a payment method and amount due > 0
                    if (
                        finalized_invoice.get("default_payment_method")
                        and finalized_invoice.get("amount_due", 0) > 0
                        and finalized_invoice.get("status") == InvoiceStatus.OPEN
                    ):
                        try:
                            logger.debug(
                                f"Webhook: Attempting immediate payment for invoice "
                                f"{invoice_id}"
                            )
                            paid_invoice = stripe.Invoice.pay(invoice_id)
                            logger.debug(
                                f"Webhook: Payment attempt completed for invoice "
                                f"{invoice_id}. Status: {paid_invoice.get('status')}"
                            )

                            return {
                                "success": True,
                                "invoice_id": invoice_id,
                                "previous_status": InvoiceStatus.DRAFT,
                                "new_status": paid_invoice.get("status"),
                                "message": "Invoice finalized and payment attempted",
                                "webhook_processed": True,
                                "payment_attempted": True,
                            }
                        except stripe.error.StripeError as pay_error:
                            logger.warning(
                                f"Webhook: Could not immediately pay invoice "
                                f"{invoice_id}: {str(pay_error)}. "
                                f"Stripe will retry automatically."
                            )
                            # Don't fail the webhook - finalization succeeded
                            # Stripe will handle automatic retries

                    return {
                        "success": True,
                        "invoice_id": invoice_id,
                        "previous_status": InvoiceStatus.DRAFT,
                        "new_status": finalized_invoice.get("status"),
                        "message": "Invoice finalized successfully",
                        "webhook_processed": True,
                    }

                except stripe.error.StripeError as e:
                    logger.error(
                        f"Webhook: Stripe error finalizing invoice {invoice_id}: "
                        f"{str(e)}"
                    )
                    raise HTTPException(
                        status_code=500,
                        detail=f"Error finalizing invoice: {str(e)}",
                    )
            else:
                logger.debug(
                    f"Webhook: Invoice {invoice_id} is not in draft status "
                    f"(status: {invoice_status}), skipping finalization"
                )
                return {
                    "success": True,
                    "invoice_id": invoice_id,
                    "status": invoice_status,
                    "message": "Invoice not in draft status, no action needed",
                    "webhook_processed": True,
                    "skipped": True,
                }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(
                f"Webhook: Error processing invoice.created for invoice "
                f"{invoice_data.get('id')}: {str(e)}"
            )
            raise HTTPException(
                status_code=500,
                detail=f"Error processing invoice.created: {str(e)}",
            )

    @staticmethod
    def handle_invoice_finalized(invoice_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle invoice.finalized webhook - attempt immediate payment

        Args:
            invoice_data: Webhook invoice data from Stripe

        Returns:
            Dict with processing results

        Raises:
            HTTPException: If payment attempt fails critically
        """
        try:
            # Ensure Stripe is initialized
            SubscriptionService._ensure_stripe_initialized()

            invoice_id = invoice_data.get("id")
            invoice_status = invoice_data.get("status")
            default_pm = invoice_data.get("default_payment_method")
            amount_due = invoice_data.get("amount_due", 0)

            logger.debug(
                f"Webhook: Processing invoice.finalized event for invoice {invoice_id} "
                f"with status: {invoice_status}, payment_method: {default_pm}, "
                f"amount_due: {amount_due}"
            )

            # Only attempt payment if invoice is open and has amount due
            if invoice_status == InvoiceStatus.OPEN and amount_due > 0:
                # Check if payment method exists
                if not default_pm:
                    logger.warning(
                        f"Webhook: Invoice {invoice_id} has no payment method, "
                        f"cannot attempt payment"
                    )
                    return {
                        "success": True,
                        "invoice_id": invoice_id,
                        "status": invoice_status,
                        "message": "No payment method available for payment",
                        "webhook_processed": True,
                        "skipped": True,
                    }

                # Attempt to pay the invoice
                try:
                    logger.debug(
                        f"Webhook: Attempting immediate payment for invoice "
                        f"{invoice_id}"
                    )
                    paid_invoice = stripe.Invoice.pay(invoice_id)
                    logger.debug(
                        f"Webhook: Payment attempt completed for invoice {invoice_id}. "
                        f"Status: {paid_invoice.get('status')}"
                    )

                    return {
                        "success": True,
                        "invoice_id": invoice_id,
                        "previous_status": InvoiceStatus.OPEN,
                        "new_status": paid_invoice.get("status"),
                        "message": "Invoice payment attempted successfully",
                        "webhook_processed": True,
                        "payment_attempted": True,
                    }

                except stripe.error.CardError as pay_error:
                    # The customer's card was refused: their outcome to resolve,
                    # not a fault in this integration.
                    logger.warning(
                        f"Webhook: Card declined paying invoice {invoice_id}: "
                        f"{str(pay_error)}"
                    )
                    return {
                        "success": False,
                        "invoice_id": invoice_id,
                        "status": invoice_status,
                        "message": f"Payment failed: {str(pay_error)}",
                        "webhook_processed": True,
                        "payment_attempted": True,
                        "payment_failed": True,
                        "card_declined": True,
                    }

                except stripe.error.StripeError as pay_error:
                    # Stripe's own auto-collection races this handler, so read
                    # the invoice back before calling a refused pay a failure.
                    settled = None
                    try:
                        settled = stripe.Invoice.retrieve(invoice_id).get("status")
                    except stripe.error.StripeError:
                        # A failed read-back must not escalate into a 500.
                        pass

                    if settled == InvoiceStatus.PAID:
                        logger.debug(
                            f"Webhook: Invoice {invoice_id} was already paid before "
                            f"this attempt: {str(pay_error)}"
                        )
                        return {
                            "success": True,
                            "invoice_id": invoice_id,
                            "previous_status": InvoiceStatus.OPEN,
                            "new_status": InvoiceStatus.PAID,
                            "message": "Invoice was already paid",
                            "webhook_processed": True,
                            "payment_attempted": True,
                        }

                    logger.error(
                        f"Webhook: Failed to pay invoice {invoice_id}: {str(pay_error)}"
                    )
                    return {
                        "success": False,
                        "invoice_id": invoice_id,
                        "status": invoice_status,
                        "message": f"Payment failed: {str(pay_error)}",
                        "webhook_processed": True,
                        "payment_attempted": True,
                        "payment_failed": True,
                    }

            else:
                logger.debug(
                    f"Webhook: Invoice {invoice_id} does not require immediate payment"
                    f"Status: {invoice_status}, Amount due: {amount_due}"
                )
                return {
                    "success": True,
                    "invoice_id": invoice_id,
                    "status": invoice_status,
                    "message": "Invoice does not require immediate payment",
                    "webhook_processed": True,
                    "skipped": True,
                }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(
                f"Webhook: Error processing invoice.finalized for invoice "
                f"{invoice_data.get('id')}: {str(e)}"
            )
            raise HTTPException(
                status_code=500,
                detail=f"Error processing invoice.finalized: {str(e)}",
            )

    @staticmethod
    def get_webhook_secret() -> str:
        webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")
        if not webhook_secret:
            logger.error("STRIPE_WEBHOOK_SECRET is not set")
            raise HTTPException(
                status_code=500,
                detail="STRIPE_WEBHOOK_SECRET environment variable is not set",
            )
        return webhook_secret

    @staticmethod
    def _sync_subscription_from_event(
        db: Session, subscription_data: Dict[str, Any], event_label: str
    ) -> Dict[str, Any]:
        """
        Mirror a Stripe ``customer.subscription.*`` event into local state
        (issue #629, Problem 1 — activation fallback).

        Upserts ``subscription_users`` (plan + expiration), syncs the storage
        quota, and invalidates the tier cache so the next ``/users/me`` call
        reflects the change. Acknowledges without a DB write when the
        customer cannot be mapped or the event has no period end, so Stripe
        does not retry.
        """
        customer_id = subscription_data.get("customer")
        stripe_subscription_id = subscription_data.get("id")
        if not customer_id:
            logger.warning(
                f"Webhook: No customer ID in subscription.{event_label} event "
                f"(subscription {stripe_subscription_id}); acknowledging"
            )
            return {
                "success": True,
                "skipped": True,
                "reason": "missing_customer_id",
                "message": "No customer ID in subscription event",
            }
        logger.info(
            f"Webhook: Handling customer.subscription.{event_label} for "
            f"customer {customer_id} (subscription {stripe_subscription_id})"
        )

        # 1. Map Stripe customer -> local user account
        user_account = (
            db.query(SubscriptionUserAccount)
            .filter(SubscriptionUserAccount.provider_customer_id == customer_id)
            .first()
        )
        if not user_account:
            logger.warning(
                f"Webhook: No user account for customer_id {customer_id}; "
                f"acknowledging subscription.{event_label} without DB change"
            )
            return {
                "success": True,
                "skipped": True,
                "reason": "missing_user_account",
                "message": f"No user account for customer: {customer_id}",
            }
        user_id = user_account.user_id

        # 2. Derive expiration from the event payload (trial overrides period)
        trial_end = subscription_data.get("trial_end")
        current_period_end = subscription_data.get("current_period_end")
        if trial_end:
            expiration_date = datetime_from_timestamp(trial_end)
        elif current_period_end:
            expiration_date = datetime_from_timestamp(current_period_end)
        else:
            logger.warning(
                f"Webhook: No period end in subscription {stripe_subscription_id}; "
                f"acknowledging subscription.{event_label} without DB change"
            )
            return {
                "success": True,
                "skipped": True,
                "reason": "missing_expiration",
                "message": (f"No period end in subscription: {stripe_subscription_id}"),
            }

        # 3. Derive plan from subscription metadata, default to premium
        metadata = subscription_data.get("metadata") or {}
        plan_id_raw = metadata.get("plan_id")
        try:
            plan_id = int(plan_id_raw) if plan_id_raw else SubscriptionPlanIds.PREMIUM
        except (TypeError, ValueError):
            logger.warning(
                f"Webhook: Invalid plan_id '{plan_id_raw}' in subscription metadata for"
                f" {stripe_subscription_id}; defaulting to PREMIUM"
            )
            plan_id = SubscriptionPlanIds.PREMIUM
        if not plan_id_raw:
            logger.warning(
                f"Webhook: No plan_id in subscription metadata for "
                f"{stripe_subscription_id}; defaulting to PREMIUM"
            )

        # 4. Upsert subscription_users (lock-safe, idempotent helper)
        CheckoutService.create_or_update_subscription(
            db, user_id, plan_id, expiration_date
        )

        # 5. Mirror cancel_at_period_end -> scheduled_downgrade.
        # Step 4's upsert (_apply_subscription_update) always resets
        # scheduled_downgrade=False, so the False case is already handled.
        # We only need a second query when cancel_at_period_end=True to
        # flip it back on.
        cancel_at_period_end = bool(subscription_data.get("cancel_at_period_end"))
        if cancel_at_period_end:
            subscription = (
                db.query(UserSubscription)
                .filter(UserSubscription.user_id == user_id)
                .first()
            )
            if subscription is not None:
                subscription.scheduled_downgrade = True
                subscription.updated_at = SubscriptionService.get_current_datetime()

        # 6. Sync storage quota to the plan (idempotent under concurrent webhooks)
        storage_quota_bytes = StorageQuota.bytes_for_plan(plan_id)
        existing_usage = (
            db.query(UserStorageUsage)
            .filter(UserStorageUsage.user_id == user_id)
            .first()
        )
        if existing_usage:
            existing_usage.storage_quota_bytes = storage_quota_bytes
        else:
            try:
                with db.begin_nested():
                    db.add(
                        UserStorageUsage(
                            user_id=user_id,
                            storage_usage_bytes=0,
                            storage_quota_bytes=storage_quota_bytes,
                        )
                    )
                    db.flush()
            except IntegrityError:
                # checkout.session.completed already inserted the row;
                # SAVEPOINT rolled back the INSERT only, outer transaction
                # (including Step 4 subscription upsert) is preserved.
                logger.warning(
                    f"Webhook: Concurrent storage insert for user "
                    f"{user_id}; falling back to update"
                )
                db.execute(
                    update(UserStorageUsage)
                    .where(UserStorageUsage.user_id == user_id)
                    .values(storage_quota_bytes=storage_quota_bytes)
                )

        db.commit()

        # 7. Invalidate tier cache so the GUI reflects the change promptly
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            invalidate_user_tier_cache(user.uid)

        logger.info(
            f"Webhook: Synced subscription.{event_label} for user {user_id} "
            f"(plan_id={plan_id}, expiration={expiration_date}, "
            f"scheduled_downgrade={cancel_at_period_end})"
        )
        return {
            "success": True,
            "user_id": user_id,
            "plan_id": plan_id,
            "expiration": expiration_date,
            "scheduled_downgrade": cancel_at_period_end,
            "message": f"Subscription {event_label} synced",
        }

    @staticmethod
    def handle_subscription_created(
        db: Session, subscription_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Handle customer.subscription.created webhook (issue #629, Problem 1).

        Activates the subscription locally so the user is no longer stuck on
        "Activation Pending" when activation cannot complete through
        ``checkout.session.completed`` alone (delayed, partial failure, or
        not delivered).
        """
        try:
            return WebhookService._sync_subscription_from_event(
                db, subscription_data, event_label="created"
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(
                f"Webhook: Error handling subscription.created: {e}",
                exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail=f"Error processing subscription.created: {e}",
            )

    @staticmethod
    def handle_subscription_updated(
        db: Session, subscription_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Handle customer.subscription.updated webhook (issue #629, Problem 2).

        Mirrors Stripe-initiated subscription changes into local state:
        - plan + expiration (via the shared upsert), and
        - cancel_at_period_end -> scheduled_downgrade (when scheduling a
          downgrade), so a Stripe-initiated change (proration, plan change,
          payment-method-failure auto-cancel, etc.) does not silently drift
          from Stripe.
        """
        try:
            return WebhookService._sync_subscription_from_event(
                db, subscription_data, event_label="updated"
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(
                f"Webhook: Error handling subscription.updated: {e}",
                exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail=f"Error processing subscription.updated: {e}",
            )

    @staticmethod
    async def _release_premium_assignment(
        db: Session, subscription_data: Dict[str, Any]
    ) -> None:
        """
        Release a dangling premium compute assignment when a subscription
        ends (issue #629, Problem 3).

        ``handle_subscription_cancelled`` only expires the DB row; without
        this the per-user EC2/ALB resources stay attached to a now-free user
        until logout / tab-close / the activity-based stale-sweep. Best-
        effort: logs but does not raise, so a release failure can never
        block webhook acknowledgement.

        Uses a short timeout (``WEBHOOK_RELEASE_TIMEOUT_SECONDS``) so a
        slow/hung release Lambda cannot stall the webhook response (which
        would trigger Stripe retries). On timeout the call fails open and
        the periodic premium-expiration sweep releases the assignment later.
        """
        # Local import avoids any import cycle with the subscription package.
        from studio.app.common.core.premium.premium_assignment_service import (
            WEBHOOK_RELEASE_TIMEOUT_SECONDS,
            premium_assignment_service,
        )

        customer_id = subscription_data.get("customer")
        # Single JOIN query to reduce DB round-trips on the latency-sensitive
        # webhook path (was two sequential queries before).
        row = (
            db.query(SubscriptionUserAccount, User)
            .join(User, User.id == SubscriptionUserAccount.user_id)
            .filter(SubscriptionUserAccount.provider_customer_id == customer_id)
            .first()
        )
        if not row:
            logger.info(
                "Webhook: No user account/user for customer %s; "
                "no premium assignment to release",
                customer_id,
            )
            return
        user_account, user = row

        try:
            result = await premium_assignment_service.release_premium_user(
                user_id=user.id,
                user_uid=user.uid,
                hard=True,
                timeout=WEBHOOK_RELEASE_TIMEOUT_SECONDS,
            )
            if result.get("success"):
                logger.info(
                    "Webhook: Released premium assignment for user %s "
                    "on subscription delete: %s",
                    user.id,
                    result.get("message"),
                )
            else:
                # Not released in-band (e.g. timed out). The background sweep
                # will release it; acknowledge the webhook regardless.
                logger.warning(
                    "Webhook: Premium release not confirmed for user "
                    "%s: %s; deferring to background sweep",
                    user.id,
                    result.get("message"),
                )
        except Exception as e:
            logger.error(
                "Webhook: Failed to release premium assignment for user %s: %s",
                user_account.user_id,
                e,
                exc_info=True,
            )

    @staticmethod
    async def dispatch_webhook_event(
        db: Session, event_type: str, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Dispatch webhook events to appropriate handlers

        Args:
            db: Database session
            event_type: The type of webhook event
            data: Webhook event data

        Returns:
            Dict with processing results
        """
        try:
            match event_type:
                case StripeWebhookEvent.CHECKOUT_SESSION_COMPLETED:
                    logger.info("Handling checkout.session.completed")
                    return WebhookService.handle_checkout_completed(db, data)

                case StripeWebhookEvent.INVOICE_PAYMENT_FAILED:
                    logger.info("Handling invoice.payment_failed")
                    WebhookService.handle_payment_failed(db, data)
                    return {
                        "success": True,
                        "message": "Payment failed event processed",
                    }

                case StripeWebhookEvent.CUSTOMER_SUBSCRIPTION_CREATED:
                    logger.info("Handling customer.subscription.created")
                    return WebhookService.handle_subscription_created(db, data)

                case StripeWebhookEvent.CUSTOMER_SUBSCRIPTION_UPDATED:
                    logger.info("Handling customer.subscription.updated")
                    return WebhookService.handle_subscription_updated(db, data)

                case StripeWebhookEvent.CUSTOMER_SUBSCRIPTION_DELETED:
                    logger.info("Handling customer.subscription.deleted")
                    WebhookService.handle_subscription_cancelled(db, data)
                    # Release the dangling premium compute assignment so a
                    # now-free user does not keep a dedicated EC2/ALB.
                    await WebhookService._release_premium_assignment(db, data)
                    return {
                        "success": True,
                        "message": "Subscription cancellation processed",
                    }

                case StripeWebhookEvent.SUBSCRIPTION_SCHEDULE_RELEASED:
                    logger.info("Handling subscription_schedule.released")
                    WebhookService.handle_subscription_schedule_released(db, data)
                    return {
                        "success": True,
                        "message": "Subscription schedule release processed",
                    }

                case StripeWebhookEvent.INVOICE_PAYMENT_SUCCEEDED:
                    logger.info("Handling invoice.payment_succeeded")
                    return WebhookService.handle_subscription_payment_succeeded(
                        db, data
                    )

                case StripeWebhookEvent.INVOICE_CREATED:
                    logger.info("Handling invoice.created")
                    return WebhookService.handle_invoice_created(data)

                case StripeWebhookEvent.INVOICE_FINALIZED:
                    logger.info("Handling invoice.finalized")
                    return WebhookService.handle_invoice_finalized(data)

                case _:
                    logger.warning(f"Unhandled webhook event type: {event_type}")
                    return {
                        "success": True,
                        "message": f"Unhandled event type: {event_type}",
                    }

        except HTTPException as e:
            # 4xx is our own validation refusing an event, so only 5xx should page
            log = logger.warning if e.status_code < 500 else logger.error
            log(f"Webhook {event_type} failed ({e.status_code}): {e.detail}")
            # Status preserved for the caller to map; the route applies the one
            # generic detail. Flattening here reported a handler's 500 as a 400,
            # so our own failures were indistinguishable from a malformed event.
            raise
        except Exception as e:
            logger.error(f"Error dispatching webhook event {event_type}: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail=f"Error dispatching webhook event {event_type}: {str(e)}",
            )
