"""What case creation spec 2.0.0 added, as the 3.2.0 suite has it, against a real MongoDB: the care rest and the free
days (crisis plan 3.2.0, take a break 3.2.0), the follow-up check-in on the account, the SB 243 count, billing during
a crisis, speech input, the AI reminder and rest offers, under 18, someone else handling it, and the forward
migration.

[SAFETY], [PRIVACY], and [LEGAL] items are release blockers.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from cairn_api.copy_store import load_break_copy, load_case_copy

from .conftest import (
    _import_database_tools,
    active_case,
    answer,
    as_user,
    expire_trial,
    new_draft,
    register,
    start_journey,
)
from .test_account_lifecycle import ALWAYS, FakeMailer, due_tomorrow, send_outbound, set_prefs
from .test_case_creation import case_doc, say, trial, without_random_material

COPY = load_case_copy()
BREAK = load_break_copy()
LEVEL_3 = {"safety_mode": "acute_distress"}


def sent_with(mailer, email, subject):
    return [m for m in mailer.to(email) if m[0] == subject]


def post(api, subject, path, body=None):
    r = api.post(path, json=body, headers=as_user(subject))
    assert r.status_code == 200, r.text
    return r.json()


def user_doc(api, subject):
    return api.db.users.find_one({"idp_subject": subject})


# ------------------------------------------------------------------ [TRIAL] [REST] DEC-26-01

def test_care_rest_pauses_the_free_days_and_moves_the_trial_end_by_exactly_the_paused_time(api):
    register(api, "v2rest")
    cid, _ = active_case(api, "v2rest")
    _, ends = trial(api, "v2rest")
    turn = say(api, "v2rest", cid, "I can't do this.")
    assert turn["safety_mode"] == "acute_distress"
    u = user_doc(api, "v2rest")
    assert u["trial_clock_paused_at"] is not None
    assert api.db.cases.find_one({"_id": UUID(cid)})["tasks_paused_until"] is not None
    # Two days pass while resting.
    paused_at = u["trial_clock_paused_at"] - timedelta(days=2)
    api.db.users.update_one({"_id": u["_id"]}, {"$set": {"trial_clock_paused_at": paused_at}})
    reminder_before = api.db.trial_reminders.find_one({"user_id": u["_id"], "kind": "trial_ends_soon"})["due_at"]
    back = post(api, "v2rest", f"/v1/cases/{cid}/journey/resume")
    u = user_doc(api, "v2rest")
    moved = u["trial_ends_at"] - ends
    assert u["trial_clock_paused_at"] is None
    assert timedelta(days=2) <= moved < timedelta(days=2, minutes=1)
    reminder = api.db.trial_reminders.find_one({"user_id": u["_id"], "kind": "trial_ends_soon"})["due_at"]
    assert reminder - reminder_before == moved  # the reminder moves with it
    assert any("paused" in n["text"] for n in back["notes"])


def test_a_rest_in_normal_mode_keeps_the_free_days_counting_and_says_so_first(api):
    register(api, "v2rest2")
    cid, _ = active_case(api, "v2rest2")
    _, ends = trial(api, "v2rest2")
    offer = post(api, "v2rest2", f"/v1/cases/{cid}/take-a-break")
    assert offer["next_step"]["action"] == "choose_rest"
    assert [o["label"] for o in offer["next_step"]["options"]] == [BREAK[k] for k in (
        "rest_choice_today", "rest_choice_3_days", "rest_choice_7_days", "rest_choice_open")]
    assert any("keep counting" in b for b in offer["body"])
    post(api, "v2rest2", f"/v1/cases/{cid}/take-a-break", {"rest_choice": "three_days"})
    u = user_doc(api, "v2rest2")
    assert u["trial_clock_paused_at"] is None and u["trial_ends_at"] == ends


def test_a_rest_chosen_at_level_2_is_a_care_rest(api):
    register(api, "v2rest3")
    cid, _ = active_case(api, "v2rest3")
    post(api, "v2rest3", f"/v1/cases/{cid}/take-a-break",
         {"rest_choice": "week", "session": {"safety_mode": "overwhelm"}})
    assert user_doc(api, "v2rest3")["trial_clock_paused_at"] is not None


def test_a_draft_take_a_break_saves_and_never_touches_the_trial(api):
    register(api, "v2rest4")
    cid = new_draft(api, "v2rest4")["case"]["id"]
    r = post(api, "v2rest4", f"/v1/cases/{cid}/take-a-break")
    assert r["next_step"]["prompt"] == BREAK["draft_pause"]
    assert trial(api, "v2rest4") == (None, None)
    assert user_doc(api, "v2rest4")["break_started_at"] is None  # a draft pause sets no break fields


def test_expire_trials_never_expires_a_paused_clock(api):
    from cairn_api import maintenance
    register(api, "v2rest5")
    cid, _ = active_case(api, "v2rest5")
    say(api, "v2rest5", cid, "I can't do this.")
    u = user_doc(api, "v2rest5")
    api.db.users.update_one({"_id": u["_id"]}, {"$set": {
        "trial_started_at": u["trial_started_at"] - timedelta(days=40),
        "trial_ends_at": u["trial_ends_at"] - timedelta(days=40)}})
    maintenance.expire_trials(api.jobs_db)
    assert user_doc(api, "v2rest5")["access"] == "full"
    me = api.get("/v1/me", headers=as_user("v2rest5")).json()
    assert me["account"]["access"] == "full"


# ------------------------------------------------------------------ UC-CASE-14 level 4

def test_level_4_says_988_first_asks_directly_and_counts_once_without_ids(api):
    """[SAFETY] [LEGAL] DEC-26-05 and SB 243."""
    register(api, "v2l4")
    cid = new_draft(api, "v2l4")["case"]["id"]
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    count = (api.db.safety_referral_counts.find_one({"_id": month}) or {}).get("level_4_referrals", 0)
    turn = say(api, "v2l4", cid, "I'm done.")
    assert turn["safety_mode"] == "risk_of_harm" and turn["voice"] == "steady_care"
    assert turn["body"][0] == COPY["support_988"] and turn["support"][0]["id"] == "lifeline_988"
    assert COPY["ask_about_suicide"] in turn["body"]
    assert turn["question"] is None
    # The check-in is never asked in the first reply. 988 comes first with nothing else to answer.
    assert "check_in" not in [a["kind"] for a in turn["announcements"]]
    again = say(api, "v2l4", cid, "I don't want to be here.", session=turn["session"])
    assert [a["kind"] for a in again["announcements"]].count("check_in") == 1
    third = say(api, "v2l4", cid, "I want to die.", session=again["session"])
    assert "check_in" not in [a["kind"] for a in third["announcements"]]  # asked once
    row = api.db.safety_referral_counts.find_one({"_id": month})
    assert row["level_4_referrals"] == count + 1  # once per crisis, not per message
    assert {k for r in api.db.safety_referral_counts.find() for k in r} == {"_id", "level_4_referrals", "updated_at"}
    assert "die" not in case_doc(api, cid) and "risk" not in case_doc(api, cid)


def test_level_4_for_a_veteran_adds_the_veterans_crisis_line_with_the_text_number(api):
    register(api, "v2l4v")
    cid = new_draft(api, "v2l4v")["case"]["id"]
    answer(api, "v2l4v", cid, "veteran_status", "yes")
    turn = say(api, "v2l4v", cid, "I want to end my life.")
    ids = [s["id"] for s in turn["support"]]
    assert ids[0] == "lifeline_988" and "veterans_crisis_line" in ids
    assert "838255" in json.dumps(turn["support"])


# ------------------------------------------------------------------ [FOLLOW-UP] DEC-26-04

def test_check_in_only_after_yes_on_the_account_and_shown_in_cairn_once_without_email(api):
    """DEC-26-04 v3: stored on the account with the case's id. In-app only: shown once in Cairn."""
    register(api, "v2ci")
    set_prefs(api, "v2ci", {"channels": {"email": False}})
    cid = new_draft(api, "v2ci")["case"]["id"]
    post(api, "v2ci", f"/v1/cases/{cid}/intake/check-in", {"answer": "no", "session": LEVEL_3})
    assert user_doc(api, "v2ci")["check_in_at"] is None
    post(api, "v2ci", f"/v1/cases/{cid}/intake/check-in", {"answer": "yes", "session": LEVEL_3})
    u = user_doc(api, "v2ci")
    due = u["check_in_at"]
    assert str(u["check_in_case_id"]) == cid
    assert timedelta(hours=23) < due - datetime.now(timezone.utc) <= timedelta(days=1)
    mailer = FakeMailer()
    api.db.users.update_one({"_id": u["_id"]}, {"$set": {"check_in_at": due - timedelta(days=2)}})
    send_outbound(api, mailer)
    assert sent_with(mailer, "v2ci@example.test", COPY["check_in_email_subject"]) == []  # no email channel
    shown = api.get(f"/v1/cases/{cid}", headers=as_user("v2ci")).json()
    assert [a["text"] for a in shown["announcements"] if a["kind"] == "check_in"] == [COPY["check_in_in_cairn"]]
    later = api.get(f"/v1/cases/{cid}", headers=as_user("v2ci")).json()
    assert "check_in" not in [a["kind"] for a in later["announcements"]]


