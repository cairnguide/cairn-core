"""UC-REG-01 to UC-REG-16, UC-REG-18 to UC-REG-20, and UC-ACCT-01, from database/docs/cairn-account-use-cases-v32.json
(account spec 3.2.0).

Tests that take the `contract_client` fixture, or no fixture, need no database. The rest
need CAIRN_TEST_MONGODB_URI (see conftest.py). Fake data only.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from cairn_api.copy_store import consent_versions, load_copy

from .conftest import REPO, SETTINGS, answer, as_user, expire_trial, onboard, start_journey, user_id

VOICES = ("steady_direct", "warm_patient", "brisk_businesslike", "plain_practical")
SPEC = json.loads((REPO / "database" / "docs" / "cairn-account-use-cases-v32.json").read_text())
COPY = SPEC["copy"]
UC = {u["id"]: u for u in SPEC["use_cases"]}
SUB_COPY = json.loads((REPO / "database" / "docs" / "cairn-subscription-use-cases-v33.json").read_text())["copy"]
CASE_BODY = {"user_role": "spouse_partner"}


def start(api, subject, method="email", email=None, **body):
    r = api.post("/v1/registrations", json=body, headers=as_user(subject, email, method=method))
    assert r.status_code in (200, 201), r.text
    return r


def adult(api, subject, method="email", answer_="yes"):
    r = api.post("/v1/onboarding/adult", json={"answer": answer_}, headers=as_user(subject, method=method))
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ copy (no database)

def test_copy_is_the_spec_copy_verbatim():
    copy = load_copy()
    assert copy.spec == COPY
    assert copy.version == SPEC["spec"]["version"] == "3.2.0"


def test_no_em_dashes_or_semicolons_in_any_user_facing_copy():
    for text in load_copy().all_strings:
        assert "—" not in text and ";" not in text, text


def test_acknowledgment_versions_follow_the_text():
    copy = load_copy()
    before = consent_versions(copy, "p1", "t1")
    changed = dataclasses.replace(copy, spec={**copy.spec, "trial_summary": copy.spec["trial_summary"] + " "})
    after = consent_versions(changed, "p1", "t1")
    assert before["trial_terms"] != after["trial_terms"]
    assert before["privacy_terms"] == after["privacy_terms"] and before["ai_notice"] == after["ai_notice"]
    assert consent_versions(copy, "p2", "t1")["privacy_terms"] != before["privacy_terms"]


def test_trial_summary_says_a_care_rest_pauses_only_the_free_days_and_a_break_never_a_subscription():
    """UC-REG-08 v3.2: both before the checkbox. A new copy version, so the trial acknowledgment is versioned up."""
    text = COPY["trial_summary"]
    assert "your free days stop counting until you come back" in text
    assert "taking a break doesn't pause or change your subscription" in text
    assert "$14.99" in text and "card" in text


def test_voices_never_use_human_first_names():
    """UC-REG-12."""
    for v in VOICES:
        assert COPY[f"voice_{v}_label"].split()[0] in ("Steady", "Warm", "Brisk", "Plain")


# ------------------------------------------------------------------ UC-REG-01 (no database)

def test_welcome_acknowledges_first_and_asks_for_nothing(contract_client):
    body = contract_client.get("/v1/welcome").json()
    assert body["acknowledgment"] == COPY["welcome_acknowledgment"]
    assert [m["label"] for m in body["methods"]] == \
        ["Continue with Google", "Continue with Apple", "Continue with email"]
    assert body["sign_in_label"] == "Already have an account? Sign in."
    assert body["not_ready"] == {"label": COPY["welcome_not_ready_link"], "url": SETTINGS.journey_map_url}
    # Take a break and Support resources on the welcome screen too (BRK-D-01, AC-26-10).
    assert body["support"]["take_a_break_label"] == COPY["take_a_break_control"] == "Take a break"
    assert body["support"]["support_resources_label"] == COPY["support_resources_link"]
    assert body["support"]["read_this_to_me"] == COPY["read_this_to_me"]
    assert body["notes"] == []


def test_welcome_carries_the_magic_link_page_and_the_session_policy(contract_client):
    """UC-REG-04: the token is used only on Continue. D-20: 5 minutes, a warning 20 seconds before, 30 days."""
    body = contract_client.get("/v1/welcome").json()
    assert body["magic_link"]["landing"] == COPY["magic_link_landing"]
    assert body["magic_link"]["landing_button"] == COPY["magic_link_landing_button"]
    assert body["magic_link"]["other_device"] == COPY["magic_link_other_device"]
    config = UC["UC-REG-19"]["config"]
    session = body["session"]
    keys = ("inactivity_timeout_seconds", "warning_before_timeout_seconds", "overall_session_days")
    assert [session[k] for k in keys] == [config[k] for k in keys]
    assert session["timeout_warning"] == COPY["timeout_warning"]
    assert session["timeout_warning_button"] == COPY["timeout_warning_button"]


def test_welcome_after_cancelled_provider_sign_in(contract_client):
    notes = contract_client.get("/v1/welcome", params={"oauth_cancelled": True}).json()["notes"]
    assert [n["text"] for n in notes] == [COPY["oauth_cancelled"]]


def test_email_is_passwordless_by_default(contract_client):
    methods = {m["method"]: m["auth0_connection"] for m in contract_client.get("/v1/sign-in-methods").json()["methods"]}
    assert methods == {"google": "google-oauth2", "apple": "apple", "email": "email"}


def test_support_resources_work_signed_out(contract_client):
    """Crisis plan support_resources and AC-26-10. No sign-in, nothing stored, nothing logged with an id."""
    body = contract_client.get("/v1/support-resources").json()
    plan = json.loads((REPO / "database" / "docs" / "cairn-support-crisis-plan-v32.json").read_text())
    assert body["intro"] == plan["support_resources"]["copy"]["page_intro"]
    ids = [r["id"] for r in body["resources"]]
    assert ids == ["lifeline_988", "veterans_crisis_line", "crisis_text_line", "emergency_911"]
    assert any("988lifeline.org" in r["url"] for r in body["resources"])


def test_take_a_break_before_sign_in_saves_nothing(contract_client):
    """UC-BRK-02 (S-01)."""
    body = contract_client.get("/v1/break").json()
    assert body["screen"] == "S-01" and body["quiet_988_line"] is None
    assert [o["value"] for o in body["next_step"]["options"]] == ["go_back"]


@pytest.mark.parametrize("method,link", [(None, None), ("email", None),
                                         ("google", "https://accounts.google.com/signin/recovery"),
                                         ("apple", "https://iforgot.apple.com")])
def test_cant_get_into_the_sign_in_email_is_never_a_dead_end(contract_client, method, link):
    """UC-REG-20 (proposed)."""
    body = contract_client.get("/v1/sign-in-help", params={"method": method} if method else {}).json()
    assert body["next_step"]["action"] == "contact_support"
    assert (body["provider_recovery"] or {}).get("url") == link
    if link is None:
        assert body["intro"] == COPY["recovery_intro"]


def test_the_need_a_moment_label_is_retired(contract_client):
    """BRK-D-01: Take a break everywhere. I need a moment is gone."""
    assert contract_client.get("/v1/onboarding/need-a-moment").status_code == 404
    assert "I need a moment" not in json.dumps(load_copy().all_strings)


def test_registration_request_takes_no_legal_name(contract_client):
    r = contract_client.post("/v1/registrations", json={"first_name": "A", "last_name": "B"},
                             headers=as_user("x"))
    assert r.status_code == 422


def test_no_stored_field_matches_anything_never_collected_at_setup():
    """data_boundary.enforcement: no field this flow adds can hold a never_collected_at_setup item."""
    import sys
    sys.path.insert(0, str(REPO / "database" / "db"))
    import schema
    users = schema.COLLECTIONS["users"]["$and"][0]["$jsonSchema"]["properties"]
    prefs = schema.COLLECTIONS["notification_preferences"]["$and"][0]["$jsonSchema"]["properties"]
    never = ("legal", "birth", "age", "phone", "address", "ssn", "social", "government", "medical", "health",
             "cause", "card", "bank", "account_number", "photo", "contact", "location", "device", "deceased")
    for field in [*users, *prefs]:
        assert not any(word in field for word in never if not (word == "age" and field != "age")), field
    assert users["name_prefill"] == {"bsonType": "null"}  # D-16: retired, always null
    assert set(SPEC["data_boundary"]["never_collected_at_setup"]) and "Phone number" in " ".join(
        SPEC["data_boundary"]["never_collected_at_setup"])


def test_no_name_from_a_sign_in_provider_is_accepted(contract_client):
    """D-16 and data_boundary.enforcement: the setup API refuses any field it doesn't list."""
    r = contract_client.post("/v1/registrations", json={"name_from_provider": "Patricia"}, headers=as_user("x"))
    assert r.status_code == 422 and "Patricia" not in r.text


