"""Security and behavior checks for the data layer. Replaces database/db/tests/verify.sql.

Three layers are checked, each on its own:
* roles: what each MongoDB login (cairnApp, cairnJobs, cairnLoader) can and can't do at all
* validators: what no writer can store, an administrator included
* store.Session: the case boundary, read-only accounts, onboarding order, the trial, and deletion
  (the rules PostgreSQL held in row-level security, SECURITY DEFINER functions, and triggers)
and then the jobs in maintenance.py, as the cairnJobs user.

Skipped unless CAIRN_TEST_MONGODB_URI is set (see conftest.py). Fake data only.
"""
from __future__ import annotations

import pathlib
import re
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from pymongo import MongoClient
from pymongo.errors import OperationFailure, WriteError

from cairn_api import maintenance
from cairn_api.db import Database, client_for
from cairn_api.errors import ApiError

UNAUTHORIZED, INVALID = 13, 121
STEPS = (("privacy_terms", "privacy_terms_accepted"), ("trial_terms", "trial_terms_accepted"),
         ("ai_notice", "ai_notice_accepted"))
JOURNEY = (1, "general")  # content/journeys/journey-selection.json


# ------------------------------------------------------------------ fixtures and helpers

class Env:
    def __init__(self, scratch):
        self.admin = scratch.db
        self.app = Database(scratch.uris["cairnApp"], scratch.name, min_size=0, max_size=4)
        self._raw = {role: MongoClient(uri, uuidRepresentation="standard", tz_aware=True)
                     for role, uri in scratch.uris.items()}
        self.raw = {role: client[scratch.name] for role, client in self._raw.items()}
        self.jobs = client_for(scratch.uris["cairnJobs"], app_name="cairn-test-jobs")[scratch.name]

    def close(self):
        self.app.close()
        self.jobs.client.close()
        for client in self._raw.values():
            client.close()


@pytest.fixture(scope="module")
def env(scratch_db):
    e = Env(scratch_db)
    yield e
    e.close()


def run(env, uid, fn):
    """fn(session) as uid, in its own committed transaction."""
    with env.app.session(user_id=uid) as s:
        return fn(s)


def refuses(env, uid, fn, code="case_access_denied"):
    with pytest.raises(ApiError) as e:
        run(env, uid, fn)
    assert e.value.code == code, e.value.code


def new_user(env, name, method="email") -> uuid.UUID:
    tag = uuid.uuid4().hex[:8]
    return run(env, None, lambda s: s.create_account(f"sec|{name}-{tag}", f"{name}-{tag}@example.test", method))


def onboard(env, uid):
    def walk(s):
        for purpose, step in STEPS:
            s.add_consent(purpose, "test", "email", "verify/1")
            s.advance_onboarding(step)
        s.update_account(preferred_name="Tester")
        s.advance_onboarding("preferred_name_saved")
        s.advance_onboarding("complete")
    run(env, uid, walk)


def ready_user(env, name) -> uuid.UUID:
    uid = new_user(env, name)
    onboard(env, uid)
    return uid


def draft(env, uid) -> uuid.UUID:
    return run(env, uid, lambda s: s.create_draft())


def started(env, uid) -> uuid.UUID:
    cid = draft(env, uid)
    run(env, uid, lambda s: s.start_journey(cid, *JOURNEY))
    return cid


def expire(env, uid, days=29):
    u = env.admin.users.find_one({"_id": uid})
    shift = timedelta(days=days)
    env.admin.users.update_one({"_id": uid}, {"$set": {"trial_started_at": u["trial_started_at"] - shift,
                                                       "trial_ends_at": u["trial_ends_at"] - shift}})


def template(env, key="notify_banks") -> dict:
    t = env.admin.task_templates.find_one({"task_key": key, "active": True})
    return {"id": t["_id"], "task_key": key}


def add_task(env, uid, cid, key="notify_banks", due: date | None = None) -> uuid.UUID:
    return run(env, uid, lambda s: s.add_task(cid, template(env, key), "not_started", due))


def denied(fn, code=UNAUTHORIZED):
    with pytest.raises((OperationFailure, WriteError)) as e:
        fn()
    assert e.value.code == code, e.value.code


def now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ roles

def test_the_app_can_append_audit_rows_and_never_read_them(env):
    app = env.raw["cairnApp"]
    app.audit_events.insert_one({"_id": uuid.uuid4(), "actor_id": None, "case_id": None, "action": "sec_probe",
                                 "object_type": None, "object_id": None, "occurred_at": now()})
    denied(lambda: app.audit_events.find_one())
    denied(lambda: app.audit_events.update_many({}, {"$set": {"action": "tampered"}}))
    denied(lambda: app.audit_events.delete_many({}))


@pytest.mark.parametrize("collection", ["action_confirmation_outbox", "action_confirmation_log",
                                        "identity_deletion_requests", "schema_migrations", "job_locks"])
def test_the_app_cannot_read_queues_logs_or_bookkeeping(env, collection):
    denied(lambda: env.raw["cairnApp"][collection].find_one())


