"""Rules for account deletion (UC-ACCT-01), the download (UC-REG-16), keeping in touch (account D-13, UC-REG-17,
case UC-CASE-19), and confirmations (UC-CASE-21) that need no database.

Specs: database/docs/cairn-account-use-cases-v32.json and cairn-case-creation-use-cases-v32.json. The
database-backed checks are in test_account_lifecycle.py and test_use_cases_v3.py.
"""
from __future__ import annotations

import ast
import json
import re
import sys

import pytest
from pydantic import ValidationError

from cairn_api import account_chat, outbound
from cairn_api import notifications as nt
from cairn_api.account import mask_email
from cairn_api.copy_store import load_break_copy, load_case_copy, load_copy, load_subscription_copy
from cairn_api.main import create_app
from cairn_api.schemas import (
    DataExport,
    NotificationChannel,
    NotificationChannelsIn,
    NotificationSettingsPatch,
)
from cairn_api.store import DEFAULT_NOTIFICATIONS, notification_values_valid

from .conftest import REPO, SETTINGS, as_user

COPY = load_copy()
CASE_COPY = load_case_copy()
BREAK_COPY = load_break_copy()
SUB_COPY = load_subscription_copy()
ACCOUNT_SPEC = json.loads((REPO / "database" / "docs" / "cairn-account-use-cases-v32.json").read_text())
CASE_SPEC = json.loads((REPO / "database" / "docs" / "cairn-case-creation-use-cases-v32.json").read_text())
UC = {u["id"]: u for u in ACCOUNT_SPEC["use_cases"] + CASE_SPEC["use_cases"]}


# ------------------------------------------------------------------ masking (UC-CASE-19, UC-CASE-21)

@pytest.mark.parametrize("email,masked", [
    ("pat.fakename@example.test", "p•••e@example.test"),
    ("abc@example.test", "a•••@example.test"),
    ("x@example.test", "x•••@example.test"),
    ("x7k2mq9zp4@privaterelay.appleid.com", "x•••4@privaterelay.appleid.com"),
])
def test_emails_are_masked_and_relay_domains_stay_recognizable(email, masked):
    assert mask_email(email) == masked


# ------------------------------------------------------------------ choice shape (account D-13)

@pytest.mark.parametrize("change", [
    {},
    {"channels": ["email", "in_app", "browser"], "browser_push_endpoint": "https://push.example.test/abc"},
    {"frequency": "none"},
    {"quiet_hours_start": "22:30", "quiet_hours_end": "07:00"},
    {"due_date_lead": "one_week", "inactivity_after": "two_weeks"},
])
def test_valid_choices(change):
    assert notification_values_valid({**DEFAULT_NOTIFICATIONS, **change})


@pytest.mark.parametrize("change", [
    {"channels": ["email"]},                                  # in_app is always on
    {"channels": ["in_app", "sms"]},                          # no text messages (D-12, OPEN-05)
    {"channels": ["in_app", "in_app"]},
    {"frequency": "hourly"},
    {"quiet_hours_start": "25:00"},
    {"due_date_lead": "two_days"},
    {"inactivity_after": "a_month"},
    {"browser_push_endpoint": "https://push.example.test/abc"},  # browser isn't chosen
    {"channels": ["in_app", "browser"], "browser_push_endpoint": "http://push.example.test/abc"},
])
def test_invalid_choices(change):
    assert not notification_values_valid({**DEFAULT_NOTIFICATIONS, **change})


def test_setup_defaults_match_the_spec():
    """UC-REG-15: email and in-app pre-selected (OPEN-11), due only, 9 PM to 8 AM quiet hours. UC-CASE-19 and
    OPEN-03: 3 days before, no inactivity notices."""
    assert DEFAULT_NOTIFICATIONS["channels"] == ["email", "in_app"]
    assert DEFAULT_NOTIFICATIONS["frequency"] == "due_only"
    assert (DEFAULT_NOTIFICATIONS["quiet_hours_start"], DEFAULT_NOTIFICATIONS["quiet_hours_end"]) == ("21:00", "08:00")
    fields = ACCOUNT_SPEC["data_requirements"]["notification_preferences"]["fields"]
    assert "Default three_days" in fields["due_date_lead"] and "Default off" in fields["inactivity_after"]
    assert DEFAULT_NOTIFICATIONS["due_date_lead"] == "three_days" and DEFAULT_NOTIFICATIONS["inactivity_after"] == "off"