def test_the_adult_question_takes_yes_or_no_and_never_an_age(contract_client):
    r = contract_client.post("/v1/onboarding/adult", json={"answer": "yes", "age": 40}, headers=as_user("x"))
    assert r.status_code == 422
    assert contract_client.post("/v1/onboarding/adult", json={"answer": 40}, headers=as_user("x")).status_code == 422


# ------------------------------------------------------------------ UC-REG-02 to UC-REG-05

def test_new_account_is_pending_and_asks_the_adult_question_first(api):
    r = start(api, "google-oauth2|reg-new", method="google", time_zone="America/Chicago")
    assert r.status_code == 201
    body = r.json()
    assert (body["account"]["status"], body["account"]["access"], body["account"]["subscription_status"]) == \
        ("pending_onboarding", "full", "none")
    assert body["account"]["sign_in_method"] == "google"
    assert body["account"]["preferred_name"] is None  # the shared name is never saved
    assert body["screen"]["id"] == "adult"
    assert body["next_step"]["prompt"] == COPY["age_question"]
    assert [o["value"] for o in body["next_step"]["options"]] == ["yes", "no"]


@pytest.mark.parametrize("method,subject", [
    ("google", "google-oauth2|reg-g"), ("apple", "apple|001.reg-a"), ("email", "email|reg-e")])
def test_account_created_with_each_method(api, method, subject):
    assert start(api, subject, method=method).json()["account"]["sign_in_method"] == method