def test_the_app_cannot_write_content_settings_or_logs(env):
    app = env.raw["cairnApp"]
    denied(lambda: app.task_templates.update_many({}, {"$set": {"title": "changed"}}))
    denied(lambda: app.task_templates.insert_one({"task_key": "zzz"}))
    denied(lambda: app.journey_templates.update_many({}, {"$set": {"active": False}}))
    denied(lambda: app.app_settings.update_one({"_id": "draft_retention_days"}, {"$set": {"value": 1}}))
    denied(lambda: app.notification_log.insert_one({"case_id": uuid.uuid4()}))
    denied(lambda: app.action_confirmation_log.insert_one({"action_type": "account_deleted"}))
    denied(lambda: app.consents.update_many({}, {"$set": {"policy_version": "x"}}))  # append-only
    denied(lambda: app.trial_reminders.update_many({}, {"$set": {"email_sent_at": now()}}))  # the job claims them


def test_the_loader_can_add_versions_and_nothing_else(env):
    loader = env.raw["cairnLoader"]
    assert loader.task_templates.find_one() is not None
    denied(lambda: loader.task_templates.delete_many({}))
    for collection in ("users", "cases", "audit_events", "app_settings"):
        denied(lambda c=collection: loader[c].find_one())


def test_the_jobs_cannot_rewrite_history_or_content(env):
    jobs = env.raw["cairnJobs"]
    denied(lambda: jobs.audit_events.find_one())
    denied(lambda: jobs.audit_events.delete_many({}))
    denied(lambda: jobs.action_confirmation_log.delete_many({}))
    denied(lambda: jobs.action_confirmation_log.find_one())
    denied(lambda: jobs.task_templates.insert_one({}))
    denied(lambda: jobs.app_settings.update_one({"_id": "draft_retention_days"}, {"$set": {"value": 1}}))


def test_no_cairn_role_can_bypass_validation_or_manage_users(env):
    info = env.admin.command("rolesInfo", 1, showPrivileges=True)["roles"]
    roles = {r["role"]: r for r in info if r["role"].startswith("cairn")}
    assert set(roles) == {"cairnApp", "cairnJobs", "cairnLoader"}
    for role in roles.values():
        actions = {a for p in role["privileges"] for a in p["actions"]}
        assert actions <= {"find", "insert", "update", "remove"}, role["role"]
        assert role["inheritedRoles"] == []


# ------------------------------------------------------------------ validators (an administrator included)

def invalid(fn):
    with pytest.raises(WriteError) as e:
        fn()
    assert e.value.code == INVALID


def test_users_validator(env):
    uid = new_user(env, "val-user")
    users = env.admin.users
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"voice": "gentle"}}))
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"voice": None}}))
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"status": "superuser"}}))
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"personality": "steady"}}))  # no such field
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"email": "Changed@example.test"}}))  # email_lower
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"trial_started_at": now()}}))  # 28 days, together
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"trial_started_at": now(), "trial_ends_at": now()}}))
    start = now().replace(microsecond=0)
    users.update_one({"_id": uid}, {"$set": {"trial_started_at": start, "trial_ends_at": start + timedelta(hours=672)}})


def test_intake_answer_validator(env):
    cid = draft(env, ready_user(env, "val-answers"))
    answers = env.admin.case_intake_answers

    def put(field, state="answered", value=None, own_words=None):
        answers.insert_one({"case_id": cid, "field_key": field, "answer_state": state, "value": value,
                            "own_words": own_words, "updated_at": now()})

    put("display_name", value="Dan", own_words="Dan")
    put("circumstance", value="sudden_natural")
    put("veteran_status", state="skipped")
    put("date_of_death", value={"precision": "this_week", "date": None})
    put("place_of_death", value={"state": "NH", "county_or_city": None, "outside_us": False})
    put("completed_items", value=["funeral_provider_chosen", "bank_notified"])
    invalid(lambda: put("cause_of_death", value="x"))
    invalid(lambda: put("user_role", value="cousin"))
    invalid(lambda: put("residence_state", state="skipped", value="different"))

    def change(field, **values):
        answers.update_one({"case_id": cid, "field_key": field}, {"$set": values})

    invalid(lambda: change("circumstance", value="he had cancer"))
    invalid(lambda: change("circumstance", own_words="a heart attack"))
    invalid(lambda: change("date_of_death", value={"precision": "exact", "date": None}))
    invalid(lambda: change("place_of_death", value={"state": "NH", "county_or_city": None, "outside_us": False,
                                                    "ssn": "1"}))
    invalid(lambda: change("place_of_death", value={"state": "NH", "county_or_city": None, "outside_us": True}))
    invalid(lambda: change("completed_items", value=["none_or_unsure", "bank_notified"]))
    invalid(lambda: change("veteran_status", value="yes"))  # a value only when answered
    invalid(lambda: change("display_name", value="Dan\x07"))


def test_case_validator(env):
    cid = draft(env, ready_user(env, "val-case"))
    cases = env.admin.cases
    invalid(lambda: cases.update_one({"_id": cid}, {"$set": {"shown_notices": ["suicide_loss"]}}))
    invalid(lambda: cases.update_one({"_id": cid}, {"$set": {"attorney_triggers": ["grief"]}}))
    invalid(lambda: cases.update_one({"_id": cid}, {"$set": {"journey_template_key": "general"}}))  # draft
    invalid(lambda: cases.update_one({"_id": cid}, {"$set": {"status": "active"}}))  # no start recorded
    invalid(lambda: cases.update_one({"_id": cid}, {"$set": {"members.0.role": "viewer"}}))  # MVP owner only
    invalid(lambda: cases.update_one({"_id": cid}, {"$set": {"distress": True}}))


