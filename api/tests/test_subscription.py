"""The subscription (database/docs/cairn-subscription-use-cases-v33.json, UC-SUB-01 to UC-SUB-23).

test_scope: these tests check Cairn's screens, copy, state, and the requests Cairn makes to Stripe, through a
mocked Stripe client (conftest.FakeStripe) and fixture events signed with the test endpoint secret. They never
check that money arrived, that a receipt is valid, or that anyone received an email (SUB-D-09).

The webhook and status mapping tests need CAIRN_TEST_MONGODB_URI. The signature and client tests don't.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
import pytest

from cairn_api import maintenance, outbound
from cairn_api.copy_store import load_copy, load_subscription_copy, subscription_terms_version
from cairn_api.main import check_prices
from cairn_api.stripe_client import SignatureError, Stripe, encode, sign_webhook, verify_webhook

from .conftest import REPO, SETTINGS, active_case, as_user, expire_trial, new_draft, register, user_id
from .test_account_lifecycle import FakeMailer, send_outbound

SPEC = json.loads((REPO / "database" / "docs" / "cairn-subscription-use-cases-v33.json").read_text())
COPY = load_subscription_copy()
UC = {u["id"]: u for u in SPEC["use_cases"]}
SECRET = SETTINGS.stripe_webhook_secret
BILLING_WORDS = ("$", "price", "payment", "subscri", "charge", "billing", "free days")


# ------------------------------------------------------------------ helpers

def event(kind: str, obj: dict, event_id: str | None = None) -> dict:
    return {"id": event_id or f"evt_{uuid.uuid4().hex[:16]}", "type": kind, "data": {"object": obj}}


def deliver(api, evt: dict, *, secret: str = SECRET, timestamp: int | None = None, signature: str | None = None):
    payload = json.dumps(evt).encode()
    header = signature if signature is not None else sign_webhook(payload, secret, timestamp)
    return api.post("/v1/stripe/webhook", content=payload, headers={"Stripe-Signature": header,
                                                                     "Content-Type": "application/json"})


def stripe_sub(api, sub_id: str, account_id, *, status="active", customer="cus_Test1", cancel=False,
               period_end: datetime | None = None, trial_end: datetime | None = None) -> dict:
    end = period_end or datetime.now(timezone.utc) + timedelta(days=30)
    sub = {"id": sub_id, "status": status, "customer": customer, "cancel_at_period_end": cancel,
           "metadata": {"account_id": str(account_id)},
           "items": {"data": [{"current_period_end": int(end.timestamp())}]}}
    if trial_end is not None:
        sub["trial_end"] = int(trial_end.timestamp())
    api.stripe.subscriptions[sub_id] = sub
    return sub


def subscribe(api, subject: str, *, customer: str | None = None, status: str = "active") -> tuple[UUID, str]:
    """A verified checkout.session.completed and the subscription it made."""
    uid = user_id(api, subject)
    sub_id = f"sub_{uuid.uuid4().hex[:12]}"
    customer = customer or f"cus_{uuid.uuid4().hex[:12]}"
    stripe_sub(api, sub_id, uid, status=status, customer=customer)
    r = deliver(api, event("checkout.session.completed", {"client_reference_id": str(uid), "customer": customer,
                                                          "subscription": sub_id}))
    assert r.status_code == 200, r.text
    return uid, sub_id


def account(api, subject: str) -> dict:
    return api.db.users.find_one({"idp_subject": subject})


def access(api, subject: str) -> str:
    """The effective access, as the API decides it on every request (store.effective_access)."""
    return api.get("/v1/me", headers=as_user(subject)).json()["account"]["access"]


def terms_and_checkout(api, subject: str, **extra):
    h = as_user(subject)
    terms = api.get("/v1/me/subscription/terms", headers=h)
    assert terms.status_code == 200, terms.text
    body = {"agreed": True, "document_version": terms.json()["checkbox"]["document_version"], "client": "web/1",
            **extra}
    return terms.json(), api.post("/v1/me/subscription/checkout", json=body, headers=h)


# ------------------------------------------------------------------ copy and price (no database)

def test_copy_is_the_spec_copy_verbatim():
    assert COPY.spec == SPEC["copy"] and COPY.version == SPEC["spec"]["version"] == "3.3.0"
    for text in COPY.all_strings:
        assert "—" not in text and ";" not in text, text


def test_the_price_shown_is_the_price_charged():
    """UC-SUB-02: one config value for display and charge. Copy that names another price refuses to start."""
    assert SETTINGS.subscription_price_cents == 1499 and SETTINGS.price_text == "$14.99"
    check_prices("$14.99", load_copy(), COPY)
    with pytest.raises(RuntimeError):
        check_prices("$19.99", COPY)


def test_no_retention_offer_help_paying_or_dispute_help_anywhere():
    """SUB-D-04, SUB-D-13, SUB-D-14."""
    text = " ".join(COPY.all_strings).lower()
    for word in ("discount", "coupon", "are you sure", "we'll miss", "hardship", "waive", "dispute", "refund you"):
        assert word not in text, word
    assert "There are no refunds" in COPY["terms_cancel"] and "no refund" in COPY["delete_account_with_subscription"]


def test_settings_say_a_break_never_pauses_the_subscription():
    """SUB-D-15 and BRK-D-08, before purchase, in Settings, and in the yearly reminder."""
    assert "doesn't pause" in COPY["terms_breaks"] and "doesn't pause" in COPY["settings_breaks_note"]
    assert "doesn't pause your subscription" in COPY["annual_reminder_body"]


# ------------------------------------------------------------------ Stripe signatures (UC-SUB-22, no database)

def test_a_good_signature_verifies_and_returns_the_event():
    payload = json.dumps(event("invoice.paid", {"customer": "cus_1"}, "evt_abc123")).encode()
    assert verify_webhook(payload, sign_webhook(payload, SECRET), SECRET)["id"] == "evt_abc123"


@pytest.mark.parametrize("header", [None, "", "t=1,v1=deadbeef", "v1=abc", "garbage"])
def test_a_bad_or_missing_signature_is_refused(header):
    payload = json.dumps(event("invoice.paid", {})).encode()
    with pytest.raises(SignatureError):
        verify_webhook(payload, header, SECRET)


def test_an_old_signature_is_refused_against_replays():
    payload = json.dumps(event("invoice.paid", {})).encode()
    old = int(time.time()) - 3600
    with pytest.raises(SignatureError):
        verify_webhook(payload, sign_webhook(payload, SECRET, old), SECRET)


def test_another_secret_is_refused():
    payload = json.dumps(event("invoice.paid", {})).encode()
    with pytest.raises(SignatureError):
        verify_webhook(payload, sign_webhook(payload, "whsec_other"), SECRET)


def test_stripe_form_encoding():
    pairs = encode({"mode": "subscription", "line_items": [{"price_data": {"unit_amount": 1499}}],
                    "automatic_tax": {"enabled": True}, "skip": None})
    assert pairs == [("mode", "subscription"), ("line_items[0][price_data][unit_amount]", "1499"),
                     ("automatic_tax[enabled]", "true")]


def test_the_checkout_request_sends_only_the_account_id_and_email():
    """UC-SUB-03 and SUB-D-10, at the HTTP level: what Cairn asks Stripe for."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content.decode(), request.headers))
        return httpx.Response(200, json={"id": "cs_test_1", "url": "https://checkout.stripe.test/cs_test_1"})

    stripe = Stripe("sk_test_fake", http=httpx.Client(transport=httpx.MockTransport(handler)))
    stripe.create_checkout_session(account_id="acc-1", email="pat@example.test", customer_id=None, price_cents=1499,
                                   product_id="prod_1", success_url="https://app.test/s", cancel_url="https://app.test/c",
                                   submit_text=COPY["checkout_submit_text"], trial_end=None)
    method, path, body, headers = seen[0]
    assert (method, path) == ("POST", "/v1/checkout/sessions")
    assert headers["Authorization"] == "Bearer sk_test_fake" and headers["Idempotency-Key"]
    from urllib.parse import parse_qsl
    form = dict(parse_qsl(body))
    assert form["mode"] == "subscription" and form["client_reference_id"] == "acc-1"
    assert form["line_items[0][price_data][unit_amount]"] == "1499"
    assert form["line_items[0][price_data][tax_behavior]"] == "inclusive"
    assert form["line_items[0][price_data][recurring][interval]"] == "month"
    assert form["automatic_tax[enabled]"] == "true" and form["billing_address_collection"] == "required"
    assert form["custom_text[submit][message]"] == COPY["checkout_submit_text"]
    assert form["customer_email"] == "pat@example.test"
    assert {k for k in form if "metadata" in k} == {"metadata[account_id]", "subscription_data[metadata][account_id]"}
    assert set(form.values()) >= {"acc-1"} and "preferred_name" not in body and "display_name" not in body