def test_apple_private_relay_address_is_accepted(api):
    relay = "abc123xyz@privaterelay.appleid.com"
    body = start(api, "apple|001.relay", method="apple", email=relay).json()
    assert body["account"]["email"] == relay


def test_email_sign_up_waits_for_the_magic_link(api):
    r = api.post("/v1/registrations", json={}, headers=as_user("email|unconfirmed", verified=False))
    assert r.status_code == 403
    assert r.json()["detail"] == COPY["email_check_inbox"]


def test_existing_email_names_the_provider_used_last_time(api):
    email = "shared-reg@example.test"
    start(api, "google-oauth2|dup-reg", method="google", email=email)
    for subject, method in (("apple|dup-reg", "apple"), ("email|dup-reg", "email")):
        r = api.post("/v1/registrations", json={}, headers=as_user(subject, email.upper(), method=method))
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["code"] == "account_exists"
        assert body["detail"] == COPY["account_exists"].format(provider="Google")
        assert body["next_step"]["options"][0] == {"value": "google", "label": "Sign in with Google",
                                                   "available": True, "unavailable_reason": None}
    # Never linked automatically: the original method still signs in to the same single account.
    assert start(api, "google-oauth2|dup-reg", method="google", email=email).status_code == 200
    assert api.db.users.count_documents({"email_lower": email.lower()}) == 1


# ------------------------------------------------------------------ UC-REG-06

def test_adult_yes_is_recorded_with_its_time_and_goes_on(api):
    subject = "email|adult-yes"
    start(api, subject)
    body = adult(api, subject)
    assert body["account"]["adult_attested"] is True
    assert body["screen"]["id"] == "privacy_terms"
    user = api.db.users.find_one({"idp_subject": subject})
    assert user["adult_attested_at"] is not None
    assert not {k for k in user if "birth" in k or k in ("age", "date_of_birth")}  # never an age


def test_adult_no_stops_onboarding_and_collects_nothing_more(api):
    """UC-REG-06 alternate flow: the message, the 988 line, the journey map and Support resources. Then nothing."""
    subject = "email|adult-no"
    start(api, subject)
    body = adult(api, subject, answer_="no")
    assert body["screen"]["id"] == "under_18"
    assert body["screen"]["body"] == [COPY["age_under_18"], COPY["age_under_18_support"]]
    assert body["screen"]["links"][0]["url"] == SETTINGS.journey_map_url
    assert body["next_step"]["options"][0]["value"] == "support_resources"
    assert body["account"]["adult_attested"] is False and body["account"]["status"] == "pending_onboarding"
    # No further data from a user who said no.
    r = api.post("/v1/onboarding/acknowledgments/privacy_terms", headers=as_user(subject),
                 json={"agreed": True, "document_version": "x", "client": "web/1"})
    assert r.status_code == 409
    assert api.put("/v1/onboarding/preferred-name", json={"preferred_name": "Kid"},
                   headers=as_user(subject)).status_code == 409
    assert adult(api, subject)["screen"]["id"] == "under_18"  # not changed by answering again
    assert api.db.users.find_one({"idp_subject": subject})["preferred_name"] is None


def test_an_account_from_before_the_adult_question_is_asked_once(api):
    """Accounts made before v3 have no answer. They are asked before going on, and nothing else repeats."""
    subject = "email|adult-legacy"
    start(api, subject)
    onboard(api, subject)
    api.db.users.update_one({"idp_subject": subject}, {"$set": {"adult_attested": None, "adult_attested_at": None}})
    h = as_user(subject)
    r = api.post("/v1/cases", json=CASE_BODY, headers=h)
    assert r.status_code == 409 and r.json()["acknowledgment"] == "adult"
    assert api.get("/v1/onboarding", headers=h).json()["screen"]["id"] == "adult"
    assert adult(api, subject)["screen"]["id"] == "setup_complete"
    assert api.post("/v1/cases", json=CASE_BODY, headers=h).status_code == 201


# ------------------------------------------------------------------ UC-REG-07 to UC-REG-16