def test_deceased_and_task_validators(env):
    uid = ready_user(env, "val-deceased")
    cid = started(env, uid)
    run(env, uid, lambda s: s.save_deceased(cid, {"legal_first_name": "Dan", "date_of_birth": date(1950, 1, 1)}))
    deceased = env.admin.deceased
    invalid(lambda: deceased.update_one({"case_id": cid}, {"$set": {"ssn_last4": "12345"}}))
    invalid(lambda: deceased.update_one({"case_id": cid}, {"$set": {"ssn": "123456789"}}))
    invalid(lambda: deceased.update_one({"case_id": cid}, {"$set": {"date_of_death": "1949-12-31"}}))
    invalid(lambda: deceased.update_one({"case_id": cid}, {"$set": {"date_of_birth": "2999-01-01"}}))
    tid = add_task(env, uid, cid)
    invalid(lambda: env.admin.case_tasks.update_one({"_id": tid}, {"$set": {"status": "done"}}))


@pytest.mark.parametrize("change", [
    {"channels": ["sms"], "reasons": ["inactivity"], "inactivity_days": 7},
    {"channels": ["in_app_only", "email"]},
    {"channels": ["email"]},
    {"channels": ["email", "email"], "reasons": ["inactivity"], "inactivity_days": 7},
    {"channels": ["email"], "reasons": ["due_date_upcoming"]},
    {"channels": ["email"], "reasons": ["inactivity"], "inactivity_days": 7, "due_date_lead_days": 3},
    {"channels": ["email"], "reasons": ["inactivity"], "inactivity_days": 5},
    {"frequency": "hourly"},
    {"channels": [None]},
    {"phone": "555-0100"},
])
def test_notification_choice_validator(env, change):
    uid = ready_user(env, "val-prefs")
    cid = draft(env, uid)
    run(env, uid, lambda s: s.ensure_default_preferences(cid))
    invalid(lambda: env.admin.notification_preferences.update_one({"_id": cid}, {"$set": change}))


def test_other_validators(env):
    invalid(lambda: env.admin.action_confirmation_log.insert_one(
        {"_id": uuid.uuid4(), "action_type": "account_deleted", "channel": "email", "sent_at": now(),
         "email": "x@example.test"}))  # content_stored is false by construction
    invalid(lambda: env.admin.app_settings.update_one({"_id": "draft_retention_days"}, {"$set": {"value": 0}}))
    invalid(lambda: env.admin.app_settings.insert_one({"_id": "anything", "value": 1, "description": "x",
                                                       "updated_at": now()}))
    invalid(lambda: env.admin.context_items.insert_one({"case_id": uuid.uuid4(), "item_key": "CONVO_SUMMARY",
                                                        "payload": {"text": "x" * 40000}, "updated_at": now(),
                                                        "expires_at": None}))
    invalid(lambda: env.admin.audit_events.insert_one({"_id": uuid.uuid4(), "actor_id": None, "case_id": None,
                                                       "action": "x", "object_type": None, "object_id": None,
                                                       "occurred_at": now(), "detail": "Dan Fakerton"}))


# ------------------------------------------------------------------ sign up and onboarding

def test_no_user_sees_nothing(env):
    assert run(env, None, lambda s: s.load_account()) is None
    assert run(env, None, lambda s: s.owned_cases()) == []
    refuses(env, None, lambda s: s.create_draft(), "registration_required")


def test_account_creation_and_onboarding_order(env):
    subject = f"sec|onboard-{uuid.uuid4().hex[:6]}"
    uid = run(env, None, lambda s: s.create_account(subject, f"{subject[4:]}@example.test", "apple", "Pat"))
    assert run(env, None, lambda s: s.resolve_user(subject)) == uid
    assert run(env, None, lambda s: s.create_account(subject, f"{subject[4:]}@example.test", "apple")) == uid
    a = run(env, uid, lambda s: s.load_account())
    assert (a["status"], a["onboarding_step"], a["trial_started_at"], a["voice"], a["name_prefill"]) == \
        ("pending_onboarding", "account_created", None, "steady_direct", "Pat")
    refuses(env, uid, lambda s: s.create_draft())  # no case before onboarding
    refuses(env, uid, lambda s: s.advance_onboarding("age_confirmed"), "invalid_value")
    refuses(env, uid, lambda s: s.advance_onboarding("bogus"), "invalid_value")
    refuses(env, uid, lambda s: s.advance_onboarding("privacy_terms_accepted"), "out_of_order")  # no consent yet
    refuses(env, uid, lambda s: s.advance_onboarding("trial_terms_accepted"), "out_of_order")    # skipping ahead
    run(env, uid, lambda s: s.add_consent("privacy_terms", "test", "apple", None))
    assert run(env, uid, lambda s: s.advance_onboarding("privacy_terms_accepted")) == "privacy_terms_accepted"
    assert run(env, uid, lambda s: s.advance_onboarding("privacy_terms_accepted")) == "privacy_terms_accepted"
    refuses(env, uid, lambda s: s.advance_onboarding("ai_notice_accepted"), "out_of_order")
    # The app sets only Settings fields, never onboarding, status, or the trial.
    for field, value in (("onboarding_step", "complete"), ("status", "subscribed"), ("trial_started_at", None),
                         ("email", "x@example.test")):
        refuses(env, uid, lambda s, f=field, v=value: s.update_account(**{f: v}))
    run(env, uid, lambda s: s.update_account(voice="brisk_businesslike"))
    refuses(env, uid, lambda s: s.update_account(voice="gentle"), "invalid_value")
    onboard_rest(env, uid)
    a = run(env, uid, lambda s: s.load_account())
    assert (a["status"], a["onboarding_step"], a["trial_started_at"], a["name_prefill"]) == \
        ("active_no_case", "complete", None, None)


