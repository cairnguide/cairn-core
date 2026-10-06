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
JOURNEY = (2, "general")  # content/journeys/journey-selection.json, the active version


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


# Notification choices with no quiet hours, so a test never depends on the time of day it runs.
ALWAYS = {"quiet_hours_start": "00:00", "quiet_hours_end": "00:00"}


def onboard(env, uid, *, email: bool = True):
    def walk(s):
        s.record_adult_answer(True)
        for purpose, step in STEPS:
            s.add_consent(purpose, "test", "email", "verify/1")
            s.advance_onboarding(step)
        s.update_account(preferred_name="Tester")
        s.advance_onboarding("preferred_name_saved")
        s.advance_onboarding("voice_saved")
        s.save_notification_preferences({"channels": ["email", "in_app"] if email else ["in_app"], **ALWAYS})
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


def zone_at(hour: int) -> str:
    """A fixed-offset time zone where the local hour is about `hour` right now. Etc/GMT signs are inverted."""
    offset = (hour - now().hour + 12) % 24 - 12
    return f"Etc/GMT{-offset:+d}"


# ------------------------------------------------------------------ roles

def test_the_app_can_append_audit_rows_and_never_read_them(env):
    app = env.raw["cairnApp"]
    app.audit_events.insert_one({"_id": uuid.uuid4(), "actor_id": None, "case_id": None, "action": "sec_probe",
                                 "object_type": None, "object_id": None, "occurred_at": now()})
    denied(lambda: app.audit_events.find_one())
    denied(lambda: app.audit_events.update_many({}, {"$set": {"action": "tampered"}}))
    denied(lambda: app.audit_events.delete_many({}))