def test_stripe_errors_keep_a_code_and_never_the_message():
    def handler(request):
        return httpx.Response(402, json={"error": {"code": "card_declined", "message": "Card 4242 was declined"}})

    stripe = Stripe("sk_test_fake", http=httpx.Client(transport=httpx.MockTransport(handler)))
    from cairn_api.stripe_client import StripeError
    with pytest.raises(StripeError) as e:
        stripe.retrieve_subscription("sub_1")
    assert str(e.value) == "stripe:402:card_declined" and "4242" not in str(e.value)


# ------------------------------------------------------------------ UC-SUB-01 and UC-SUB-07

def test_the_subscribe_prompt_shows_once_a_day_at_level_1_only(api):
    register(api, "su01")
    active_case(api, "su01")
    expire_trial(api, "su01")
    h = as_user("su01")
    assert api.get("/v1/home", params={"care_level": 3}, headers=h).json()["subscribe_prompt"] is None
    assert api.get("/v1/home", params={"care_level": 2}, headers=h).json()["subscribe_prompt"] is None
    home = api.get("/v1/home", headers=h).json()
    prompt = home["subscribe_prompt"]
    assert (prompt["title"], prompt["body"]) == (COPY["subscribe_prompt_title"], COPY["subscribe_prompt_body"])
    assert [o["label"] for o in prompt["options"]] == [COPY["subscribe_prompt_button"],
                                                       COPY["subscribe_prompt_not_now"]]
    assert any(n["text"] == COPY["read_only_banner"] for n in home["notes"])
    assert api.get("/v1/home", headers=h).json()["subscribe_prompt"] is None  # never more than once a day
    home = api.get("/v1/home", params={"care_level": 3}, headers=h).json()
    shown = json.dumps([home["notes"], home["cases"], home["subscribe_prompt"], home["greeting"]]).lower()
    for word in ("subscri", "$", "read-only"):
        assert word not in shown  # [SAFETY] nothing about subscribing at levels 3 and 4


def test_no_prompt_during_a_break_or_while_a_care_rest_stops_the_free_days(api):
    register(api, "su02")
    active_case(api, "su02")
    h = as_user("su02")
    api.post("/v1/me/break", json={"choice": "until_back", "care_level": 3}, headers=h)
    expire_trial(api, "su02", days=40)
    home = api.get("/v1/home", headers=h).json()
    assert home["route"] == "resting" and home["subscribe_prompt"] is None
    assert api.get("/v1/me", headers=h).json()["account"]["access"] == "full"  # never read-only while paused


def test_a_new_journey_on_a_read_only_account_needs_a_subscription(api):
    """UC-SUB-07: the draft is unchanged either way."""
    register(api, "su03")
    active_case(api, "su03")
    expire_trial(api, "su03")
    draft = new_draft(api, "su03")["case"]["id"]
    preview = api.get(f"/v1/cases/{draft}/journey/preview", headers=as_user("su03")).json()
    assert preview["next_step"]["prompt"] == COPY["new_journey_needs_subscription"]
    assert preview["start_available"] is False
    assert api.db.cases.find_one({"_id": UUID(draft)})["status"] == "draft"


# ------------------------------------------------------------------ UC-SUB-02 and UC-SUB-03