def test_full_onboarding_sequence(api):
    subject = "apple|001.full"
    h = as_user(subject, method="apple")
    start(api, subject, method="apple", time_zone="America/Denver")
    screen = adult(api, subject, method="apple")["screen"]
    assert screen["id"] == "privacy_terms"
    assert screen["body"] == [COPY["privacy_terms_summary"]]
    assert screen["checkbox"]["label"] == COPY["privacy_terms_checkbox"]
    assert screen["checkbox"]["checked"] is False
    assert screen["ai_provider"] == SETTINGS.ai_provider_name
    assert {link["url"] for link in screen["links"]} == {SETTINGS.privacy_policy_url, SETTINGS.terms_url}

    body = api.post("/v1/onboarding/acknowledgments/privacy_terms", headers=h, json={
        "agreed": True, "document_version": screen["checkbox"]["document_version"], "client": "web/1.0"}).json()
    assert body["screen"]["id"] == "trial_terms"
    assert body["screen"]["body"] == [COPY["trial_summary"]]  # the care rest line before the checkbox
    assert body["account"]["trial_started_at"] is None

    body = api.post("/v1/onboarding/acknowledgments/trial_terms", headers=h, json={
        "agreed": True, "document_version": body["screen"]["checkbox"]["document_version"],
        "client": "web/1.0"}).json()
    assert body["account"]["trial_started_at"] is None  # the clock doesn't start at the acknowledgment
    assert body["screen"]["body"] == [COPY["ai_notice"]]
    assert body["screen"]["legal_notice"] == [COPY["ai_notice_legal"]]
    assert "988" in body["screen"]["body"][0]
    assert body["account"]["ai_label"] is None

    body = api.post("/v1/onboarding/acknowledgments/ai_notice", headers=h, json={
        "agreed": True, "document_version": body["screen"]["checkbox"]["document_version"],
        "client": "web/1.0"}).json()
    assert body["account"]["ai_label"] == COPY["ai_persistent_label"]
    assert body["screen"]["id"] == "preferred_name"
    assert body["screen"]["input"]["prefill"] is None  # UC-REG-11: never pre-filled from a provider
    assert body["screen"]["body"] == [COPY["preferred_name_helper"]]
    assert body["screen"]["input"]["optional_link_label"] == COPY["pronunciation_link"]
    assert body["next_step"]["prompt"] == COPY["preferred_name_question"]
    # UC-REG-09: the AI notice counts as the session-start reminder for that day (UC-CASE-23).
    assert api.db.users.find_one({"idp_subject": subject})["ai_reminder_shown_on"] is not None

    body = api.put("/v1/onboarding/preferred-name", headers=h,
                   json={"preferred_name": "Trish", "name_pronunciation": "trish"}).json()
    assert body["account"]["preferred_name"] == "Trish"
    assert body["screen"]["id"] == "personality"
    assert body["next_step"]["prompt"] == COPY["voice_question"]
    assert body["screen"]["body"] == [COPY["voice_sample_intro"]]
    assert [c["value"] for c in body["screen"]["choices"]] == list(VOICES)
    for c in body["screen"]["choices"]:
        assert (c["label"], c["tagline"], c["sample"]) == (COPY[f"voice_{c['value']}_label"],
                                                           COPY[f"voice_{c['value']}_description"],
                                                           COPY[f"voice_{c['value']}_sample"])
    assert [o["value"] for o in body["next_step"]["options"]] == [*VOICES, "choose_for_me"]
    assert body["next_step"]["options"][-1]["label"] == COPY["voice_default_button"]

    body = api.put("/v1/onboarding/personality", headers=h, json={"choice": "choose_for_me"}).json()
    assert body["account"]["voice"] == "steady_direct"
    assert body["notes"][0]["text"] == COPY["voice_confirm"].format(preferred_name="Trish")
    assert body["screen"]["id"] == "notification_channels"
    assert body["screen"]["acknowledgment"] == COPY["notify_intro"]
    assert body["next_step"]["prompt"] == COPY["notify_channel_question"]
    choices = {c["value"]: c for c in body["screen"]["choices"]}
    assert choices["email"]["label"] == COPY["notify_channel_email"].format(email=f"{subject}@example.test")
    assert choices["email"]["tagline"] == "selected" and choices["in_app"]["tagline"] == "always_on"
    assert choices["in_app"]["label"] == COPY["notify_channel_inapp"]
    assert body["screen"]["push_public_key"] == SETTINGS.vapid_public_key

    body = api.put("/v1/onboarding/notification-channels", headers=h,
                   json={"email": True, "browser": True, "browser_permission": "denied"}).json()
    assert body["notes"][0]["text"] == COPY["notify_browser_denied"]
    assert body["screen"]["id"] == "notification_frequency"
    assert body["next_step"]["prompt"] == COPY["notify_frequency_question"]
    assert [o["label"] for o in body["next_step"]["options"]] == [
        COPY["notify_frequency_due_only"], COPY["notify_frequency_daily"], COPY["notify_frequency_weekly"],
        COPY["notify_frequency_none"]]
    assert body["screen"]["body"] == [COPY["notify_quiet_hours_note"],
                                      COPY["notify_service_notice"].format(email=f"{subject}@example.test")]
    prefs = api.db.notification_preferences.find_one({"_id": user_id(api, subject)})
    assert prefs["channels"] == ["email", "in_app"] and prefs["browser_push_endpoint"] is None

    body = api.put("/v1/onboarding/notification-frequency", headers=h, json={"frequency": "weekly"}).json()
    assert body["account"]["status"] == "setup_complete" and body["account"]["onboarding_step"] == "complete"
    assert body["account"]["trial_started_at"] is None  # no free days used in setup
    assert body["screen"]["id"] == "setup_complete"
    assert body["screen"]["acknowledgment"] == COPY["setup_complete"].format(preferred_name="Trish")
    summary = COPY["setup_complete_summary"].format(voice="Steady and Direct", frequency="a short weekly summary",
                                                    channels="email and inside Cairn",
                                                    email=f"{subject}@example.test")
    assert body["screen"]["body"] == [summary, COPY["setup_complete_next"]]
    assert [o["label"] for o in body["next_step"]["options"]] == [COPY["setup_complete_primary_button"],
                                                                  COPY["setup_complete_secondary_button"]]
    assert any(n["text"] == COPY["signout_shared_device_tip"] for n in body["notes"])  # once, here (UC-REG-19)

    user = api.db.users.find_one({"idp_subject": subject})
    rows = list(api.db.consents.find({"user_id": user["_id"]}, sort=[("granted_at", 1), ("_id", 1)]))
    assert sorted(r["purpose"] for r in rows) == ["ai_notice", "privacy_terms", "trial_terms"]
    assert all(r["auth_provider"] == "apple" and r["client"] == "web/1.0" for r in rows)
    assert user["name_prefill"] is None
    # UC-REG-16: one welcome confirmation to the sign-in email (UC-CASE-21).
    assert [r["action_type"] for r in api.db.action_confirmation_outbox.find({"user_id": user["_id"]})] == [
        "setup_complete"]


