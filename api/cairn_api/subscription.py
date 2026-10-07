"""The subscription (database/docs/cairn-subscription-use-cases-v33.json).

$14.99 a month for the whole account (D-04), through Stripe (SUB-D-01). Access
changes only after a verified Stripe event, and Cairn reads the subscription's
current state from Stripe before acting on it, so events that arrive late or
twice can't leave stale state (SUB-D-02, UC-SUB-22). The browser's return from
Checkout never grants anything.

How Stripe statuses map to the account (status_mapping, account D-19):

  incomplete, incomplete_expired   unchanged (an expired one forgets its id)
  trialing, active, past_due       subscription_status active, access full
  unpaid, canceled                 subscription_status lapsed, read_only unless free days remain

No break or care rest ever changes any of this (BRK-D-08, SUB-D-15). The only
billing effect of a break is that a care rest moves an early subscriber's first
charge, because it pauses free days that haven't ended (UC-SUB-06).

Nothing here logs an address, a name, or a Stripe payload. Owner alerts carry
the event id and type only.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from uuid import UUID

from . import account as acct
from .copy_store import Copy
from .db import Session
from .schemas import NextStep, Option, SubscriptionOut
from .store import free_days_running
from .stripe_client import Stripe, StripeError, from_unix

log = logging.getLogger("cairn_api.subscription")

ACTIVE = {"trialing", "active", "past_due"}
ENDED = {"unpaid", "canceled"}
YEAR = timedelta(days=365)
# Events that only need recording and an owner alert (SUB-D-14). Never a message to the user, never a change.
ALERT_ONLY = {"charge.dispute.created", "charge.refunded"}
HANDLED = {"checkout.session.completed", "customer.subscription.created", "customer.subscription.updated",
           "customer.subscription.deleted", "invoice.paid", "invoice.payment_failed", "invoice.payment_action_required",
           "customer.updated", *ALERT_ONLY}


def alert_owner(reason: str, event_id: str, event_type: str) -> None:
    """The owner alert (UC-SUB-20, UC-SUB-22). Ids and type only. Route ERROR logs from cairn_api.subscription to
    the owner's alerting (cloudflare/README or the log drain)."""
    log.error("owner alert: %s event=%s type=%s", reason, event_id, event_type)


def _uuid(value) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None


def period_end(sub: dict) -> datetime | None:
    """current_period_end lives on the subscription in older API versions and on its items in newer ones."""
    if sub.get("current_period_end"):
        return from_unix(sub["current_period_end"])
    items = (sub.get("items") or {}).get("data") or []
    ends = [i["current_period_end"] for i in items if i.get("current_period_end")]
    return from_unix(max(ends)) if ends else None


def _ids(event: dict) -> tuple[UUID | None, str | None, str | None]:
    """The account id, customer id, and subscription id an event names."""
    obj = event["data"]["object"]
    kind = event["type"]
    if kind == "checkout.session.completed":
        return _uuid(obj.get("client_reference_id")), obj.get("customer"), obj.get("subscription")
    if kind.startswith("customer.subscription."):
        return _uuid((obj.get("metadata") or {}).get("account_id")), obj.get("customer"), obj.get("id")
    if kind.startswith("invoice."):
        sub = obj.get("subscription") or ((obj.get("parent") or {}).get("subscription_details") or {}).get(
            "subscription")
        return None, obj.get("customer"), sub
    if kind == "customer.updated":
        return None, obj.get("id"), None
    return None, obj.get("customer"), None


def fields_for(sub: dict, user: dict, now: datetime, event_type: str) -> tuple[dict, bool]:
    """Account fields from what Stripe reports now, and whether this made a new subscription active (for the one
    subscribed confirmation, UC-SUB-04). An event about an older subscription never overwrites a newer one."""
    stored = user.get("stripe_subscription_id")
    if stored and stored != sub["id"] and user["subscription_status"] == "active":
        return {}, False
    status = sub["status"]
    out: dict = {"stripe_customer_id": sub.get("customer") or user.get("stripe_customer_id")}
    if status == "incomplete":
        out["stripe_subscription_id"] = sub["id"]
        return out, False
    if status == "incomplete_expired":
        if stored == sub["id"]:
            out["stripe_subscription_id"] = None
        return out, False
    if status in ACTIVE:
        new = stored != sub["id"] or user["subscription_status"] != "active"
        subscribed_at = now if new or user.get("subscribed_at") is None else user["subscribed_at"]
        notice = user.get("billing_notice", "none")
        if event_type == "invoice.payment_action_required":
            notice = "action_required"
        elif status == "past_due":
            notice = "payment_failed" if notice == "none" or event_type == "invoice.payment_failed" else notice
        else:
            notice = "none"
        out.update({"stripe_subscription_id": sub["id"], "subscription_status": "active", "access": "full",
                    "current_period_end": period_end(sub),
                    "cancel_at_period_end": bool(sub.get("cancel_at_period_end")),
                    "billing_notice": notice, "subscribed_at": subscribed_at,
                    "annual_reminder_due_at": (subscribed_at + YEAR if new else user.get("annual_reminder_due_at")
                                               or subscribed_at + YEAR),
                    "stripe_checkout_session_id": None})
        if not sub.get("cancel_at_period_end"):
            out["cancel_requested_at"] = None
        return out, new
    if status in ENDED:
        out.update({"stripe_subscription_id": sub["id"], "subscription_status": "lapsed",
                    "access": "full" if free_days_running(user, now) else "read_only", "cancel_at_period_end": False,
                    "billing_notice": "none", "annual_reminder_due_at": None, "cancel_requested_at": None,
                    "current_period_end": period_end(sub) or user.get("current_period_end")})
        return out, False
    return out, False