def test_terms_show_everything_before_payment_and_record_the_consent(api):
    register(api, "su04")
    active_case(api, "su04")
    expire_trial(api, "su04")
    api.stripe.calls.clear()
    terms, r = terms_and_checkout(api, "su04")
    assert terms["lines"] == [COPY["terms_price"], COPY["terms_tax"], COPY["terms_renewal"], COPY["terms_cancel"],
                              COPY["terms_includes"], COPY["terms_breaks"], COPY["terms_secure_note"]]
    assert terms["checkbox"]["checked"] is False and terms["checkbox"]["label"] == COPY["terms_checkbox"]
    assert terms["button"] == COPY["terms_button"] and terms["price"] == "$14.99"
    assert r.status_code == 200 and r.json()["checkout_url"].startswith("https://checkout.stripe.test/")
    [(name, kw)] = api.stripe.calls
    assert name == "create_checkout_session"
    assert kw["account_id"] == str(user_id(api, "su04")) and kw["price_cents"] == 1499
    assert kw["submit_text"] == COPY["checkout_submit_text"] and kw["trial_end"] is None
    assert kw["success_url"].endswith("outcome=success") and kw["cancel_url"].endswith("outcome=cancelled")
    consent = api.db.consents.find_one({"user_id": user_id(api, "su04"), "purpose": "subscription_terms"})
    assert consent["price_shown"] == "$14.99" and consent["policy_version"] == subscription_terms_version(COPY)
    u = account(api, "su04")
    assert access(api, "su04") == "read_only" and u["subscription_status"] == "none"  # a session changes nothing
    assert u["stripe_checkout_session_id"].startswith("cs_test_")


def test_only_one_open_checkout_session_per_account(api):
    register(api, "su05")
    active_case(api, "su05")
    expire_trial(api, "su05")
    terms_and_checkout(api, "su05")
    first = account(api, "su05")["stripe_checkout_session_id"]
    api.stripe.calls.clear()
    terms_and_checkout(api, "su05")
    assert api.stripe.names() == ["expire_checkout_session", "create_checkout_session"]
    assert api.stripe.calls[0][1]["session_id"] == first


def test_subscribing_early_charges_first_when_the_free_days_end(api):
    """UC-SUB-06 and SUB-D-07."""
    register(api, "su06")
    active_case(api, "su06")
    api.stripe.calls.clear()
    terms, r = terms_and_checkout(api, "su06")
    ends = account(api, "su06")["trial_ends_at"]
    assert COPY["terms_renewal_early"].split("{")[0] in terms["lines"][2]
    assert api.stripe.calls[-1][1]["trial_end"] == ends


def test_checkout_waits_at_levels_2_to_4_and_says_plainly_when_stripe_is_down(api):
    register(api, "su07")
    active_case(api, "su07")
    expire_trial(api, "su07")
    h = as_user("su07")
    assert api.get("/v1/me/subscription/terms", params={"care_level": 3}, headers=h).status_code == 409
    api.stripe.fail.add("create_checkout_session")
    try:
        _, r = terms_and_checkout(api, "su07")
    finally:
        api.stripe.fail.clear()
    assert r.status_code == 503 and r.json()["detail"] == COPY["payments_unavailable"]
    assert api.db.consents.count_documents({"user_id": user_id(api, "su07"), "purpose": "subscription_terms"}) == 0


def test_terms_must_be_the_ones_shown(api):
    register(api, "su08")
    active_case(api, "su08")
    r = api.post("/v1/me/subscription/checkout", headers=as_user("su08"),
                 json={"agreed": True, "document_version": "copy=old", "client": "web/1"})
    assert r.status_code == 409 and r.json()["code"] == "terms_outdated"


# ------------------------------------------------------------------ UC-SUB-04, UC-SUB-05, UC-SUB-22

def test_the_success_page_never_grants_access_without_a_verified_event(api):
    register(api, "su09")
    active_case(api, "su09")
    expire_trial(api, "su09")
    h = as_user("su09")
    r = api.get("/v1/me/subscription/checkout-result", params={"outcome": "success"}, headers=h).json()
    assert r["state"] == "finishing" and r["message"] == COPY["finishing"]
    r = api.get("/v1/me/subscription/checkout-result", params={"outcome": "success", "waited_seconds": 31},
                headers=h).json()
    assert r["state"] == "finishing_slow" and "pay again" in r["message"]
    assert access(api, "su09") == "read_only"
    uid, sub_id = subscribe(api, "su09")
    u = account(api, "su09")
    assert (u["subscription_status"], u["access"], u["stripe_subscription_id"]) == ("active", "full", sub_id)
    r = api.get("/v1/me/subscription/checkout-result", params={"outcome": "success"}, headers=h).json()
    assert r["state"] == "success" and "su09@example.test" in r["message"]
    # Read-only data is editable again, with nothing lost.
    assert api.get("/v1/me", headers=h).json()["account"]["access"] == "full"
    # The subscribed confirmation is queued exactly once.
    kinds = [x["action_type"] for x in api.db.action_confirmation_outbox.find({"user_id": uid})]
    assert kinds.count("subscription_started") == 1


def test_leaving_checkout_changes_nothing_and_sends_nothing(api):
    register(api, "su10")
    active_case(api, "su10")
    expire_trial(api, "su10")
    terms_and_checkout(api, "su10")
    r = api.get("/v1/me/subscription/checkout-result", params={"outcome": "cancelled"},
                headers=as_user("su10")).json()
    assert r["state"] == "left" and r["message"] == COPY["checkout_left"]
    u = account(api, "su10")
    assert (access(api, "su10"), u["subscription_status"]) == ("read_only", "none")
    assert api.db.consents.count_documents({"user_id": u["_id"], "purpose": "subscription_terms"}) == 1  # proof kept


def test_events_are_verified_recorded_once_and_applied_from_stripes_current_state(api):
    register(api, "su11")
    active_case(api, "su11")
    uid, sub_id = subscribe(api, "su11")
    bad = deliver(api, event("customer.subscription.deleted", {"id": sub_id, "customer": "cus_x"}), secret="whsec_x")
    assert bad.status_code == 400 and account(api, "su11")["subscription_status"] == "active"  # unsigned: nothing
    # Out of order: an old "updated" arrives after Stripe already reports it canceled. Cairn follows Stripe now.
    customer = account(api, "su11")["stripe_customer_id"]
    stripe_sub(api, sub_id, uid, status="canceled", customer=customer)
    stale = event("customer.subscription.updated", {"id": sub_id, "customer": customer, "status": "active",
                                                    "metadata": {"account_id": str(uid)}})
    assert deliver(api, stale).status_code == 200
    u = account(api, "su11")
    assert u["subscription_status"] == "lapsed"
    # The same event twice changes state once.
    stripe_sub(api, sub_id, uid, status="active", customer=customer)
    assert deliver(api, stale).json().get("duplicate") is True
    assert account(api, "su11")["subscription_status"] == "lapsed"
    row = api.db.stripe_events.find_one({"_id": stale["id"]})
    assert set(row) == {"_id", "type", "account_id", "received_at", "processed_at", "attempts"}  # no payload


