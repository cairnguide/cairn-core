"""UC-REG-01 to UC-REG-14 and UC-ACCT-01, from database/docs/cairn-registration-use-cases.json.

Tests that take the `contract_client` fixture, or no fixture, need no database. The rest
need CAIRN_TEST_ADMIN_URL (see conftest.py). Fake data only.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest

from cairn_api.copy_store import consent_versions, load_copy

from .conftest import REPO, SETTINGS, answer, as_user, onboard, start_journey

VOICES = ("steady_direct", "warm_patient", "brisk_businesslike", "plain_practical")

SPEC = json.loads((REPO / "database" / "docs" / "cairn-registration-use-cases.json").read_text())
COPY = SPEC["copy"]
CASE_BODY = {"user_role": "spouse_partner"}


def start(api, subject, method="email", email=None, **body):
    r = api.post("/v1/registrations", json=body, headers=as_user(subject, email, method=method))
    assert r.status_code in (200, 201), r.text
    return r


def owner_sql(url, query, params=()):
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(query, params)
        return cur.fetchall() if cur.description else None


def expire_trial(url, subject):
    """Moves an account's trial 29 days into the past. The guard trigger is lifted only for this statement."""
    with psycopg.connect(url) as conn:
        conn.execute("ALTER TABLE cairn.users DISABLE TRIGGER users_trial_set_once")
        conn.execute("UPDATE cairn.users SET trial_started_at = trial_started_at - interval '29 days', "
                     "trial_ends_at = trial_ends_at - interval '29 days' WHERE idp_subject = %s", (subject,))
        conn.execute("ALTER TABLE cairn.users ENABLE TRIGGER users_trial_set_once")


# ------------------------------------------------------------------ copy (no database)

def test_copy_is_the_spec_copy_verbatim():
    copy = load_copy()
    assert copy.spec == COPY
    assert copy.version == SPEC["spec"]["version"]


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


def test_every_voice_has_a_confirmation_that_uses_the_preferred_name():
    copy = load_copy()
    for voice in VOICES:
        assert "{preferred_name}" in copy[f"voice_{voice}_confirm"]
    assert not [k for k in copy.draft if k.startswith("personality_")]


# ------------------------------------------------------------------ UC-REG-01 (no database)

def test_welcome_acknowledges_first_and_asks_for_nothing(contract_client):
    body = contract_client.get("/v1/welcome").json()
    assert body["acknowledgment"] == COPY["welcome_acknowledgment"]
    assert [m["label"] for m in body["methods"]] == \
        ["Continue with Google", "Continue with Apple", "Continue with email"]
    assert body["sign_in_label"] == "Already have an account? Sign in."
    assert body["not_ready"] == {"label": COPY["welcome_not_ready_link"], "url": SETTINGS.journey_map_url}
    assert body["support"]["need_a_moment_label"] == COPY["need_a_moment_control"]
    assert body["notes"] == []


def test_welcome_after_cancelled_provider_sign_in(contract_client):
    notes = contract_client.get("/v1/welcome", params={"oauth_cancelled": True}).json()["notes"]
    assert [n["text"] for n in notes] == [COPY["oauth_cancelled"]]


def test_email_is_passwordless_by_default(contract_client):
    methods = {m["method"]: m["auth0_connection"] for m in contract_client.get("/v1/sign-in-methods").json()["methods"]}
    assert methods == {"google": "google-oauth2", "apple": "apple", "email": "email"}


# ------------------------------------------------------------------ UC-REG-14 (no database)

def test_need_a_moment_stops_and_offers_988(contract_client):
    body = contract_client.get("/v1/onboarding/need-a-moment").json()
    assert body["screen"]["id"] == "paused"
    assert COPY["crisis_resource"] in body["screen"]["body"]
    assert body["next_step"]["action"] == "paused"
    assert [o["label"] for o in body["next_step"]["options"]] == ["I'm ready to continue"]