@pytest.mark.parametrize("collection", ["action_confirmation_outbox", "action_confirmation_log",
                                        "identity_deletion_requests", "schema_migrations", "job_locks",
                                        "safety_referral_counts"])
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
    denied(lambda: app.stripe_events.delete_many({}))  # the record of what Stripe sent stays


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
    denied(lambda: jobs.stripe_events.insert_one({"_id": "evt_forged", "type": "invoice.paid"}))
    denied(lambda: jobs.stripe_events.delete_many({}))


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
    for old in ("active_no_case", "trial_active", "read_only", "subscribed"):  # D-19: setup lifecycle only
        invalid(lambda o=old: users.update_one({"_id": uid}, {"$set": {"status": o}}))
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"access": "partial"}}))
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"subscription_status": "paid"}}))
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"billing_notice": "overdue"}}))
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"personality": "steady"}}))  # no such field
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"date_of_birth": "1990-01-01"}}))  # never an age
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"phone": "555-0100"}}))  # no phone number (D-12)
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"card_last4": "4242"}}))  # no card details (SUB-D-01)
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"break_reason": "grief"}}))  # never a reason
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"name_prefill": "Patricia"}}))  # no provider name (D-16)
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"email": "Changed@example.test"}}))  # email_lower
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"trial_started_at": now()}}))  # 28 days, together
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"trial_started_at": now(), "trial_ends_at": now()}}))
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"adult_attested": True}}))  # with its time
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"break_until": now()}}))  # only during a break
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"break_started_at": now(),
                                                             "break_until": now() - timedelta(days=1)}}))
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"check_in_case_id": uuid.uuid4()}}))  # needs a time
    invalid(lambda: users.update_one({"_id": uid}, {"$set": {"stripe_customer_id": "4242424242424242"}}))
    start = now().replace(microsecond=0)
    users.update_one({"_id": uid}, {"$set": {"trial_started_at": start, "trial_ends_at": start + timedelta(hours=672)}})
    users.update_one({"_id": uid}, {"$set": {"adult_attested": False, "adult_attested_at": start,
                                             "stripe_customer_id": "cus_TestCustomer1"}})


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
    put("place_of_death", value={"jurisdiction": "NH", "county_or_city": None, "outside_us": False})
    put("completed_items", value=["funeral_provider_chosen", "bank_insurer_or_employer_notified"])
    invalid(lambda: put("cause_of_death", value="x"))
    invalid(lambda: put("user_role", value="cousin"))
    invalid(lambda: put("residence_jurisdiction", state="skipped", value="different"))

    def change(field, **values):
        answers.update_one({"case_id": cid, "field_key": field}, {"$set": values})

    invalid(lambda: change("circumstance", value="he had cancer"))
    invalid(lambda: change("circumstance", own_words="a heart attack"))
    invalid(lambda: change("date_of_death", value={"precision": "exact", "date": None}))
    invalid(lambda: change("place_of_death", value={"jurisdiction": "NH", "county_or_city": None, "outside_us": False,
                                                    "ssn": "1"}))
    invalid(lambda: change("place_of_death", value={"jurisdiction": "NH", "county_or_city": None, "outside_us": True}))
    invalid(lambda: change("completed_items", value=["none_or_unsure", "bank_insurer_or_employer_notified"]))
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
    {"channels": ["email"]},                                   # in_app is always on (UC-REG-15)
    {"channels": ["in_app", "sms"]},                           # no text messages (D-12, OPEN-05)
    {"channels": ["in_app", "in_app"]},
    {"channels": [None]},
    {"frequency": "hourly"},
    {"due_date_lead": "two_days"},
    {"inactivity_after": "monthly"},
    {"quiet_hours_start": "9pm"},
    {"browser_push_endpoint": "https://push.example.test/sub"},  # only with browser chosen
    {"channels": ["in_app", "browser"], "browser_push_endpoint": "http://push.example.test/sub"},
    {"phone": "555-0100"},
    {"reasons": ["inactivity"]},                               # per-journey fields are gone (account D-13)
])
def test_notification_choice_validator(env, change):
    uid = ready_user(env, "val-prefs")
    invalid(lambda: env.admin.notification_preferences.update_one({"_id": uid}, {"$set": change}))


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
    uid = run(env, None, lambda s: s.create_account(subject, f"{subject[4:]}@example.test", "apple"))
    assert run(env, None, lambda s: s.resolve_user(subject)) == uid
    assert run(env, None, lambda s: s.create_account(subject, f"{subject[4:]}@example.test", "apple")) == uid
    a = run(env, uid, lambda s: s.load_account())
    assert (a["status"], a["access"], a["subscription_status"], a["onboarding_step"], a["trial_started_at"],
            a["voice"], a["name_prefill"], a["adult_attested"]) == \
        ("pending_onboarding", "full", "none", "account_created", None, "steady_direct", None, None)
    refuses(env, uid, lambda s: s.create_draft())  # no case before onboarding
    refuses(env, uid, lambda s: s.advance_onboarding("age_confirmed"), "invalid_value")
    refuses(env, uid, lambda s: s.advance_onboarding("bogus"), "invalid_value")
    refuses(env, uid, lambda s: s.advance_onboarding("adult_confirmed"), "out_of_order")         # no yes yet
    refuses(env, uid, lambda s: s.advance_onboarding("privacy_terms_accepted"), "out_of_order")  # skipping ahead
    run(env, uid, lambda s: s.record_adult_answer(True))  # UC-REG-06, a yes moves past the question
    assert run(env, uid, lambda s: s.load_account())["onboarding_step"] == "adult_confirmed"
    refuses(env, uid, lambda s: s.advance_onboarding("privacy_terms_accepted"), "out_of_order")  # no consent yet
    run(env, uid, lambda s: s.add_consent("privacy_terms", "test", "apple", None))
    assert run(env, uid, lambda s: s.advance_onboarding("privacy_terms_accepted")) == "privacy_terms_accepted"
    assert run(env, uid, lambda s: s.advance_onboarding("privacy_terms_accepted")) == "privacy_terms_accepted"
    refuses(env, uid, lambda s: s.advance_onboarding("ai_notice_accepted"), "out_of_order")
    # The app sets only Settings fields, never onboarding, status, access, the subscription, or the trial.
    for field, value in (("onboarding_step", "complete"), ("status", "setup_complete"), ("access", "full"),
                         ("subscription_status", "active"), ("trial_started_at", None), ("adult_attested", True),
                         ("break_started_at", now()), ("check_in_at", now()), ("email", "x@example.test")):
        refuses(env, uid, lambda s, f=field, v=value: s.update_account(**{f: v}))
    refuses(env, uid, lambda s: s.update_subscription(trial_ends_at=now()))
    run(env, uid, lambda s: s.update_account(voice="brisk_businesslike"))
    refuses(env, uid, lambda s: s.update_account(voice="gentle"), "invalid_value")
    onboard_rest(env, uid)
    a = run(env, uid, lambda s: s.load_account())
    assert (a["status"], a["onboarding_step"], a["trial_started_at"], a["name_prefill"]) == \
        ("setup_complete", "complete", None, None)