def test_an_event_for_an_unknown_customer_is_never_applied_to_a_guessed_account(api, caplog):
    register(api, "su12")
    evt = event("invoice.paid", {"customer": "cus_nobody", "subscription": "sub_nobody",
                                 "customer_email": "su12@example.test"})
    assert deliver(api, evt).status_code == 200
    assert account(api, "su12")["subscription_status"] == "none"  # never looked up by email
    assert "owner alert" in caplog.text and evt["id"] in caplog.text
    assert "su12@example.test" not in caplog.text


@pytest.mark.parametrize("stripe_status,expected", [
    ("trialing", ("active", "full")), ("active", ("active", "full")), ("past_due", ("active", "full")),
    ("unpaid", ("lapsed", "read_only")), ("canceled", ("lapsed", "read_only"))])
def test_status_mapping(api, stripe_status, expected):
    """status_mapping and SUB-D-06: full access while Stripe retries. Read-only only once it ends."""
    subject = f"su13-{stripe_status}"
    register(api, subject)
    active_case(api, subject)
    expire_trial(api, subject)
    uid, sub_id = subscribe(api, subject)
    customer = account(api, subject)["stripe_customer_id"]
    stripe_sub(api, sub_id, uid, status=stripe_status, customer=customer)
    deliver(api, event("customer.subscription.updated", {"id": sub_id, "customer": customer,
                                                         "metadata": {"account_id": str(uid)}}))
    u = account(api, subject)
    assert (u["subscription_status"], u["access"]) == expected
    assert api.get("/v1/me", headers=as_user(subject)).json()["account"]["access"] == expected[1]


def test_an_incomplete_payment_that_expires_is_not_a_subscription(api):
    """UC-SUB-05 alternate flow: incomplete, then incomplete_expired forgets the id."""
    register(api, "su14")
    active_case(api, "su14")
    expire_trial(api, "su14")
    uid, sub_id = subscribe(api, "su14", status="incomplete")
    u = account(api, "su14")
    assert (u["subscription_status"], access(api, "su14"), u["stripe_subscription_id"]) == ("none", "read_only", sub_id)
    stripe_sub(api, sub_id, uid, status="incomplete_expired", customer=u["stripe_customer_id"])
    deliver(api, event("customer.subscription.updated", {"id": sub_id, "customer": u["stripe_customer_id"],
                                                         "metadata": {"account_id": str(uid)}}))
    assert account(api, "su14")["stripe_subscription_id"] is None


# ------------------------------------------------------------------ UC-SUB-08 to UC-SUB-11

def test_a_failed_renewal_keeps_access_and_shows_a_quiet_notice_at_level_1_only(api):
    register(api, "su15")
    active_case(api, "su15")
    expire_trial(api, "su15")
    uid, sub_id = subscribe(api, "su15")
    customer = account(api, "su15")["stripe_customer_id"]
    stripe_sub(api, sub_id, uid, status="past_due", customer=customer)
    deliver(api, event("invoice.payment_failed", {"customer": customer, "subscription": sub_id}))
    u = account(api, "su15")
    assert (u["billing_notice"], u["access"]) == ("payment_failed", "full")
    h = as_user("su15")
    assert COPY["payment_failed_inapp"] in [n["text"] for n in api.get("/v1/home", headers=h).json()["notes"]]
    assert COPY["payment_failed_inapp"] not in json.dumps(api.get("/v1/home", params={"care_level": 3},
                                                                  headers=h).json())
    mailer = FakeMailer()
    send_outbound(api, mailer)
    assert not [m for m in mailer.to("su15@example.test") if "payment" in m[1].lower()]  # Stripe emails, not Cairn
    stripe_sub(api, sub_id, uid, status="active", customer=customer)
    deliver(api, event("invoice.paid", {"customer": customer, "subscription": sub_id}))
    assert account(api, "su15")["billing_notice"] == "none"


def test_the_bank_asking_to_confirm_opens_stripes_page(api):
    register(api, "su16")
    active_case(api, "su16")
    uid, sub_id = subscribe(api, "su16")
    customer = account(api, "su16")["stripe_customer_id"]
    stripe_sub(api, sub_id, uid, status="past_due", customer=customer)
    deliver(api, event("invoice.payment_action_required", {"customer": customer, "subscription": sub_id}))
    assert account(api, "su16")["billing_notice"] == "action_required"
    out = api.get("/v1/me/subscription", headers=as_user("su16")).json()
    assert "confirm_payment" in [a["value"] for a in out["actions"]]
    r = api.post("/v1/me/subscription/confirm-payment", headers=as_user("su16")).json()
    assert r["portal_url"].startswith("https://invoice.stripe.test/")


def test_a_renewal_moves_the_period_end_and_sends_nothing(api):
    register(api, "su17")
    active_case(api, "su17")
    uid, sub_id = subscribe(api, "su17")
    customer = account(api, "su17")["stripe_customer_id"]
    later = datetime.now(timezone.utc) + timedelta(days=60)
    stripe_sub(api, sub_id, uid, customer=customer, period_end=later)
    before = api.db.action_confirmation_outbox.count_documents({"user_id": uid})
    deliver(api, event("invoice.paid", {"customer": customer, "subscription": sub_id}))
    assert int(account(api, "su17")["current_period_end"].timestamp()) == int(later.timestamp())
    assert api.db.action_confirmation_outbox.count_documents({"user_id": uid}) == before


# ------------------------------------------------------------------ UC-SUB-12 to UC-SUB-16

def test_portal_sessions_are_made_on_demand_and_never_stored(api):
    register(api, "su18")
    active_case(api, "su18")
    subscribe(api, "su18")
    r = api.post("/v1/me/subscription/portal", json={"purpose": "update_payment"}, headers=as_user("su18")).json()
    assert r["portal_url"].startswith("https://billing.stripe.test/")
    assert api.stripe.calls[-1][1]["update_payment"] is True
    assert "billing.stripe.test" not in json.dumps(account(api, "su18"), default=str)


