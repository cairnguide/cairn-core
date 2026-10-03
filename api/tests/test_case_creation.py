"""UC-CASE-01 to UC-CASE-18 acceptance criteria against a real MongoDB database, as the cairnApp user.

Spec: database/docs/cairn-case-creation-use-cases.json. Each test names the use
case it covers. [SAFETY] and [PRIVACY] criteria are release blockers.
Rules that need no database are in test_case_creation_rules.py.

Skipped unless CAIRN_TEST_MONGODB_URI points at a scratch MongoDB replica set.
Fake data only.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from pymongo.errors import WriteError

from cairn_api import maintenance
from cairn_api.copy_store import load_case_copy
from cairn_api.errors import RuleViolation
from cairn_api.store import Session

from .conftest import (
    active_case,
    answer,
    as_user,
    expire_trial,
    find_task,
    new_draft,
    register,
    start_journey,
    task_keys,
    user_id,
)

COPY = load_case_copy()
FIELDS = ("user_role", "display_name", "date_of_death", "place_of_death", "residence_state", "circumstance",
          "veteran_status", "estate_plan_status", "completed_items")


# Ids, timestamps, and the driver's DEBUG bookkeeping are random hex and digits, so they can contain a
# short fragment such as "4111" or "078" by chance. Leak checks remove them first. No sensitive number has
# any of these shapes, and pymongo's logged commands keep every stored value, so nothing real is hidden.
RANDOM_MATERIAL = re.compile("|".join([
    r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",                                     # UUIDs
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}(?::?\d{2})?)?",     # ISO timestamps
    r"\b[0-9a-f]{24}\b",                                                                # ObjectIds
    r'\\?"base64\\?": \\?"[A-Za-z0-9+/=]*',                                             # binary ids
    r'\\?"(?:requestId|operationId|driverConnectionId|serverConnectionId|serverPort|durationMS|txnNumber|t|i)'
    r'\\?": [0-9.e+-]+',                                                                # driver counters, clocks
    r"\brtt: [0-9.e+-]+",                                                               # round-trip time
]), re.I)


def without_random_material(text: str) -> str:
    return RANDOM_MATERIAL.sub("<random>", text)


def trial(api, subject):
    u = api.db.users.find_one({"idp_subject": subject})
    return (u["trial_started_at"], u["trial_ends_at"])


def answer_rows(api, case_id):
    return {r["field_key"]: (r["answer_state"], r["value"], r["own_words"])
            for r in api.db.case_intake_answers.find({"case_id": UUID(case_id)})}


def case_doc(api, case_id) -> str:
    """The whole stored case, as text, for checking that nothing sensitive was kept."""
    return json.dumps(api.db.cases.find_one({"_id": UUID(case_id)}), default=str)


def say(api, subject, case_id, text, session=None):
    r = api.post(f"/v1/cases/{case_id}/intake/messages", headers=as_user(subject),
                 json={"text": text, **({"session": session} if session else {})})
    assert r.status_code == 200, r.text
    return r.json()


def confirm(api, subject, case_id, turn):
    body = {"answers": [{"field": p["field"], "value": p["value"], "own_words": p["own_words"]}
                        for p in turn["proposals"]], "session": turn["session"]}
    r = api.post(f"/v1/cases/{case_id}/intake/confirmations", json=body, headers=as_user(subject))
    assert r.status_code == 200, r.text
    return r.json()


def questions_in(turn: dict) -> int:
    """How many questions a turn asks: acknowledgment and statements must ask none."""
    statements = " ".join([turn.get("acknowledgment") or "", *turn.get("body", [])])
    return statements.count("?") + (turn["next_step"]["prompt"].count("?"))


def everything_the_user_saw(api, subject, case_id) -> str:
    texts = []
    for path in ("", "/review", "/journey/preview", "/journey"):
        r = api.get(f"/v1/cases/{case_id}{path}", headers=as_user(subject))
        if r.status_code == 200:
            texts.append(r.text)
    return "\n".join(texts)


# ------------------------------------------------------------------ UC-CASE-01

def test_uc01_new_case_is_a_draft_and_does_not_start_the_trial(api):
    register(api, "cc01")
    turn = new_draft(api, "cc01")
    assert turn["case"]["status"] == "draft"
    assert turn["case"]["draft_expires_at"] is not None
    assert trial(api, "cc01") == (None, None)
    # The first message acknowledges the loss, explains what happens, and asks at most one question.
    assert turn["acknowledgment"]
    assert turn["body"] == [COPY["intro"]]
    assert questions_in(turn) <= 1
    assert [o["value"] for o in turn["next_step"]["options"]] == ["one_question_at_a_time", "own_words"]
    assert turn["read_aloud"]["label"] == "Read this to me" and turn["acknowledgment"] in turn["read_aloud"]["text"]


def test_uc01_own_words_read_back_and_only_data_fields_are_saved(api):
    """[PRIVACY] Free-text intake persists only recognized data_fields keys."""
    register(api, "cc01b")
    cid = new_draft(api, "cc01b")["case"]["id"]
    text = ("My mom died yesterday in Manchester, NH. She had been in hospice. She was a Navy veteran and had a "
            "will, it's in the safe. Her favorite hymn was Amazing Grace and her doctor was Dr. Fakewell.")
    turn = say(api, "cc01b", cid, text)
    assert answer_rows(api, cid) == {}  # nothing is saved before the user confirms
    assert turn["next_step"]["prompt"] == "Did I get that right?"
    assert {p["field"] for p in turn["proposals"]} <= set(FIELDS)
    assert "Amazing Grace" not in turn["read_aloud"]["text"]

    saved = confirm(api, "cc01b", cid, turn)
    rows = answer_rows(api, cid)
    assert set(rows) <= set(FIELDS)
    stored = json.dumps({k: [v[1], v[2]] for k, v in rows.items()})
    for fragment in ("Amazing Grace", "Fakewell", "hymn", "been in hospice"):
        assert fragment not in stored
    assert rows["circumstance"] == ("answered", "expected_illness_or_hospice", None)
    # Cairn then asks only about fields still missing.
    assert saved["question"]["field"] not in rows


def test_uc01_confirmations_refuse_anything_outside_data_fields(api):
    register(api, "cc01c")
    cid = new_draft(api, "cc01c")["case"]["id"]
    r = api.post(f"/v1/cases/{cid}/intake/confirmations", headers=as_user("cc01c"),
                 json={"answers": [{"field": "circumstance", "value": "she had cancer"}]})
    assert r.status_code == 422
    assert answer_rows(api, cid) == {}


# ------------------------------------------------------------------ UC-CASE-02

def test_uc02_skipping_role_stores_skipped_and_defaults_to_family_member(api):
    register(api, "cc02")
    cid = new_draft(api, "cc02")["case"]["id"]
    turn = answer(api, "cc02", cid, "user_role", state="skipped")
    assert answer_rows(api, cid)["user_role"] == ("skipped", None, None)
    assert turn["acknowledgment"] == COPY["answer_skipped"]
    assert turn["question"]["field"] != "user_role"


def test_uc02_power_of_attorney_note_renders_once_per_case_with_citation(api):
    register(api, "cc02b")
    cid = new_draft(api, "cc02b")["case"]["id"]
    first = answer(api, "cc02b", cid, "user_role", "power_of_attorney")
    poa = [n for n in first["notes"] if n["text"] == COPY["poa_authority_note"]]
    assert len(poa) == 1
    assert poa[0]["source_urls"] == ["https://www.alperlaw.com/?p=9650"]
    assert poa[0]["attorney_line"] and poa[0]["legal_review_required"]
    answer(api, "cc02b", cid, "user_role", "child")
    again = answer(api, "cc02b", cid, "user_role", "power_of_attorney")
    assert not any(n["text"] == COPY["poa_authority_note"] for n in again["notes"])


def test_uc02_role_from_onboarding_handoff_is_not_asked_again(api):
    register(api, "cc02c")
    handoff = api.post("/v1/onboarding/case-handoff", json={"relationship": "power_of_attorney"},
                       headers=as_user("cc02c")).json()
    assert handoff["user_role"] == "power_of_attorney"
    assert "legal first name" not in handoff["next_step"]["prompt"]
    turn = new_draft(api, "cc02c", user_role=handoff["user_role"])
    assert any(n["text"] == COPY["poa_authority_note"] for n in turn["notes"])
    cid = turn["case"]["id"]
    nxt = api.post(f"/v1/cases/{cid}/intake/continue", headers=as_user("cc02c"), json={}).json()
    assert nxt["question"]["field"] == "display_name"


def test_uc02_professional_fiduciary_is_offered_a_shorter_pace(api):
    register(api, "cc02d", voice="warm_patient")
    cid = new_draft(api, "cc02d")["case"]["id"]
    turn = answer(api, "cc02d", cid, "user_role", "professional_fiduciary")
    assert turn["next_step"]["action"] == "choose_pace"
    r = api.put(f"/v1/cases/{cid}/intake/preferences", json={"skip_explainers": True}, headers=as_user("cc02d"))
    assert r.json()["case"]["skip_explainers"] is True
    for f in ("display_name", "date_of_death", "place_of_death"):
        answer(api, "cc02d", cid, f, state="skipped", session={"consecutive_skips": 0})
    q = api.post(f"/v1/cases/{cid}/intake/continue", headers=as_user("cc02d"), json={}).json()["question"]
    assert q["field"] == "circumstance"
    assert q["prompt"] == COPY["question_circumstance"] and q["pre_question"] is None  # regardless of voice


# ------------------------------------------------------------------ UC-CASE-03

def test_uc03_display_name_is_used_but_never_as_a_legal_name(api):
    register(api, "cc03")
    cid = new_draft(api, "cc03")["case"]["id"]
    turn = answer(api, "cc03", cid, "display_name", "Grandpa Joe")
    assert "Grandpa Joe" in turn["acknowledgment"]
    assert turn["case"]["display_name"] == "Grandpa Joe"
    answer(api, "cc03", cid, "place_of_death", {"state": "NH"})
    start_journey(api, "cc03", cid)
    status = api.get(f"/v1/cases/{cid}/status", headers=as_user("cc03")).json()
    assert status["deceased_name"] == "Grandpa Joe"
    assert api.db.deceased.count_documents({"case_id": UUID(cid)}) == 0


def test_uc03_skipped_name_uses_your_loved_one_or_the_person_who_died(api):
    register(api, "cc03b", voice="plain_practical")
    cid = new_draft(api, "cc03b")["case"]["id"]
    turn = answer(api, "cc03b", cid, "display_name", state="skipped")
    assert turn["case"]["display_name"] == "your loved one"
    assert turn["next_step"]["action"] == "choose_name_fallback"
    r = api.put(f"/v1/cases/{cid}/intake/preferences", json={"name_fallback": "the_person_who_died"},
                headers=as_user("cc03b"))
    assert r.json()["case"]["display_name"] == "the person who died"


def test_legal_identity_is_refused_on_a_draft(api):
    """never_collect_at_case_creation: legal names and date of birth come later, inside a task."""
    register(api, "cc03c")
    cid = new_draft(api, "cc03c")["case"]["id"]
    r = api.patch(f"/v1/cases/{cid}/deceased", json={"legal_first_name": "Dan"}, headers=as_user("cc03c"))
    assert r.status_code == 409
    answer(api, "cc03c", cid, "place_of_death", {"state": "NH"})
    start_journey(api, "cc03c", cid)
    r = api.patch(f"/v1/cases/{cid}/deceased", json={"legal_first_name": "Dan", "date_of_birth": "1950-01-01"},
                  headers=as_user("cc03c"))
    assert r.status_code == 200 and r.json()["deceased"]["legal_first_name"] == "Dan"


# ------------------------------------------------------------------ UC-CASE-04

@pytest.mark.parametrize("precision", ["today", "this_week", "unknown"])
def test_uc04_approximate_dates_are_accepted(api, precision):
    subject = f"cc04-{precision}"
    register(api, subject)
    cid = new_draft(api, subject)["case"]["id"]
    answer(api, subject, cid, "date_of_death", {"precision": precision})
    state, value, _ = answer_rows(api, cid)["date_of_death"]
    assert state == "answered" and value["precision"] == precision
    assert (value["date"] is not None) == (precision == "today")


def test_uc04_certificate_office_uses_place_of_death_not_residence(api):
    register(api, "cc04b")
    cid = new_draft(api, "cc04b")["case"]["id"]
    turn = answer(api, "cc04b", cid, "place_of_death", {"state": "NV", "county_or_city": "Reno"}, away_from_home=True)
    assert turn["question"]["field"] == "residence_state"  # asked because the death was away from home
    answer(api, "cc04b", cid, "residence_state", {"choice": "different", "state": "CA"}, session=turn["session"])
    start_journey(api, "cc04b", cid)
    journey = api.get(f"/v1/cases/{cid}/journey", headers=as_user("cc04b")).json()
    task = find_task(journey, "order_death_certificates")
    detail = api.get(f"/v1/cases/{cid}/tasks/{task['id']}", headers=as_user("cc04b")).json()
    assert detail["task"]["death_state"] == "NV"


def test_uc04_residence_is_not_asked_unless_away_from_home(api):
    register(api, "cc04c")
    cid = new_draft(api, "cc04c")["case"]["id"]
    turn = answer(api, "cc04c", cid, "place_of_death", {"state": "NH"})
    for f in ("user_role", "display_name", "date_of_death"):
        turn = answer(api, "cc04c", cid, f, state="skipped", session={})
        assert turn["question"]["field"] != "residence_state"
    assert turn["question"]["field"] == "circumstance"


def test_uc04_place_unknown_adds_the_certificate_note(api):
    register(api, "cc04d")
    cid = new_draft(api, "cc04d")["case"]["id"]
    answer(api, "cc04d", cid, "place_of_death", state="unsure")
    preview = api.get(f"/v1/cases/{cid}/journey/preview", headers=as_user("cc04d")).json()
    certs = next(t for w in preview["weeks"] for t in w["tasks"] if t["task_key"] == "order_death_certificates")
    assert COPY["place_unknown"] in [n["text"] for n in certs["notes"]]


def test_uc04_outside_us_keeps_the_case_and_suggests_help(api):
    register(api, "cc04e")
    cid = new_draft(api, "cc04e")["case"]["id"]
    turn = answer(api, "cc04e", cid, "place_of_death", {"outside_us": True})
    assert COPY["outside_us_ack"] in turn["body"]
    assert turn["case"]["status"] == "draft"
    preview = api.get(f"/v1/cases/{cid}/journey/preview", headers=as_user("cc04e")).json()
    keys = {t["task_key"] for w in preview["weeks"] for t in w["tasks"]}
    assert "talk_to_estate_attorney" in keys


# ------------------------------------------------------------------ UC-CASE-05

def test_uc05_only_the_enum_is_stored_and_free_text_is_never_logged(api, caplog):
    """[PRIVACY] Only the enum value is persisted. Free text for this question is never stored or logged."""
    register(api, "cc05")
    cid = new_draft(api, "cc05")["case"]["id"]
    caplog.set_level(logging.DEBUG)
    turn = say(api, "cc05", cid, "It was so sudden, a heart attack at the Fakeville gym.")
    assert "heart attack" not in json.dumps(turn["proposals"])
    confirm(api, "cc05", cid, turn)
    assert answer_rows(api, cid)["circumstance"] == ("answered", "sudden_natural", None)
    assert "heart attack" not in caplog.text and "Fakeville" not in caplog.text
    r = api.put(f"/v1/cases/{cid}/intake/answers/circumstance", headers=as_user("cc05"),
                json={"state": "answered", "value": "sudden_natural", "own_words": "a heart attack"})
    assert r.status_code == 422
    with pytest.raises(WriteError) as refused:  # the validator refuses it, even for an administrator
        api.db.case_intake_answers.update_one({"case_id": UUID(cid), "field_key": "circumstance"},
                                              {"$set": {"value": "heart attack"}})
    assert refused.value.code == 121


def test_uc05_confirm_without_repeating_and_no_follow_up_after_prefer_not_to_say(api):
    register(api, "cc05b")
    cid = new_draft(api, "cc05b")["case"]["id"]
    turn = answer(api, "cc05b", cid, "circumstance", "prefer_not_to_say")
    assert turn["acknowledgment"] == COPY["circumstance_confirmed"]
    assert COPY["label_circumstance_prefer_not_to_say"] not in turn["read_aloud"]["text"]
    assert turn["question"]["field"] != "circumstance"
    preview = api.get(f"/v1/cases/{cid}/journey/preview", headers=as_user("cc05b")).json()
    assert preview["journey_template_key"] == "general"


def test_uc05_volunteered_suicide_loss_is_acknowledged_and_resources_offered_once(api):
    register(api, "cc05c")
    cid = new_draft(api, "cc05c")["case"]["id"]
    turn = say(api, "cc05c", cid, "My brother died by suicide on Monday.")
    assert turn["safety_mode"] == "normal"  # a loss, not a risk to the user
    assert turn["acknowledgment"] == COPY["volunteered_cause_ack"]
    assert [s["url"] for s in turn["support"]] == ["https://afsp.org/ive-lost-someone/"]
    assert turn["session"]["sensitivity"] == "raised"
    assert "suicide" not in json.dumps(turn["proposals"] or [])
    again = say(api, "cc05c", cid, "His suicide was a shock.", session=turn["session"])
    assert not any(s["id"] == "loss_survivor_resources" for s in again["support"])
    rows = case_doc(api, cid)
    assert "suicide" not in rows and "loss_survivor" not in rows


# ------------------------------------------------------------------ UC-CASE-06

def test_uc06_va_step_with_va_gov_citation_and_crisis_line(api):
    register(api, "cc06")
    cid = new_draft(api, "cc06")["case"]["id"]
    turn = answer(api, "cc06", cid, "veteran_status", "yes")
    va = [n for n in turn["notes"] if "VA" in n["text"]]
    assert va and "https://www.va.gov/burials-memorials/" in va[0]["source_urls"]
    assert COPY["veterans_crisis_line_added"] in turn["body"]
    preview = api.get(f"/v1/cases/{cid}/journey/preview", headers=as_user("cc06")).json()
    task = next(t for w in preview["weeks"] for t in w["tasks"] if t["task_key"] == "notify_va_if_veteran")
    assert "https://www.va.gov/burials-memorials/" in [c["url"] for c in task["citations"]]
    assert "veterans_crisis_line" in [s["id"] for s in preview["support"]]


def test_uc06_unknown_adds_the_va_step_but_not_the_crisis_line(api):
    register(api, "cc06b")
    cid = new_draft(api, "cc06b")["case"]["id"]
    answer(api, "cc06b", cid, "veteran_status", state="unsure")
    preview = api.get(f"/v1/cases/{cid}/journey/preview", headers=as_user("cc06b")).json()
    assert "notify_va_if_veteran" in {t["task_key"] for w in preview["weeks"] for t in w["tasks"]}
    assert "veterans_crisis_line" not in [s["id"] for s in preview["support"]]


def test_uc06_uc07_no_document_request_during_case_creation(api):
    register(api, "cc06c")
    cid = new_draft(api, "cc06c")["case"]["id"]
    seen = [json.dumps(answer(api, "cc06c", cid, "veteran_status", "yes")),
            json.dumps(answer(api, "cc06c", cid, "estate_plan_status", "yes_location_unknown"))]
    seen.append(everything_the_user_saw(api, "cc06c", cid))
    text = " ".join(seen).lower()
    for word in ("discharge", "dd-214", "dd214", "upload", "attach", "service records", "send us a copy"):
        assert word not in text, word


# ------------------------------------------------------------------ UC-CASE-07

def test_uc07_no_will_reassures_and_adds_the_explainer_with_attorney_flag(api):
    register(api, "cc07")
    cid = new_draft(api, "cc07")["case"]["id"]
    turn = answer(api, "cc07", cid, "estate_plan_status", "no")
    assert COPY["no_will_reassure"] in turn["body"]
    added = [n for n in turn["notes"] if "no will" in n["text"].lower()]
    assert added and added[0]["attorney_line"]
    assert "inherit" not in " ".join(turn["body"]).lower()


def test_uc07_family_disagreement_adds_the_attorney_task_without_taking_sides(api):
    register(api, "cc07b")
    cid = new_draft(api, "cc07b")["case"]["id"]
    turn = say(api, "cc07b", cid, "My sisters and I can't agree about the funeral.")
    assert COPY["family_disagreement_ack"] in turn["body"]
    preview = api.get(f"/v1/cases/{cid}/journey/preview", headers=as_user("cc07b")).json()
    task = next(t for w in preview["weeks"] for t in w["tasks"] if t["task_key"] == "talk_to_estate_attorney")
    assert task["attorney_line"]


# ------------------------------------------------------------------ UC-CASE-08

def test_uc08_funeral_home_chosen_changes_the_ssa_task(api):
    register(api, "cc08")
    cid = new_draft(api, "cc08")["case"]["id"]
    answer(api, "cc08", cid, "completed_items", ["funeral_provider_chosen"])
    preview = api.get(f"/v1/cases/{cid}/journey/preview", headers=as_user("cc08")).json()
    tasks = {t["task_key"]: t for w in preview["weeks"] for t in w["tasks"]}
    assert tasks["confirm_funeral_home_reported_death"]["title"] == "Confirm the funeral home reported the death"
    assert "notify_social_security" not in tasks
    assert tasks["choose_funeral_provider"]["status"] == "done"
    assert tasks["choose_funeral_provider"]["status_label"] == "Done"
    assert tasks["notify_banks"]["status_label"] == "When you're ready"


def test_uc08_none_or_unsure_marks_relevant_tasks_check_on_this(api):
    register(api, "cc08b")
    cid, journey = active_case(api, "cc08b", {"place_of_death": {"state": "NH"},
                                              "completed_items": ["none_or_unsure"]})
    assert find_task(journey, "order_death_certificates")["status"] == "check_on_this"
    assert find_task(journey, "notify_life_insurers")["status"] == "not_started"


# ------------------------------------------------------------------ UC-CASE-09

def test_uc09_answer_states_and_skipped_questions_are_not_asked_again(api):
    register(api, "cc09")
    cid = new_draft(api, "cc09")["case"]["id"]
    session, done = None, []
    for field, state in (("user_role", "skipped"), ("display_name", "unsure"), ("date_of_death", "answered")):
        turn = answer(api, "cc09", cid, field, {"precision": "this_week"} if state == "answered" else None,
                      state=state, **({"session": session} if session else {}))
        session, done = turn["session"], [*done, field]
        assert turn["question"]["field"] not in done  # never re-asked, whatever the answer state
    resumed = api.post(f"/v1/cases/{cid}/intake/continue", json={"session": session}, headers=as_user("cc09"))
    assert resumed.json()["question"]["field"] == "place_of_death"
    assert {k: v[0] for k, v in answer_rows(api, cid).items()} == {
        "user_role": "skipped", "display_name": "unsure", "date_of_death": "answered"}


def test_uc09_changing_an_answer_says_what_changed_in_one_line(api):
    register(api, "cc09b")
    cid = new_draft(api, "cc09b")["case"]["id"]
    answer(api, "cc09b", cid, "veteran_status", "no")
    turn = answer(api, "cc09b", cid, "veteran_status", "yes")
    line = turn["body"][-1]
    assert "\n" not in line and "Check for VA burial benefits" in line
    same = answer(api, "cc09b", cid, "veteran_status", "yes")
    assert same["body"][-1] == COPY["changed_nothing"]


def test_uc09_change_on_active_case_keeps_progress(api):
    register(api, "cc09c")
    cid, journey = active_case(api, "cc09c", {"place_of_death": {"state": "NH"},
                                              "estate_plan_status": "yes_location_unknown"})
    h = as_user("cc09c")
    will = find_task(journey, "find_the_will")
    banks = find_task(journey, "notify_banks")
    api.patch(f"/v1/cases/{cid}/tasks/{will['id']}", headers=h, json={"status": "in_progress"})
    api.patch(f"/v1/cases/{cid}/tasks/{banks['id']}", headers=h, json={"status": "done"})
    turn = answer(api, "cc09c", cid, "estate_plan_status", "no")
    assert "Learn what happens when there's no will" in turn["body"][-1]
    after = api.get(f"/v1/cases/{cid}/journey", headers=h).json()
    keys = task_keys(after)
    assert "no_will_explainer" in keys
    assert "confirm_named_executor" not in keys             # untouched add-on set aside
    assert find_task(after, "find_the_will")["status"] == "in_progress"  # progress kept
    assert find_task(after, "notify_banks")["status"] == "done"          # unaffected task kept
    assert api.db.case_tasks.count_documents({"case_id": UUID(cid)}) >= len(journey["weeks"][0]["tasks"])


# ------------------------------------------------------------------ UC-CASE-10

def test_uc10_pause_saves_says_28_days_and_never_starts_trial_or_reminders(api):
    register(api, "cc10")
    cid = new_draft(api, "cc10")["case"]["id"]
    answer(api, "cc10", cid, "user_role", "child")
    r = api.post(f"/v1/cases/{cid}/intake/pause", headers=as_user("cc10"), json={})
    turn = r.json()
    assert turn["next_step"]["prompt"] == COPY["pause"] and "28 days" in COPY["pause"]
    assert turn["case"]["last_intake_step"] == "display_name"
    assert trial(api, "cc10") == (None, None)
    assert api.db.trial_reminders.count_documents({"user_id": user_id(api, "cc10")}) == 0
    say(api, "cc10", cid, "I need a break")
    assert trial(api, "cc10") == (None, None)


def test_uc10_return_greets_and_says_where_they_left_off(api):
    register(api, "cc10b")
    cid = new_draft(api, "cc10b")["case"]["id"]
    answer(api, "cc10b", cid, "user_role", "child")
    back = api.get(f"/v1/cases/{cid}", headers=as_user("cc10b")).json()
    assert back["acknowledgment"] == COPY["resume_greeting"]
    assert back["body"] == [COPY["resume_where"].format(step=COPY["step_display_name"])]
    assert back["next_step"]["prompt"] == COPY["resume_question"]


def _age_draft(api, case_id, days):
    """Moves a case's last activity into the past. Only a test does this: the API always writes the server time."""
    api.db.cases.update_one({"_id": UUID(case_id)},
                            {"$set": {"last_activity_at": datetime.now(timezone.utc) - timedelta(days=days)}})