def test_an_adult_no_stops_onboarding_and_collects_nothing_more(env):
    """UC-REG-06. A no is stored as false with its time, never an age. Onboarding stops there for good."""
    uid = new_user(env, "minor")
    run(env, uid, lambda s: s.record_adult_answer(False))
    a = run(env, uid, lambda s: s.load_account())
    assert (a["adult_attested"], a["onboarding_step"], a["status"]) == (False, "account_created", "pending_onboarding")
    assert a["adult_attested_at"] is not None
    assert run(env, uid, lambda s: s.record_adult_answer(True)) is False  # not changed here (OPEN-09)
    refuses(env, uid, lambda s: s.advance_onboarding("adult_confirmed"), "out_of_order")
    refuses(env, uid, lambda s: s.create_draft())
    stored = env.admin.users.find_one({"_id": uid})
    assert not {k for k in stored if "age" in k or "birth" in k}


def onboard_rest(env, uid):
    def walk(s):
        for purpose, step in STEPS[1:]:
            s.add_consent(purpose, "test", "apple", None)
            s.advance_onboarding(step)
        s.update_account(preferred_name="Tester")
        s.advance_onboarding("preferred_name_saved")
        s.advance_onboarding("voice_saved")
    run(env, uid, walk)
    refuses(env, uid, lambda s: s.advance_onboarding("complete"), "out_of_order")  # notification choices first
    run(env, uid, lambda s: s.save_notification_preferences({"channels": ["in_app"]}))
    run(env, uid, lambda s: s.advance_onboarding("complete"))


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
    started = (case["status"], case["journey_template_key"], case["journey_template_version"])
    assert started == ("active", *JOURNEY[::-1])
    assert case["journey_started_at"] == user["trial_started_at"] and case["journey_started_on"]
    assert (user["status"], user["access"]) == ("setup_complete", "full")  # D-19: the trial isn't a status
    assert user["trial_ends_at"] - user["trial_started_at"] == timedelta(hours=672)
    due = {r["kind"]: r["due_at"] - user["trial_started_at"] for r in env.admin.trial_reminders.find({"user_id": uid})}
    assert due == {"trial_ends_soon": timedelta(hours=672 - 168)}  # D-14: one note, a week before
    refuses(env, uid, lambda s: s.start_journey(cid, *JOURNEY), "out_of_order")
    assert env.admin.audit_events.count_documents({"case_id": cid, "action": "journey_started"}) == 1

    second = draft(env, uid)
    assert run(env, uid, lambda s: s.start_journey(second, *JOURNEY)) is False  # DEC-04
    assert env.admin.users.find_one({"_id": uid})["trial_started_at"] == user["trial_started_at"]
    assert env.admin.trial_reminders.count_documents({"user_id": uid}) == 1


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
    run(env, alice, lambda s: s.write_context(cid, "CERT_ORDER", {"copies": 3}))
    tid = add_task(env, alice, cid)

    assert run(env, bob, lambda s: s.load_case(cid)) is None
    assert run(env, bob, lambda s: s.load_answers(cid)) == {}
    assert run(env, bob, lambda s: s.load_tasks(cid)) == []
    assert run(env, bob, lambda s: s.load_task(cid, tid)) is None
    assert run(env, bob, lambda s: s.notification_preferences())["_id"] == bob  # each account has only its own
    assert run(env, bob, lambda s: s.read_context(cid, "CERT_ORDER")) is None
    assert run(env, bob, lambda s: s.owned_cases()) == []
    assert run(env, bob, lambda s: s.is_case_member(cid)) is False
    for read in (lambda s: s.load_deceased(cid), lambda s: s.context_items(cid), lambda s: s.notifications_sent(cid),
                 lambda s: s.task_states(cid)):
        refuses(env, bob, read)

    for write in (lambda s: s.save_answer(cid, "user_role", "skipped", None, None),
                  lambda s: s.save_deceased(cid, {"legal_first_name": "Hacked"}),
                  lambda s: s.begin_rest(cid, now() + timedelta(days=1), care=True),
                  lambda s: s.end_rest(cid),
                  lambda s: s.set_check_in(now(), cid),
                  lambda s: s.start_journey(cid, *JOURNEY),
                  lambda s: s.add_task(cid, template(env), "not_started", None),
                  lambda s: s.request_case_deletion(cid, "now"),
                  lambda s: s.cancel_case_deletion(cid),
                  lambda s: s.write_context(cid, "CERT_ORDER", {}),
                  lambda s: s.lock_context(cid, "BANK_NOTICES", {})):
        refuses(env, bob, write)
    assert run(env, bob, lambda s: s.update_task(cid, tid, status="skipped")) is False
    before = env.admin.cases.find_one({"_id": cid})["last_activity_at"]
    run(env, bob, lambda s: s.touch(cid))
    assert env.admin.cases.find_one({"_id": cid})["last_activity_at"] == before
    run(env, bob, lambda s: s.begin_break(None, care=True))  # Bob's break is Bob's alone (BRK-D-06)
    assert env.admin.cases.find_one({"_id": cid})["tasks_paused_until"] is None
    assert env.admin.users.find_one({"_id": alice})["break_started_at"] is None

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
    a = run(env, uid, lambda s: s.load_account())
    assert (a["status"], a["access"]) == ("setup_complete", "read_only")
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
    # Always free: Settings, notification choices, a break, and deleting (D-2026-09-25-F1).
    run(env, uid, lambda s: s.update_account(voice="warm_patient"))
    run(env, uid, lambda s: s.save_notification_preferences({"frequency": "weekly", "inactivity_after": "one_week"}))
    run(env, uid, lambda s: s.begin_break(now() + timedelta(days=3), care=False))
    run(env, uid, lambda s: s.end_break())
    assert run(env, uid, lambda s: s.request_case_deletion(second, "hold")) is not None
    assert run(env, uid, lambda s: s.request_case_deletion(second, "now")) is None
    assert run(env, uid, lambda s: s.request_case_deletion(d, "now")) is None
    assert len(run(env, uid, lambda s: s.owned_cases())) == 1

    maintenance.expire_trials(env.jobs)
    assert env.admin.users.find_one({"_id": uid})["access"] == "read_only"  # kept current for reporting
    env.admin.users.update_one({"_id": uid}, {"$set": {"subscription_status": "active"}})
    assert run(env, uid, lambda s: s.account_can_write()) is True
    maintenance.expire_trials(env.jobs)
    assert env.admin.users.find_one({"_id": uid})["access"] == "full"  # a subscription keeps access full
    env.admin.users.update_one({"_id": uid}, {"$set": {"subscription_status": "lapsed"}})
    assert run(env, uid, lambda s: s.account_can_write()) is False


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
    run(env, uid, lambda s: s.save_deceased(gone, {"legal_first_name": "Gus"}))
    run(env, uid, lambda s: s.set_check_in(now() + timedelta(days=1), gone))
    add_task(env, uid, gone)
    assert run(env, uid, lambda s: s.request_case_deletion(gone, "now")) is None
    for collection in ("case_intake_answers", "case_tasks", "deceased", "context_items", "notification_log"):
        assert env.admin[collection].count_documents({"case_id": gone}) == 0, collection
    assert env.admin.users.find_one({"_id": uid})["check_in_at"] is None  # cancelled with its case
    assert env.admin.notification_preferences.count_documents({"_id": uid}) == 1  # the account's, not the case's
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
    run(env, uid, lambda s: s.set_check_in(now() + timedelta(days=1), a))
    run(env, uid, lambda s: s.save_notification_preferences({
        "channels": ["email", "in_app", "browser"], "browser_push_endpoint": "https://push.example.test/sub/1"}))
    b = draft(env, uid)
    run(env, uid, lambda s: s.request_case_deletion(b, "now"))  # a pending case confirmation
    expire(env, uid)  # read-only accounts can delete too
    run(env, uid, lambda s: s.delete_my_account())

    assert env.admin.users.count_documents({"_id": uid}) == 0
    for collection in ("consents", "trial_reminders"):
        assert env.admin[collection].count_documents({"user_id": uid}) == 0, collection
    assert env.admin.notification_preferences.count_documents({"_id": uid}) == 0  # the push endpoint with it
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