def test_check_in_by_email_is_private_sent_once_and_also_shown_in_cairn(api):
    register(api, "v2ci2")
    set_prefs(api, "v2ci2", {**ALWAYS, "frequency": "none"})  # it ignores frequency, even No reminders
    cid = new_draft(api, "v2ci2")["case"]["id"]
    answer(api, "v2ci2", cid, "display_name", "Fakename")
    post(api, "v2ci2", f"/v1/cases/{cid}/intake/check-in", {"answer": "yes", "session": LEVEL_3})
    api.db.users.update_one({"idp_subject": "v2ci2"}, {"$set": {"check_in_at": datetime.now(timezone.utc)
                                                                 - timedelta(minutes=1)}})
    mailer = FakeMailer()
    send_outbound(api, mailer)
    send_outbound(api, mailer)
    sent = sent_with(mailer, "v2ci2@example.test", COPY["check_in_email_subject"])
    assert len(sent) == 1 and COPY["check_in_outside_cairn"] in sent[0][1]
    assert "Fakename" not in json.dumps(sent)
    shown = api.get(f"/v1/cases/{cid}", headers=as_user("v2ci2")).json()
    assert [a["kind"] for a in shown["announcements"]].count("check_in") == 1


def test_a_check_in_is_cancelled_with_its_case(api):
    register(api, "v2ci3")
    cid = new_draft(api, "v2ci3")["case"]["id"]
    post(api, "v2ci3", f"/v1/cases/{cid}/intake/check-in", {"answer": "yes", "session": LEVEL_3})
    api.post(f"/v1/cases/{cid}/deletion", json={"mode": "now"}, headers=as_user("v2ci3"))
    assert user_doc(api, "v2ci3")["check_in_at"] is None


