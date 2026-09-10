from datetime import timezone
from typing import List, Optional

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from studio.app.common.core.auth.auth_dependencies import get_current_user
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.subscription.checkout_service import CheckoutService
from studio.app.common.core.subscription.constants import (
    INVOICE_LIST_LIMIT,
    StripeCheckoutPaymentStatus,
    StripeCheckoutSessionStatus,
    SubscriptionPlanIds,
    SubscriptionUserStatus,
)
from studio.app.common.core.subscription.stripe_service import (
    StripeService,
    get_or_create_stripe_customer,
)
from studio.app.common.core.subscription.subscription_service import SubscriptionService
from studio.app.common.core.subscription.webhook_service import WebhookService
from studio.app.common.core.utils.datetime_utils import (
    datetime_from_timestamp,
    get_current_datetime,
    get_current_timestamp,
)
from studio.app.common.db.database import get_db
from studio.app.common.models.subscription import SubscriptionPlans
from studio.app.common.models.user import User as UserModel
from studio.app.common.schemas.checkouts import (
    CheckoutSessionRequest,
    CheckoutValidationResponse,
    CheckoutValidationStatus,
)
from studio.app.common.schemas.subscriptions import (
    CancelSubscriptionResponse,
    CreateCheckoutSessionRequest,
    CreateCheckoutSessionResponse,
    CreateSetupIntentResponse,
    DeletionPriorityRequest,
    DeletionPriorityResponse,
    InvoiceResponse,
    PaymentMethodResponse,
    SubscriptionPlanResponse,
    UpdatePaymentMethodResponse,
    UpdateSubscriptionRequest,
    UpdateSubscriptionResponse,
    UserSubscriptionResponse,
)
from studio.app.common.schemas.users import User

# Load callback URL at module level (doesn't require secrets for module import)
try:
    STRIPE_CALLBACK_URL = SubscriptionService.get_base_url()
except ValueError:
    STRIPE_CALLBACK_URL = None  # Will be set when needed


def stripe_dependency():
    """Dependency to ensure Stripe is initialized before handling requests"""
    SubscriptionService._ensure_stripe_initialized()


router = APIRouter(
    prefix="/api/subsc",
    tags=["Subscriptions"],
    dependencies=[Depends(stripe_dependency)],
)
webhook_router = APIRouter(
    prefix="/api/subsc/webhooks",
    tags=["Subscription Webhooks"],
    dependencies=[Depends(stripe_dependency)],
)
logger = AppLogger.get_logger()


@router.get("/mgmts/plans", response_model=List[SubscriptionPlanResponse])
def get_subscription_plans(db: Session = Depends(get_db)):
    try:
        plans: List[SubscriptionPlans] = SubscriptionService.get_active_plans(db)

        if not plans:
            logger.warning("No subscription plans found")
            return []

        result: List[SubscriptionPlanResponse] = []
        for plan in plans:
            try:
                # SQLModel inherits from Pydantic, so .dict() should work
                plan_dict = plan.dict()
                plan_response = SubscriptionPlanResponse(**plan_dict)
                result.append(plan_response)
            except Exception as plan_error:
                logger.error(f"Error processing plan {plan.id}: {plan_error}")
                continue
        return result
    except Exception as e:
        logger.error(f"Error fetching subscription plans: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch subscription plans: {str(e)}"
        )