def test_the_trial_note_is_a_service_notice_sent_once_and_held_during_a_break(env):
    """Account D-14: a week before, to the sign-in email whatever the reminder choices, and once. During a break it
    shows only in Cairn (UC-BRK-08). Never sent late with a date that no longer fits."""
    uid = new_user(env, "reminder")
    onboard(env, uid, email=False)  # no email chosen for reminders
    started(env, uid)
    env.admin.trial_reminders.update_one({"user_id": uid}, {"$set": {"due_at": now() - timedelta(minutes=1)}})
    mine = lambda rows: [r for r in rows if r["email"].startswith("reminder")]  # noqa: E731
    run(env, uid, lambda s: s.begin_break(now() + timedelta(days=3), care=False))
    assert mine(maintenance.claim_due_trial_reminders(env.jobs)) == []
    assert env.admin.trial_reminders.find_one({"user_id": uid})["skipped_at"] is not None  # in Cairn only
    run(env, uid, lambda s: s.end_break())
    assert mine(maintenance.claim_due_trial_reminders(env.jobs)) == []  # never emailed late after a break

    other = new_user(env, "reminder2")
    onboard(env, other, email=False)
    started(env, other)
    env.admin.trial_reminders.update_one({"user_id": other}, {"$set": {"due_at": now() - timedelta(minutes=1)}})
    rows = [r for r in maintenance.claim_due_trial_reminders(env.jobs) if r["email"].startswith("reminder2")]
    assert len(rows) == 1 and rows[0]["subscription_status"] == "none"
    assert [r for r in maintenance.claim_due_trial_reminders(env.jobs) if r["email"].startswith("reminder2")] == []
    env.admin.trial_reminders.update_one({"user_id": other}, {"$set": {"email_sent_at": None,
                                                                       "due_at": now() - timedelta(hours=49)}})
    assert [r for r in maintenance.claim_due_trial_reminders(env.jobs) if r["email"].startswith("reminder2")] == []


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

    def prefs(**change):
        run(env, uid, lambda s: s.save_notification_preferences(change))

    def claim():
        return [r for r in maintenance.claim_due_notifications(env.jobs, 1000) if r["email"].startswith("notify")]

    def log(reason=None):
        return env.admin.notification_log.count_documents({"case_id": cid, **({"reason": reason} if reason else {})})

    prefs(frequency="daily", due_date_lead="three_days")
    assert [(r["reason"], r["channels"]) for r in claim()] == [("due_date_upcoming", ["email"])]  # one at a time
    assert claim() == []                                              # daily
    prefs(frequency="due_only")
    assert len(claim()) == 1                                          # the second task's turn
    assert claim() == []                                              # each task once
    assert log() == 2

    prefs(due_date_lead="day_before", inactivity_after="three_days")
    assert claim() == []
    env.admin.cases.update_one({"_id": cid}, {"$set": {"last_activity_at": now() - timedelta(days=4)}})
    assert [r["reason"] for r in claim()] == ["inactivity"]
    assert claim() == []                                              # once per quiet stretch
    run(env, uid, lambda s: s.update_task(cid, first, status="in_progress"))
    assert env.admin.cases.find_one({"_id": cid})["last_activity_at"] > now() - timedelta(minutes=1)

    # Nothing during a break, set to be deleted, with no reminders, in-app only, in quiet hours, or read-only.
    env.admin.notification_log.delete_many({"case_id": cid, "reason": "inactivity"})
    env.admin.cases.update_one({"_id": cid}, {"$set": {"last_activity_at": now() - timedelta(days=10)}})
    run(env, uid, lambda s: s.begin_break(now() + timedelta(days=1), care=False))
    assert claim() == []
    run(env, uid, lambda s: s.end_break())
    env.admin.cases.update_one({"_id": cid}, {"$set": {"last_activity_at": now() - timedelta(days=10),
                                                       "deletion_requested_at": now()}})
    assert claim() == []
    env.admin.cases.update_one({"_id": cid}, {"$set": {"deletion_requested_at": None}})
    prefs(frequency="none")
    assert claim() == []
    prefs(frequency="due_only", channels=["in_app"])
    assert claim() == []
    prefs(channels=["email", "in_app"], quiet_hours_start="00:00", quiet_hours_end="23:59")
    assert claim() == []
    prefs(**ALWAYS)
    expire(env, uid)
    assert claim() == []
    env.admin.users.update_one({"_id": uid}, {"$set": {"subscription_status": "active"}})
    assert len(claim()) == 1                                          # control: the same journey is due