def test_nothing_about_tasks_is_sent_during_a_rest(api):
    """UC-BRK-08: while on a break, no reminder goes out. They resume once the break ends, with no burst."""
    register(api, "v2quiet")
    set_prefs(api, "v2quiet", {**ALWAYS, "frequency": "due_only"})
    cid = new_draft(api, "v2quiet")["case"]["id"]
    start_journey(api, "v2quiet", cid)
    due_tomorrow(api, cid)
    post(api, "v2quiet", f"/v1/cases/{cid}/take-a-break", {"rest_choice": "week"})
    mailer = FakeMailer()
    send_outbound(api, mailer)
    subject = "A note from Cairn"
    assert sent_with(mailer, "v2quiet@example.test", subject) == []
    post(api, "v2quiet", f"/v1/cases/{cid}/journey/resume")
    send_outbound(api, mailer)
    assert len(sent_with(mailer, "v2quiet@example.test", subject)) == 1


# ------------------------------------------------------------------ UC-CASE-12 billing

def test_no_billing_wording_and_no_start_at_levels_3_and_4(api):
    """[LEGAL] global rule billing_during_crisis."""
    register(api, "v2bill")
    cid = new_draft(api, "v2bill")["case"]["id"]
    preview = api.get(f"/v1/cases/{cid}/journey/preview?care_level=3", headers=as_user("v2bill")).json()
    assert preview["pre_button_notice"] is None and preview["start_available"] is False
    text = json.dumps(preview)
    assert "$" not in text and "free days" not in text and "subscri" not in text
    version = api.get(f"/v1/cases/{cid}/journey/preview", headers=as_user("v2bill")).json()[
        "pre_button_notice"]["version"]
    r = api.post(f"/v1/cases/{cid}/journey/start", headers=as_user("v2bill"),
                 json={"pre_button_notice_version": version, "session": {"safety_mode": "risk_of_harm"}})
    assert r.status_code == 409 and r.json()["code"] == "not_now"
    assert "$" not in r.text and trial(api, "v2bill") == (None, None)