def test_age_is_never_asked(contract_client):
    assert contract_client.post("/v1/onboarding/age", json={"is_18_or_older": True},
                                headers=as_user("x")).status_code == 404
    spec = contract_client.app.openapi()
    assert "/v1/onboarding/age" not in spec["paths"]
    assert "age_confirmed" not in json.dumps(spec)


def test_registration_request_takes_no_legal_name(contract_client):
    r = contract_client.post("/v1/registrations", json={"first_name": "A", "last_name": "B"},
                             headers=as_user("x"))
    assert r.status_code == 422


# ------------------------------------------------------------------ UC-REG-02 to UC-REG-05

def test_new_account_is_pending_and_goes_straight_to_privacy_terms(api):
    r = start(api, "google-oauth2|reg-new", method="google", name_from_provider="Patricia",
              time_zone="America/Chicago")
    assert r.status_code == 201
    body = r.json()
    assert body["account"]["status"] == "pending_onboarding"
    assert body["account"]["sign_in_method"] == "google"
    assert body["account"]["preferred_name"] is None  # the shared name is only a pre-fill
    assert body["screen"]["id"] == "privacy_terms"  # no age question
    assert body["next_step"]["prompt"] == COPY["privacy_terms_checkbox"]
    assert body["support"]["need_a_moment_label"] == COPY["need_a_moment_control"]


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
    assert owner_sql(api.scratch_url, "SELECT count(*) FROM cairn.users WHERE lower(email) = %s", (email,))[0][0] == 1


# ------------------------------------------------------------------ UC-REG-07 to UC-REG-12 (no UC-REG-06 age step)

def test_full_onboarding_sequence(api):
    subject = "apple|001.full"
    h = as_user(subject, method="apple")
    body = start(api, subject, method="apple", name_from_provider="Patricia", time_zone="America/Denver").json()
    screen = body["screen"]
    assert screen["id"] == "privacy_terms"
    assert screen["body"] == [COPY["privacy_terms_summary"]]
    assert screen["checkbox"]["label"] == COPY["privacy_terms_checkbox"]
    assert screen["checkbox"]["checked"] is False
    assert screen["ai_provider"] == SETTINGS.ai_provider_name
    assert {link["url"] for link in screen["links"]} == {SETTINGS.privacy_policy_url, SETTINGS.terms_url}

    body = api.post("/v1/onboarding/acknowledgments/privacy_terms", headers=h, json={
        "agreed": True, "document_version": screen["checkbox"]["document_version"], "client": "ios/1.0"}).json()
    assert body["screen"]["id"] == "trial_terms"
    assert body["screen"]["body"] == [COPY["trial_summary"]]
    assert body["account"]["trial_started_at"] is None

    body = api.post("/v1/onboarding/acknowledgments/trial_terms", headers=h, json={
        "agreed": True, "document_version": body["screen"]["checkbox"]["document_version"],
        "client": "ios/1.0"}).json()
    assert body["account"]["trial_started_at"] is None  # the clock doesn't start at the acknowledgment
    assert body["screen"]["body"] == [COPY["ai_notice"]]
    assert body["screen"]["legal_notice"] == [COPY["ai_notice_legal"]]
    assert "988" in body["screen"]["body"][0]
    assert body["account"]["ai_label"] is None

    body = api.post("/v1/onboarding/acknowledgments/ai_notice", headers=h, json={
        "agreed": True, "document_version": body["screen"]["checkbox"]["document_version"],
        "client": "ios/1.0"}).json()
    assert body["account"]["ai_label"] == COPY["ai_persistent_label"]
    assert body["screen"]["id"] == "preferred_name"
    assert body["screen"]["input"]["prefill"] == "Patricia"
    assert body["screen"]["input"]["optional_link_label"] == COPY["pronunciation_link"]
    assert body["next_step"]["prompt"] == COPY["preferred_name_question"]

    body = api.put("/v1/onboarding/preferred-name", headers=h,
                   json={"preferred_name": "Trish", "name_pronunciation": "trish"}).json()
    assert body["account"]["preferred_name"] == "Trish"
    assert body["screen"]["id"] == "personality"
    assert body["next_step"]["prompt"] == COPY["personality_question"]
    assert [c["value"] for c in body["screen"]["choices"]] == list(VOICES)
    assert all(c["label"] and c["tagline"] and c["sample"] for c in body["screen"]["choices"])
    assert body["screen"]["sample_situation"]
    assert [o["value"] for o in body["next_step"]["options"]] == [*VOICES, "choose_for_me"]
    assert body["next_step"]["options"][-1]["label"] == COPY["personality_default_button"]
    assert body["account"]["voice"] == "steady_direct"  # the column default, before a choice

    body = api.put("/v1/onboarding/personality", headers=h, json={"choice": "choose_for_me"}).json()
    assert body["account"]["voice"] == "steady_direct"
    assert body["account"]["status"] == "active_no_case"
    assert body["screen"]["id"] == "case_handoff"
    assert "Trish" in body["screen"]["acknowledgment"]
    assert body["next_step"]["action"] == "choose_relationship"

    rows = owner_sql(api.scratch_url,
                     "SELECT c.purpose, c.auth_provider, c.client, u.name_prefill "
                     "FROM cairn.consents c JOIN cairn.users u ON u.id = c.user_id "
                     "WHERE u.idp_subject = %s ORDER BY c.granted_at", (subject,))
    assert [r[0] for r in rows] == ["privacy_terms", "trial_terms", "ai_notice"]
    assert all(r[1] == "apple" and r[2] == "ios/1.0" and r[3] is None for r in rows)