def test_uc10_any_answer_edit_or_open_resets_the_28_days(api):
    register(api, "cc10c")
    cid = new_draft(api, "cc10c")["case"]["id"]
    for act in (lambda: answer(api, "cc10c", cid, "user_role", "child"),
                lambda: api.get(f"/v1/cases/{cid}", headers=as_user("cc10c")),
                lambda: answer(api, "cc10c", cid, "user_role", "friend")):
        _age_draft(api, cid, 27)
        act()
        age = datetime.now(timezone.utc) - api.db.cases.find_one({"_id": UUID(cid)})["last_activity_at"]
        assert age < timedelta(minutes=1)


def test_uc10_cleanup_deletes_idle_drafts_only(api):
    """[PRIVACY] A draft idle for 28 days is fully deleted, with its answers and tied conversation text.
    The job never deletes an active or read_only case, the account, or trial fields."""
    register(api, "cc10d")
    old = new_draft(api, "cc10d")["case"]["id"]
    answer(api, "cc10d", old, "display_name", "Fakey")
    api.db.context_items.insert_one({"case_id": UUID(old), "item_key": "CONVO_SUMMARY",
                                     "payload": {"text": "fake conversation"},
                                     "updated_at": datetime.now(timezone.utc), "expires_at": None})
    fresh = new_draft(api, "cc10d")["case"]["id"]
    started, _ = active_case(api, "cc10d")
    _age_draft(api, old, 28)
    _age_draft(api, fresh, 27)
    _age_draft(api, started, 400)
    before = trial(api, "cc10d")

    maintenance.purge_inactive_drafts(api.jobs_db)
    uid = user_id(api, "cc10d")
    remaining = {str(c["_id"]) for c in api.db.cases.find({"created_by": uid})}
    assert old not in remaining and {fresh, started} <= remaining
    for collection in ("case_intake_answers", "context_items", "case_tasks"):
        assert api.db[collection].count_documents({"case_id": UUID(old)}) == 0, collection
    assert trial(api, "cc10d") == before
    audit = [(a["actor_id"], a["action"], a["object_type"], a["object_id"])
             for a in api.db.audit_events.find({"case_id": UUID(old), "action": "draft_case_expired"})]
    assert audit == [(None, "draft_case_expired", "user", uid)]