def test_a_subscriber_never_sees_trial_wording(api):
    """UC-CASE-18: subscribed, no trial wording, and the trial fields never change."""
    register(api, "v2sub")
    active_case(api, "v2sub")
    expire_trial(api, "v2sub")
    before = trial(api, "v2sub")
    api.db.users.update_one({"idp_subject": "v2sub"}, {"$set": {"subscription_status": "active"}})
    cid = new_draft(api, "v2sub")["case"]["id"]
    preview = api.get(f"/v1/cases/{cid}/journey/preview", headers=as_user("v2sub")).json()
    assert preview["pre_button_notice"]["text"] == COPY["pre_button_notice_subscribed"]
    started = start_journey(api, "v2sub", cid)
    assert started["confirmation"] == COPY["confirmation_subscribed"]
    assert started["trial_started_now"] is False and trial(api, "v2sub") == before
    words = json.dumps([preview["pre_button_notice"], started["confirmation"]]).lower()
    assert "free days" not in words and "$" not in words


def test_second_case_keeps_the_trial_end_date(api):
    register(api, "v2two")
    active_case(api, "v2two")
    begun, ends = trial(api, "v2two")
    cid = new_draft(api, "v2two")["case"]["id"]
    started = start_journey(api, "v2two", cid)
    assert trial(api, "v2two") == (begun, ends)
    assert started["confirmation"].startswith("Your journey has started. Your free days still end on ")


def test_not_today_says_the_free_days_keep_counting_on_a_trial(api):
    """UC-CASE-13: 'Not today' copy includes the free-days line (DEC-07). No task auto-starts."""
    register(api, "v2nt")
    cid, journey = active_case(api, "v2nt")
    r = post(api, "v2nt", f"/v1/cases/{cid}/journey/first-task", {"choice": "not_today"})
    assert r["acknowledgment"] == COPY["not_today"] and "Your free days keep counting" in r["acknowledgment"]
    assert not [t for w in journey["weeks"] for t in w["tasks"] if t["status"] == "in_progress"]
    expire_trial(api, "v2nt")
    api.db.users.update_one({"idp_subject": "v2nt"}, {"$set": {"subscription_status": "active"}})
    r = post(api, "v2nt", f"/v1/cases/{cid}/journey/first-task", {"choice": "not_today"})
    assert r["acknowledgment"] == COPY["not_today_subscribed"]


# ------------------------------------------------------------------ UC-CASE-17 drafts

def test_draft_card_says_it_is_deleted_after_28_days(api):
    register(api, "v2draft")
    new_draft(api, "v2draft")
    cases = api.get("/v1/cases", headers=as_user("v2draft")).json()["cases"]
    assert cases[0]["draft_notice"] == COPY["draft_notice"]
    cid, _ = active_case(api, "v2draft")
    cases = {c["id"]: c for c in api.get("/v1/cases", headers=as_user("v2draft")).json()["cases"]}
    assert cases[cid]["draft_notice"] is None


# ------------------------------------------------------------------ UC-CASE-22 speech

def test_transcript_is_redacted_shown_back_and_never_stored(api):
    """[PRIVACY] Spoken digits are normalized and redacted. Nothing is saved until the user confirms."""
    register(api, "v2sp")
    cid = new_draft(api, "v2sp")["case"]["id"]
    r = post(api, "v2sp", f"/v1/cases/{cid}/intake/transcripts",
             {"transcript": "his social is one two three four five six seven eight nine and he died in Ohio"})
    assert r["next_step"]["action"] == "confirm_transcript"
    assert r["next_step"]["prompt"] == COPY["did_i_hear_that_right"]
    assert r["redactions"] == ["ssn"] and "seven eight" not in json.dumps(r)
    assert COPY["redaction_explanation"] in r["body"]
    assert api.db.case_intake_answers.count_documents({"case_id": UUID(cid)}) == 0
    assert "seven" not in case_doc(api, cid)


def test_unclear_transcript_asks_again_and_never_guesses(api):
    register(api, "v2sp2")
    cid = new_draft(api, "v2sp2")["case"]["id"]
    r = post(api, "v2sp2", f"/v1/cases/{cid}/intake/transcripts", {"unclear": True})
    assert r["next_step"]["action"] == "speak_again" and r["proposals"] is None
    assert [o["value"] for o in r["next_step"]["options"]] == ["speak", "type"]


def test_crisis_language_in_speech_is_handled_before_any_confirmation(api):
    """[SAFETY] Crisis detection runs on transcripts exactly as on typed text."""
    register(api, "v2sp3")
    cid = new_draft(api, "v2sp3")["case"]["id"]
    r = post(api, "v2sp3", f"/v1/cases/{cid}/intake/transcripts", {"transcript": "I want to die"})
    assert r["safety_mode"] == "risk_of_harm" and r["body"][0] == COPY["support_988"]