def process_event(s: Session, stripe: Stripe | None, event: dict) -> str:
    """Applies one verified event (UC-SUB-22). Returns an outcome code for the log. Raises StripeError when Stripe
    can't be reached, so the event stays unprocessed and is retried."""
    event_id, kind = event["id"], event["type"]
    if kind not in HANDLED:
        return "ignored"
    account_id, customer_id, subscription_id = _ids(event)
    user = s.stripe_account(account_id, customer_id)
    if kind in ALERT_ONLY:
        # SUB-D-14. Recorded and alerted. No message, no screen, no access change, and Cairn never refunds.
        alert_owner("dispute or reversed charge", event_id, kind)
        return "alerted"
    if user is None:
        alert_owner("event for an unknown customer", event_id, kind)
        return "unknown_account"
    if kind == "customer.updated":
        return "applied"  # Cairn keeps no payment method details (UC-SUB-12)
    if subscription_id is None:
        if kind == "checkout.session.completed" and customer_id:
            s.apply_stripe_state(user["_id"], {"stripe_customer_id": customer_id})
        return "applied"
    if stripe is None:
        raise StripeError("not_configured")
    sub = stripe.retrieve_subscription(subscription_id)
    owner = _uuid((sub.get("metadata") or {}).get("account_id"))
    if owner is not None and owner != user["_id"]:
        alert_owner("subscription belongs to another account", event_id, kind)
        return "mismatch"
    fields, new = fields_for(sub, user, s.now, kind)
    if fields:
        s.apply_stripe_state(user["_id"], fields)
    if new:
        s.queue_confirmation_for(user["_id"], user["email"], "subscription_started")
    return "applied"


# ------------------------------------------------------------------ Settings, Subscription (UC-SUB-23)

def subscription_out(account: dict, copy: Copy, *, care_level: int = 1) -> SubscriptionOut:
    """One status line from Cairn's stored fields, without calling Stripe, and only the actions that apply.
    At care levels 3 and 4 only the cancel action shows, with no price wording."""
    subscribed = acct.has_subscription(account)
    end = acct.local_date(account, account.get("current_period_end"))
    trial_end = acct.local_trial_end(account)
    fmt = acct.format_date
    if acct.on_free_days(account) and subscribed:
        line = copy["settings_status_trial_subscribed"].format(trial_end_date=fmt(trial_end))
    elif acct.on_free_days(account):
        line = copy["settings_status_trial"].format(trial_end_date=fmt(trial_end))
    elif subscribed and account["cancel_at_period_end"]:
        line = copy["settings_status_canceling"].format(period_end_date=fmt(end) if end else "")
    elif subscribed and account["billing_notice"] != "none":
        line = copy["settings_status_payment_issue"]
    elif subscribed:
        line = copy["settings_status_active"].format(next_charge_date=fmt(end) if end else "")
    elif acct.is_read_only(account):
        line = copy["settings_status_none"]
    else:
        line = copy["settings_status_not_started"]
    actions: list[Option] = []
    can_subscribe = not subscribed and (acct.is_read_only(account) or acct.on_free_days(account)
                                        or account["subscription_status"] == "lapsed")
    if can_subscribe:
        lapsed = account["subscription_status"] == "lapsed"
        actions.append(Option(value="subscribe", label=copy["subscribe_again" if lapsed else "subscribe"]))
    if subscribed:
        if account["billing_notice"] == "action_required":
            actions.append(Option(value="confirm_payment", label=copy["action_required_button"]))
        actions += [Option(value="update_payment", label=copy["settings_update_payment"]),
                    Option(value="invoices", label=copy["settings_invoices"])]
        if account["cancel_at_period_end"]:
            actions.append(Option(value="undo_cancel", label=copy["undo_cancel_button"]))
        else:
            actions.append(Option(value="cancel", label=copy["settings_cancel"]))
    if care_level >= 3:
        line, actions = None, [a for a in actions if a.value == "cancel"]
    breaks_note = copy["settings_breaks_note"] if subscribed and care_level == 1 else None
    notice = None
    if subscribed and care_level == 1 and account["billing_notice"] != "none" and not account["on_break"]:
        notice = copy["payment_failed_inapp" if account["billing_notice"] == "payment_failed"
                      else "action_required_inapp"]
    return SubscriptionOut(
        status_line=line, breaks_note=breaks_note, subscription_status=account["subscription_status"],
        access=account["access"], billing_notice=notice, cancel_at_period_end=account["cancel_at_period_end"],
        current_period_end=account.get("current_period_end"), actions=actions,
        next_step=NextStep(action="subscription", prompt=line or copy["not_now_billing"], options=actions))


def first_charge_at(account: dict, now: datetime) -> datetime | None:
    """UC-SUB-06. While the free days run, the first charge is their end. While a care rest has them stopped,
    the end keeps moving, so it counts the pause so far."""
    if not acct.on_free_days(account):
        return None
    ends = account["trial_ends_at"]
    if account.get("trial_clock_paused_at") is not None:
        ends += now - account["trial_clock_paused_at"]
    return ends


def sync_first_charge(stripe: Stripe | None, account: dict, now: datetime) -> bool:
    """After a care rest moved the free days' end, move an early subscriber's first charge with it (UC-SUB-06).
    Best effort: the sync_early_subscriptions job does it again if Stripe can't be reached."""
    if stripe is None or not acct.has_subscription(account) or not account.get("stripe_subscription_id"):
        return False
    when = first_charge_at(account, now)
    if when is None:
        return False
    try:
        sub = stripe.retrieve_subscription(account["stripe_subscription_id"])
        if sub.get("status") == "trialing" and sub.get("trial_end") != int(when.timestamp()):
            stripe.set_trial_end(sub["id"], when)
            return True
    except StripeError as exc:
        log.warning("first charge not moved code=%s", exc)
    return False
