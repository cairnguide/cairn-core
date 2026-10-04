"""Rules for UC-REG-15, UC-REG-16, and UC-CASE-19 to UC-CASE-21 that need no database.

The database-backed checks are in test_account_lifecycle.py.
"""
from __future__ import annotations

import ast
import json
import sys

import pytest
from pydantic import ValidationError

from cairn_api import account_chat, outbound
from cairn_api import notifications as nt
from cairn_api.account import mask_email
from cairn_api.copy_store import load_case_copy, load_copy
from cairn_api.main import create_app
from cairn_api.schemas import (
    DataExport,
    NotificationChangeIn,
    NotificationChannel,
    NotificationChoice,
    NotificationSetIn,
)

from .conftest import REPO, SETTINGS, as_user

COPY = load_copy()
CASE_COPY = load_case_copy()
ACCOUNT_SPEC = json.loads((REPO / "database" / "docs" / "cairn-account-use-cases-2026-09-25.json").read_text())
CASE_SPEC = json.loads((REPO / "database" / "docs" / "cairn-case-creation-use-cases-2026-09-25.json").read_text())
NEW = {u["id"]: u for u in ACCOUNT_SPEC["new_use_cases"] + CASE_SPEC["new_use_cases"]}


# ------------------------------------------------------------------ masking (UC-CASE-19, UC-CASE-21)

@pytest.mark.parametrize("email,masked", [
    ("pat.fakename@example.test", "p•••e@example.test"),
    ("abc@example.test", "a•••@example.test"),
    ("x@example.test", "x•••@example.test"),
    ("x7k2mq9zp4@privaterelay.appleid.com", "x•••4@privaterelay.appleid.com"),
])
def test_emails_are_masked_and_relay_domains_stay_recognizable(email, masked):
    assert mask_email(email) == masked


# ------------------------------------------------------------------ choice shape (UC-CASE-19)

@pytest.mark.parametrize("choice", [
    {"channels": ["in_app_only"]},
    {"channels": ["email"], "reasons": ["due_date_upcoming"], "due_date_lead_days": 3},
    {"channels": ["email", "push"], "reasons": ["due_date_upcoming", "inactivity"], "due_date_lead_days": 7,
     "inactivity_days": 14, "frequency": "as_it_happens"},
])
def test_valid_choices(choice):
    NotificationChoice.model_validate(choice)


@pytest.mark.parametrize("choice", [
    {"channels": []},
    {"channels": ["sms"], "reasons": ["inactivity"], "inactivity_days": 3},
    {"channels": ["in_app_only", "email"], "reasons": ["inactivity"], "inactivity_days": 3},
    {"channels": ["in_app_only"], "reasons": ["inactivity"], "inactivity_days": 3},
    {"channels": ["email"]},
    {"channels": ["email"], "reasons": ["due_date_upcoming"]},
    {"channels": ["email"], "reasons": ["due_date_upcoming"], "due_date_lead_days": 2},
    {"channels": ["email"], "reasons": ["inactivity"], "inactivity_days": 3, "due_date_lead_days": 3},
    {"channels": ["email", "email"], "reasons": ["inactivity"], "inactivity_days": 3},
    {"channels": ["email"], "reasons": ["inactivity"], "inactivity_days": 3, "frequency": "hourly"},
])
def test_invalid_choices(choice):
    with pytest.raises(ValidationError):
        NotificationChoice.model_validate(choice)


def test_keep_it_simple_matches_the_spec_shortcut():
    preset = NEW["UC-CASE-19"]["shortcut"]["preset"]
    assert nt.KEEP_IT_SIMPLE.model_dump(mode="json", exclude={"inactivity_days"}) == {
        "channels": preset["channels"], "reasons": preset["reasons"],
        "due_date_lead_days": preset["due_date_lead_days"], "frequency": preset["frequency"]}


def test_set_and_change_requests_say_exactly_one_thing():
    with pytest.raises(ValidationError):
        NotificationSetIn.model_validate({"preset": "keep_it_simple", "choice": {"channels": ["in_app_only"]}})
    with pytest.raises(ValidationError):
        NotificationSetIn.model_validate({"preset": "same_as"})
    with pytest.raises(ValidationError):
        NotificationChangeIn.model_validate({"stop_everything": True, "choice": {"channels": ["in_app_only"]}})
    with pytest.raises(ValidationError):
        NotificationChangeIn.model_validate({})


