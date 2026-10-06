"""The subscription (database/docs/cairn-subscription-use-cases-v33.json, UC-SUB-01 to UC-SUB-23).

Payments happen on Stripe's own pages. Cairn's server never receives a card
number, bank account, or billing address (SUB-D-01), and sends Stripe only
the sign-in email and the account id (SUB-D-10). Access changes only from
verified Stripe events (POST /v1/stripe/webhook, SUB-D-02), never from the
browser coming back from Checkout.

Nothing about billing at care levels 3 and 4, no subscribe prompt at level 2,
and no account action at level 4 in the same turn (UC-SUB-21, AC-26-11).
Cancelling is online, at any time, in as few steps as subscribing, with no
reason asked and no retention offer (SUB-D-04). There are no refunds
(SUB-D-05). Subscribing, cancelling, and undoing are confirmed to the sign-in
email (UC-CASE-21).
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from .. import account as acct
from .. import subscription as subs
from ..auth import Identity, get_identity
from ..copy_store import Copy, subscription_terms_version
from ..errors import ApiError
from ..schemas import (
    CancelSubscriptionIn,
    CancelSubscriptionResponse,
    Checkbox,
    CheckoutIn,
    CheckoutResponse,
    CheckoutResultResponse,
    Link,
    NextStep,
    Option,
    PortalIn,
    PortalResponse,
    SubscriptionOut,
    SubscriptionTermsResponse,
)
from ..stripe_client import SignatureError, StripeError, verify_webhook

router = APIRouter(tags=["Subscription"])
log = logging.getLogger("cairn_api.subscription")

# UC-SUB-04. After this long without a verified event, the success page says it is taking longer than usual.
FINISHING_SLOW_SECONDS = 30


def _copy(request: Request) -> Copy:
    return request.app.state.subscription_copy


def _stripe(request: Request):
    stripe = request.app.state.stripe
    if stripe is None:
        raise ApiError(503, "payments_unavailable", _copy(request)["payments_unavailable"])
    return stripe


def _not_now(request: Request, care_level: int, *, level: int = 2) -> None:
    if care_level >= level:
        raise ApiError(409, "not_now", _copy(request)["not_now_billing"])


@router.get("/v1/me/subscription", response_model=SubscriptionOut, summary="Settings, Subscription",
            description="UC-SUB-23. One status line from Cairn's stored fields, without calling Stripe, the note that "
                        "a break doesn't pause a subscription, and only the actions that apply. Dates are in the "
                        "user's time zone. At care levels 3 and 4, only the cancel action and no price wording.")
def get_subscription(request: Request, identity: Identity = Depends(get_identity),
                     care_level: int = Query(default=1, ge=1, le=4)) -> SubscriptionOut:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        return subs.subscription_out(acct.load_account(s), _copy(request), care_level=care_level)


@router.get(
    "/v1/me/subscription/terms",
    response_model=SubscriptionTermsResponse,
    summary="Review the price and renewal terms",
    description=(
        "UC-SUB-02. One Cairn screen with the price, that tax is included, that it renews monthly until cancelled "
        "and when charges happen (from the free days' end for an early subscriber), how to cancel, that there are "
        "no refunds, what's included, that breaks don't pause it, and that Stripe takes the payment details. The "
        "checkbox is never pre-checked and the renewal terms sit next to the button. Care level 1 only."
    ),
    responses={409: {"description": "not_now (care levels 2 to 4), or already subscribed."}},
)
def terms(request: Request, identity: Identity = Depends(get_identity),
          care_level: int = Query(default=1, ge=1, le=4)) -> SubscriptionTermsResponse:
    copy = _copy(request)
    st = request.app.state.settings
    _not_now(request, care_level)
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        account = acct.load_account(s)
    if acct.has_subscription(account):
        raise ApiError(409, "already_subscribed", copy["already_subscribed"])
    first = subs.first_charge_at(account, datetime.now(timezone.utc))
    renewal = (copy["terms_renewal_early"].format(trial_end_date=acct.format_date(acct.local_date(account, first)))
               if first else copy["terms_renewal"])
    return SubscriptionTermsResponse(
        title=copy["terms_title"], price=st.price_text,
        lines=[copy["terms_price"], copy["terms_tax"], renewal, copy["terms_cancel"], copy["terms_includes"],
               copy["terms_breaks"], copy["terms_secure_note"]],
        checkbox=Checkbox(label=copy["terms_checkbox"], document_version=subscription_terms_version(copy)),
        button=copy["terms_button"],
        links=[Link(label=copy["terms_link"], url=st.terms_url), Link(label=copy["privacy_link"],
                                                                      url=st.privacy_policy_url)])


@router.post(
    "/v1/me/subscription/checkout",
    response_model=CheckoutResponse,
    summary="Continue to payment",
    description=(
        "UC-SUB-02 and UC-SUB-03. Records the subscription_terms consent with the copy version, the price shown, "
        "the time, and the client, then creates a Stripe Checkout Session: subscription mode, the configured price "
        "(tax inclusive, automatic tax, billing address on Stripe's page), the account id as client_reference_id "
        "and the only metadata, the sign-in email, and the renewal terms next to Stripe's pay button. During the "
        "free days the first charge is on their end (UC-SUB-06). One open session per account. Creating one never "
        "changes access. Redirect to checkout_url right away."
    ),
    responses={409: {"description": "not_now, already_subscribed, terms_outdated, or no journey started yet."},
               503: {"description": "payments_unavailable. Nothing was charged."}},
)
def checkout(req: CheckoutIn, request: Request, identity: Identity = Depends(get_identity)) -> CheckoutResponse:
    copy = _copy(request)
    st = request.app.state.settings
    _not_now(request, req.care_level)
    stripe = _stripe(request)
    if req.document_version != subscription_terms_version(copy):
        raise ApiError(409, "terms_outdated", copy["terms_title"], document_version=subscription_terms_version(copy))
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        account = acct.load_account(s)
        if acct.has_subscription(account):
            raise ApiError(409, "already_subscribed", copy["already_subscribed"])
        if account["trial_started_at"] is None and account["subscription_status"] == "none":
            raise ApiError(409, "no_journey_yet", copy["settings_status_not_started"])
        s.add_subscription_consent(req.document_version, st.price_text, identity.sign_in_method.value, req.client)
        try:
            if account["stripe_checkout_session_id"]:
                stripe.expire_checkout_session(account["stripe_checkout_session_id"])
            back = f"{st.app_url}/subscription/return"
            session = stripe.create_checkout_session(
                account_id=str(account["id"]), email=account["email"], customer_id=account["stripe_customer_id"],
                price_cents=st.subscription_price_cents, product_id=st.stripe_product_id,
                success_url=f"{back}?outcome=success", cancel_url=f"{back}?outcome=cancelled",
                submit_text=copy["checkout_submit_text"], trial_end=subs.first_charge_at(account, s.now))
        except StripeError as exc:
            log.warning("checkout session not created code=%s", exc)
            raise ApiError(503, "payments_unavailable", copy["payments_unavailable"]) from None
        s.update_subscription(stripe_checkout_session_id=session["id"])
        s.audit("subscription_checkout_started", object_type="user", object_id=account["id"])
    return CheckoutResponse(checkout_url=session["url"])


@router.get(
    "/v1/me/subscription/checkout-result",
    response_model=CheckoutResultResponse,
    summary="Back from Stripe Checkout",
    description=(
        "UC-SUB-04 and UC-SUB-05. From Cairn's own status, never from the redirect: visiting the success page grants "
        "nothing. finishing until the verified event arrives, finishing_slow after 30 seconds (never asks to pay "
        "again), then success with the next or first payment date and where the confirmation goes. outcome=cancelled "
        "says nothing was charged and changes nothing. No reminder is ever sent about an abandoned checkout."
    ),
)
def checkout_result(request: Request, identity: Identity = Depends(get_identity),
                    outcome: str = Query(pattern="^(success|cancelled)$"),
                    waited_seconds: int = Query(default=0, ge=0, le=3600)) -> CheckoutResultResponse:
    copy = _copy(request)
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        account = acct.load_account(s)
    home = NextStep(action="open_home", prompt=copy["checkout_left"])
    if acct.has_subscription(account):
        if acct.on_free_days(account):
            text = copy["success_early"].format(trial_end_date=acct.format_date(acct.local_trial_end(account)),
                                                email=account["email"])
        else:
            end = acct.local_date(account, account["current_period_end"])
            text = copy["success"].format(next_charge_date=acct.format_date(end) if end else "",
                                          email=account["email"])
        return CheckoutResultResponse(state="success", message=text, next_step=NextStep(action="open_home",
                                                                                        prompt=text))
    if outcome == "cancelled":
        return CheckoutResultResponse(state="left", message=copy["checkout_left"], next_step=home)
    if waited_seconds >= FINISHING_SLOW_SECONDS:
        return CheckoutResultResponse(state="finishing_slow", message=copy["finishing_slow"],
                                      next_step=NextStep(action="keep_checking", prompt=copy["finishing_slow"]))
    return CheckoutResultResponse(state="finishing", message=copy["finishing"],
                                  next_step=NextStep(action="keep_checking", prompt=copy["finishing"]))


@router.post(
    "/v1/me/subscription/portal",
    response_model=PortalResponse,
    summary="Update payment details or see past payments",
    description="UC-SUB-12. A Stripe customer portal session, created on demand and never stored or emailed. The "
                "portal is configured in Stripe without retention offers. It comes back to Settings, Subscription.",
    responses={409: {"description": "No Stripe customer yet."}, 503: {"description": "payments_unavailable"}},
)
def portal(req: PortalIn, request: Request, identity: Identity = Depends(get_identity)) -> PortalResponse:
    copy = _copy(request)
    stripe = _stripe(request)
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        account = acct.load_account(s)
    if not account["stripe_customer_id"]:
        raise ApiError(409, "no_subscription", copy["settings_status_none"])
    try:
        session = stripe.create_portal_session(account["stripe_customer_id"],
                                               f"{request.app.state.settings.app_url}/settings/subscription",
                                               update_payment=req.purpose == "update_payment")
    except StripeError as exc:
        log.warning("portal session not created code=%s", exc)
        raise ApiError(503, "payments_unavailable", copy["payments_unavailable"]) from None
    return PortalResponse(portal_url=session["url"])


@router.post(
    "/v1/me/subscription/confirm-payment",
    response_model=PortalResponse,
    summary="Confirm a payment the bank asked about",
    description="UC-SUB-10. Stripe's hosted page for the invoice waiting on the bank, fetched when the user asks and "
                "never stored. Access stays full meanwhile.",
    responses={409: {"description": "Nothing is waiting on the bank."}, 503: {"description": "payments_unavailable"}},
)
def confirm_payment(request: Request, identity: Identity = Depends(get_identity)) -> PortalResponse:
    copy = _copy(request)
    stripe = _stripe(request)
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        account = acct.load_account(s)
    if account["billing_notice"] != "action_required" or not account["stripe_subscription_id"]:
        raise ApiError(409, "nothing_to_confirm", copy["settings_status_active"].format(next_charge_date=""))
    try:
        url = stripe.latest_invoice_url(account["stripe_subscription_id"])
    except StripeError as exc:
        log.warning("invoice page not found code=%s", exc)
        url = None
    if not url:
        raise ApiError(503, "payments_unavailable", copy["payments_unavailable"])
    return PortalResponse(portal_url=url)


@router.post(
    "/v1/me/subscription/cancel",
    response_model=CancelSubscriptionResponse,
    summary="Cancel my subscription",
    description=(
        "UC-SUB-13. confirm=false returns the explainer with the period end date and two buttons, Cancel my "
        "subscription and Keep my subscription. No reason is asked and nothing is offered to keep the user. "
        "confirm=true records the request time first, then asks Stripe to cancel at the end of the paid month, and "
        "queues the cancellation confirmation. Access stays full until then. No refund. If Stripe can't be reached, "
        "it says so plainly and the job tries again from the recorded request. Works on a read-only account and "
        "during a normal break. At care level 4 nothing changes in that turn (AC-26-11)."
    ),
    responses={409: {"description": "No active subscription, or already cancelled."}},
)
def cancel(req: CancelSubscriptionIn, request: Request,
           identity: Identity = Depends(get_identity)) -> CancelSubscriptionResponse:
    copy = _copy(request)
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        account = acct.load_account(s)
        if not acct.has_subscription(account) or account["cancel_at_period_end"]:
            raise ApiError(409, "no_subscription_to_cancel", copy["settings_status_none"])
        end = acct.local_date(account, account["current_period_end"])
        end_text = acct.format_date(end) if end else ""
        if req.care_level == 4 or not req.confirm:
            text = copy["not_now_billing"] if req.care_level == 4 else copy["cancel_explainer"].format(
                period_end_date=end_text)
            options = [] if req.care_level == 4 else [
                Option(value="confirm", label=copy["cancel_confirm_button"]),
                Option(value="keep", label=copy["cancel_keep_button"])]
            return CancelSubscriptionResponse(
                canceled=False, message=text,
                subscription=subs.subscription_out(account, copy, care_level=req.care_level),
                next_step=NextStep(action="confirm_cancel" if options else "not_now", prompt=text, options=options))
        # A cancel request is recorded with a timestamp before calling Stripe (UC-SUB-13).
        s.update_subscription(cancel_requested_at=s.now)
        s.audit("subscription_cancel_requested", object_type="user", object_id=account["id"])
    try:
        _stripe(request).set_cancel_at_period_end(account["stripe_subscription_id"], True)
    except (StripeError, ApiError) as exc:
        log.warning("cancellation not sent to Stripe yet code=%s", getattr(exc, "code", exc))
        with request.app.state.db.session(identity.subject) as s:
            s.require_user()
            out = subs.subscription_out(acct.load_account(s), copy)
        return CancelSubscriptionResponse(canceled=False, message=copy["cancel_failed"], subscription=out,
                                          next_step=NextStep(action="cancel_pending", prompt=copy["cancel_failed"]))
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        s.update_subscription(cancel_at_period_end=True)
        s.queue_confirmation("subscription_canceled")
        out = subs.subscription_out(acct.load_account(s), copy)
    text = copy["cancel_done"].format(period_end_date=end_text)
    return CancelSubscriptionResponse(canceled=True, message=text, subscription=out,
                                      next_step=NextStep(action="done", prompt=text))


@router.post(
    "/v1/me/subscription/undo-cancel",
    response_model=CancelSubscriptionResponse,
    summary="Keep my subscription after all",
    description="UC-SUB-14. Before the paid month ends, removes the scheduled cancellation in Stripe. No new consent "
                "is needed because the terms are unchanged, and nothing is charged before the next normal renewal.",
    responses={409: {"description": "Nothing to undo."}, 503: {"description": "payments_unavailable"}},
)
def undo_cancel(request: Request, identity: Identity = Depends(get_identity)) -> CancelSubscriptionResponse:
    copy = _copy(request)
    stripe = _stripe(request)
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        account = acct.load_account(s)
    if not (acct.has_subscription(account) and account["cancel_at_period_end"]):
        raise ApiError(409, "nothing_to_undo", copy["settings_status_active"].format(next_charge_date=""))
    try:
        stripe.set_cancel_at_period_end(account["stripe_subscription_id"], False)
    except StripeError as exc:
        log.warning("undo cancel not sent code=%s", exc)
        raise ApiError(503, "payments_unavailable", copy["payments_unavailable"]) from None
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        s.update_subscription(cancel_at_period_end=False, cancel_requested_at=None)
        s.audit("subscription_cancel_undone", object_type="user", object_id=account["id"])
        account = acct.load_account(s)
    end = acct.local_date(account, account["current_period_end"])
    text = copy["undo_cancel_done"].format(next_charge_date=acct.format_date(end) if end else "")
    return CancelSubscriptionResponse(canceled=False, message=text, subscription=subs.subscription_out(account, copy),
                                      next_step=NextStep(action="done", prompt=text))


# ------------------------------------------------------------------ Stripe events (UC-SUB-22)

@router.post(
    "/v1/stripe/webhook",
    include_in_schema=False,
)
async def stripe_webhook(request: Request) -> JSONResponse:
    """UC-SUB-22. Verifies the Stripe signature with the endpoint secret and rejects anything that fails. Records the
    event id once (a repeat delivery changes nothing), finds the account by the account id or the Stripe customer id
    (never by email), reads the subscription's current state from Stripe, and sets Cairn's fields from it. The event
    record keeps ids, type, and times only. An event that couldn't be finished is retried by Stripe and by the
    process_stripe_events job."""
    st = request.app.state.settings
    payload = await request.body()
    try:
        event = verify_webhook(payload, request.headers.get("stripe-signature"), st.stripe_webhook_secret or "")
    except SignatureError:
        return JSONResponse({"received": False}, status_code=400)
    return handle_event(request, event)


def handle_event(request: Request, event: dict) -> JSONResponse:
    db = request.app.state.db
    account_id, customer_id, _ = subs._ids(event) if event.get("type") in subs.HANDLED else (None, None, None)
    try:
        with db.session() as s:
            known = s.stripe_account(account_id, customer_id) if (account_id or customer_id) else None
            fresh = s.record_stripe_event(event["id"], event["type"], known["_id"] if known else None)
    except ApiError as exc:
        if exc.status == 409:  # the same event arrived twice at the same moment
            return JSONResponse({"received": True})
        raise
    if not fresh:
        return JSONResponse({"received": True, "duplicate": True})
    try:
        with db.session() as s:
            outcome = subs.process_event(s, request.app.state.stripe, event)
            s.finish_stripe_event(event["id"], processed=True)
    except StripeError as exc:
        log.warning("stripe event not finished id=%s code=%s", event["id"], exc)
        with db.session() as s:
            s.finish_stripe_event(event["id"], processed=False)
        return JSONResponse({"received": True, "processed": False}, status_code=500)
    log.info("stripe event id=%s type=%s outcome=%s", event["id"], event["type"], outcome)
    return JSONResponse({"received": True})