def onboard_rest(env, uid):
    def walk(s):
        for purpose, step in STEPS[1:]:
            s.add_consent(purpose, "test", "apple", None)
            s.advance_onboarding(step)
        s.update_account(preferred_name="Tester")
        s.advance_onboarding("preferred_name_saved")
        s.advance_onboarding("complete")
    run(env, uid, walk)


# ------------------------------------------------------------------ case creation and the trial

def test_a_draft_starts_nothing_and_holds_no_identity(env):
    uid = ready_user(env, "draft")
    cid = draft(env, uid)
    case = run(env, uid, lambda s: s.load_case(cid))
    assert case["status"] == "draft" and case["journey_started_at"] is None and case["journey_started_on"] is None
    assert run(env, uid, lambda s: s.load_account())["trial_started_at"] is None
    assert env.admin.trial_reminders.count_documents({"user_id": uid}) == 0
    refuses(env, uid, lambda s: s.save_deceased(cid, {"legal_first_name": "E"}))  # never on a draft
    for field, value in (("status", "active"), ("journey_started_at", now()), ("deletion_requested_at", now()),
                         ("created_by", uuid.uuid4()), ("members", [])):
        refuses(env, uid, lambda s, f=field, v=value: s.update_case(cid, **{f: v}))
    refuses(env, uid, lambda s: s.update_case(cid, journey_template_key="general"), "invalid_value")
    refuses(env, uid, lambda s: s.update_case(cid, shown_notices=["suicide_loss"]), "invalid_value")
    refuses(env, uid, lambda s: s.save_answer(cid, "cause_of_death", "answered", "x", None), "invalid_value")
    refuses(env, uid, lambda s: s.save_answer(cid, "user_role", "answered", "cousin", None), "invalid_value")
    refuses(env, uid, lambda s: s.save_answer(cid, "circumstance", "answered", "sudden_natural", "heart attack"),
            "invalid_value")
    refuses(env, uid, lambda s: s.save_answer(cid, "veteran_status", "skipped", "yes", None), "invalid_value")


def test_start_journey_starts_the_trial_once(env):
    uid = ready_user(env, "trial")
    cid = draft(env, uid)
    refuses(env, uid, lambda s: s.start_journey(cid, 1, "nonexistent_path"), "invalid_value")
    refuses(env, uid, lambda s: s.start_journey(cid, 424242, "general"), "invalid_value")
    run(env, uid, lambda s: s.update_case(cid, death_not_yet_occurred=True))
    refuses(env, uid, lambda s: s.start_journey(cid, *JOURNEY), "out_of_order")
    run(env, uid, lambda s: s.update_case(cid, death_not_yet_occurred=False))
    assert run(env, uid, lambda s: s.load_account())["trial_started_at"] is None

    assert run(env, uid, lambda s: s.start_journey(cid, *JOURNEY)) is True
    case = env.admin.cases.find_one({"_id": cid})
    user = env.admin.users.find_one({"_id": uid})
    assert (case["status"], case["journey_template_key"], case["journey_template_version"]) == ("active", "general", 1)
    assert case["journey_started_at"] == user["trial_started_at"] and case["journey_started_on"]
    assert user["status"] == "trial_active"
    assert user["trial_ends_at"] - user["trial_started_at"] == timedelta(hours=672)
    due = {r["kind"]: r["due_at"] - user["trial_started_at"] for r in env.admin.trial_reminders.find({"user_id": uid})}
    assert due == {"trial_day_21": timedelta(hours=504), "trial_day_27": timedelta(hours=648),
                   "trial_ends_soon": timedelta(hours=600)}
    refuses(env, uid, lambda s: s.start_journey(cid, *JOURNEY), "out_of_order")
    assert env.admin.audit_events.count_documents({"case_id": cid, "action": "journey_started"}) == 1

    second = draft(env, uid)
    assert run(env, uid, lambda s: s.start_journey(second, *JOURNEY)) is False  # DEC-04
    assert env.admin.users.find_one({"_id": uid})["trial_started_at"] == user["trial_started_at"]
    assert env.admin.trial_reminders.count_documents({"user_id": uid}) == 3


def test_the_deceased_two_step_and_death_after_birth(env):
    uid = ready_user(env, "deceased")
    cid = started(env, uid)
    did, created = run(env, uid, lambda s: s.save_deceased(cid, {"legal_first_name": "Dan", "legal_last_name": "A",
                                                                 "date_of_birth": date(1950, 1, 1)}))
    assert created
    run(env, uid, lambda s: s.save_deceased(cid, {"date_of_death": date.today() - timedelta(days=2),
                                                  "death_state": "NH"}))
    assert run(env, uid, lambda s: s.save_deceased(cid, {"legal_middle_name": "Q"})) == (did, False)
    refuses(env, uid, lambda s: s.save_deceased(cid, {"date_of_birth": date.today()}), "invalid_value")
    refuses(env, uid, lambda s: s.save_deceased(cid, {"ssn_last4": "12345"}), "invalid_value")
    refuses(env, uid, lambda s: s.save_deceased(cid, {"case_id": uuid.uuid4()}))
    assert "ssn_last4" not in run(env, uid, lambda s: s.load_deceased(cid, for_export=True))