def test_steps_must_go_in_order_and_repeats_are_harmless(api):
    subject = "email|order"
    version = start(api, subject).json()["screen"]["checkbox"]["document_version"]
    r = api.put("/v1/onboarding/personality", json={"choice": "warm_patient"}, headers=as_user(subject))
    assert r.status_code == 409 and r.json()["next_step"]["action"] == "acknowledge_privacy_terms"
    ack = {"agreed": True, "document_version": version, "client": "web/1"}
    api.post("/v1/onboarding/acknowledgments/privacy_terms", json=ack, headers=as_user(subject))
    again = api.post("/v1/onboarding/acknowledgments/privacy_terms", json=ack, headers=as_user(subject))
    assert again.status_code == 200 and again.json()["screen"]["id"] == "trial_terms"
    assert owner_sql(api.scratch_url, "SELECT count(*) FROM cairn.consents c JOIN cairn.users u ON u.id = c.user_id "
                                      "WHERE u.idp_subject = %s", (subject,))[0][0] == 1


@pytest.mark.parametrize("consent_type", ["privacy_terms", "trial_terms", "ai_notice"])
def test_declining_an_acknowledgment_is_never_a_dead_end(api, consent_type):
    subject = f"email|decline-{consent_type}"
    h = as_user(subject)
    body = start(api, subject).json()
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
    r = api.post("/v1/onboarding/acknowledgments/privacy_terms", headers=as_user(subject),
                 json={"agreed": True, "document_version": "privacy=old", "client": "web/1"})
    assert r.status_code == 422 and r.json()["code"] == "acknowledgment_outdated"


def test_pre_filled_name_is_never_saved_silently(api):
    subject = "google-oauth2|prefill"
    start(api, subject, method="google", name_from_provider="Robert")
    onboard(api, subject, method="google", preferred_name="Bob")
    assert api.get("/v1/me", headers=as_user(subject, method="google")).json()["account"]["preferred_name"] == "Bob"
    assert owner_sql(api.scratch_url, "SELECT name_prefill FROM cairn.users WHERE idp_subject = %s",
                     (subject,))[0][0] is None