# ------------------------------------------------------------------ UC-CASE-11

def test_uc11_review_uses_own_words_except_circumstance(api):
    register(api, "cc11")
    cid = new_draft(api, "cc11")["case"]["id"]
    turn = say(api, "cc11", cid, "My dad passed away last night in Concord, New Hampshire, very suddenly.")
    confirm(api, "cc11", cid, turn)
    answer(api, "cc11", cid, "veteran_status", state="skipped")
    review = api.get(f"/v1/cases/{cid}/review", headers=as_user("cc11")).json()
    told = {x["field"]: x["answer"] for x in review["told_me"]}
    assert told["date_of_death"] == "last night"
    assert told["circumstance"] == COPY["label_circumstance_sudden_natural"]
    later = {x["field"]: x for x in review["later"]}
    assert later["veteran_status"]["answer"] == COPY["state_skipped"]
    assert all(x["edit"]["options"][0]["label"] == "Edit" for x in review["told_me"] + review["later"])
    assert review["told_me_heading"] == "What you told me" and review["later_heading"] == "What we can figure out later"
    assert review["next_step"]["prompt"] == "Does this look right?"


# ------------------------------------------------------------------ UC-CASE-12

def test_uc12_preview_then_start_starts_trial_once_in_local_time(api):
    register(api, "cc12", time_zone="Pacific/Honolulu")
    cid = new_draft(api, "cc12")["case"]["id"]
    for f in ("user_role", "display_name"):
        answer(api, "cc12", cid, f, state="skipped")
    assert trial(api, "cc12") == (None, None)  # edits never start it
    preview = api.get(f"/v1/cases/{cid}/journey/preview", headers=as_user("cc12")).json()
    assert preview["explanation"] and preview["start_available"]
    assert [w["week"] for w in preview["weeks"]] == [1, 2, 3, 4]
    assert preview["pre_button_notice"]["text"] == COPY["pre_button_notice"]
    # Step 3 (UC-CASE-19) comes first: how Cairn keeps in touch. Then the notice and the buttons.
    assert preview["notifications_chosen"] is False
    assert preview["next_step"]["action"] == "choose_notifications"
    assert [o["value"] for o in preview["next_step"]["options"]] == ["keep_it_simple", "set_up", "skip"]
    r = api.put(f"/v1/cases/{cid}/notification-preferences", json={"preset": "skip"}, headers=as_user("cc12"))
    assert r.status_code == 200, r.text
    preview = api.get(f"/v1/cases/{cid}/journey/preview", headers=as_user("cc12")).json()
    assert preview["notifications_chosen"] is True
    assert [o["value"] for o in preview["next_step"]["options"]] == ["start_journey", "not_yet"]
    assert trial(api, "cc12") == (None, None)  # viewing never starts it

    # The notice must have been shown before Start journey is enabled.
    r = api.post(f"/v1/cases/{cid}/journey/start", json={"pre_button_notice_version": "stale"},
                 headers=as_user("cc12"))
    assert r.status_code == 409 and r.json()["code"] == "notice_changed"
    assert trial(api, "cc12") == (None, None)

    started = start_journey(api, "cc12", cid)
    begun, ends = trial(api, "cc12")
    assert ends - begun == timedelta(days=28)
    local_end = ends.astimezone(ZoneInfo("Pacific/Honolulu")).date()
    assert started["trial_end_date"] == local_end.isoformat()
    # in_app_only: the trial reminder shows in Cairn only, and the confirmation says so.
    assert started["confirmation"] == COPY["confirmation_first_case"].format(
        trial_end_date=f"{local_end:%B} {local_end.day}, {local_end.year}",
        reminder_sentence=COPY["reminder_sentence_in_app_only"])
    case = started["case"]
    assert case["status"] == "active" and case["journey_template_key"] == "general"
    assert case["journey_template_version"] == 1 and case["journey_started_at"]
    reminder = [r["due_at"] for r in api.db.trial_reminders.find({"user_id": user_id(api, "cc12"),
                                                                  "kind": "trial_ends_soon"})]
    assert reminder == [ends - timedelta(days=3)]