# ------------------------------------------------------------------ the case boundary

def test_another_user_sees_and_changes_nothing(env):
    alice, bob = ready_user(env, "alice"), ready_user(env, "bob")
    cid = started(env, alice)
    run(env, alice, lambda s: s.save_answer(cid, "display_name", "answered", "Dan", None))
    run(env, alice, lambda s: s.save_deceased(cid, {"legal_first_name": "Dan"}))
    run(env, alice, lambda s: s.ensure_default_preferences(cid))
    run(env, alice, lambda s: s.write_context(cid, "CERT_ORDER", {"copies": 3}))
    tid = add_task(env, alice, cid)

    assert run(env, bob, lambda s: s.load_case(cid)) is None
    assert run(env, bob, lambda s: s.load_answers(cid)) == {}
    assert run(env, bob, lambda s: s.load_tasks(cid)) == []
    assert run(env, bob, lambda s: s.load_task(cid, tid)) is None
    assert run(env, bob, lambda s: s.notification_preferences(cid)) is None
    assert run(env, bob, lambda s: s.read_context(cid, "CERT_ORDER")) is None
    assert run(env, bob, lambda s: s.owned_cases()) == []
    assert run(env, bob, lambda s: s.is_case_member(cid)) is False
    for read in (lambda s: s.load_deceased(cid), lambda s: s.context_items(cid), lambda s: s.notifications_sent(cid),
                 lambda s: s.task_states(cid)):
        refuses(env, bob, read)

    for write in (lambda s: s.save_answer(cid, "user_role", "skipped", None, None),
                  lambda s: s.save_deceased(cid, {"legal_first_name": "Hacked"}),
                  lambda s: s.update_case(cid, tasks_paused_until=now()),
                  lambda s: s.start_journey(cid, *JOURNEY),
                  lambda s: s.add_task(cid, template(env), "not_started", None),
                  lambda s: s.request_case_deletion(cid, "now"),
                  lambda s: s.cancel_case_deletion(cid),
                  lambda s: s.save_notification_preferences(cid, {
                      "channels": ["in_app_only"], "reasons": [], "due_date_lead_days": None,
                      "inactivity_days": None, "frequency": "daily_max", "push_permission_granted": False}),
                  lambda s: s.ensure_default_preferences(cid),
                  lambda s: s.write_context(cid, "CERT_ORDER", {}),
                  lambda s: s.lock_context(cid, "BANK_NOTICES", {})):
        refuses(env, bob, write)
    assert run(env, bob, lambda s: s.update_task(cid, tid, status="skipped")) is False
    before = env.admin.cases.find_one({"_id": cid})["last_activity_at"]
    run(env, bob, lambda s: s.touch(cid))
    assert env.admin.cases.find_one({"_id": cid})["last_activity_at"] == before

    # Alice still has everything.
    assert env.admin.deceased.find_one({"case_id": cid})["legal_first_name"] == "Dan"
    assert env.admin.case_tasks.find_one({"_id": tid})["status"] == "not_started"
    assert run(env, alice, lambda s: s.load_answers(cid))["display_name"]["value"] == "Dan"


def test_a_task_change_is_activity_and_done_records_the_time(env):
    uid = ready_user(env, "tasks")
    cid = started(env, uid)
    tid = add_task(env, uid, cid)
    env.admin.cases.update_one({"_id": cid}, {"$set": {"last_activity_at": now() - timedelta(days=4)}})
    assert run(env, uid, lambda s: s.update_task(cid, tid, selected=False)) is True
    assert env.admin.cases.find_one({"_id": cid})["last_activity_at"] < now() - timedelta(days=3)  # rules aren't
    assert run(env, uid, lambda s: s.update_task(cid, tid, status="done")) is True
    task = env.admin.case_tasks.find_one({"_id": tid})
    assert task["completed_at"] is not None
    assert env.admin.cases.find_one({"_id": cid})["last_activity_at"] > now() - timedelta(minutes=1)
    assert run(env, uid, lambda s: s.update_task(cid, tid, status="in_progress", only_from=("not_started",))) is False


# ------------------------------------------------------------------ read-only after the trial (D-05)