# ------------------------------------------------------------------ breaks, check-ins, and notices

def test_a_break_covers_every_journey_and_never_touches_the_subscription(env):
    """BRK-D-06, BRK-D-08, DEC-26-01, AC-26-04, AC-BRK-05, AC-BRK-09."""
    uid = ready_user(env, "rest")
    one, two, d = started(env, uid), started(env, uid), draft(env, uid)
    env.admin.users.update_one({"_id": uid}, {"$set": {"subscription_status": "active",
                                                       "stripe_subscription_id": "sub_TestRest1",
                                                       "current_period_end": now() + timedelta(days=20)}})
    before = env.admin.users.find_one({"_id": uid})
    sub_fields = ("subscription_status", "stripe_subscription_id", "current_period_end", "cancel_at_period_end",
                  "billing_notice", "access")

    # A normal rest keeps the free days counting.
    until = now() + timedelta(days=3)
    assert run(env, uid, lambda s: s.begin_break(until, care=False)) is False
    cases = {c["_id"]: c for c in env.admin.cases.find({"created_by": uid})}
    assert cases[one]["tasks_paused_until"] == cases[two]["tasks_paused_until"]
    assert cases[one]["tasks_paused_until"] is not None and cases[d]["tasks_paused_until"] is None
    u = env.admin.users.find_one({"_id": uid})
    assert u["trial_clock_paused_at"] is None and u["trial_ends_at"] == before["trial_ends_at"]
    assert {f: u[f] for f in sub_fields} == {f: before[f] for f in sub_fields}
    assert run(env, uid, lambda s: s.end_break()) is None

    # A care rest stops the free days while they run, and moves their end by exactly the rest.
    assert run(env, uid, lambda s: s.begin_break(None, care=True)) is True
    paused_at = env.admin.users.find_one({"_id": uid})["trial_clock_paused_at"]
    env.admin.users.update_one({"_id": uid}, {"$set": {"trial_clock_paused_at": paused_at - timedelta(days=2),
                                                       "break_started_at": paused_at - timedelta(days=2)}})
    paused = run(env, uid, lambda s: s.end_break())
    u = env.admin.users.find_one({"_id": uid})
    assert u["trial_ends_at"] - before["trial_ends_at"] == paused and paused >= timedelta(days=2)
    assert {f: u[f] for f in sub_fields} == {f: before[f] for f in sub_fields}
    assert u["break_started_at"] is None and all(
        c["tasks_paused_until"] is None for c in env.admin.cases.find({"created_by": uid}))

    # Free days that have ended: a care rest changes nothing about billing (BRK-D-08).
    expire(env, uid, days=40)
    assert run(env, uid, lambda s: s.begin_break(None, care=True)) is False
    assert env.admin.users.find_one({"_id": uid})["trial_clock_paused_at"] is None