def test_uc12_not_yet_keeps_the_draft_and_the_trial_unstarted(api):
    register(api, "cc12b")
    cid = new_draft(api, "cc12b")["case"]["id"]
    r = api.post(f"/v1/cases/{cid}/journey/not-yet", headers=as_user("cc12b")).json()
    assert r["case"]["status"] == "draft" and r["acknowledgment"] == COPY["not_yet_saved"]
    assert trial(api, "cc12b") == (None, None)


def test_uc12_no_subscription_prompt_before_the_trial_ends(api):
    """Subscription prompt is shown only when now >= trial_ends_at."""
    register(api, "cc12c")
    cid = new_draft(api, "cc12c")["case"]["id"]
    seen = [json.dumps(answer(api, "cc12c", cid, f, state="skipped", session={})) for f in FIELDS[:3]]
    seen.append(json.dumps(start_journey(api, "cc12c", cid)))
    seen.append(everything_the_user_saw(api, "cc12c", cid))
    seen.append(api.get("/v1/me", headers=as_user("cc12c")).text)
    text = "\n".join(seen)
    assert "choose_subscription" not in text and "Choose a subscription" not in text


def test_uc12_start_journey_never_asks_for_payment(api):
    """[PRIVACY] No payment form, payment field, or payment SDK call in case creation or Start journey."""
    spec = api.app.openapi()
    pattern = re.compile(r"card|payment|billing|cvv|cvc|expiry|expiration|iban|routing", re.I)
    for path, ops in spec["paths"].items():
        for op in ops.values():
            for param in op.get("parameters", []):
                assert not pattern.search(param["name"]), f"{path} {param['name']}"
    for name, schema in spec["components"]["schemas"].items():
        for prop in schema.get("properties", {}):
            assert not pattern.search(prop), f"{name}.{prop}"