def test_read_only_reads_drafts_settings_and_deletion_stay_available(env):
    uid = ready_user(env, "readonly")
    active = started(env, uid)
    run(env, uid, lambda s: s.save_answer(active, "display_name", "answered", "Dan", None))
    tid = add_task(env, uid, active)
    second = started(env, uid)
    expire(env, uid)

    assert run(env, uid, lambda s: s.account_can_write()) is False
    assert run(env, uid, lambda s: s.load_account())["status"] == "read_only"
    assert len(run(env, uid, lambda s: s.owned_cases())) == 2
    assert run(env, uid, lambda s: s.load_tasks(active))
    # UC-CASE-18. A draft can be made and edited, but its journey can't start.
    d = draft(env, uid)
    run(env, uid, lambda s: s.save_answer(d, "user_role", "skipped", None, None))
    run(env, uid, lambda s: s.update_case(d, last_intake_step="display_name"))
    refuses(env, uid, lambda s: s.start_journey(d, *JOURNEY))
    # An active case is read-only, answers included.
    for write in (lambda s: s.save_deceased(second, {"legal_first_name": "E"}),
                  lambda s: s.write_context(active, "CERT_ORDER", {}),
                  lambda s: s.update_case(active, tasks_paused_until=now()),
                  lambda s: s.save_answer(active, "display_name", "unsure", None, None),
                  lambda s: s.add_task(active, template(env, "notify_life_insurers"), "not_started", None)):
        refuses(env, uid, write)
    assert run(env, uid, lambda s: s.update_task(active, tid, status="skipped")) is False
    # Always free: Settings, notification preferences, and deleting (D-2026-09-25-F1).
    run(env, uid, lambda s: s.update_account(voice="warm_patient"))
    run(env, uid, lambda s: s.ensure_default_preferences(active))
    run(env, uid, lambda s: s.save_notification_preferences(active, {
        "channels": ["email"], "reasons": ["inactivity"], "due_date_lead_days": None, "inactivity_days": 7,
        "frequency": "weekly_max", "push_permission_granted": False}))
    assert run(env, uid, lambda s: s.request_case_deletion(second, "hold")) is not None
    assert run(env, uid, lambda s: s.request_case_deletion(second, "now")) is None
    assert run(env, uid, lambda s: s.request_case_deletion(d, "now")) is None
    assert len(run(env, uid, lambda s: s.owned_cases())) == 1

    env.admin.users.update_one({"_id": uid}, {"$set": {"status": "subscribed"}})
    assert run(env, uid, lambda s: s.account_can_write()) is True
    maintenance.expire_trials(env.jobs)
    assert env.admin.users.find_one({"_id": uid})["status"] == "subscribed"  # expire_trials skips subscribed


# ------------------------------------------------------------------ deleting

def test_case_deletion_now_and_with_a_hold(env):
    uid = ready_user(env, "delete")
    held = draft(env, uid)
    refuses(env, uid, lambda s: s.request_case_deletion(held, "later"), "invalid_value")
    when = run(env, uid, lambda s: s.request_case_deletion(held, "hold"))
    requested = env.admin.cases.find_one({"_id": held})["deletion_requested_at"]
    assert when == requested + timedelta(days=7)
    assert run(env, uid, lambda s: s.request_case_deletion(held, "hold")) == when  # asking again keeps the date
    assert run(env, uid, lambda s: s.cancel_case_deletion(held)) is True
    assert run(env, uid, lambda s: s.cancel_case_deletion(held)) is False
    run(env, uid, lambda s: s.request_case_deletion(held, "hold"))

    gone = started(env, uid)
    run(env, uid, lambda s: s.save_answer(gone, "display_name", "answered", "Gus", None))
    run(env, uid, lambda s: s.ensure_default_preferences(gone))
    run(env, uid, lambda s: s.save_deceased(gone, {"legal_first_name": "Gus"}))
    add_task(env, uid, gone)
    assert run(env, uid, lambda s: s.request_case_deletion(gone, "now")) is None
    for collection in ("case_intake_answers", "case_tasks", "deceased", "context_items", "notification_log"):
        assert env.admin[collection].count_documents({"case_id": gone}) == 0, collection
    assert env.admin.notification_preferences.count_documents({"_id": gone}) == 0
    email = env.admin.users.find_one({"_id": uid})["email"]
    assert [(r["action_type"], r["user_id"]) for r in env.admin.action_confirmation_outbox.find({"email": email})] == \
        [("case_deleted_now", uid)]
    assert env.admin.audit_events.count_documents({"case_id": gone, "action": "case_deleted_now"}) == 1

    # The hold ends: deleted then, and not before, with its own confirmation.
    maintenance.purge_held_cases(env.jobs)
    assert env.admin.cases.count_documents({"_id": held}) == 1  # not before the hold ends
    env.admin.cases.update_one({"_id": held}, {"$set": {"deletion_requested_at": now() - timedelta(days=7)}})
    assert maintenance.purge_held_cases(env.jobs) >= 1
    assert env.admin.cases.count_documents({"_id": held}) == 0
    assert env.admin.action_confirmation_outbox.count_documents(
        {"user_id": uid, "action_type": "case_deleted_after_hold"}) == 1


def test_account_deletion_takes_everything_and_sends_one_confirmation(env):
    uid = ready_user(env, "goodbye")
    email = env.admin.users.find_one({"_id": uid})["email"]
    a = started(env, uid)
    run(env, uid, lambda s: s.save_answer(a, "display_name", "answered", "Eve", None))
    run(env, uid, lambda s: s.save_deceased(a, {"legal_first_name": "Eve"}))
    run(env, uid, lambda s: s.write_context(a, "CONVO_SUMMARY", {"text": "fake"}))
    b = draft(env, uid)
    run(env, uid, lambda s: s.request_case_deletion(b, "now"))  # a pending case confirmation
    expire(env, uid)  # read-only accounts can delete too
    run(env, uid, lambda s: s.delete_my_account())

    assert env.admin.users.count_documents({"_id": uid}) == 0
    for collection in ("consents", "trial_reminders"):
        assert env.admin[collection].count_documents({"user_id": uid}) == 0, collection
    assert env.admin.cases.count_documents({"created_by": uid}) == 0
    for collection in ("case_intake_answers", "deceased", "context_items", "case_tasks"):
        assert env.admin[collection].count_documents({"case_id": a}) == 0, collection
    assert [(r["action_type"], r["user_id"]) for r in env.admin.action_confirmation_outbox.find({"email": email})] == \
        [("account_deleted", None)]
    assert env.admin.identity_deletion_requests.count_documents({"idp_subject": {"$regex": "goodbye"}}) == 1
    assert env.admin.audit_events.count_documents({"actor_id": uid, "action": "account_deleted"}) == 1
    assert env.admin.audit_events.count_documents({"case_id": a, "action": "case_deleted_with_account"}) == 1