def test_a_check_in_is_on_the_account_and_goes_once_through_the_chosen_channels(env):
    """DEC-26-04, AC-26-08, AC-26-12."""
    setup = new_user(env, "checkin-setup")  # during setup: no case, no channels yet
    # Quiet hours are 9 PM to 8 AM in the user's time zone. Pick one where it is midday now.
    env.admin.users.update_one({"_id": setup}, {"$set": {"time_zone": zone_at(12)}})
    run(env, setup, lambda s: s.set_check_in(now() - timedelta(minutes=1), None, by_email=True))
    u = env.admin.users.find_one({"_id": setup})
    assert u["check_in_case_id"] is None
    mine = lambda rows, tag: [r for r in rows if r["email"].startswith(tag)]  # noqa: E731
    rows = mine(maintenance.claim_due_check_ins(env.jobs), "checkin-setup")
    assert [r["channels"] for r in rows] == [["email"]]
    assert mine(maintenance.claim_due_check_ins(env.jobs), "checkin-setup") == []  # exactly once
    assert run(env, setup, lambda s: s.take_due_check_in()) is True                 # and once in Cairn
    assert run(env, setup, lambda s: s.take_due_check_in()) is False

    night = new_user(env, "checkin-night")  # the same check-in during quiet hours waits for the morning
    env.admin.users.update_one({"_id": night}, {"$set": {"time_zone": zone_at(23)}})
    run(env, night, lambda s: s.set_check_in(now() - timedelta(minutes=1), None, by_email=True))
    assert mine(maintenance.claim_due_check_ins(env.jobs), "checkin-night") == []

    quiet = ready_user(env, "checkin-quiet")
    run(env, quiet, lambda s: s.save_notification_preferences({"channels": ["in_app"], "frequency": "none"}))
    run(env, quiet, lambda s: s.set_check_in(now() - timedelta(minutes=1)))
    assert mine(maintenance.claim_due_check_ins(env.jobs), "checkin-quiet") == []  # in-app only: shown in Cairn
    assert run(env, quiet, lambda s: s.take_due_check_in()) is True