# ------------------------------------------------------------------ UC-CASE-13

def test_uc13_first_task_choice_never_auto_starts_and_not_today_leaves_none_in_progress(api):
    register(api, "cc13")
    cid = new_draft(api, "cc13")["case"]["id"]
    answer(api, "cc13", cid, "place_of_death", {"state": "NH"})
    started = start_journey(api, "cc13", cid)
    assert 1 <= len(started["recommended"]) <= 2
    assert all(r["why_now"] or r["task"]["due_on"] for r in started["recommended"])
    assert started["next_step"]["prompt"] == "What feels doable right now?"
    values = [o["value"] for o in started["next_step"]["options"]]
    assert values[1:] == ["small_task", "not_today"] and len(values) == 3
    def in_progress():
        return api.db.case_tasks.count_documents({"case_id": UUID(cid), "status": "in_progress"})
    assert in_progress() == 0

    r = api.post(f"/v1/cases/{cid}/journey/first-task", json={"choice": "not_today"}, headers=as_user("cc13")).json()
    assert r["acknowledgment"] == COPY["not_today"] and r["case"]["status"] == "active"
    assert in_progress() == 0

    other = api.get(f"/v1/cases/{cid}/journey", headers=as_user("cc13")).json()["weeks"][3]["tasks"][0]
    r = api.post(f"/v1/cases/{cid}/journey/first-task", json={"choice": other["id"]}, headers=as_user("cc13")).json()
    assert r["task"]["id"] == other["id"] and r["acknowledgment"] is None  # opened without comment
    assert in_progress() == 1