# ------------------------------------------------------------------ jobs (cairnJobs)

def test_purge_inactive_drafts_deletes_idle_drafts_only(env):
    uid = ready_user(env, "idle")
    old, fresh, active = draft(env, uid), draft(env, uid), started(env, uid)
    run(env, uid, lambda s: s.save_answer(old, "display_name", "answered", "Fakey", None))
    env.admin.context_items.insert_one({"case_id": old, "item_key": "CONVO_SUMMARY", "payload": {"text": "fake"},
                                        "updated_at": now(), "expires_at": None})
    trial = env.admin.users.find_one({"_id": uid})["trial_started_at"]
    env.admin.cases.update_many({"_id": {"$in": [old, active]}},
                                {"$set": {"last_activity_at": now() - timedelta(days=28)}})
    env.admin.cases.update_one({"_id": fresh}, {"$set": {"last_activity_at": now() - timedelta(days=27, hours=23)}})
    maintenance.purge_inactive_drafts(env.jobs)
    assert env.admin.cases.count_documents({"_id": old}) == 0
    assert env.admin.cases.count_documents({"_id": {"$in": [fresh, active]}}) == 2
    for collection in ("case_intake_answers", "context_items"):
        assert env.admin[collection].count_documents({"case_id": old}) == 0
    assert env.admin.users.find_one({"_id": uid})["trial_started_at"] == trial
    assert env.admin.audit_events.count_documents({"case_id": old, "action": "draft_case_expired", "actor_id": None,
                                                   "object_type": "user", "object_id": uid}) == 1
    # An answer is activity: it moves the deletion date out again.
    env.admin.cases.update_one({"_id": fresh}, {"$set": {"last_activity_at": now() - timedelta(days=30)}})
    run(env, uid, lambda s: s.save_answer(fresh, "user_role", "unsure", None, None))
    maintenance.purge_inactive_drafts(env.jobs)
    assert env.admin.cases.count_documents({"_id": fresh}) == 1


def test_purge_expired_and_stale_accounts(env):
    uid = ready_user(env, "expired")
    cid = started(env, uid)
    env.admin.cases.update_one({"_id": cid}, {"$set": {"purge_after": now() - timedelta(days=1)}})
    assert maintenance.purge_expired_cases(env.jobs) >= 1
    assert env.admin.cases.count_documents({"_id": cid}) == 0
    assert env.admin.audit_events.count_documents({"case_id": cid, "action": "case_purged"}) == 1
    assert env.admin.audit_events.count_documents({"case_id": cid, "action": "journey_started"}) == 1  # kept

    carol = new_user(env, "carol", method="apple")
    maintenance.purge_stale_accounts(env.jobs, timedelta(days=1))
    assert env.admin.users.count_documents({"_id": carol}) == 1
    env.admin.users.update_one({"_id": carol}, {"$set": {"created_at": now() - timedelta(days=2)}})
    maintenance.purge_stale_accounts(env.jobs, timedelta(days=1))
    assert env.admin.users.count_documents({"_id": carol}) == 0
    assert env.admin.identity_deletion_requests.count_documents({"idp_subject": {"$regex": "carol"},
                                                                 "provider": "apple"}) == 1


def test_trial_reminders_go_by_email_only_when_chosen_and_once(env):
    uid = ready_user(env, "reminder")
    cid = started(env, uid)
    env.admin.trial_reminders.update_one({"user_id": uid, "kind": "trial_day_21"},
                                         {"$set": {"due_at": now() - timedelta(minutes=1)}})
    mine = lambda rows: [r for r in rows if r["email"].startswith("reminder")]  # noqa: E731
    assert mine(maintenance.claim_due_trial_reminders(env.jobs)) == []
    run(env, uid, lambda s: s.save_notification_preferences(cid, {
        "channels": ["email"], "reasons": ["due_date_upcoming"], "due_date_lead_days": 3, "inactivity_days": None,
        "frequency": "daily_max", "push_permission_granted": False}))
    assert len(mine(maintenance.claim_due_trial_reminders(env.jobs))) == 1
    assert mine(maintenance.claim_due_trial_reminders(env.jobs)) == []
    env.admin.trial_reminders.update_one({"user_id": uid, "kind": "trial_day_27"},
                                         {"$set": {"due_at": now() - timedelta(hours=49)}})
    assert mine(maintenance.claim_due_trial_reminders(env.jobs)) == []  # never sent late