def test_the_break_ending_notice_goes_once_by_the_chosen_channels(env):
    """UC-BRK-09, AC-BRK-04, AC-BRK-08."""
    uid = ready_user(env, "brknotice")
    started(env, uid)
    until = now() + timedelta(days=3)
    run(env, uid, lambda s: s.begin_break(until, care=False, notice_at=now() - timedelta(minutes=1)))
    mine = lambda rows: [r for r in rows if r["email"].startswith("brknotice")]  # noqa: E731
    assert [r["channels"] for r in mine(maintenance.claim_break_notices(env.jobs))] == [["email"]]
    assert mine(maintenance.claim_break_notices(env.jobs)) == []
    # A changed break never sends a second notice (UC-BRK-11).
    run(env, uid, lambda s: s.begin_break(until + timedelta(days=4), care=False, notice_at=now()))
    assert env.admin.users.find_one({"_id": uid})["break_notice_sent_at"] is not None
    assert mine(maintenance.claim_break_notices(env.jobs)) == []
    run(env, uid, lambda s: s.end_break())
    # A care rest schedules none (BRK-D-03). Deleting the last journey cancels one (UC-BRK-09).
    run(env, uid, lambda s: s.begin_break(until, care=True))
    assert env.admin.users.find_one({"_id": uid})["break_notice_at"] is None


def test_stripe_events_are_recorded_once_with_ids_only(env):
    """UC-SUB-22. A repeat delivery changes nothing, and the record keeps ids, type, and times only."""
    uid = ready_user(env, "stripe")
    eid = f"evt_{uuid.uuid4().hex[:16]}"
    assert run(env, None, lambda s: s.record_stripe_event(eid, "invoice.paid", uid)) is True
    assert run(env, None, lambda s: s.record_stripe_event(eid, "invoice.paid", uid)) is True  # not finished yet
    run(env, None, lambda s: s.finish_stripe_event(eid, processed=True))
    assert run(env, None, lambda s: s.record_stripe_event(eid, "invoice.paid", uid)) is False
    row = env.admin.stripe_events.find_one({"_id": eid})
    assert set(row) == {"_id", "type", "account_id", "received_at", "processed_at", "attempts"}
    invalid(lambda: env.admin.stripe_events.update_one({"_id": eid}, {"$set": {"payload": {"amount": 1499}}}))
    refuses(env, None, lambda s: s.apply_stripe_state(uid, {"trial_ends_at": now()}))
    refuses(env, None, lambda s: s.apply_stripe_state(uid, {"break_started_at": now()}))
    assert run(env, None, lambda s: s.stripe_account(customer_id="cus_NobodyHere")) is None


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


def test_a_care_rest_that_ends_on_its_own_starts_the_free_days_again(env):
    """DEC-26-01 and AC-26-04, for a user who hasn't come back: the job moves trial_ends_at and the unsent note by
    exactly the rest, ended at its chosen end, and never while the break is still running."""
    uid = ready_user(env, "settle")
    started(env, uid)
    until = now() + timedelta(days=3)
    run(env, uid, lambda s: s.begin_break(until, care=True))
    before = env.admin.users.find_one({"_id": uid})
    note_before = env.admin.trial_reminders.find_one({"user_id": uid})["due_at"]
    maintenance.settle_trial_clocks(env.jobs)
    assert env.admin.users.find_one({"_id": uid})["trial_clock_paused_at"] is not None  # still resting
    paused_at = before["trial_clock_paused_at"]
    env.admin.users.update_one({"_id": uid}, {"$set": {"break_started_at": paused_at - timedelta(days=5),
                                                       "break_until": paused_at + timedelta(days=2),
                                                       "trial_clock_paused_at": paused_at - timedelta(days=5)}})
    # The rest ran from 5 days before paused_at to 2 days after it: 7 days, now over.
    env.admin.users.update_one({"_id": uid}, {"$set": {"break_until": now() - timedelta(minutes=1)}})
    rest = (now() - timedelta(minutes=1)) - (paused_at - timedelta(days=5))
    assert maintenance.settle_trial_clocks(env.jobs) >= 1
    after = env.admin.users.find_one({"_id": uid})
    assert after["trial_clock_paused_at"] is None
    assert abs((after["trial_ends_at"] - before["trial_ends_at"]) - rest) < timedelta(seconds=5)
    assert env.admin.trial_reminders.find_one({"user_id": uid})["due_at"] - note_before == \
        after["trial_ends_at"] - before["trial_ends_at"]


def test_one_notification_sender_at_a_time(env):
    """The job lock: a second run while one holds it picks nothing."""
    held = now()
    assert maintenance._take_lock(env.jobs, "test_lock", held, timedelta(minutes=5)) is True
    assert maintenance._take_lock(env.jobs, "test_lock", held, timedelta(minutes=5)) is False
    maintenance._release_lock(env.jobs, "test_lock", held)
    assert maintenance._take_lock(env.jobs, "test_lock", held, timedelta(minutes=5)) is True