# ------------------------------------------------------------------ UC-CASE-14

def test_uc14_risk_of_harm_first_response_has_988_and_no_task_or_question(api):
    """[SAFETY] In risk_of_harm, the first response contains 988 and contains no task or intake question."""
    register(api, "cc14", voice="brisk_businesslike")
    cid = new_draft(api, "cc14")["case"]["id"]
    turn = say(api, "cc14", cid, "Honestly I just want to die.")
    text = turn["read_aloud"]["text"]
    assert "988" in text and turn["safety_mode"] == "risk_of_harm"
    assert turn["question"] is None and turn["proposals"] is None
    assert turn["next_step"]["action"] == "stay_with_user"
    assert turn["voice"] == "steady_care"
    assert "veterans_crisis_line" not in [s["id"] for s in turn["support"]]
    assert "911" in text


def test_uc14_veteran_cases_include_the_veterans_crisis_line(api):
    """[SAFETY] Veteran cases include the Veterans Crisis Line in risk_of_harm responses."""
    register(api, "cc14b")
    cid = new_draft(api, "cc14b")["case"]["id"]
    answer(api, "cc14b", cid, "veteran_status", "yes")
    turn = say(api, "cc14b", cid, "I don't want to live without him")
    assert "veterans_crisis_line" in [s["id"] for s in turn["support"]]
    assert "988 and pressing 1" in turn["read_aloud"]["text"]