@router.get(
    "/mgmts",
    response_model=Optional[UserSubscriptionResponse],
)
async def get_user_subscription(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get user's current active subscription
    """
    try:
        # Get the most recent active subscription
        subscription = SubscriptionService.get_user_subscription(db, current_user.id)
        user_purchase = SubscriptionService.get_user_subscription_purchase(
            db, current_user.id
        )
        logger.debug(f"Fetched subscription for user {current_user.id}: {subscription}")

        if subscription is None:
            # Check if user has any expired subscriptions
            expired_subscription = SubscriptionService.get_user_expired_subscription(
                db, current_user.id
            )

            if expired_subscription and user_purchase:
                try:
                    sub_data, plan_data, _ = expired_subscription
                    subscription_dict = {
                        **sub_data.dict(),
                        "plan_name": plan_data.name,
                        "plan_price": plan_data.price,
                        "is_expired": True,
                        "status": SubscriptionUserStatus.EXPIRED.value,
                    }
                    subscription_response = UserSubscriptionResponse(
                        **subscription_dict
                    )
                    return subscription_response
                except Exception as sub_error:
                    logger.error(
                        f"Error processing expired subscription for user "
                        f"{current_user.id}: {sub_error}"
                    )
                    return None

            return None

        # If we get here, result is not None, so we can safely unpack
        try:
            subscription, subscription_plans = subscription
            sub_data, plan_data = subscription, subscription_plans

            # Check subscription status based on plan ID and cancellation state
            is_cancelled = SubscriptionService.is_subscription_cancelled(
                db, current_user.id
            )

            subscription_status = SubscriptionService.get_subscription_status(
                plan_data.id, is_cancelled
            )

            # Ensure both datetimes are timezone-aware for comparison
            current_time = SubscriptionService.get_current_datetime()
            expiration_time = sub_data.expiration

            # If expiration is naive, make it timezone-aware
            if expiration_time.tzinfo is None:
                expiration_time = expiration_time.replace(tzinfo=timezone.utc)

            # If current_time is naive, make it timezone-aware
            if current_time.tzinfo is None:
                current_time = current_time.replace(tzinfo=timezone.utc)

            subscription_dict = {
                **sub_data.dict(),
                "plan_name": plan_data.name,
                "plan_price": plan_data.price,
                "is_expired": expiration_time < current_time,
                "status": subscription_status,
            }
            subscription_response = UserSubscriptionResponse(**subscription_dict)
            logger.debug(f"Subscription response: {subscription_response}")
            return subscription_response
        except Exception as sub_error:
            logger.error(
                f"Error processing active subscription for user "
                f"{current_user.id}: {sub_error}"
            )
            return None
    except Exception as e:
        logger.error(
            f"Error fetching subscription for user {current_user.id}: {str(e)}"
        )
        raise HTTPException(
            status_code=subscription_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch user subscription: {str(e)}",
        )


@router.put("/mgmts", response_model=UpdateSubscriptionResponse)
async def update_user_subscription(
    request: UpdateSubscriptionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # return await StripeService.update_user_subscription(db, user, request)
    """
    This endpoint is currently not in use
    """
    raise HTTPException(
        status_code=501,
        detail="This API endpoint is not implemented and currently not in use",
    )


@router.get("/mgmts/server-time")
async def get_server_time():
    utc_time = get_current_datetime()
    return {"server_time": utc_time.isoformat()}


@router.get("/deletion-priority", response_model=DeletionPriorityResponse)
async def get_deletion_priority(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    priority = SubscriptionService.get_deletion_priority(db, current_user.id)
    return DeletionPriorityResponse(priority=priority)


@router.put("/deletion-priority", response_model=DeletionPriorityResponse)
async def update_deletion_priority(
    request: DeletionPriorityRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    SubscriptionService.update_deletion_priority(db, current_user.id, request.priority)
    return DeletionPriorityResponse(priority=request.priority)


@router.delete("/mgmts/cancel", response_model=CancelSubscriptionResponse)
async def cancel_user_subscription(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Cancel user's subscription at period end
    - Subscription will be cancelled when current billing period ends
    - User retains access until then
    - Database updates handled via webhook
    """
    user_id = getattr(user, "id", None)
    try:
        result = await StripeService.handle_cancel_user_subscription(db, user)
        return result

    except stripe.error.StripeError as e:
        logger.error(f"Stripe error cancelling subscription: {str(e)}")
        raise HTTPException(
            status_code=400, detail=f"Payment processing error: {str(e)}"
        )
    except HTTPException as e:
        log_fn = logger.error if e.status_code >= 500 else logger.warning
        log_fn(
            f"Cancel subscription failed for user {user_id}: "
            f"{e.status_code} {e.detail}"
        )
        raise
    except Exception as e:
        logger.error(f"Error cancelling subscription for user {user_id}: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to cancel subscription: {str(e)}"
        )


@router.post("/mgmts/reactivate/{user_id}", response_model=CancelSubscriptionResponse)
async def reactivate_user_subscription(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Cancel the scheduled cancellation of user's subscription
    - Removes the cancellation scheduled at period end
    - User subscription will continue normally
    """
    try:
        # Verify user access
        if current_user.id != user_id:
            raise HTTPException(
                status_code=403,
                detail="Access denied: Can only manage your own subscription",
            )

        # Get current user subscription
        current_subscription_result = SubscriptionService.get_user_subscription(
            db, user_id
        )
        if not current_subscription_result:
            raise HTTPException(status_code=404, detail="No active subscription found")

        sub_data, current_plan = current_subscription_result

        # Get Stripe customer (unified lookup: DB first, then Stripe API)
        customer = await get_or_create_stripe_customer(db, current_user)

        # Get active or trialing Stripe subscription
        # First try to find active subscription
        stripe_subscriptions = stripe.Subscription.list(
            customer=customer.id, status="active", limit=1
        )

        # If no active subscription found, check for trialing subscription
        if not stripe_subscriptions.data:
            stripe_subscriptions = stripe.Subscription.list(
                customer=customer.id, status="trialing", limit=1
            )

        if not stripe_subscriptions.data:
            raise HTTPException(
                status_code=404,
                detail="No active or trialing Stripe subscription found",
            )

        stripe_subscription = stripe_subscriptions.data[0]

        # Check if subscription is scheduled for cancellation
        if not stripe_subscription.cancel_at_period_end:
            raise HTTPException(
                status_code=400, detail="Subscription is not scheduled for cancellation"
            )

        logger.info(f"Cancelling scheduled cancellation for user {user_id}")

        # Remove the scheduled cancellation
        stripe.Subscription.modify(
            stripe_subscription.id,
            cancel_at_period_end=False,
            metadata={
                **stripe_subscription.metadata,
                "cancellation_requested": "false",
                "reactivation_requested_at": str(int(get_current_timestamp())),
            },
        )

        # Update database to remove scheduled downgrade
        SubscriptionService.update_scheduled_downgrade(db, user_id, False)

        message: str = (
            "Subscription cancellation has been cancelled. "
            "Your subscription will continue normally."
        )

        logger.info(f"Successfully cancelled cancellation for user {user_id}")

        return CancelSubscriptionResponse(
            success=True,
            message=message,
            cancellation_date="",
            access_until="Subscription will continue normally",
        )

    except stripe.error.StripeError as e:
        logger.error(f"Stripe error cancelling cancellation: {str(e)}")
        raise HTTPException(
            status_code=400, detail=f"Payment processing error: {str(e)}"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error cancelling cancellation for user {user_id}: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to cancel cancellation: {str(e)}"
        )


@router.get("/payment-methods/default", response_model=Optional[PaymentMethodResponse])
async def get_user_default_payment_method(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await StripeService.get_default_payment_method(db, user)


@router.get("/payment-methods", response_model=List[PaymentMethodResponse])
async def get_user_payment_methods():
    """
    This endpoint is currently not in use
    """
    raise HTTPException(
        status_code=501,
        detail="This API endpoint is not implemented and currently not in use",
    )


@router.post("/payment-methods/setup-intent", response_model=CreateSetupIntentResponse)
async def setup_intent():
    """
    This endpoint is currently not in use
    """
    raise HTTPException(
        status_code=501,
        detail="This API endpoint is not implemented and currently not in use",
    )


@router.put("/payment-methods", response_model=UpdatePaymentMethodResponse)
async def update_default_payment_method(
    payment_method_id: str,
):
    """
    This endpoint is currently not in use
    """
    raise HTTPException(
        status_code=501,
        detail="This API endpoint is not implemented and currently not in use",
    )


@router.delete("/payment-methods/{payment_method_id}")
async def delete_payment_method(
    payment_method_id: str,
):
    """
    This endpoint is currently not in use
    """
    raise HTTPException(
        status_code=501,
        detail="This API endpoint is not implemented and currently not in use",
    )


@router.post(
    "/checkout/create-checkout-session", response_model=CreateCheckoutSessionResponse
)
async def create_checkout_session(
    request: CreateCheckoutSessionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await CheckoutService.handle_checkout_session(db, request, user)


@router.post(
    "/checkout/validate-checkout-session", response_model=CheckoutValidationResponse
)
async def validate_checkout_session(
    request: CheckoutSessionRequest,
    db: Session = Depends(get_db),
):
    """
    Validate a Stripe checkout session ID and verify database was
    updated with premium subscription

    Returns detailed status:
    - "success": Payment succeeded and webhook updated database
    - "payment_failed": Payment itself failed
      (card declined, insufficient funds, etc.)
    - "webhook_failed": Payment succeeded but webhook didn't update
      database (internal error)
    """
    try:
        # Retrieve the session from Stripe
        logger.info(f"Validating checkout session ID: {request.session_id}")
        session = stripe.checkout.Session.retrieve(request.session_id)

        # Check if the session is complete and paid
        if not (
            session.payment_status == StripeCheckoutPaymentStatus.PAID
            and session.status == StripeCheckoutSessionStatus.COMPLETE
        ):
            logger.warning(
                f"Checkout session {request.session_id} is not complete/paid"
            )
            return CheckoutValidationResponse(
                status=CheckoutValidationStatus.PAYMENT_FAILED,
                message=(
                    "Payment was not completed. Please check your payment "
                    "information and try again."
                ),
            )

        # Get customer email from session
        customer_email = (
            session.customer_details.email if session.customer_details else None
        )
        if not customer_email:
            logger.error(f"No customer email found in session {request.session_id}")
            return CheckoutValidationResponse(
                status=CheckoutValidationStatus.WEBHOOK_FAILED,
                message="An internal error occurred. Please contact support.",
            )

        # Find user by email. Filter active=True so a soft-deleted user
        # (active=0) with the same email doesn't shadow the new active user
        # after a re-registration — get_user_subscription only matches active
        # users, so resolving to the inactive row would always return None
        # and the UI would stay on "Activation Pending" (issue #629 P5).
        user = (
            db.query(UserModel)
            .filter(
                UserModel.email == customer_email,
                UserModel.active.is_(True),
            )
            .first()
        )
        if not user:
            logger.error(f"No active user found with email {customer_email}")
            return CheckoutValidationResponse(
                status=CheckoutValidationStatus.WEBHOOK_FAILED,
                message="An internal error occurred. Please contact support.",
            )

        # Verify database was updated by webhook - check if user has
        # active premium subscription
        subscription = SubscriptionService.get_user_subscription(db, user.id)
        if not subscription or (
            subscription and subscription[1].id == SubscriptionPlanIds.FREE
        ):
            # Webhook may have failed (server was down). Attempt recovery
            # by checking Stripe for an active subscription and syncing it.
            logger.warning(
                f"Checkout session {request.session_id} is complete but no "
                f"premium subscription found in database for user {user.id}. "
                f"Attempting subscription recovery."
            )

            # Get customer ID for recovery
            subscription_account = CheckoutService.get_subscription_account(db, user.id)
            if subscription_account:
                # Get plan_id from session metadata
                metadata = session.metadata or {}
                plan_id = metadata.get("plan_id")
                if plan_id:
                    recovered = CheckoutService.recover_existing_stripe_subscription(
                        db,
                        user.id,
                        subscription_account.provider_customer_id,
                        int(plan_id),
                    )
                    if recovered:
                        logger.info(
                            f"Successfully recovered subscription for user "
                            f"{user.id} during checkout validation."
                        )
                        return CheckoutValidationResponse(
                            status=CheckoutValidationStatus.SUCCESS,
                            message=(
                                "Payment successful! Your premium "
                                "subscription is now active."
                            ),
                        )

            return CheckoutValidationResponse(
                status=CheckoutValidationStatus.WEBHOOK_FAILED,
                message=(
                    "Payment was successful, but subscription activation "
                    "is pending. Please contact support if this persists."
                ),
            )

        sub_data, plan_data = subscription
        logger.info(
            f"Checkout session {request.session_id} is valid and database is updated "
            f"with premium subscription (plan_id={plan_data.id}) for user {user.id}"
        )
        return CheckoutValidationResponse(
            status=CheckoutValidationStatus.SUCCESS,
            message="Payment successful! Your premium subscription is now active.",
        )

    except stripe.error.StripeError as e:
        logger.error(f"Stripe error validating checkout session: {str(e)}")
        raise HTTPException(
            status_code=400, detail=f"Failed to validate checkout session: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Error validating checkout session: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/checkout/failed-checkout-session", response_model=bool)
async def validate_failed_checkout_session(
    request: CheckoutSessionRequest,
):
    """
    Validate a Stripe checkout session ID for FAILED page
    Returns True if session exists and is in a failed/incomplete state
    """
    try:
        # Retrieve the session from Stripe
        session = stripe.checkout.Session.retrieve(request.session_id)

        # Check if the session exists and is in a legitimate failed state
        # Valid failed states: expired, open with unpaid status
        if session.status == StripeCheckoutSessionStatus.EXPIRED or (
            session.status == StripeCheckoutSessionStatus.OPEN
            and session.payment_status == StripeCheckoutPaymentStatus.UNPAID
        ):
            return True
        else:
            return False

    except stripe.error.InvalidRequestError:
        # Session doesn't exist or is invalid
        return False
    except stripe.error.StripeError as e:
        logger.error(f"Stripe error validating failed checkout session: {str(e)}")
        raise HTTPException(
            status_code=400, detail=f"Failed to validate checkout session: {str(e)}"
        )


@router.get("/invoices/{user_id}", response_model=List[InvoiceResponse])
async def get_user_invoices(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get user's invoices from Stripe
    """
    if current_user.id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this user's invoices",
        )

    try:
        # Get user and subscription (if exists) for invoice lookup
        result = SubscriptionService.get_user_for_invoice_lookup(db, user_id)

        if not result:
            raise HTTPException(status_code=404, detail="User not found")

        subscription_user, user = result
        logger.debug(f"Fetched user subscription record: {subscription_user}")

        logger.debug(f"Fetching invoices for user {user_id} with email {user.email}")

        # Find Stripe customer (read-only — don't create if missing)
        from studio.app.common.core.subscription.stripe_service import (
            get_stripe_customer,
        )

        customer = await get_stripe_customer(db, current_user)
        if not customer:
            logger.info(f"No Stripe customer found for user {user_id}")
            return []

        # Get all invoices for this customer
        invoices = stripe.Invoice.list(
            customer=customer.id,
            limit=INVOICE_LIST_LIMIT,
            expand=["data.subscription"],  # Expand subscription data for more details
        )

        result = []
        for invoice in invoices.data:
            # Convert Stripe invoice to our response format
            invoice_response = InvoiceResponse(
                id=invoice.id,
                date=datetime_from_timestamp(invoice.created).isoformat(),
                total=f"${(invoice.total / 100):.2f}",  # Convert cents to dollars
                status=invoice.status.title(),  # Capitalize status
                invoice_url=invoice.hosted_invoice_url or invoice.invoice_pdf or "",
                amount_paid=invoice.amount_paid,
                amount_due=invoice.amount_due,
                currency=invoice.currency.upper(),
                description=invoice.description or "Subscription payment",
                period_start=(
                    datetime_from_timestamp(invoice.period_start).isoformat()
                    if invoice.period_start
                    else None
                ),
                period_end=(
                    datetime_from_timestamp(invoice.period_end).isoformat()
                    if invoice.period_end
                    else None
                ),
            )
            result.append(invoice_response)

        # Sort by date (newest first)
        result.sort(key=lambda x: x.date, reverse=True)

        return result

    except stripe.error.StripeError as e:
        logger.error(
            f"Stripe error when fetching invoices for user {user_id}: {str(e)}"
        )
        raise HTTPException(
            status_code=400,
            detail=f"Failed to fetch invoices from Stripe: {str(e)}",
        )
    except Exception as e:
        logger.error(f"Error fetching invoices for user {user_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch invoices: {str(e)}",
        )
    except Exception as e:
        logger.error(f"Error validating failed checkout session: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")


@webhook_router.post("/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        # Get raw body and signature header
        body = await request.body()
        sig_header = request.headers.get("stripe-signature")

        logger.debug(f"Webhook received - Body length: {len(body)}")
        logger.debug(
            f"Signature header: {sig_header[:50] if sig_header else 'None'}..."
        )

        # Your webhook endpoint secret from Stripe Dashboard
        endpoint_secret = WebhookService.get_webhook_secret()

        secret_display = "***" + endpoint_secret[-4:] if endpoint_secret else "None"
        logger.debug(f"Using webhook secret: {secret_display}")

        # Verify the webhook signature
        try:
            event = stripe.Webhook.construct_event(body, sig_header, endpoint_secret)
            logger.info("Webhook signature verified successfully")
        except ValueError as e:
            logger.error(f"Invalid payload: {str(e)}")
            raise HTTPException(status_code=400, detail="Invalid payload")
        except stripe.error.SignatureVerificationError as e:
            logger.error(f"Invalid signature: {str(e)}")
            raise HTTPException(status_code=400, detail="Invalid signature")

        # Now use the verified event data
        event_type = event["type"]
        data = event["data"]["object"]

        logger.info(f"Processing event type: {event_type}")

        await WebhookService.dispatch_webhook_event(db, event_type, data)

        logger.info(f"Successfully processed {event_type}")
        return {"received": True, "processed": event_type}

    except HTTPException as e:
        # Generic detail so the response cannot name which check failed, but the
        # inner status is kept. Stripe retries every non-2xx alike, so this is not
        # about redelivery: a masked 400 reports our own failures as malformed
        # requests, which keeps them out of the 5xx alarm and sends whoever reads
        # the delivery log to debug the wrong side.
        # Logged at the raise site (dispatch, signature checks), not again here
        raise HTTPException(
            status_code=e.status_code, detail="Webhook processing failed"
        )
    except Exception as e:
        logger.error(f"Webhook processing error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Webhook processing failed")