def test_cancel_records_first_cancels_at_period_end_and_can_be_undone(api):
    register(api, "su19")
    active_case(api, "su19")
    expire_trial(api, "su19")
    uid, sub_id = subscribe(api, "su19")
    h = as_user("su19")
    explain = api.post("/v1/me/subscription/cancel", json={"confirm": False}, headers=h).json()
    assert explain["canceled"] is False and "There are no refunds" in explain["message"]
    assert [o["label"] for o in explain["next_step"]["options"]] == [COPY["cancel_confirm_button"],
                                                                     COPY["cancel_keep_button"]]
    api.stripe.calls.clear()
    done = api.post("/v1/me/subscription/cancel", json={"confirm": True}, headers=h).json()
    assert done["canceled"] is True and done["message"].startswith("Your subscription is canceled.")
    assert api.stripe.calls == [("set_cancel_at_period_end", {"subscription_id": sub_id, "cancel": True})]
    u = account(api, "su19")
    assert u["cancel_requested_at"] is not None and u["cancel_at_period_end"] is True and u["access"] == "full"
    assert [x["action_type"] for x in api.db.action_confirmation_outbox.find({"user_id": uid})].count(
        "subscription_canceled") == 1
    status = api.get("/v1/me/subscription", headers=h).json()
    assert status["status_line"].startswith("Canceled.") and "undo_cancel" in [a["value"] for a in status["actions"]]
    undo = api.post("/v1/me/subscription/undo-cancel", headers=h).json()
    assert undo["message"].startswith("Your subscription will continue.")
    assert account(api, "su19")["cancel_at_period_end"] is False


def test_cancel_waits_at_level_4_and_a_failed_call_is_retried_by_the_job(api):
    register(api, "su20")
    active_case(api, "su20")
    uid, sub_id = subscribe(api, "su20")
    h = as_user("su20")
    r = api.post("/v1/me/subscription/cancel", json={"confirm": True, "care_level": 4}, headers=h).json()
    assert r["canceled"] is False and account(api, "su20")["cancel_requested_at"] is None  # AC-26-11
    api.stripe.fail.add("set_cancel_at_period_end")
    try:
        r = api.post("/v1/me/subscription/cancel", json={"confirm": True}, headers=h).json()
    finally:
        api.stripe.fail.clear()
    assert r["canceled"] is False and r["message"] == COPY["cancel_failed"]
    assert account(api, "su20")["cancel_requested_at"] is not None  # recorded before calling Stripe
    done, failed = maintenance.retry_cancellations(api.jobs_db, api.stripe)
    assert done >= 1 and account(api, "su20")["cancel_at_period_end"] is True


def test_at_period_end_the_account_becomes_read_only_and_nothing_is_deleted(api):
    """UC-SUB-11 and UC-SUB-13 step 5."""
    register(api, "su21")
    cid, _ = active_case(api, "su21")
    expire_trial(api, "su21")
    uid, sub_id = subscribe(api, "su21")
    customer = account(api, "su21")["stripe_customer_id"]
    stripe_sub(api, sub_id, uid, status="canceled", customer=customer)
    deliver(api, event("customer.subscription.deleted", {"id": sub_id, "customer": customer,
                                                         "metadata": {"account_id": str(uid)}}))
    u = account(api, "su21")
    assert (u["subscription_status"], u["access"]) == ("lapsed", "read_only")
    assert api.get(f"/v1/cases/{cid}", headers=as_user("su21")).status_code == 200
    prompt = api.get("/v1/home", headers=as_user("su21")).json()["subscribe_prompt"]
    assert prompt["body"] == COPY["ended_read_only"] and prompt["options"][0]["label"] == COPY["subscribe_again"]


def test_subscribing_again_reuses_the_customer_and_offers_no_new_free_days(api):
    """UC-SUB-15."""
    register(api, "su22")
    active_case(api, "su22")
    expire_trial(api, "su22")
    uid, sub_id = subscribe(api, "su22", customer="cus_SameCustomer22")
    stripe_sub(api, sub_id, uid, status="canceled", customer="cus_SameCustomer22")
    deliver(api, event("customer.subscription.deleted", {"id": sub_id, "customer": "cus_SameCustomer22"}))
    api.stripe.calls.clear()
    terms, r = terms_and_checkout(api, "su22")
    kw = api.stripe.calls[-1][1]
    assert kw["customer_id"] == "cus_SameCustomer22" and kw["trial_end"] is None
    assert terms["lines"][2] == COPY["terms_renewal"]


def test_deleting_the_account_cancels_stripe_first_and_never_deletes_if_that_fails(api):
    """UC-SUB-16."""
    register(api, "su23")
    active_case(api, "su23")
    uid, sub_id = subscribe(api, "su23")
    h = as_user("su23")
    info = api.get("/v1/me/deletion", headers=h).json()
    assert info["subscription_note"]["text"] == COPY["delete_account_with_subscription"]
    api.stripe.fail.add("cancel_now")
    try:
        r = api.post("/v1/me/deletion", json={"confirm": True}, headers=h)
    finally:
        api.stripe.fail.clear()
    assert r.status_code == 503 and r.json()["detail"] == COPY["delete_cancel_failed"]
    assert account(api, "su23") is not None  # nothing deleted
    api.stripe.calls.clear()
    assert api.post("/v1/me/deletion", json={"confirm": True}, headers=h).status_code == 200
    assert api.stripe.calls == [("cancel_now", {"subscription_id": sub_id})]
    assert account(api, "su23") is None


# ------------------------------------------------------------------ UC-SUB-17, UC-SUB-20, UC-SUB-21, UC-SUB-23

def test_the_yearly_reminder_is_queued_once_and_waits_for_a_break(api):
    register(api, "su24")
    active_case(api, "su24")
    uid, _ = subscribe(api, "su24")
    due = account(api, "su24")["annual_reminder_due_at"]
    assert due - account(api, "su24")["subscribed_at"] == timedelta(days=365)
    api.db.users.update_one({"_id": uid}, {"$set": {"annual_reminder_due_at": datetime.now(timezone.utc)
                                                    - timedelta(minutes=1)}})
    api.post("/v1/me/break", json={"choice": "week"}, headers=as_user("su24"))
    mine = lambda: [r for r in maintenance.claim_annual_reminders(api.jobs_db) if r["user_id"] == uid]  # noqa: E731
    assert mine() == []  # waits for the break, within 30 days
    api.delete("/v1/me/break", headers=as_user("su24"))
    mailer = FakeMailer()
    outbound.run_once(api.jobs_db, mailer, load_copy(), api.app.state.case_copy, sub_copy=COPY)
    assert len([m for m in mailer.to("su24@example.test") if m[1] == COPY["annual_reminder_body"]]) == 1
    assert mine() == []  # the next one is a year on