def test_uc14_acute_distress_asks_nothing_until_the_user_continues(api):
    """[SAFETY] In acute_distress, no intake question is asked until the user asks to continue."""
    register(api, "cc14c")
    cid = new_draft(api, "cc14c")["case"]["id"]
    turn = say(api, "cc14c", cid, "I can't do this.")
    assert turn["safety_mode"] == "acute_distress" and turn["voice"] == "steady_care"
    assert turn["acknowledgment"] == COPY["steady_care_example"]
    assert turn["question"] is None and "988" in turn["read_aloud"]["text"]
    session = turn["session"]
    later = say(api, "cc14c", cid, "My mom died in Ohio.", session=session)
    assert later["question"] is None and later["proposals"] is None
    tapped = answer(api, "cc14c", cid, "user_role", "child", session=session)
    assert tapped["question"] is None and tapped["next_step"]["action"] == "stay_with_user"
    back = api.post(f"/v1/cases/{cid}/intake/continue", json={"session": session}, headers=as_user("cc14c")).json()
    assert back["safety_mode"] == "normal" and back["question"] is not None


def test_uc14_overwhelm_stops_questions_and_offers_a_pause_or_one_small_thing(api):
    register(api, "cc14d")
    cid = new_draft(api, "cc14d")["case"]["id"]
    session = {}
    for f in FIELDS[:3]:
        turn = answer(api, "cc14d", cid, f, state="skipped", session=session)
        session = turn["session"]
    assert turn["safety_mode"] == "overwhelm" and turn["question"] is None
    assert [o["value"] for o in turn["next_step"]["options"]] == ["pause", "small_thing"]
    assert say(api, "cc14d", cid, "This is too much")["next_step"]["action"] == "overwhelm_choice"


def test_uc14_distress_is_never_persisted(api):
    """database/CLAUDE.md decision 7: no inference about the user's emotional state is stored."""
    register(api, "cc14e")
    cid = new_draft(api, "cc14e")["case"]["id"]
    say(api, "cc14e", cid, "I can't do this, I want to die")
    dump = case_doc(api, cid)
    assert "risk" not in dump and "distress" not in dump and "die" not in dump
    assert answer_rows(api, cid) == {}


# ------------------------------------------------------------------ UC-CASE-15

def test_uc15_sensitive_numbers_are_redacted_before_storage_logs_and_reply(api, caplog):
    """[PRIVACY] Server-side redaction before any write, log line, or reply. The reply never echoes any part."""
    register(api, "cc15")
    cid = new_draft(api, "cc15")["case"]["id"]
    caplog.set_level(logging.DEBUG)
    text = ("Her SSN is 078-05-1120 and the card was 4111 1111 1111 1111, account number 000123456789. "
            "She died in Ohio.")
    turn = say(api, "cc15", cid, text)
    assert set(turn["redactions"]) == {"ssn", "card_number", "account_number"}
    assert COPY["redaction_explanation"] in turn["body"]
    reply = without_random_material(json.dumps(turn))
    for fragment in ("078-05-1120", "078", "1120", "4111", "1111", "000123456789", "6789"):
        assert fragment not in reply, fragment
    assert "[removed]" in turn["masked_text"]
    confirm(api, "cc15", cid, turn)
    answer(api, "cc15", cid, "display_name", "Aunt 078-05-1120")
    dump = json.dumps([list(api.db[c].find({"case_id": UUID(cid)})) for c in ("case_intake_answers", "audit_events")],
                      default=str)
    dump += case_doc(api, cid)
    dump, logs = without_random_material(dump), without_random_material(caplog.text)
    for fragment in ("078-05-1120", "078051120", "4111", "000123456789"):
        assert fragment not in dump and fragment not in logs, fragment


# ------------------------------------------------------------------ UC-CASE-16