def test_confirmations_claim_release_complete(env):
    for i in range(2):
        uid = ready_user(env, f"confirm{i}")
        cid = draft(env, uid)
        run(env, uid, lambda s, cid=cid: s.request_case_deletion(cid, "now"))
    mine = lambda rows: [r for r in rows if r["email"].startswith("confirm")]  # noqa: E731
    claimed = mine(maintenance.claim_action_confirmations(env.jobs, 500))
    assert len(claimed) == 2
    assert mine(maintenance.claim_action_confirmations(env.jobs, 500)) == []
    maintenance.release_action_confirmation(env.jobs, claimed[0]["confirmation_id"], "smtp_failed")
    again = mine(maintenance.claim_action_confirmations(env.jobs, 500))
    assert [r["confirmation_id"] for r in again] == [claimed[0]["confirmation_id"]]
    before = env.admin.action_confirmation_log.count_documents({})
    assert all(maintenance.complete_action_confirmation(env.jobs, r["confirmation_id"]) for r in claimed)
    assert maintenance.complete_action_confirmation(env.jobs, claimed[0]["confirmation_id"]) is False
    assert env.admin.action_confirmation_outbox.count_documents({"email": {"$regex": "^confirm"}}) == 0
    assert env.admin.action_confirmation_log.count_documents({}) == before + 2
    assert {k for r in env.admin.action_confirmation_log.find() for k in r} == {"_id", "action_type", "channel",
                                                                                "sent_at"}


def test_notifications_follow_the_choice_and_the_pace(env):
    uid = ready_user(env, "notify")
    cid = started(env, uid)
    tomorrow, day_after = date.today() + timedelta(days=1), date.today() + timedelta(days=2)
    first = add_task(env, uid, cid, "notify_banks", tomorrow)
    add_task(env, uid, cid, "notify_life_insurers", day_after)

    current = {"channels": ["email"], "reasons": ["due_date_upcoming"], "due_date_lead_days": 3,
               "inactivity_days": None, "frequency": "daily_max", "push_permission_granted": False}

    def prefs(**change):
        """Change only what is named, like an UPDATE of those fields."""
        current.update(change)
        run(env, uid, lambda s: s.save_notification_preferences(cid, dict(current)))

    def claim():
        return [r for r in maintenance.claim_due_notifications(env.jobs, 1000) if r["email"].startswith("notify")]

    def log(reason=None):
        return env.admin.notification_log.count_documents({"case_id": cid, **({"reason": reason} if reason else {})})

    prefs()
    assert [r["reason"] for r in claim()] == ["due_date_upcoming"]  # one message for the journey
    assert claim() == []                                              # daily_max
    prefs(frequency="as_it_happens")
    assert len(claim()) == 1                                          # the second task's turn
    assert claim() == []                                              # each task once
    assert log() == 2

    prefs(reasons=["inactivity"], due_date_lead_days=None, inactivity_days=3)
    assert claim() == []
    env.admin.cases.update_one({"_id": cid}, {"$set": {"last_activity_at": now() - timedelta(days=4)}})
    assert [r["reason"] for r in claim()] == ["inactivity"]
    assert claim() == []                                              # once per quiet stretch
    run(env, uid, lambda s: s.update_task(cid, first, status="in_progress"))
    assert env.admin.cases.find_one({"_id": cid})["last_activity_at"] > now() - timedelta(minutes=1)

    # Nothing while paused, set to be deleted, in_app_only, or read-only. Each check stands alone.
    env.admin.notification_log.delete_many({"case_id": cid, "reason": "inactivity"})
    env.admin.cases.update_one({"_id": cid}, {"$set": {"last_activity_at": now() - timedelta(days=10),
                                                       "tasks_paused_until": now() + timedelta(days=1)}})
    assert claim() == []
    env.admin.cases.update_one({"_id": cid}, {"$set": {"tasks_paused_until": None, "deletion_requested_at": now()}})
    assert claim() == []
    env.admin.cases.update_one({"_id": cid}, {"$set": {"deletion_requested_at": None}})
    prefs(channels=["in_app_only"], reasons=[], due_date_lead_days=None, inactivity_days=None)
    assert claim() == []
    prefs(channels=["email"], reasons=["inactivity"], inactivity_days=3)
    expire(env, uid)
    assert claim() == []
    env.admin.users.update_one({"_id": uid}, {"$set": {"status": "subscribed"}})
    assert len(claim()) == 1                                          # control: the same journey is due


# ------------------------------------------------------------------ structure

API = pathlib.Path(__file__).resolve().parents[1] / "cairn_api"
DATA_LAYER = {"store.py", "db.py", "maintenance.py", "jobs.py", "outbound.py", "identity_cleanup.py",
              "errors.py"}  # errors.py maps pymongo's error classes and touches no collection


def test_only_the_data_layer_touches_mongodb():
    """Everything else goes through store.Session, so the case boundary can't be skipped by accident."""
    for path in API.rglob("*.py"):
        if path.name in DATA_LAYER:
            continue
        source = path.read_text()
        assert not re.search(r"\b(pymongo|bson)\b", source), path.name
        assert not re.search(r"\._db\b|\._c\(|\.db\[|\bclient\[", source), path.name


def test_the_api_never_connects_as_anything_but_its_own_user():
    source = (API / "config.py").read_text() + (API / "main.py").read_text()
    assert "CAIRN_JOBS_MONGODB_URI" not in source and "CAIRN_ADMIN_MONGODB_URI" not in source