def test_browser_notifications_need_permission_and_keep_the_endpoint_only_while_chosen(api):
    subject = "email|browser-push"
    start(api, subject)
    adult(api, subject)
    onboard(api, subject)  # finishes with email only
    h = as_user(subject)
    endpoint = "https://push.example.test/sub/abc"
    r = api.patch("/v1/me/notification-preferences", headers=h, json={"channels": {
        "email": True, "browser": True, "browser_permission": "granted", "browser_push_endpoint": endpoint}})
    assert r.status_code == 200 and r.json()["preferences"]["browser_notifications_on"] is True
    assert endpoint not in r.text  # never sent back
    r = api.patch("/v1/me/notification-preferences", headers=h, json={"channels": {"email": True}})
    prefs = api.db.notification_preferences.find_one({"_id": user_id(api, subject)})
    assert prefs["channels"] == ["email", "in_app"] and prefs["browser_push_endpoint"] is None  # UC-REG-17


def test_steps_must_go_in_order_and_repeats_are_harmless(api):
    subject = "email|order"
    start(api, subject)
    r = api.put("/v1/onboarding/personality", json={"choice": "warm_patient"}, headers=as_user(subject))
    assert r.status_code == 409 and r.json()["next_step"]["action"] == "confirm_adult"
    version = adult(api, subject)["screen"]["checkbox"]["document_version"]
    ack = {"agreed": True, "document_version": version, "client": "web/1"}
    api.post("/v1/onboarding/acknowledgments/privacy_terms", json=ack, headers=as_user(subject))
    again = api.post("/v1/onboarding/acknowledgments/privacy_terms", json=ack, headers=as_user(subject))
    assert again.status_code == 200 and again.json()["screen"]["id"] == "trial_terms"
    assert api.db.consents.count_documents({"user_id": user_id(api, subject)}) == 1
    r = api.put("/v1/onboarding/notification-frequency", json={"frequency": "daily"}, headers=as_user(subject))
    assert r.status_code == 409


@pytest.mark.parametrize("consent_type", ["privacy_terms", "trial_terms", "ai_notice"])
def test_declining_an_acknowledgment_is_never_a_dead_end(api, consent_type):
    subject = f"email|decline-{consent_type}"
    h = as_user(subject)
    start(api, subject)
    body = adult(api, subject)
    for earlier in ("privacy_terms", "trial_terms", "ai_notice"):
        if earlier == consent_type:
            break
        body = api.post(f"/v1/onboarding/acknowledgments/{earlier}", headers=h, json={
            "agreed": True, "document_version": body["screen"]["checkbox"]["document_version"],
            "client": "web/1"}).json()
    body = api.post(f"/v1/onboarding/acknowledgments/{consent_type}", headers=h,
                    json={"agreed": False, "client": "web/1"}).json()
    assert body["screen"]["id"] == "declined"
    assert body["next_step"]["prompt"] == COPY["decline_acknowledgment"]
    assert [o["value"] for o in body["next_step"]["options"]] == \
        [f"read_again:{consent_type}", "journey_map", "contact_support"]
    assert body["account"]["status"] == "pending_onboarding"
    # Coming back shows the same acknowledgment again.
    assert api.get("/v1/onboarding", headers=h).json()["screen"]["id"] == consent_type


def test_agreeing_to_an_old_version_is_refused(api):
    subject = "email|stale-version"
    start(api, subject)
    adult(api, subject)
    r = api.post("/v1/onboarding/acknowledgments/privacy_terms", headers=as_user(subject),
                 json={"agreed": True, "document_version": "privacy=old", "client": "web/1"})
    assert r.status_code == 422 and r.json()["code"] == "acknowledgment_outdated"


def _to_name_step(api, subject):
    h = as_user(subject)
    start(api, subject)
    body = adult(api, subject)
    for t in ("privacy_terms", "trial_terms", "ai_notice"):
        body = api.post(f"/v1/onboarding/acknowledgments/{t}", headers=h, json={
            "agreed": True, "document_version": body["screen"]["checkbox"]["document_version"],
            "client": "web/1"}).json()
    return h


@pytest.mark.parametrize("name,code", [("Pat 12345", "preferred_name_invalid"),
                                       ("123-45-6789", "preferred_name_invalid"),
                                       ("P" * 51, "preferred_name_invalid")])