def test_the_data_model_matches_the_spec_entity():
    """UC-CASE-19 data: the same enums. SMS fields are left out until SMS is decided (card 50)."""
    fields = NEW["UC-CASE-19"]["data"]["fields"]
    assert fields["channels"] == "array<enum: email|sms|push|in_app_only>"
    assert {c.value for c in NotificationChannel} == {"email", "push", "in_app_only"}
    sys.path.insert(0, str(REPO / "database" / "db"))
    import schema
    stored = schema.COLLECTIONS["notification_preferences"]["$and"][0]["$jsonSchema"]["properties"]
    assert set(stored) == {"_id", "channels", "reasons", "due_date_lead_days", "inactivity_days", "frequency",
                           "push_permission_granted", "updated_at"}
    assert stored["channels"]["items"]["enum"] == ["email", "push", "in_app_only"]
    assert stored["due_date_lead_days"]["enum"] == [1, 3, 7, None]
    assert stored["inactivity_days"]["enum"] == [3, 7, 14, None]


# ------------------------------------------------------------------ plain-language readback

def test_readback_is_one_plain_sentence():
    # skipped_or_in_app_only: nothing sent outside the app, due dates and updates show in Cairn only.
    assert nt.readback(CASE_COPY, nt.IN_APP_ONLY) == CASE_COPY["readback_in_app_only"]
    assert "outside the app" in CASE_COPY["readback_in_app_only"] and "in Cairn" in CASE_COPY["readback_in_app_only"]
    line = nt.readback(CASE_COPY, nt.KEEP_IT_SIMPLE)
    assert line == "Cairn will reach you by email 3 days before a step is due, no more than once a day."
    both = NotificationChoice(channels=["email", "push"], reasons=["due_date_upcoming", "inactivity"],
                              due_date_lead_days=1, inactivity_days=7, frequency="as_it_happens")
    assert nt.readback(CASE_COPY, both) == (
        "Cairn will reach you by email and browser notifications the day before a step is due and when you "
        "haven't been in Cairn for 7 days, as things come up.")


# ------------------------------------------------------------------ what goes out (UC-CASE-19, UC-CASE-21)

def test_outbound_messages_are_short_private_and_never_repeat_content():
    """Never the name of the person who died, the circumstance, task details, or anything deleted."""
    messages = [outbound.confirmation(kind, COPY) for kind in outbound.CONFIRMATION_KEYS]
    messages += [outbound.notification(r, COPY, CASE_COPY) for r in ("due_date_upcoming", "inactivity")]
    assert outbound.notification("due_date_upcoming", COPY, CASE_COPY).body == "You have a step coming up in Cairn."
    for m in messages:
        text = m.subject + " " + m.body
        assert "{" not in text and len(m.body) <= 160
        for word in ("died", "death", "certificate", "Social Security", "bank", "funeral", "veteran", "will"):
            assert word.lower() not in text.lower(), (word, text)


def test_confirmation_types_match_the_spec_log():
    kinds = NEW["UC-CASE-21"]["data"]["fields"]["action_type"]
    assert kinds == "enum: " + "|".join(outbound.CONFIRMATION_KEYS)
    assert NEW["UC-CASE-21"]["data"]["fields"]["content_stored"] is False


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


def test_email_me_instead_switches_the_channel_and_keeps_the_rest():
    current = NotificationChoice(channels=["push"], reasons=["inactivity"], inactivity_days=14,
                                 frequency="weekly_max")
    switched = account_chat.switch_channel(current, NotificationChannel.email, nt.KEEP_IT_SIMPLE)
    assert switched.channels == [NotificationChannel.email] and switched.inactivity_days == 14
    fresh = account_chat.switch_channel(nt.IN_APP_ONLY, NotificationChannel.email, nt.KEEP_IT_SIMPLE)
    assert fresh == nt.KEEP_IT_SIMPLE


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
                 "/v1/me/notification-preferences", "/v1/me/notification-preferences/changes",
                 "/v1/cases/{case_id}/notification-preferences",
                 "/v1/cases/{case_id}/notification-preferences/readback",
                 "/v1/cases/{case_id}/notification-preferences/push-permission",
                 "/v1/cases/{case_id}/deletion"):
        assert path in paths, path
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
    """UC-REG-15 rules: no reason asked, no retention offer, discount, or persuasion."""
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