def test_a_dispute_alerts_the_owner_and_changes_nothing_for_the_user(api, caplog):
    """UC-SUB-20 and SUB-D-14."""
    register(api, "su25")
    active_case(api, "su25")
    uid, _ = subscribe(api, "su25")
    customer = account(api, "su25")["stripe_customer_id"]
    before = api.db.action_confirmation_outbox.count_documents({"user_id": uid})
    evt = event("charge.dispute.created", {"charge": "ch_1", "customer": customer})
    assert deliver(api, evt).status_code == 200
    assert "owner alert" in caplog.text and evt["id"] in caplog.text
    u = account(api, "su25")
    assert (u["subscription_status"], u["access"]) == ("active", "full")
    assert api.db.action_confirmation_outbox.count_documents({"user_id": uid}) == before
    assert "refund" not in [n for n, _ in api.stripe.calls]


def test_breaks_never_call_stripe_or_change_a_subscription_field(api):
    """UC-SUB-21, SUB-D-15, BRK-D-08, AC-BRK-09."""
    register(api, "su26")
    active_case(api, "su26")
    expire_trial(api, "su26")
    subscribe(api, "su26")
    fields = ("subscription_status", "stripe_subscription_id", "current_period_end", "cancel_at_period_end",
              "billing_notice", "access", "annual_reminder_due_at")
    before = {f: account(api, "su26")[f] for f in fields}
    api.stripe.calls.clear()
    h = as_user("su26")
    started = api.post("/v1/me/break", json={"choice": "week"}, headers=h).json()
    assert api.app.state.break_copy["rest_subscription_note"] in started["body"]  # level 1, subscribed
    api.put("/v1/me/break", json={"choice": "three_days"}, headers=h)
    api.delete("/v1/me/break", headers=h)
    api.post("/v1/me/break", json={"choice": "until_back", "care_level": 4}, headers=h)
    api.delete("/v1/me/break", headers=h)
    assert api.stripe.calls == []
    assert {f: account(api, "su26")[f] for f in fields} == before


@pytest.mark.parametrize("level", [3, 4])
def test_no_billing_wording_at_levels_3_and_4(api, level):
    """[SAFETY] UC-SUB-21 and UC-SUB-23: only the cancel action, and no price wording."""
    subject = f"su27-{level}"
    register(api, subject)
    active_case(api, subject)
    subscribe(api, subject)
    h = as_user(subject)
    status = api.get("/v1/me/subscription", params={"care_level": level}, headers=h).json()
    assert status["status_line"] is None and [a["value"] for a in status["actions"]] == ["cancel"]
    assert status["breaks_note"] is None and status["billing_notice"] is None
    rest = api.post("/v1/me/break", json={"care_level": level}, headers=h).json()
    text = json.dumps({k: rest[k] for k in ("text", "body")}).lower()
    for word in BILLING_WORDS:
        assert word not in text, word


def test_settings_status_lines(api):
    """UC-SUB-23: from stored fields, in the user's time zone, without calling Stripe."""
    register(api, "su28")
    h = as_user("su28")
    assert api.get("/v1/me/subscription", headers=h).json()["status_line"] == COPY["settings_status_not_started"]
    active_case(api, "su28")
    line = api.get("/v1/me/subscription", headers=h).json()["status_line"]
    assert line.startswith("Free days end on ")
    subscribe(api, "su28")
    api.stripe.calls.clear()
    out = api.get("/v1/me/subscription", headers=h).json()
    assert out["status_line"].startswith("Subscribed. Your first payment of $14.99 is on ")  # early subscriber
    assert out["breaks_note"] == COPY["settings_breaks_note"]
    assert {"update_payment", "invoices", "cancel"} <= {a["value"] for a in out["actions"]}
    assert api.stripe.calls == []


# ------------------------------------------------------------------ UC-SUB-06 and the retry job

def test_a_care_rest_moves_an_early_subscribers_first_charge(api):
    register(api, "su29")
    active_case(api, "su29")
    uid, sub_id = subscribe(api, "su29", status="trialing")
    customer = account(api, "su29")["stripe_customer_id"]
    stripe_sub(api, sub_id, uid, status="trialing", customer=customer,
               trial_end=account(api, "su29")["trial_ends_at"])
    h = as_user("su29")
    api.post("/v1/me/break", json={"choice": "until_back", "care_level": 3}, headers=h)
    paused_at = account(api, "su29")["trial_clock_paused_at"]
    api.db.users.update_one({"_id": uid}, {"$set": {"trial_clock_paused_at": paused_at - timedelta(days=2),
                                                    "break_started_at": paused_at - timedelta(days=2)}})
    api.stripe.calls.clear()
    api.delete("/v1/me/break", headers=h)
    ends = account(api, "su29")["trial_ends_at"]
    assert ("set_trial_end", {"subscription_id": sub_id, "when": ends}) in api.stripe.calls


def test_the_retry_job_applies_an_event_the_webhook_couldnt_finish(api):
    register(api, "su30")
    active_case(api, "su30")
    expire_trial(api, "su30")
    uid = user_id(api, "su30")
    sub_id = f"sub_{uuid.uuid4().hex[:12]}"
    stripe_sub(api, sub_id, uid, customer="cus_Retry30")
    evt = event("checkout.session.completed", {"client_reference_id": str(uid), "customer": "cus_Retry30",
                                               "subscription": sub_id})
    api.stripe.fail.add("retrieve_subscription")
    try:
        assert deliver(api, evt).status_code == 500  # Stripe will retry, and so will the job
    finally:
        api.stripe.fail.clear()
    assert account(api, "su30")["subscription_status"] == "none"
    api.db.stripe_events.update_one({"_id": evt["id"]}, {"$set": {"received_at": datetime.now(timezone.utc)
                                                                  - timedelta(minutes=10)}})
    api.stripe.events[evt["id"]] = evt
    done, failed = maintenance.process_stripe_events(api.jobs_db, api.stripe)
    assert done >= 1 and account(api, "su30")["subscription_status"] == "active"
    assert api.db.stripe_events.find_one({"_id": evt["id"]})["processed_at"] is not None


# ------------------------------------------------------------------ the Stripe client, at the HTTP level (no database)