def test_a_name_with_numbers_or_too_long_is_refused_and_never_kept(api, name, code, caplog):
    """data_boundary.enforcement: at most 50 characters and no 5 or more digits. Not saved, not logged."""
    subject = f"email|bad-name-{len(name)}-{name[:3]}"
    h = _to_name_step(api, subject)
    r = api.put("/v1/onboarding/preferred-name", headers=h, json={"preferred_name": name})
    assert r.status_code == 422 and r.json()["code"] == code
    assert r.json()["detail"] in (COPY["preferred_name_invalid"], load_copy()["preferred_name_invalid_too_long"])
    assert name not in r.text and name not in caplog.text
    assert api.db.users.find_one({"idp_subject": subject})["preferred_name"] is None
    assert api.put("/v1/onboarding/preferred-name", headers=h, json={"preferred_name": "Pat 2"}).status_code == 200


def test_distress_in_free_text_pauses_saves_nothing_and_offers_the_check_in(api):
    """UC-REG-14 (crisis plan context account_setup). Level 3 offers the follow-up once. Before UC-REG-15 it asks
    about email in the same question (checkin_email_ask_setup)."""
    subject = "email|distress"
    h = _to_name_step(api, subject)
    r = api.put("/v1/onboarding/preferred-name", headers=h, json={"preferred_name": "I can't do this"})
    body = r.json()
    assert r.status_code == 200
    assert body["screen"]["id"] == "paused"
    assert COPY["crisis_resource"] in body["screen"]["body"]
    assert body["account"]["preferred_name"] is None
    assert body["account"]["onboarding_step"] == "ai_notice_accepted"  # progress kept
    assert body["next_step"]["prompt"] == COPY["checkin_email_ask_setup"]
    assert [o["value"] for o in body["next_step"]["options"]][:3] == ["yes_email", "yes_in_cairn", "no"]
    body = api.post("/v1/onboarding/check-in", headers=h, json={"answer": "yes_email"}).json()
    user = api.db.users.find_one({"idp_subject": subject})
    assert user["check_in_at"] is not None and user["check_in_case_id"] is None and user["check_in_by_email"]
    assert body["screen"]["id"] == "preferred_name"  # setup resumes where it was


def test_risk_of_harm_in_the_name_field_puts_988_first_with_nothing_to_answer(api):
    """AC-26-01 in setup: the first reply is 988 and no question. The follow-up comes on the next turn."""
    subject = "email|distress-4"
    h = _to_name_step(api, subject)
    body = api.put("/v1/onboarding/preferred-name", headers=h, json={"preferred_name": "I want to die"}).json()
    assert body["screen"]["id"] == "paused" and body["next_step"]["action"] == "paused"
    assert [o["value"] for o in body["next_step"]["options"]] == ["continue"]
    offer = api.get("/v1/onboarding", params={"offer_check_in": True}, headers=h).json()
    assert offer["next_step"]["action"] == "check_in_offer"
    body = api.post("/v1/onboarding/check-in", headers=h, json={"answer": "no"}).json()
    assert api.db.users.find_one({"idp_subject": subject})["check_in_at"] is None  # a no stores nothing


def test_setup_screens_are_identical_for_every_voice(api):
    """UC-REG-14 and the crisis protocol: the same for every voice."""
    screens = []
    for v in VOICES:
        subject = f"email|crisis-{v}"
        start(api, subject)
        onboard(api, subject, voice=v)
        screens.append(api.get("/v1/me/break", headers=as_user(subject)).json())
        me = api.get("/v1/me", headers=as_user(subject)).json()["account"]
        assert me["voice"] == v and me["ai_label"] == COPY["ai_persistent_label"]
    assert all(s == screens[0] for s in screens)


def test_voice_can_change_anytime_in_settings_and_each_change_is_read_back_and_confirmed(api):
    """UC-REG-17."""
    subject = "email|settings"
    start(api, subject)
    onboard(api, subject)
    h = as_user(subject)
    r = api.patch("/v1/me", json={"voice": "plain_practical", "time_zone": "Pacific/Honolulu"}, headers=h)
    assert r.status_code == 200
    assert r.json()["account"]["voice"] == "plain_practical"
    ack = r.json()["notes"][0]["text"]
    assert "Plain and Practical" in ack and "A confirmation is on its way" in ack
    assert api.patch("/v1/me", json={"time_zone": "Mars/Base"}, headers=h).status_code == 422
    assert api.patch("/v1/me", json={"preferred_name": "Card 4111 1111"}, headers=h).status_code == 422
    uid = user_id(api, subject)
    kinds = [r["action_type"] for r in api.db.action_confirmation_outbox.find({"user_id": uid})]
    assert kinds.count("settings_changed") == 1  # one waiting confirmation, however many changes


def test_settings_wait_at_care_level_4(api):
    """AC-26-11: no Settings change in the same turn at level 4."""
    subject = "email|settings-4"
    start(api, subject)
    onboard(api, subject)
    h = as_user(subject)
    r = api.patch("/v1/me", json={"voice": "warm_patient", "care_level": 4}, headers=h)
    assert r.status_code == 200 and r.json()["account"]["voice"] == "steady_direct"
    r = api.patch("/v1/me/notification-preferences", json={"stop_all_reminders": True, "care_level": 4}, headers=h)
    assert r.json()["next_step"]["action"] == "not_now"
    assert api.db.notification_preferences.find_one({"_id": user_id(api, subject)})["frequency"] == "due_only"
    assert api.post("/v1/me/deletion", json={"confirm": True, "care_level": 4}, headers=h).status_code == 409
    assert api.get("/v1/me", headers=h).status_code == 200  # nothing was deleted


