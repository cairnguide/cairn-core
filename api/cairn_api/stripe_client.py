"""Stripe, for the subscription (database/docs/cairn-subscription-use-cases-v33.json).

Cairn uses Stripe Checkout (Stripe's hosted page) to start a subscription, the
Stripe customer portal for payment details and past payments, and the
Subscriptions API to cancel, undo a cancellation, and move the first charge of
an early subscription. Cairn never sees a card number, bank account, or billing
address (SUB-D-01). It sends Stripe only the sign-in email and the account id
(SUB-D-10).

The price is sent with each Checkout Session (price_data) from the same
setting the copy is checked against, so what is shown and what is charged
can't differ (UC-SUB-02). It is tax inclusive with automatic tax on, so the
customer always pays exactly that amount (SUB-D-12).

Webhook events are verified with the endpoint secret before anything reads
them (verify_webhook, UC-SUB-22). Errors keep Stripe's error code and the HTTP
status only, never a message that could echo a value.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

API_BASE = "https://api.stripe.com"
# Stripe's default tolerance for a webhook timestamp, against replays.
WEBHOOK_TOLERANCE_SECONDS = 300
# Stripe Checkout refuses a trial_end less than 48 hours away. A first charge can come later than the free days'
# end, never earlier (UC-SUB-06).
CHECKOUT_MIN_TRIAL = 48 * 3600 + 300


class StripeError(Exception):
    """A Stripe request failed. str() is a short code, safe to log."""

    def __init__(self, code: str, status: int | None = None):
        super().__init__(f"stripe:{status or 'network'}:{code}"[:100])
        self.code = code
        self.status = status


class SignatureError(Exception):
    """The webhook request isn't from Stripe, or is too old."""