def test_requests_say_what_changes():
    with pytest.raises(ValidationError):
        NotificationSettingsPatch.model_validate({})
    with pytest.raises(ValidationError):  # an endpoint needs browser and permission granted
        NotificationChannelsIn.model_validate({"email": True, "browser_push_endpoint": "https://push.example.test/a"})
    NotificationSettingsPatch.model_validate({"stop_all_reminders": True})


def test_the_data_model_matches_the_spec_entity():
    """account data_requirements.notification_preferences: one row per account, these fields and enums."""
    sys.path.insert(0, str(REPO / "database" / "db"))
    import schema
    fields = ACCOUNT_SPEC["data_requirements"]["notification_preferences"]["fields"]
    stored = schema.COLLECTIONS["notification_preferences"]["$and"][0]["$jsonSchema"]["properties"]
    assert set(stored) == {"_id", "channels", "frequency", "quiet_hours_start", "quiet_hours_end",
                           "browser_push_endpoint", "due_date_lead", "inactivity_after", "journey_confirmed_at",
                           "updated_at"}
    assert set(f for f in fields if f not in ("account_id", "scope_note")) <= set(stored) | {"updated_at"}
    assert stored["channels"]["items"]["enum"] == ["email", "in_app", "browser"]
    assert stored["frequency"]["enum"] == ["due_only", "daily", "weekly", "none"]
    assert stored["due_date_lead"]["enum"] == ["day_before", "three_days", "one_week"]
    assert stored["inactivity_after"]["enum"] == ["off", "three_days", "one_week", "two_weeks"]
    assert {c.value for c in NotificationChannel} == {"email", "in_app", "browser"}


# ------------------------------------------------------------------ plain-language readback

def test_readback_is_one_plain_line():
    none = {**DEFAULT_NOTIFICATIONS, "frequency": "none"}
    assert nt.readback(COPY, none) == COPY["notify_readback_none"]
    assert nt.readback(COPY, DEFAULT_NOTIFICATIONS) == (
        "I'll reach you by email and inside Cairn, with only when something is due. I won't send anything between "
        "9 PM and 8 AM your time.")


def test_quiet_hours_wrap_past_midnight_and_move_a_time_to_their_end():
    from datetime import datetime, timezone
    prefs = DEFAULT_NOTIFICATIONS
    tz = "America/New_York"
    late = datetime(2026, 10, 7, 2, 30, tzinfo=timezone.utc)   # 10:30 PM in New York
    noon = datetime(2026, 10, 7, 16, 0, tzinfo=timezone.utc)   # noon in New York
    assert nt.in_quiet_hours(late, tz, prefs) and not nt.in_quiet_hours(noon, tz, prefs)
    moved = nt.after_quiet_hours(late, tz, prefs)
    assert moved == datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)  # 8 AM in New York
    assert nt.after_quiet_hours(noon, tz, prefs) == noon


# ------------------------------------------------------------------ what goes out (UC-CASE-19, UC-CASE-21)

def test_outbound_messages_are_short_private_and_never_repeat_content():
    """Never the name of the person who died, the circumstance, task details, or anything deleted."""
    messages = [outbound.confirmation({"action_type": kind, "preferred_name": "Pat"}, COPY, SUB_COPY)
                for kind in outbound.CONFIRMATION_KEYS]
    messages += [outbound.notification(r, COPY, CASE_COPY) for r in ("due_date_upcoming", "inactivity")]
    messages += [outbound.check_in(CASE_COPY), outbound.break_notice(BREAK_COPY)]
    assert outbound.notification("due_date_upcoming", COPY, CASE_COPY).body == "You have a step coming up in Cairn."
    for m in messages:
        text = m.subject + " " + m.body
        assert "{" not in text and len(m.body) <= 320
        for word in ("died", "death", "certificate", "Social Security", "bank", "funeral", "veteran"):
            assert word.lower() not in text.lower(), (word, text)
        assert not re.search(r"\b(a|the|their) will\b", text, re.I), text  # the legal document, not the verb


def test_confirmation_types_cover_the_spec():
    """UC-CASE-21 v3: setup welcome, a Settings change, case deleted now or after the hold, account deleted. The
    subscription spec adds subscribed and cancelled (changes_needed_in_other_specs)."""
    rules = " ".join(UC["UC-CASE-21"]["rules"])
    for words in ("setup complete welcome", "Settings change", "case deleted now", "7-day hold", "account deleted"):
        assert words in rules
    sys.path.insert(0, str(REPO / "database" / "db"))
    import schema
    assert set(outbound.CONFIRMATION_KEYS) == set(schema.CONFIRMATION_TYPES)
    assert {"subscription_started", "subscription_canceled"} <= set(schema.CONFIRMATION_TYPES)