# ------------------------------------------------------------------ UC-REG-13, UC-REG-18, UC-REG-19

def test_returning_user_resumes_where_they_left_off(api):
    subject = "email|resume"
    start(api, subject)
    version = adult(api, subject)["screen"]["checkbox"]["document_version"]
    api.post("/v1/onboarding/acknowledgments/privacy_terms", headers=as_user(subject),
             json={"agreed": True, "document_version": version, "client": "web/1"})
    r = start(api, subject)
    assert r.status_code == 200
    body = r.json()
    assert body["notes"][0]["text"] == COPY["resume_onboarding"]
    assert body["screen"]["id"] == "trial_terms"


def test_finished_user_with_no_case_goes_to_setup_complete_then_home(api):
    subject = "email|resume-no-case"
    start(api, subject)
    onboard(api, subject)
    body = start(api, subject).json()
    assert body["screen"]["id"] == "setup_complete"
    assert body["account"]["trial_started_at"] is None  # no free days used yet
    home = api.get("/v1/home", params={"session_start": True}, headers=as_user(subject)).json()
    assert home["route"] == "home" and home["cases"] == []
    assert home["next_step"]["action"] == "start_case"


def test_changed_policy_version_is_acknowledged_again_before_continuing(api):
    subject = "email|reack"
    start(api, subject)
    onboard(api, subject)
    h = as_user(subject)
    original = api.app.state.settings
    api.app.state.settings = dataclasses.replace(original, privacy_version="privacy-v2")
    try:
        body = start(api, subject).json()
        assert body["screen"]["id"] == "privacy_terms"
        assert any(n["text"] == load_copy()["acknowledgment_version_changed"] for n in body["notes"])
        r = api.post("/v1/cases", json=CASE_BODY, headers=h)
        assert r.status_code == 409 and r.json()["code"] == "acknowledgment_required"
        body = api.post("/v1/onboarding/acknowledgments/privacy_terms", headers=h, json={
            "agreed": True, "document_version": body["screen"]["checkbox"]["document_version"],
            "client": "web/2"}).json()
        assert body["screen"]["id"] == "setup_complete"  # trial and AI notice weren't asked again
        assert api.post("/v1/cases", json=CASE_BODY, headers=h).status_code == 201
    finally:
        api.app.state.settings = original


def test_signing_out_ends_the_session_and_a_new_sign_in_works(api):
    """UC-REG-19 and D-20. The token that signed out stops working. Everything is saved."""
    subject = "email|signout"
    start(api, subject)
    onboard(api, subject)
    old = datetime.now(timezone.utc) - timedelta(minutes=1)
    h = {**as_user(subject), "X-Test-Issued-At": old.isoformat()}
    assert api.get("/v1/me", headers=h).status_code == 200
    r = api.post("/v1/me/sign-out", headers=h, params={"care_level": 3})
    assert r.json()["message"] == COPY["signed_out"]
    assert r.json()["support"] == [COPY["crisis_resource"], COPY["support_resources_link"]]
    r = api.get("/v1/me", headers=h)
    assert r.status_code == 401 and r.json()["code"] == "session_timed_out"
    assert r.json()["detail"] == COPY["session_timed_out"]
    fresh = {**as_user(subject), "X-Test-Issued-At": datetime.now(timezone.utc).isoformat()}
    assert api.get("/v1/me", headers=fresh).status_code == 200
    assert api.get("/v1/me", headers=as_user(subject)).json()["account"]["preferred_name"] == "Pat"