def _client(responses: dict):
    """A Stripe client whose HTTP calls are answered from responses[(method, path)] and recorded."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content.decode(), str(request.url.query, "utf-8")
                     if isinstance(request.url.query, bytes) else str(request.url.query)))
        status, body = responses.get((request.method, request.url.path), (200, {}))
        return httpx.Response(status, json=body)

    return Stripe("sk_test_fake", http=httpx.Client(transport=httpx.MockTransport(handler))), seen


def test_client_portal_cancel_undo_trial_and_invoice_requests():
    stripe, seen = _client({
        ("POST", "/v1/billing_portal/sessions"): (200, {"url": "https://billing.stripe.test/p"}),
        ("GET", "/v1/subscriptions/sub_1"): (200, {"id": "sub_1", "latest_invoice": {
            "hosted_invoice_url": "https://invoice.stripe.test/i"}}),
        ("GET", "/v1/events/evt_1"): (200, {"id": "evt_1"}),
    })
    assert stripe.create_portal_session("cus_1", "https://app.test/back", update_payment=True)["url"]
    stripe.create_portal_session("cus_1", "https://app.test/back", update_payment=False)
    stripe.set_cancel_at_period_end("sub_1", True)
    stripe.set_cancel_at_period_end("sub_1", False)
    stripe.cancel_now("sub_1")
    stripe.set_trial_end("sub_1", datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert stripe.latest_invoice_url("sub_1") == "https://invoice.stripe.test/i"
    assert stripe.retrieve_event("evt_1") == {"id": "evt_1"}
    stripe.expire_checkout_session("cs_1")
    bodies = [(m, p, b) for m, p, b, _ in seen]
    assert ("POST", "/v1/billing_portal/sessions",
            "customer=cus_1&return_url=https%3A%2F%2Fapp.test%2Fback&flow_data%5Btype%5D=payment_method_update") \
        in bodies
    assert ("POST", "/v1/subscriptions/sub_1", "cancel_at_period_end=true&proration_behavior=none") in bodies
    assert ("POST", "/v1/subscriptions/sub_1", "cancel_at_period_end=false&proration_behavior=none") in bodies
    assert ("DELETE", "/v1/subscriptions/sub_1", "") in bodies  # no refund, no proration credit (SUB-D-05)
    assert ("POST", "/v1/subscriptions/sub_1", "trial_end=1793491200&proration_behavior=none") in bodies
    assert ("POST", "/v1/checkout/sessions/cs_1/expire", "") in bodies
    assert any("expand" in q for *_, q in seen)
    assert not [b for m, p, b, _ in seen if "refund" in p]  # Cairn never starts a refund


def test_client_with_a_known_customer_and_early_subscription():
    stripe, seen = _client({("POST", "/v1/checkout/sessions"): (200, {"id": "cs_1", "url": "https://c.test"})})
    soon = datetime.now(timezone.utc) + timedelta(hours=3)
    stripe.create_checkout_session(account_id="a", email="pat@example.test", customer_id="cus_9", price_cents=1499,
                                   product_id="prod_1", success_url="https://s", cancel_url="https://c",
                                   submit_text="x", trial_end=soon)
    from urllib.parse import parse_qsl
    form = dict(parse_qsl(seen[0][2]))
    assert form["customer"] == "cus_9" and "customer_email" not in form and form["customer_update[address]"] == "auto"
    # Checkout needs a trial end at least 48 hours away. The first charge can come later, never earlier.
    assert int(form["subscription_data[trial_end]"]) >= int(soon.timestamp())
    assert int(form["subscription_data[trial_end]"]) >= int(time.time()) + 48 * 3600


def test_client_tolerates_what_is_already_gone_and_reports_network_errors():
    from cairn_api.stripe_client import StripeError
    stripe, _ = _client({("DELETE", "/v1/subscriptions/sub_gone"): (404, {"error": {"code": "resource_missing"}}),
                         ("POST", "/v1/checkout/sessions/cs_done/expire"): (400, {"error": {"type": "invalid"}}),
                         ("POST", "/v1/checkout/sessions/cs_bad/expire"): (500, {"error": {}})})
    assert stripe.cancel_now("sub_gone")["status"] == "canceled"
    stripe.expire_checkout_session("cs_done")
    with pytest.raises(StripeError):
        stripe.expire_checkout_session("cs_bad")

    def down(request):
        raise httpx.ConnectError("no route")

    offline = Stripe("sk_test_fake", http=httpx.Client(transport=httpx.MockTransport(down)))
    with pytest.raises(StripeError) as e:
        offline.retrieve_subscription("sub_1")
    assert str(e.value) == "stripe:network:ConnectError"


def test_a_payload_that_is_not_an_event_is_refused():
    for payload in (b"not json", json.dumps({"id": "nope"}).encode(), json.dumps([1]).encode()):
        with pytest.raises(SignatureError):
            verify_webhook(payload, sign_webhook(payload, SECRET), SECRET)


def test_status_mapping_edge_cases():
    """fields_for keeps a newer subscription, keeps an action-required notice, and reads either period end."""
    from cairn_api.subscription import fields_for, period_end
    now = datetime.now(timezone.utc)
    user = {"_id": uuid.uuid4(), "stripe_subscription_id": "sub_new", "subscription_status": "active",
            "subscribed_at": now, "billing_notice": "action_required", "annual_reminder_due_at": None,
            "trial_started_at": None, "current_period_end": None}
    assert fields_for({"id": "sub_old", "status": "canceled", "customer": "cus_1"}, user, now, "x") == ({}, False)
    fields, new = fields_for({"id": "sub_new", "status": "past_due", "customer": "cus_1",
                              "current_period_end": int(now.timestamp())}, user, now, "invoice.paid")
    assert fields["billing_notice"] == "action_required" and new is False
    assert fields["annual_reminder_due_at"] == now + timedelta(days=365)
    assert period_end({"current_period_end": 1700000000}) == datetime.fromtimestamp(1700000000, tz=timezone.utc)
    assert period_end({"items": {"data": []}}) is None
    assert fields_for({"id": "sub_new", "status": "paused", "customer": "cus_1"}, user, now, "x")[0] == {
        "stripe_customer_id": "cus_1"}


def test_events_cairn_does_not_handle_are_recorded_and_ignored(api):
    register(api, "su31")
    evt = event("payment_intent.created", {"customer": "cus_x"})
    assert deliver(api, evt).status_code == 200
    assert api.db.stripe_events.find_one({"_id": evt["id"]})["processed_at"] is not None


def test_customer_updated_and_a_checkout_without_a_subscription_store_ids_only(api):
    register(api, "su32")
    uid = user_id(api, "su32")
    assert deliver(api, event("checkout.session.completed", {"client_reference_id": str(uid),
                                                             "customer": "cus_Only32"})).status_code == 200
    assert account(api, "su32")["stripe_customer_id"] == "cus_Only32"
    assert deliver(api, event("customer.updated", {"id": "cus_Only32", "address": {"line1": "1 Fake St"}})
                   ).status_code == 200
    assert "Fake St" not in json.dumps(account(api, "su32"), default=str)  # no billing address (SUB-D-10)


def test_a_subscription_that_belongs_to_another_account_is_not_applied(api, caplog):
    register(api, "su33")
    register(api, "su33x")
    uid, other = user_id(api, "su33"), user_id(api, "su33x")
    stripe_sub(api, "sub_Mismatch33", other, customer="cus_33")
    deliver(api, event("checkout.session.completed", {"client_reference_id": str(uid), "customer": "cus_33",
                                                      "subscription": "sub_Mismatch33"}))
    assert account(api, "su33")["subscription_status"] == "none" and "another account" in caplog.text


def test_the_early_subscription_job_keeps_the_first_charge_at_the_free_days_end(api):
    register(api, "su34")
    active_case(api, "su34")
    uid, sub_id = subscribe(api, "su34", status="trialing")
    customer = account(api, "su34")["stripe_customer_id"]
    stripe_sub(api, sub_id, uid, status="trialing", customer=customer, trial_end=datetime.now(timezone.utc))
    api.stripe.calls.clear()
    assert maintenance.sync_early_subscriptions(api.jobs_db, api.stripe) >= 1
    assert ("set_trial_end", {"subscription_id": sub_id, "when": account(api, "su34")["trial_ends_at"]}) in \
        api.stripe.calls


def test_portal_and_confirm_need_a_customer_and_a_waiting_payment(api):
    register(api, "su35")
    h = as_user("su35")
    assert api.post("/v1/me/subscription/portal", json={"purpose": "invoices"}, headers=h).status_code == 409
    assert api.post("/v1/me/subscription/confirm-payment", headers=h).status_code == 409
    assert api.post("/v1/me/subscription/undo-cancel", headers=h).status_code == 409
    assert api.post("/v1/me/subscription/cancel", json={"confirm": True}, headers=h).status_code == 409
    r = api.post("/v1/me/subscription/checkout", headers=h,
                 json={"agreed": True, "document_version": subscription_terms_version(COPY), "client": "web/1"})
    assert r.status_code == 409 and r.json()["code"] == "no_journey_yet"  # the free days come first


# ------------------------------------------------------------------ UC-SUB-18 a price change

def test_a_price_change_notice_goes_once_inside_the_window_and_waits_for_a_break(api, caplog):
    register(api, "su36")
    active_case(api, "su36")
    uid, _ = subscribe(api, "su36")
    register(api, "su36b")
    active_case(api, "su36b")
    resting, _ = subscribe(api, "su36b")
    api.post("/v1/me/break", json={"choice": "week"}, headers=as_user("su36b"))
    effective = datetime.now(timezone.utc) + timedelta(days=20)
    mailer = FakeMailer()
    outbound.send_price_change_notices(api.jobs_db, mailer, COPY, effective, "$16.99")
    outbound.send_price_change_notices(api.jobs_db, mailer, COPY, effective, "$16.99")
    sent = [m for m in mailer.to("su36@example.test") if m[0] == COPY["price_change_subject"]]
    assert len(sent) == 1 and "$16.99" in sent[0][1] and "Settings, then Subscription" in sent[0][1]
    assert mailer.to("su36b@example.test") == []  # held during the break, inside the window
    # Too close to the date: never sent late. The owner moves the date for that account instead.
    soon = datetime.now(timezone.utc) + timedelta(days=3)
    out = outbound.send_price_change_notices(api.jobs_db, FakeMailer(), COPY, soon, "$16.99")
    assert out.failed.get("price_change_notices_missed", 0) >= 1
    assert f"user={resting}" in caplog.text and "su36b@example.test" not in caplog.text
    # Too early: nothing yet.
    far = datetime.now(timezone.utc) + timedelta(days=45)
    assert outbound.send_price_change_notices(api.jobs_db, FakeMailer(), COPY, far, "$16.99").sent == {}


def test_a_scheduled_price_change_shows_in_cairn_at_level_1(api):
    import dataclasses
    register(api, "su37")
    active_case(api, "su37")
    subscribe(api, "su37")
    original = api.app.state.settings
    effective = (datetime.now(timezone.utc) + timedelta(days=10)).date().isoformat()
    api.app.state.settings = dataclasses.replace(original, price_change_effective_date=effective,
                                                 price_change_new_price="$16.99")
    try:
        notes = api.get("/v1/me", headers=as_user("su37")).json()["notes"]
        assert any("$16.99" in n["text"] for n in notes)
        home = api.get("/v1/home", params={"care_level": 3}, headers=as_user("su37")).json()
        assert "$16.99" not in json.dumps(home["notes"])
    finally:
        api.app.state.settings = original


def test_a_price_change_needs_both_settings():
    import dataclasses
    with pytest.raises(RuntimeError):
        dataclasses.replace(SETTINGS, price_change_effective_date="2027-03-01")


def test_the_read_only_banner_is_left_out_at_care_levels_2_to_4(api):
    """UC-SUB-01 alternate flow: no wording about subscribing at levels 2 to 4, on every read route."""
    register(api, "su38")
    cid, journey = active_case(api, "su38")
    expire_trial(api, "su38")
    h = as_user("su38")
    task = journey["next_action"]["id"]
    for path in ("/v1/me", f"/v1/cases/{cid}", f"/v1/cases/{cid}/journey", f"/v1/cases/{cid}/tasks/{task}"):
        assert COPY["read_only_banner"] in api.get(path, headers=h).text, path
        assert COPY["read_only_banner"] not in api.get(path, params={"care_level": 3}, headers=h).text, path
    assert COPY["read_only_banner"] not in api.get(f"/v1/cases/{cid}/status", params={"care_level": 3},
                                                   headers=h).text