# ------------------------------------------------------------------ account requests in chat

@pytest.mark.parametrize("text,intent", [
    ("Please delete my account", "delete_account"),
    ("Can you erase all of my data?", "delete_account"),
    ("Download my data", "download_data"),
    ("Can I get a copy of everything you have about me?", "download_data"),
    ("Stop texting me", "stop_notifications"),
    ("Don't email me anymore", "stop_notifications"),
    ("unsubscribe", "stop_notifications"),
    ("Email me instead", "change_notifications"),
    ("Stop texting me, email me instead", "change_notifications"),
    ("Can you text me?", "sms_not_available"),
    ("What's the weather?", "help"),
])
def test_account_requests_are_recognized(text, intent):
    assert account_chat.read(text).intent == intent


def test_email_me_instead_switches_the_channel_only():
    switched = account_chat.switch_channel(NotificationChannel.email)
    assert switched.email and not switched.browser
    assert nt.channels_from(switched) == (["email", "in_app"], None)


def test_risk_of_harm_is_seen_alongside_an_account_request():
    """[SAFETY] UC-REG-15 alternate flow asked_in_chat_with_risk_of_harm_signal."""
    reading = account_chat.read("Delete my account. I don't want to be here anymore.")
    assert reading.intent == "delete_account" and reading.risk_of_harm
    assert not account_chat.read("Delete my account, my mother died by suicide").risk_of_harm


# ------------------------------------------------------------------ the download (UC-REG-16)

def test_the_download_has_no_field_for_sensitive_numbers():
    schema = json.dumps(DataExport.model_json_schema()).lower()
    for field in ("ssn", "social_security", "account_number", "card_number", "routing"):
        assert field not in schema
    import inspect

    from cairn_api.store import Session
    reads = inspect.getsource(Session.load_deceased)
    assert "ssn" not in reads.split('"""')[2]  # the fields read, after the docstring


# ------------------------------------------------------------------ contract and copy layer

def test_new_routes_are_in_the_contract():
    paths = create_app(settings=SETTINGS).openapi()["paths"]
    for path in ("/v1/me/deletion", "/v1/me/data-export", "/v1/me/data-export/file", "/v1/me/messages",
                 "/v1/me/notification-preferences", "/v1/cases/{case_id}/keep-in-touch",
                 "/v1/cases/{case_id}/deletion"):
        assert path in paths, path
    assert {"get", "patch"} <= set(paths["/v1/me/notification-preferences"])
    for old in ("/v1/me/notification-preferences/changes", "/v1/cases/{case_id}/notification-preferences"):
        assert old not in paths, old  # case UC-CASE-20 merged into account UC-REG-17 (account D-13)
    assert {"get", "post", "delete"} <= set(paths["/v1/cases/{case_id}/deletion"])


def test_no_user_facing_copy_is_inline_in_the_new_code():
    for rel in ("cairn_api/notifications.py", "cairn_api/routers/notifications.py", "cairn_api/routers/account.py",
                "cairn_api/export.py", "cairn_api/outbound.py"):
        tree = ast.parse((REPO / "api" / rel).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("NextStep", "Note", "Option",
                                                                                  "Message", "SupportResource"):
                for kw in node.keywords:
                    if kw.arg in ("prompt", "text", "label") and isinstance(kw.value, ast.Constant):
                        raise AssertionError(f"inline copy in {rel}:{node.lineno}")


def test_deletion_copy_never_asks_why_or_tries_to_keep_the_user():
    """UC-ACCT-01: no reason asked, no retention offer, discount, or persuasion."""
    keys = [k for k in {**COPY.flow, **COPY.draft} if k.startswith(("deletion_", "delete_", "email_account"))]
    assert keys
    for k in keys:
        for word in ("why", "reason", "discount", "offer", "miss", "sure", "stay"):
            assert word not in COPY[k].lower(), (k, word)


def test_new_endpoints_need_a_signed_in_user(contract_client):
    for method, path in (("get", "/v1/me/data-export/file"), ("post", "/v1/me/messages"),
                         ("get", "/v1/me/notification-preferences")):
        assert getattr(contract_client, method)(path).status_code == 401


def test_chat_validation_never_echoes_the_text(contract_client):
    r = contract_client.post("/v1/me/messages", json={"text": "delete my account 123-45-6789", "extra": 1},
                             headers=as_user("u1"))
    assert r.status_code == 422 and "123-45-6789" not in r.text