def test_five_minutes_with_no_activity_signs_out(api):
    """D-20: a token from a session that went quiet for more than 5 minutes is refused. A new token works."""
    subject = "email|timeout"
    start(api, subject)
    onboard(api, subject)
    issued = datetime.now(timezone.utc) - timedelta(minutes=10)
    h = {**as_user(subject), "X-Test-Issued-At": issued.isoformat()}
    api.db.users.update_one({"idp_subject": subject}, {"$set": {"last_active_at": issued + timedelta(seconds=30)}})
    r = api.get("/v1/me", headers=h)
    assert r.status_code == 401 and r.json()["code"] == "session_timed_out"
    api.db.users.update_one({"idp_subject": subject}, {"$set": {"last_active_at": datetime.now(timezone.utc)}})
    assert api.get("/v1/me", headers=h).status_code == 200  # activity within 5 minutes keeps it
    old = {**as_user(subject), "X-Test-Issued-At": (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()}
    assert api.get("/v1/me", headers=old).json()["code"] == "session_timed_out"  # 30 days at most


# ------------------------------------------------------------------ trial (D-02 to D-06, D-14, UC-REG-08)

def test_case_needs_finished_onboarding(api):
    subject = "email|early-case"
    start(api, subject)
    r = api.post("/v1/cases", json=CASE_BODY, headers=as_user(subject))
    assert r.status_code == 409 and r.json()["code"] == "onboarding_incomplete"


def test_trial_starts_at_the_first_start_journey_not_at_case_creation(api):
    """Case creation spec DEC-01 and DEC-04: the clock starts at Start journey. D-14: one note, a week before."""
    subject = "email|trial"
    start(api, subject, time_zone="America/Los_Angeles")
    onboard(api, subject)
    h = as_user(subject)
    first = api.post("/v1/cases", json=CASE_BODY, headers=h).json()["case"]["id"]
    answer(api, subject, first, "display_name", "Dan")
    me = api.get("/v1/me", headers=h).json()["account"]
    assert me["trial_started_at"] is None and me["status"] == "setup_complete" and not me["free_days_running"]

    started = start_journey(api, subject, first)
    me = api.get("/v1/me", headers=h).json()["account"]
    assert me["free_days_running"] and me["access"] == "full" and started["trial_started_now"] is True
    ends = me["trial_ends_at"]
    assert datetime.fromisoformat(ends) - datetime.fromisoformat(me["trial_started_at"]) == timedelta(days=28)
    local_end = datetime.fromisoformat(ends).astimezone(ZoneInfo("America/Los_Angeles")).date()
    assert me["trial_end_date"] == local_end.isoformat()

    second = api.post("/v1/cases", json=CASE_BODY, headers=h).json()["case"]["id"]
    again = start_journey(api, subject, second)
    assert again["trial_started_now"] is False
    assert api.get("/v1/me", headers=h).json()["account"]["trial_started_at"] == me["trial_started_at"]
    user = api.db.users.find_one({"idp_subject": subject})
    kinds = [(r["kind"], r["due_at"] - user["trial_started_at"])
             for r in api.db.trial_reminders.find({"user_id": user["_id"]}, sort=[("kind", 1)])]
    assert kinds == [("trial_ends_soon", timedelta(days=21))]


def test_the_trial_note_shows_in_the_app(api):
    subject = "email|reminder"
    start(api, subject, time_zone="America/New_York")
    onboard(api, subject)
    cid = api.post("/v1/cases", json=CASE_BODY, headers=as_user(subject)).json()["case"]["id"]
    start_journey(api, subject, cid)
    api.db.trial_reminders.update_one({"user_id": user_id(api, subject)},
                                      {"$set": {"due_at": datetime.now(ZoneInfo("UTC")) - timedelta(minutes=1)}})
    notes = api.get("/v1/me", headers=as_user(subject)).json()["notes"]
    assert notes[0]["kind"] == "reminder"
    assert notes[0]["text"].startswith("Your free time with Cairn ends in a week, on ")


def test_after_the_trial_the_account_is_read_only_and_nothing_is_lost(api):
    subject = "email|expired"
    start(api, subject)
    onboard(api, subject)
    h = as_user(subject)
    cid = api.post("/v1/cases", json=CASE_BODY, headers=h).json()["case"]["id"]
    answer(api, subject, cid, "display_name", "Dan")
    start_journey(api, subject, cid)
    expire_trial(api, subject)

    me = api.get("/v1/me", headers=h).json()
    assert (me["account"]["status"], me["account"]["access"]) == ("setup_complete", "read_only")
    assert me["notes"][0]["text"] == SUB_COPY["read_only_banner"]
    view = api.get(f"/v1/cases/{cid}", headers=h)
    assert view.status_code == 200 and view.json()["case"]["display_name"] == "Dan"
    # Read-only is the account's access, never a case status.
    assert (view.json()["case"]["status"], view.json()["case"]["account_access"]) == ("active", "read_only")
    assert view.json()["notes"][0]["text"] == SUB_COPY["read_only_banner"]
    r = api.put(f"/v1/cases/{cid}/intake/answers/display_name", json={"state": "answered", "value": "Changed"},
                headers=h)
    assert r.status_code == 403 and r.json()["code"] == "account_read_only"
    assert r.json()["next_step"]["action"] == "choose_subscription"
    # Settings and deletion stay available (UC-REG-17, UC-ACCT-01).
    assert api.patch("/v1/me", json={"voice": "warm_patient"}, headers=h).status_code == 200
    assert api.post("/v1/me/deletion", json={"confirm": True}, headers=h).status_code == 200


# ------------------------------------------------------------------ UC-ACCT-01

def test_delete_account_explains_then_deletes_everything(api):
    subject = "apple|001.delete"
    start(api, subject, method="apple")
    onboard(api, subject, method="apple")
    h = as_user(subject, method="apple")
    api.post("/v1/cases", json=CASE_BODY, headers=h)
    info = api.get("/v1/me/deletion", headers=h).json()
    # One button, no reason asked, nothing offered to keep the user.
    assert info["explanation"] and [o["label"] for o in info["next_step"]["options"]] == [
        "Delete my account and everything in it"]
    assert info["subscription_note"] is None
    assert api.post("/v1/me/deletion", json={"confirm": False}, headers=h).status_code == 422
    r = api.post("/v1/me/deletion", json={"confirm": True}, headers=h)
    assert r.status_code == 200
    assert api.get("/v1/me", headers=h).json()["code"] == "registration_required"
    assert [r["provider"] for r in api.db.identity_deletion_requests.find({"idp_subject": subject})] == ["apple"]
    users = [u["_id"] for u in api.db.users.find({}, {"_id": 1})]
    assert api.db.consents.count_documents({"user_id": {"$nin": users}}) == 0
    assert api.db.notification_preferences.count_documents({"_id": {"$nin": users}}) == 0