def test_uc16_attorney_trigger_never_blocks_the_journey(api):
    register(api, "cc16")
    cid = new_draft(api, "cc16")["case"]["id"]
    r = api.post(f"/v1/cases/{cid}/intake/attorney-referrals", json={"trigger": "contested_will"},
                 headers=as_user("cc16")).json()
    assert COPY["attorney_task_added"] in r["body"]
    assert r["notes"][0]["attorney_line"] == COPY["attorney_referral_line"]
    started = start_journey(api, "cc16", cid)
    journey = api.get(f"/v1/cases/{cid}/journey", headers=as_user("cc16")).json()
    task = find_task(journey, "talk_to_estate_attorney")
    assert task["attorney_referral"] and task["attorney_line"]
    assert len(task_keys(journey)) > 3 and started["case"]["status"] == "active"


def test_uc16_unverified_state_adds_the_state_office_task(api):
    register(api, "cc16b")
    cid, journey = active_case(api, "cc16b", {"place_of_death": {"state": "WY"}})
    assert "ask_state_vital_records_office" in task_keys(journey)


# ------------------------------------------------------------------ UC-CASE-17

def test_uc17_death_not_yet_offers_a_draft_and_never_starts_the_journey(api):
    register(api, "cc17")
    cid = new_draft(api, "cc17")["case"]["id"]
    turn = say(api, "cc17", cid, "She's in hospice now, she hasn't died yet.")
    assert turn["next_step"]["action"] == "death_not_yet"
    assert [o["value"] for o in turn["next_step"]["options"]] == ["save_draft", "come_back_later"]
    saved = api.post(f"/v1/cases/{cid}/intake/death-not-yet", json={"choice": "save_draft"},
                     headers=as_user("cc17")).json()
    assert saved["next_step"]["prompt"] == COPY["draft_notice"]
    preview = api.get(f"/v1/cases/{cid}/journey/preview", headers=as_user("cc17")).json()
    assert preview["start_available"] is False and preview["pre_button_notice"] is None
    assert "start_journey" not in [o["value"] for o in preview["next_step"]["options"]]
    r = api.post(f"/v1/cases/{cid}/journey/start", json={"pre_button_notice_version": "x"}, headers=as_user("cc17"))
    assert r.status_code == 409
    # The data layer refuses it too, not only the endpoint.
    db = api.app.state.db
    with pytest.raises(RuleViolation) as refused, db.client.start_session() as cs, cs.start_transaction():
        Session(db.db, cs, user_id(api, "cc17")).start_journey(UUID(cid), 1, "general")
    assert refused.value.kind == "prerequisite"
    assert trial(api, "cc17") == (None, None)


# ------------------------------------------------------------------ UC-CASE-18

def test_uc18_second_case_acknowledges_another_loss_and_keeps_the_trial(api):
    register(api, "cc18")
    first, _ = active_case(api, "cc18")
    begun, ends = trial(api, "cc18")
    turn = new_draft(api, "cc18")
    assert turn["acknowledgment"] == COPY["ack_another_case"]
    second = turn["case"]["id"]
    preview = api.get(f"/v1/cases/{second}/journey/preview", headers=as_user("cc18")).json()
    local_end = ends.astimezone(ZoneInfo("America/New_York")).date()
    end_text = f"{local_end:%B} {local_end.day}, {local_end.year}"
    assert end_text in preview["pre_button_notice"]["text"]
    started = start_journey(api, "cc18", second)
    assert started["confirmation"] == COPY["confirmation_existing_trial"].format(trial_end_date=end_text)
    assert trial(api, "cc18") == (begun, ends)


def test_uc18_read_only_account_can_draft_but_not_start_without_a_subscription(api):
    register(api, "cc18b")
    active_case(api, "cc18b")
    expire_trial(api, "cc18b")
    draft = new_draft(api, "cc18b")["case"]["id"]
    turn = answer(api, "cc18b", draft, "display_name", "Fakey")
    assert turn["case"]["status"] == "draft"
    preview = api.get(f"/v1/cases/{draft}/journey/preview", headers=as_user("cc18b")).json()
    assert preview["start_available"] is False and preview["next_step"]["action"] == "choose_subscription"
    assert preview["next_step"]["prompt"] == COPY["subscription_needed_new_journey"]
    r = api.post(f"/v1/cases/{draft}/journey/start", json={"pre_button_notice_version": "x"}, headers=as_user("cc18b"))
    assert r.status_code == 403 and r.json()["next_step"]["action"] == "choose_subscription"
    assert api.db.cases.find_one({"_id": UUID(draft)})["status"] == "draft"


# ------------------------------------------------------------------ access

@pytest.mark.parametrize("method,path,body", [
    ("get", "", None), ("get", "/review", None), ("get", "/journey/preview", None),
    ("put", "/intake/answers/user_role", {"state": "skipped"}),
    ("post", "/intake/messages", {"text": "hello"}), ("post", "/intake/pause", {}),
    ("post", "/journey/start", {"pre_button_notice_version": "x"}),
])
def test_other_users_cannot_read_or_change_a_draft(api, method, path, body):
    register(api, "cc-owner")
    register(api, "cc-stranger")
    cid = new_draft(api, "cc-owner")["case"]["id"]
    r = getattr(api, method)(f"/v1/cases/{cid}{path}", headers=as_user("cc-stranger"),
                             **({"json": body} if body is not None else {}))
    assert r.status_code == 403
    assert answer_rows(api, cid) == {}


def test_case_list_shows_drafts_with_their_deletion_date(api):
    register(api, "cc-list")
    cid = new_draft(api, "cc-list")["case"]["id"]
    cases = api.get("/v1/cases", headers=as_user("cc-list")).json()["cases"]
    mine = next(c for c in cases if c["id"] == cid)
    expires = datetime.fromisoformat(mine["draft_expires_at"]) - datetime.fromisoformat(mine["last_activity_at"])
    assert mine["status"] == "draft" and expires == timedelta(days=28)
    assert date.today() <= datetime.fromisoformat(mine["draft_expires_at"]).date()