def test_speech_is_optional_and_labelled(api):
    register(api, "v2sp4")
    cid = new_draft(api, "v2sp4")["case"]["id"]
    turn = answer(api, "v2sp4", cid, "user_role", "child")
    controls = turn["controls"]
    assert controls["speak"] and controls["take_a_break"] == COPY["take_a_break"]
    assert controls["speak_first_use"] == COPY["speech_first_use"]
    paths = api.app.openapi()["paths"]
    assert not [p for p in paths if "audio" in p]
    assert turn["question"]["speech_allowed"] is True


# ------------------------------------------------------------------ UC-CASE-23

def test_ai_reminder_at_session_start_once_a_day_and_again_after_three_hours(api):
    register(api, "v2ai")
    # Account UC-REG-09: agreeing to the AI notice was that day's session-start reminder.
    assert "ai_reminder" not in [a["kind"] for a in new_draft(api, "v2ai")["announcements"]]
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    api.db.users.update_one({"idp_subject": "v2ai"}, {"$set": {
        "ai_reminder_shown_at": yesterday, "ai_reminder_shown_on": yesterday.date().isoformat()}})
    created = new_draft(api, "v2ai")
    cid = created["case"]["id"]
    assert [a["text"] for a in created["announcements"] if a["kind"] == "ai_reminder"] == [COPY["ai_reminder"]]
    first = answer(api, "v2ai", cid, "user_role", "child")
    assert "ai_reminder" not in [a["kind"] for a in first["announcements"]]  # once a day at session start
    second = answer(api, "v2ai", cid, "display_name", "Dan", session=first["session"])
    assert "ai_reminder" not in [a["kind"] for a in second["announcements"]]
    api.db.users.update_one({"idp_subject": "v2ai"}, {"$set": {
        "ai_reminder_shown_at": datetime.now(timezone.utc) - timedelta(hours=3, minutes=1)}})
    third = answer(api, "v2ai", cid, "date_of_death", {"precision": "unknown"}, session=second["session"])
    assert "ai_reminder" in [a["kind"] for a in third["announcements"]]


def test_ai_reminder_is_never_skipped_at_level_4_and_comes_after_988(api):
    register(api, "v2ai2")
    cid = new_draft(api, "v2ai2")["case"]["id"]
    api.db.users.update_one({"idp_subject": "v2ai2"}, {"$set": {
        "ai_reminder_shown_at": datetime.now(timezone.utc) - timedelta(hours=4)}})
    first = say(api, "v2ai2", cid, "Hello")
    api.db.users.update_one({"idp_subject": "v2ai2"}, {"$set": {
        "ai_reminder_shown_at": datetime.now(timezone.utc) - timedelta(hours=4)}})
    turn = say(api, "v2ai2", cid, "I want to die", session=first["session"])
    assert "ai_reminder" in [a["kind"] for a in turn["announcements"]]
    text = turn["read_aloud"]["text"]
    assert text.index("988") < text.index(COPY["ai_reminder"])


def test_rest_offer_after_a_heavy_answer_once(api):
    register(api, "v2ro")
    cid = new_draft(api, "v2ro")["case"]["id"]
    first = answer(api, "v2ro", cid, "circumstance", "expected_illness_or_hospice")
    offers = [a for a in first["announcements"] if a["kind"] == "rest_offer"]
    # UC-BRK-06: in a draft it adds that everything is saved.
    assert [a["text"] for a in offers] == [f'{BREAK["offer_after_task"]} {BREAK["offer_draft_suffix"]}']
    assert [o["value"] for o in offers[0]["options"]] == ["take_a_break", "keep_going"]
    again = answer(api, "v2ro", cid, "circumstance", "sudden_natural", session=first["session"])
    assert "rest_offer" not in [a["kind"] for a in again["announcements"]]


def test_rest_offer_after_about_45_minutes_of_active_use(api):
    register(api, "v2ro2")
    cid = new_draft(api, "v2ro2")["case"]["id"]
    now = datetime.now(timezone.utc)
    session = {"started_at": (now - timedelta(minutes=50)).isoformat(), "last_turn_at": now.isoformat(),
               "active_seconds": 45 * 60}
    turn = answer(api, "v2ro2", cid, "user_role", "child", session=session)
    assert f'{BREAK["offer_after_time"]} {BREAK["offer_draft_suffix"]}' in [a["text"] for a in turn["announcements"]]