def test_distress_in_free_text_pauses_and_saves_nothing(api):
    subject = "email|distress"
    h = as_user(subject)
    body = start(api, subject).json()
    for t in ("privacy_terms", "trial_terms", "ai_notice"):
        body = api.post(f"/v1/onboarding/acknowledgments/{t}", headers=h, json={
            "agreed": True, "document_version": body["screen"]["checkbox"]["document_version"],
            "client": "web/1"}).json()
    r = api.put("/v1/onboarding/preferred-name", headers=h, json={"preferred_name": "I want to die"})
    body = r.json()
    assert r.status_code == 200
    assert body["screen"]["id"] == "paused"
    assert COPY["crisis_resource"] in body["screen"]["body"]
    assert body["account"]["preferred_name"] is None
    assert body["account"]["onboarding_step"] == "ai_notice_accepted"  # progress kept
    assert api.get("/v1/onboarding", headers=h).json()["screen"]["id"] == "preferred_name"


def test_crisis_response_is_identical_for_every_voice(api):
    screens = []
    for v in VOICES:
        subject = f"email|crisis-{v}"
        start(api, subject)
        onboard(api, subject, voice=v)
        screens.append(api.get("/v1/onboarding/need-a-moment").json())
        me = api.get("/v1/me", headers=as_user(subject)).json()["account"]
        assert me["voice"] == v and me["ai_label"] == COPY["ai_persistent_label"]
    assert all(s == screens[0] for s in screens)


def test_voice_can_change_anytime_in_settings(api):
    subject = "email|settings"
    start(api, subject)
    onboard(api, subject)
    r = api.patch("/v1/me", json={"voice": "plain_practical", "time_zone": "Pacific/Honolulu"},
                  headers=as_user(subject))
    assert r.status_code == 200
    assert r.json()["account"]["voice"] == "plain_practical"
    assert api.patch("/v1/me", json={"time_zone": "Mars/Base"}, headers=as_user(subject)).status_code == 422


# ------------------------------------------------------------------ UC-REG-13

def test_returning_user_resumes_where_they_left_off(api):
    subject = "email|resume"
    version = start(api, subject).json()["screen"]["checkbox"]["document_version"]
    api.post("/v1/onboarding/acknowledgments/privacy_terms", headers=as_user(subject),
             json={"agreed": True, "document_version": version, "client": "web/1"})
    r = start(api, subject)
    assert r.status_code == 200
    body = r.json()
    assert body["notes"][0]["text"] == COPY["resume_onboarding"]
    assert body["screen"]["id"] == "trial_terms"


def test_finished_user_with_no_case_is_guided_to_case_creation(api):
    subject = "email|resume-no-case"
    start(api, subject)
    onboard(api, subject)
    body = start(api, subject).json()
    assert body["screen"]["id"] == "case_handoff"
    assert body["notes"][0]["text"] == COPY["resume_onboarding"]
    assert body["account"]["trial_started_at"] is None  # no free days used yet


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
        assert body["screen"]["id"] == "case_handoff"  # trial and AI notice weren't asked again
        assert api.post("/v1/cases", json=CASE_BODY, headers=h).status_code == 201
    finally:
        api.app.state.settings = original


# ------------------------------------------------------------------ trial (D-02 to D-06, UC-REG-08)

def test_case_needs_finished_onboarding(api):
    subject = "email|early-case"
    start(api, subject)
    r = api.post("/v1/cases", json=CASE_BODY, headers=as_user(subject))
    assert r.status_code == 409 and r.json()["code"] == "onboarding_incomplete"