def test_a_cancellation_stripe_still_refuses_is_counted_and_kept(env):
    """UC-SUB-13: the request time stays as proof, and the job tries again next run."""
    uid = ready_user(env, "cancelretry")
    env.admin.users.update_one({"_id": uid}, {"$set": {"subscription_status": "active",
                                                       "stripe_subscription_id": "sub_RetryLater1",
                                                       "cancel_requested_at": now()}})

    class Down:
        def set_cancel_at_period_end(self, *a):
            from cairn_api.stripe_client import StripeError
            raise StripeError("api_connection_error")

    done, failed = maintenance.retry_cancellations(env.jobs, Down())
    assert failed >= 1
    u = env.admin.users.find_one({"_id": uid})
    assert u["cancel_requested_at"] is not None and u["cancel_at_period_end"] is False


def test_the_v3_migration_reshapes_old_accounts_without_losing_anything(env):
    """database/db/apply.py use_cases_v3: one account state model (D-19), one notification choice per account from
    the newest journey choice (D-13), the check-in on the account, one trial note a week before (D-14), and no
    provider name. Every migrated document passes the v3 validators."""
    from .conftest import _import_database_tools
    db_apply = _import_database_tools()[0]
    admin = env.admin
    uid = ready_user(env, "migrate")
    cid, other = started(env, uid), started(env, uid)
    user = admin.users.find_one({"_id": uid})
    start = user["trial_started_at"]
    # Back to the shape every account had before v3.
    old_user = {k: user[k] for k in ("_id", "idp_subject", "email", "email_lower", "sign_in_method", "preferred_name",
                                     "name_pronunciation", "voice", "time_zone", "onboarding_step", "trial_started_at",
                                     "trial_ends_at", "trial_clock_paused_at", "ai_reminder_shown_at",
                                     "ai_reminder_shown_on", "created_at")}
    admin.users.replace_one({"_id": uid}, {**old_user, "status": "trial_active", "name_prefill": "Provider Name"},
                            bypass_document_validation=True)
    check_in = (now() + timedelta(hours=5)).replace(microsecond=0)
    admin.cases.update_one({"_id": cid}, {"$set": {"check_in_at": check_in}}, bypass_document_validation=True)
    admin.cases.update_one({"_id": other}, {"$set": {"check_in_at": None}}, bypass_document_validation=True)
    admin.notification_preferences.delete_one({"_id": uid})
    for case_id, updated, channels in ((cid, start, ["in_app_only"]), (other, start + timedelta(days=1), ["email"])):
        admin.notification_preferences.insert_one({
            "_id": case_id, "channels": channels, "reasons": [] if channels == ["in_app_only"] else ["inactivity"],
            "due_date_lead_days": None, "inactivity_days": None if channels == ["in_app_only"] else 7,
            "frequency": "weekly_max", "push_permission_granted": False, "updated_at": updated},
            bypass_document_validation=True)
    admin.trial_reminders.delete_many({"user_id": uid})
    for kind, due in (("trial_day_21", start + timedelta(days=21)), ("trial_ends_soon", start + timedelta(days=25))):
        admin.trial_reminders.insert_one({"_id": uuid.uuid4(), "user_id": uid, "kind": kind, "due_at": due,
                                          "email_sent_at": None}, bypass_document_validation=True)

    db_apply.use_cases_v3(admin)

    u = admin.users.find_one({"_id": uid})
    assert (u["status"], u["access"], u["subscription_status"]) == ("setup_complete", "full", "none")
    assert u["name_prefill"] is None and u["adult_attested"] is None
    assert (u["check_in_at"], u["check_in_case_id"]) == (check_in, cid)
    assert admin.cases.count_documents({"_id": {"$in": [cid, other]}, "check_in_at": {"$exists": True}}) == 0
    prefs = admin.notification_preferences.find_one({"_id": uid})
    assert (prefs["channels"], prefs["frequency"], prefs["inactivity_after"]) == (["email", "in_app"], "weekly",
                                                                                 "one_week")  # the newest choice
    assert admin.notification_preferences.count_documents({"_id": {"$in": [cid, other]}}) == 0
    reminders = list(admin.trial_reminders.find({"user_id": uid}))
    assert [(r["kind"], r["due_at"]) for r in reminders] == [("trial_ends_soon", start + timedelta(days=21))]
    # Every reshaped document is valid under the v3 validators.
    for coll, key in (("users", uid), ("notification_preferences", uid), ("cases", cid)):
        doc = admin[coll].find_one({"_id": key})
        admin[coll].replace_one({"_id": key}, doc)
    for r in reminders:
        admin.trial_reminders.replace_one({"_id": r["_id"]}, r)
    run(env, uid, lambda s: s.delete_my_account())