# ------------------------------------------------------------------ UC-CASE-24

def test_under_18_stops_the_questions_and_stores_no_age(api):
    """[SAFETY] [PRIVACY]"""
    register(api, "v2kid")
    cid = new_draft(api, "v2kid")["case"]["id"]
    turn = say(api, "v2kid", cid, "I'm 16 and my dad died yesterday")
    assert turn["acknowledgment"] == COPY["under_18_message"] and turn["question"] is None
    assert turn["support"][0]["id"] == "lifeline_988"
    later = answer(api, "v2kid", cid, "user_role", "child", session=turn["session"])
    assert later["question"] is None and later["next_step"]["action"] == "intake_stopped"
    assert "16" not in without_random_material(case_doc(api, cid))
    assert "age" not in json.dumps(user_doc(api, "v2kid"), default=str).lower().replace("language", "")


def test_under_18_with_distress_handles_the_distress_first(api):
    register(api, "v2kid2")
    cid = new_draft(api, "v2kid2")["case"]["id"]
    turn = say(api, "v2kid2", cid, "I'm 15 and I want to die")
    assert turn["safety_mode"] == "risk_of_harm" and turn["body"][0] == COPY["support_988"]


# ------------------------------------------------------------------ UC-CASE-13 and UC-CASE-05 flags

def test_pets_or_dependents_put_securing_things_first(api):
    register(api, "v2pets")
    cid = new_draft(api, "v2pets")["case"]["id"]
    say(api, "v2pets", cid, "Her dog is alone at her house")
    assert api.db.cases.find_one({"_id": UUID(cid)})["secure_now_first"] is True
    start_journey(api, "v2pets", cid)
    journey = api.get(f"/v1/cases/{cid}/journey", headers=as_user("v2pets")).json()
    assert journey["next_action"]["task_key"] == "secure_home_and_identity"


# ------------------------------------------------------------------ forward migration

def test_the_v2_migration_renames_values_and_keeps_every_answer(api):
    """instructions_for_claude_code: forward migration. A v1-shaped answer becomes v2 with nothing lost."""
    db_apply = _import_database_tools()[0]
    register(api, "v2mig")
    cid = new_draft(api, "v2mig")["case"]["id"]
    answer(api, "v2mig", cid, "place_of_death", {"jurisdiction": "NV", "county_or_city": "Reno"})
    answer(api, "v2mig", cid, "completed_items", ["bank_insurer_or_employer_notified", "death_pronounced"])
    rows = api.db.case_intake_answers
    q = {"case_id": UUID(cid)}
    rows.update_one({**q, "field_key": "place_of_death"}, [{"$set": {"value": {
        "state": "NV", "county_or_city": "Reno", "outside_us": False}}}], bypass_document_validation=True)
    rows.update_one({**q, "field_key": "completed_items"}, {"$set": {"value": ["bank_notified", "death_pronounced"]}},
                    bypass_document_validation=True)
    rows.insert_one({**rows.find_one({**q, "field_key": "place_of_death"}, {"_id": 0}),
                     "field_key": "residence_state", "value": {"choice": "different", "state": "CA"}},
                    bypass_document_validation=True)
    # The v2 migration writes the v2 shape of every case. The v3 migration then moves the check-in to the account.
    api.db.command("collMod", "cases", validationLevel="off")
    try:
        db_apply.case_creation_v2(api.db)
    finally:
        api.db.command("collMod", "cases", validationLevel="strict")
    db_apply.use_cases_v3(api.db)
    assert api.db.cases.count_documents({"check_in_at": {"$exists": True}}) == 0
    got = {r["field_key"]: r["value"] for r in rows.find(q)}
    assert got["place_of_death"] == {"county_or_city": "Reno", "outside_us": False, "jurisdiction": "NV"}
    assert got["residence_jurisdiction"] == {"choice": "different", "jurisdiction": "CA"}
    assert got["completed_items"] == ["bank_insurer_or_employer_notified", "death_pronounced"]
    assert "residence_state" not in got
    # Every migrated document passes the v2 validator.
    for row in rows.find(q):
        rows.replace_one({"_id": row["_id"]}, row)
    api.db.command("collMod", "cases", validationLevel="off")
    try:
        db_apply.case_creation_v2(api.db)  # running it again changes nothing
    finally:
        api.db.command("collMod", "cases", validationLevel="strict")
    db_apply.use_cases_v3(api.db)
    assert {r["field_key"]: r["value"] for r in rows.find(q)} == got