def test_trial_starts_at_the_first_start_journey_not_at_case_creation(api):
    """Case creation spec DEC-01 and DEC-04 replace registration D-03: the clock starts at Start journey."""
    subject = "email|trial"
    start(api, subject, time_zone="America/Los_Angeles")
    onboard(api, subject)
    h = as_user(subject)
    first = api.post("/v1/cases", json=CASE_BODY, headers=h).json()["case"]["id"]
    answer(api, subject, first, "display_name", "Dan")
    me = api.get("/v1/me", headers=h).json()["account"]
    assert me["trial_started_at"] is None and me["status"] == "active_no_case"

    started = start_journey(api, subject, first)
    me = api.get("/v1/me", headers=h).json()["account"]
    assert me["status"] == "trial_active" and started["trial_started_now"] is True
    ends = me["trial_ends_at"]
    assert datetime.fromisoformat(ends) - datetime.fromisoformat(me["trial_started_at"]) == timedelta(days=28)
    local_end = datetime.fromisoformat(ends).astimezone(ZoneInfo("America/Los_Angeles")).date()
    assert me["trial_end_date"] == local_end.isoformat()

    second = api.post("/v1/cases", json=CASE_BODY, headers=h).json()["case"]["id"]
    again = start_journey(api, subject, second)
    assert again["trial_started_now"] is False
    assert api.get("/v1/me", headers=h).json()["account"]["trial_started_at"] == me["trial_started_at"]
    kinds = owner_sql(api.scratch_url, "SELECT r.kind, r.due_at - u.trial_started_at FROM cairn.trial_reminders r "
                                       "JOIN cairn.users u ON u.id = r.user_id WHERE u.idp_subject = %s "
                                       "ORDER BY r.kind", (subject,))
    assert kinds == [("trial_day_21", timedelta(days=21)), ("trial_day_27", timedelta(days=27)),
                     ("trial_ends_soon", timedelta(days=25))]


def test_day_21_reminder_shows_in_the_app(api):
    subject = "email|reminder"
    start(api, subject, time_zone="America/New_York")
    onboard(api, subject)
    cid = api.post("/v1/cases", json=CASE_BODY, headers=as_user(subject)).json()["case"]["id"]
    start_journey(api, subject, cid)
    owner_sql(api.scratch_url, "UPDATE cairn.trial_reminders r SET due_at = now() - interval '1 minute' "
                               "FROM cairn.users u WHERE u.id = r.user_id AND u.idp_subject = %s "
                               "AND r.kind = 'trial_day_21'", (subject,))
    notes = api.get("/v1/me", headers=as_user(subject)).json()["notes"]
    assert notes[0]["kind"] == "reminder"
    assert notes[0]["text"].startswith("Your free time with Cairn ends in 7 days, on ")


def test_after_the_trial_the_account_is_read_only_and_nothing_is_lost(api):
    subject = "email|expired"
    start(api, subject)
    onboard(api, subject)
    h = as_user(subject)
    cid = api.post("/v1/cases", json=CASE_BODY, headers=h).json()["case"]["id"]
    answer(api, subject, cid, "display_name", "Dan")
    start_journey(api, subject, cid)
    expire_trial(api.scratch_url, subject)

    me = api.get("/v1/me", headers=h).json()
    assert me["account"]["status"] == "read_only"
    assert me["notes"][0]["text"] == COPY["read_only_banner"]
    view = api.get(f"/v1/cases/{cid}", headers=h)
    assert view.status_code == 200 and view.json()["case"]["display_name"] == "Dan"
    assert view.json()["case"]["status"] == "read_only"
    assert view.json()["notes"][0]["text"] == COPY["read_only_banner"]
    r = api.put(f"/v1/cases/{cid}/intake/answers/display_name", json={"state": "answered", "value": "Changed"},
                headers=h)
    assert r.status_code == 403 and r.json()["code"] == "account_read_only"
    assert r.json()["next_step"]["action"] == "choose_subscription"
    # Settings and deletion stay available.
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
    assert info["explanation"] and [o["value"] for o in info["next_step"]["options"]] == ["confirm", "cancel"]
    assert api.post("/v1/me/deletion", json={"confirm": False}, headers=h).status_code == 422
    r = api.post("/v1/me/deletion", json={"confirm": True}, headers=h)
    assert r.status_code == 200
    assert api.get("/v1/me", headers=h).json()["code"] == "registration_required"
    assert owner_sql(api.scratch_url, "SELECT provider FROM cairn.identity_deletion_requests "
                                      "WHERE idp_subject = %s", (subject,)) == [("apple",)]
    assert owner_sql(api.scratch_url, "SELECT count(*) FROM cairn.consents c LEFT JOIN cairn.users u "
                                      "ON u.id = c.user_id WHERE u.id IS NULL")[0][0] == 0