def encode(params: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's form encoding: nested keys in brackets, booleans as true or false."""
    out: list[tuple[str, str]] = []
    for key, value in params.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if value is None:
            continue
        if isinstance(value, dict):
            out += encode(value, name)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                out += encode(item, f"{name}[{i}]") if isinstance(item, dict) else [(f"{name}[{i}]", str(item))]
        elif isinstance(value, bool):
            out.append((name, "true" if value else "false"))
        else:
            out.append((name, str(value)))
    return out


def unix(when: datetime) -> int:
    return int(when.timestamp())


def from_unix(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def verify_webhook(payload: bytes, header: str | None, secret: str, now: float | None = None,
                   tolerance: int = WEBHOOK_TOLERANCE_SECONDS) -> dict:
    """Checks the Stripe-Signature header (HMAC SHA-256 of "timestamp.payload" with the endpoint secret) and the
    timestamp, then returns the event. Anything else raises SignatureError and changes nothing (UC-SUB-22)."""
    if not header or not secret:
        raise SignatureError("missing")
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        key, _, value = item.strip().partition("=")
        parts.setdefault(key, []).append(value)
    try:
        timestamp = int(parts["t"][0])
    except (KeyError, ValueError):
        raise SignatureError("malformed") from None
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", [])):
        raise SignatureError("mismatch")
    if abs((now if now is not None else time.time()) - timestamp) > tolerance:
        raise SignatureError("too_old")
    try:
        event = json.loads(payload)
    except ValueError:
        raise SignatureError("malformed") from None
    if not isinstance(event, dict) or not str(event.get("id", "")).startswith("evt_"):
        raise SignatureError("malformed")
    return event


def sign_webhook(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """A Stripe-Signature header for a payload. For tests and local fixtures signed with the test secret."""
    t = int(time.time()) if timestamp is None else timestamp
    sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


class Stripe:
    """The few Stripe calls Cairn makes. One instance per process. Never logs a request or response body."""

    def __init__(self, secret_key: str, http: httpx.Client | None = None, api_base: str = API_BASE):
        self._key = secret_key
        self._http = http or httpx.Client(timeout=15)
        self._base = api_base

    def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._key}"}
        body = None
        if method == "POST":
            headers["Idempotency-Key"] = str(uuid.uuid4())
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body = urlencode(encode(params or {}))
        try:
            r = self._http.request(method, f"{self._base}{path}", headers=headers, content=body,
                                   params=encode(params or {}) if method == "GET" and params else None)
        except httpx.HTTPError as exc:
            raise StripeError(type(exc).__name__) from None
        if r.status_code >= 400:
            try:
                code = str(r.json()["error"].get("code") or r.json()["error"].get("type") or "error")
            except (ValueError, KeyError, TypeError, AttributeError):
                code = "error"
            raise StripeError(code, r.status_code)
        return r.json()

    # ------------------------------------------------------------------ Checkout (UC-SUB-03)

    def create_checkout_session(self, *, account_id: str, email: str, customer_id: str | None, price_cents: int,
                                product_id: str, success_url: str, cancel_url: str, submit_text: str,
                                trial_end: datetime | None) -> dict:
        params: dict[str, Any] = {
            "mode": "subscription",
            "client_reference_id": account_id,
            "line_items": [{"quantity": 1, "price_data": {
                "currency": "usd", "unit_amount": price_cents, "product": product_id,
                "recurring": {"interval": "month"}, "tax_behavior": "inclusive"}}],
            "automatic_tax": {"enabled": True},
            "billing_address_collection": "required",
            "custom_text": {"submit": {"message": submit_text}},
            "subscription_data": {"metadata": {"account_id": account_id}},
            "metadata": {"account_id": account_id},
            "success_url": success_url,
            "cancel_url": cancel_url,
        }
        if customer_id:
            params["customer"] = customer_id
            params["customer_update"] = {"address": "auto"}
        else:
            params["customer_email"] = email
        if trial_end is not None:
            earliest = int(time.time()) + CHECKOUT_MIN_TRIAL
            params["subscription_data"]["trial_end"] = max(unix(trial_end), earliest)
        return self._request("POST", "/v1/checkout/sessions", params)

    def expire_checkout_session(self, session_id: str) -> None:
        try:
            self._request("POST", f"/v1/checkout/sessions/{session_id}/expire")
        except StripeError as exc:
            if exc.status not in (400, 404):  # already completed or expired
                raise

    # ------------------------------------------------------------------ the customer portal (UC-SUB-12)

    def create_portal_session(self, customer_id: str, return_url: str, *, update_payment: bool) -> dict:
        params: dict[str, Any] = {"customer": customer_id, "return_url": return_url}
        if update_payment:
            params["flow_data"] = {"type": "payment_method_update"}
        return self._request("POST", "/v1/billing_portal/sessions", params)

    # ------------------------------------------------------------------ subscriptions

    def retrieve_subscription(self, subscription_id: str) -> dict:
        return self._request("GET", f"/v1/subscriptions/{subscription_id}")

    def set_cancel_at_period_end(self, subscription_id: str, cancel: bool) -> dict:
        """UC-SUB-13 and UC-SUB-14. No proration, no refund."""
        return self._request("POST", f"/v1/subscriptions/{subscription_id}",
                             {"cancel_at_period_end": cancel, "proration_behavior": "none"})

    def cancel_now(self, subscription_id: str) -> dict:
        """UC-SUB-16. Cancels right away with no refund and no proration credit (SUB-D-05)."""
        try:
            return self._request("DELETE", f"/v1/subscriptions/{subscription_id}")
        except StripeError as exc:
            if exc.status == 404 or exc.code == "resource_missing":
                return {"id": subscription_id, "status": "canceled"}
            raise

    def set_trial_end(self, subscription_id: str, when: datetime) -> dict:
        """UC-SUB-06. Moves an early subscriber's first charge to the free days' end."""
        return self._request("POST", f"/v1/subscriptions/{subscription_id}",
                             {"trial_end": unix(when), "proration_behavior": "none"})

    def latest_invoice_url(self, subscription_id: str) -> str | None:
        """UC-SUB-10. Stripe's hosted page for the invoice the bank wants confirmed. Fetched when the user asks,
        never stored."""
        sub = self._request("GET", f"/v1/subscriptions/{subscription_id}", {"expand": ["latest_invoice"]})
        invoice = sub.get("latest_invoice")
        return invoice.get("hosted_invoice_url") if isinstance(invoice, dict) else None

    def retrieve_event(self, event_id: str) -> dict:
        """For events the webhook couldn't finish. Stripe keeps them for 30 days."""
        return self._request("GET", f"/v1/events/{event_id}")
