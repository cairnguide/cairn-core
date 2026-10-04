"""Scheduled work that runs as the cairnJobs user, never as the API.

These were owner-only PostgreSQL functions until 2026-10-01. The API's cairnApp
role can't do any of this: it can't read the outbox, the identity cleanup
queue, or other people's cases. Each function takes the Cairn Database
(pymongo) and returns a count or the rows claimed. Each case or account it
deletes goes in its own transaction, so one failure never leaves half a case.

Nothing here logs a value. Callers log counts and error codes only.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Callable

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .store import (
    OPEN_TASK_STATUSES,
    audit_doc,
    delete_case,
    effective_account_status,
    from_date,
    local_date,
    queue_confirmation,
    setting_int,
    utcnow,
)


def _in_transaction(db, fn: Callable) -> object:
    with db.client.start_session() as cs:
        return cs.with_transaction(fn)


# ------------------------------------------------------------------ deleting

def purge_held_cases(db) -> int:
    """Deletes cases whose 7-day hold has ended and queues their confirmation (UC-END-13). At least hourly."""
    now = utcnow()
    cutoff = now - timedelta(days=setting_int(db, None, "case_deletion_hold_days"))
    n = 0
    for case in db.cases.find({"deletion_requested_at": {"$ne": None, "$lte": cutoff}}, {"created_by": 1}):
        def run(cs, case=case):
            if not db.cases.find_one({"_id": case["_id"], "deletion_requested_at": {"$ne": None, "$lte": cutoff}},
                                     session=cs):
                return 0  # cancelled since the scan
            delete_case(db, cs, case["_id"])
            db.audit_events.insert_one(audit_doc(None, "case_deleted_after_hold", case["_id"], "user",
                                                 case["created_by"], now=now), session=cs)
            owner = db.users.find_one({"_id": case["created_by"]}, {"email": 1}, session=cs)
            if owner:
                queue_confirmation(db, cs, "case_deleted_after_hold", owner["email"], owner["_id"], now)
            return 1
        n += _in_transaction(db, run)
    return n


def purge_inactive_drafts(db) -> int:
    """Hard-deletes drafts idle for draft_retention_days (DEC-07), with their answers and conversation text.
    Never touches active cases, accounts, or trial fields. The audit row holds ids only. At least daily."""
    now = utcnow()
    cutoff = now - timedelta(days=setting_int(db, None, "draft_retention_days"))
    n = 0
    for case in db.cases.find({"status": "draft", "last_activity_at": {"$lte": cutoff}}, {"created_by": 1}):
        def run(cs, case=case):
            if not db.cases.find_one({"_id": case["_id"], "status": "draft", "last_activity_at": {"$lte": cutoff}},
                                     session=cs):
                return 0  # opened or answered since the scan
            delete_case(db, cs, case["_id"])
            db.audit_events.insert_one(audit_doc(None, "draft_case_expired", case["_id"], "user",
                                                 case["created_by"], now=now), session=cs)
            return 1
        n += _in_transaction(db, run)
    return n


def purge_expired_cases(db) -> int:
    """Retention: deletes cases past purge_after. Their data goes with them. Audit rows stay."""
    now = utcnow()
    n = 0
    for case in db.cases.find({"purge_after": {"$ne": None, "$lt": now}}, {"_id": 1}):
        def run(cs, case=case):
            if not delete_case(db, cs, case["_id"]):
                return 0
            db.audit_events.insert_one(audit_doc(None, "case_purged", case["_id"], "case", case["_id"], now=now),
                                       session=cs)
            return 1
        n += _in_transaction(db, run)
    return n


def purge_stale_accounts(db, pending_older_than: timedelta, no_case_older_than: timedelta | None = None) -> int:
    """Deletes accounts that stopped before finishing onboarding (UC-REG-10), and optionally accounts that
    finished but never created a case (UC-REG-13). The periods come from the retention schedule and have
    no defaults on purpose. [LEGAL REVIEW REQUIRED]"""
    now = utcnow()
    who = [{"status": "pending_onboarding", "created_at": {"$lt": now - pending_older_than}}]
    if no_case_older_than is not None:
        who.append({"status": "active_no_case", "created_at": {"$lt": now - no_case_older_than}})
    n = 0
    for user in db.users.find({"$or": who}, {"idp_subject": 1, "sign_in_method": 1}):
        def run(cs, user=user):
            if db.cases.find_one({"$or": [{"created_by": user["_id"]}, {"members.user_id": user["_id"]}]},
                                 {"_id": 1}, session=cs):
                return 0
            db.identity_deletion_requests.insert_one(
                {"_id": uuid.uuid4(), "idp_subject": user["idp_subject"], "provider": user.get("sign_in_method"),
                 "requested_at": now, "attempts": 0, "last_error": None}, session=cs)
            db.consents.delete_many({"user_id": user["_id"]}, session=cs)
            db.trial_reminders.delete_many({"user_id": user["_id"]}, session=cs)
            db.users.delete_one({"_id": user["_id"]}, session=cs)
            db.audit_events.insert_one(audit_doc(None, "stale_account_purged", None, "user", user["_id"], now=now),
                                       session=cs)
            return 1
        n += _in_transaction(db, run)
    return n


def expire_trials(db) -> int:
    """Marks trials that have ended as read_only, for reporting. Enforcement doesn't wait for this:
    store.effective_account_status compares against trial_ends_at. A trial stopped by a care rest never
    expires while it is stopped (DEC-26-01)."""
    settle_trial_clocks(db)
    return db.users.update_many({"status": "trial_active", "trial_ends_at": {"$lte": utcnow()},
                                 "trial_clock_paused_at": None},
                                {"$set": {"status": "read_only"}}).modified_count


def settle_trial_clocks(db) -> int:
    """Starts the free days again for care rests that ended on their own (the rest period the user chose ran
    out), and moves trial_ends_at and unsent reminders later by exactly the paused time. The same rule as
    store.Session.settle_trial_clock, for users who haven't come back yet."""
    now = utcnow()
    n = 0
    for u in db.users.find({"trial_clock_paused_at": {"$ne": None}}, {"trial_clock_paused_at": 1,
                                                                      "trial_ends_at": 1}):
        def run(cs, u=u):
            rests = [c["tasks_paused_until"] for c in db.cases.find(
                {"created_by": u["_id"], "status": {"$ne": "draft"}, "tasks_paused_until": {"$ne": None}},
                {"tasks_paused_until": 1}, session=cs)]
            if any(r > now for r in rests):
                return 0
            paused_at = u["trial_clock_paused_at"]
            ended = max([r for r in rests if r >= paused_at], default=now)
            paused = max(ended - paused_at, timedelta(0))
            if not db.users.update_one({"_id": u["_id"], "trial_clock_paused_at": paused_at},
                                       {"$set": {"trial_clock_paused_at": None,
                                                 "trial_ends_at": u["trial_ends_at"] + paused}},
                                       session=cs).modified_count:
                return 0
            for r in db.trial_reminders.find({"user_id": u["_id"], "email_sent_at": None}, session=cs):
                db.trial_reminders.update_one({"_id": r["_id"]}, {"$set": {"due_at": r["due_at"] + paused}},
                                              session=cs)
            return 1
        n += _in_transaction(db, run)
    return n


def resting(db, user_id, now: datetime) -> bool:
    """Any of the user's journeys is in a rest. While one is, nothing goes outside Cairn except an opted-in
    check-in or an action confirmation (crisis plan)."""
    return db.cases.find_one({"created_by": user_id, "tasks_paused_until": {"$gt": now}}, {"_id": 1}) is not None


# ------------------------------------------------------------------ confirmations (UC-CASE-21, UC-REG-15)

def claim_action_confirmations(db, limit: int = 50, stale: timedelta = timedelta(minutes=15)) -> list[dict]:
    """Claims confirmations to send. A claim older than stale is claimed again, so a sender that crashed
    mid-send is retried. That can repeat a message in that one rare case, which is safer than never
    sending it. Each claim is one atomic update, so two senders never claim the same row."""
    now = utcnow()
    out = []
    for _ in range(limit):
        row = db.action_confirmation_outbox.find_one_and_update(
            {"$or": [{"claimed_at": None}, {"claimed_at": {"$lt": now - stale}}]},
            {"$set": {"claimed_at": now}, "$inc": {"attempts": 1}},
            sort=[("queued_at", 1)], return_document=ReturnDocument.AFTER)
        if row is None:
            break
        out.append({"confirmation_id": row["_id"], "action_type": row["action_type"], "email": row["email"]})
    return out


def complete_action_confirmation(db, confirmation_id) -> bool:
    """The confirmation went out: purge the address and log the send, without content."""
    def run(cs):
        row = db.action_confirmation_outbox.find_one_and_delete({"_id": confirmation_id}, session=cs)
        if row is None:
            return False
        db.action_confirmation_log.insert_one(
            {"_id": uuid.uuid4(), "action_type": row["action_type"], "channel": "email", "sent_at": utcnow()},
            session=cs)
        return True
    return _in_transaction(db, run)


def release_action_confirmation(db, confirmation_id, error: str) -> None:
    """Sending failed. Release the claim for the next run, with a short error code."""
    db.action_confirmation_outbox.update_one({"_id": confirmation_id},
                                             {"$set": {"claimed_at": None, "last_error": error[:200]}})


# ------------------------------------------------------------------ trial reminders

def claim_due_trial_reminders(db, limit: int = 100) -> list[dict]:
    """Returns trial reminders that are due and marks them sent, at most once each. The reminder always
    shows in Cairn. It goes out by email only when the user chose email for a journey (UC-CASE-12 change,
    D-2026-09-25-N1). A reminder more than 48 hours overdue is not sent late with a date that no longer
    fits. Skips subscribed accounts and trials that have already ended.
    [OPEN QUESTION card 50, Q1] whether the reminder follows notification choices."""
    now = utcnow()
    out = []
    due = db.trial_reminders.find({"email_sent_at": None, "due_at": {"$lte": now, "$gt": now - timedelta(hours=48)}},
                                  sort=[("due_at", 1)])
    for r in due:
        if len(out) >= limit:
            break
        u = db.users.find_one({"_id": r["user_id"]})
        if (u is None or u["status"] in ("subscribed", "pending_deletion") or u["trial_ends_at"] is None
                or now >= u["trial_ends_at"] or u.get("trial_clock_paused_at") is not None):
            continue
        if resting(db, u["_id"], now):
            continue  # left unsent. It shows in Cairn only (crisis plan, while resting)
        cases = [c["_id"] for c in db.cases.find({"created_by": u["_id"]}, {"_id": 1})]
        if not db.notification_preferences.find_one({"_id": {"$in": cases}, "channels": "email"}, {"_id": 1}):
            continue
        if not db.trial_reminders.update_one({"_id": r["_id"], "email_sent_at": None},
                                             {"$set": {"email_sent_at": now}}).modified_count:
            continue  # another sender got it first
        out.append({"reminder_id": r["_id"], "kind": r["kind"], "email": u["email"],
                    "preferred_name": u.get("preferred_name"), "trial_ends_at": u["trial_ends_at"],
                    "time_zone": u.get("time_zone")})
    return out


# ------------------------------------------------------------------ notifications (UC-CASE-19)

LOCK_NOTIFICATIONS = "claim_due_notifications"
FREQUENCY_WINDOW = {"daily_max": timedelta(hours=24), "weekly_max": timedelta(days=7),
                    "as_it_happens": timedelta(0)}


def _take_lock(db, name: str, now: datetime, hold: timedelta) -> bool:
    """Takes the named lock until now + hold. A lock that is held makes the upsert collide with its _id."""
    try:
        db.job_locks.update_one({"_id": name, "locked_until": {"$lte": now}},
                                {"$set": {"locked_until": now + hold}}, upsert=True)
    except DuplicateKeyError:
        return False  # held by another sender
    return True


def _release_lock(db, name: str, now: datetime) -> None:
    db.job_locks.update_one({"_id": name}, {"$set": {"locked_until": now}})


def claim_due_notifications(db, limit: int = 100) -> list[dict]:
    """Picks the journeys due a message by email, logs each pick, and returns it. At most one message per
    journey per run, and none more often than the journey's frequency allows. A step coming up is sent once
    per task and comes before inactivity. Inactivity is sent once per quiet stretch. Nothing is sent for a
    draft, a paused journey, a case set to be deleted, or a read-only account. Logging at claim time means
    a failed send is not retried: a missed nudge is better than a repeated one."""
    now = utcnow()
    if not _take_lock(db, LOCK_NOTIFICATIONS, now, timedelta(minutes=10)):
        return []  # one sender at a time, so two runs never pick the same journey
    try:
        return _claim_notifications(db, now, limit)
    finally:
        _release_lock(db, LOCK_NOTIFICATIONS, now)


def _claim_notifications(db, now: datetime, limit: int) -> list[dict]:
    out = []
    for p in db.notification_preferences.find({"channels": "email"}):
        if len(out) >= limit:
            break
        case = db.cases.find_one({"_id": p["_id"], "status": "active", "deletion_requested_at": None,
                                  "$or": [{"tasks_paused_until": None}, {"tasks_paused_until": {"$lte": now}}]})
        if case is None:
            continue
        u = db.users.find_one({"_id": case["created_by"]})
        if u is None or effective_account_status(u["status"], u["trial_ends_at"], now,
                                                 u.get("trial_clock_paused_at")) not in ("trial_active",
                                                                                         "subscribed"):
            continue
        window = FREQUENCY_WINDOW[p["frequency"]]
        if window and db.notification_log.find_one({"case_id": case["_id"], "sent_at": {"$gt": now - window}},
                                                   {"_id": 1}):
            continue
        pick = None
        if "due_date_upcoming" in p["reasons"]:
            today = local_date(now, u.get("time_zone"))
            sent = {r["case_task_id"] for r in db.notification_log.find(
                {"case_id": case["_id"], "case_task_id": {"$ne": None}}, {"case_task_id": 1})}
            soon = [t for t in db.case_tasks.find({
                "case_id": case["_id"], "selected": True, "status": {"$in": list(OPEN_TASK_STATUSES)},
                "$or": [{"snoozed_until": None}, {"snoozed_until": {"$lte": now}}],
                "due_on": {"$gte": from_date(today),
                           "$lte": from_date(today + timedelta(days=p["due_date_lead_days"]))}})
                if t["_id"] not in sent]
            if soon:
                task = min(soon, key=lambda t: (t["due_on"], str(t["_id"])))
                pick = ("due_date_upcoming", task["_id"])
        if pick is None and "inactivity" in p["reasons"]:
            quiet = case["last_activity_at"] <= now - timedelta(days=p["inactivity_days"])
            if quiet and not db.notification_log.find_one({"case_id": case["_id"], "reason": "inactivity",
                                                           "sent_at": {"$gt": case["last_activity_at"]}}, {"_id": 1}):
                pick = ("inactivity", None)
        if pick is None:
            continue
        log_id = uuid.uuid4()
        db.notification_log.insert_one({"_id": log_id, "case_id": case["_id"], "case_task_id": pick[1],
                                        "reason": pick[0], "channel": "email", "sent_at": now})
        out.append({"notification_id": log_id, "reason": pick[0], "email": u["email"], "user_id": u["_id"]})
    return out


# ------------------------------------------------------------------ check-ins (DEC-26-04)

def claim_due_check_ins(db, limit: int = 100) -> list[dict]:
    """The one check-in a user said yes to after a hard moment, by email when they chose email for that
    journey. It is the only message sent outside their notification reasons, and it goes even during a rest.
    Cleared when claimed, so it is sent once. Without email it shows in Cairn the next time they open it."""
    now = utcnow()
    out = []
    for case in db.cases.find({"check_in_at": {"$ne": None, "$lte": now}}, {"created_by": 1, "check_in_at": 1}):
        if len(out) >= limit:
            break
        prefs = db.notification_preferences.find_one({"_id": case["_id"]}, {"channels": 1})
        if not prefs or "email" not in prefs["channels"]:
            continue
        u = db.users.find_one({"_id": case["created_by"]}, {"email": 1})
        if u is None:
            continue
        if not db.cases.update_one({"_id": case["_id"], "check_in_at": case["check_in_at"]},
                                   {"$set": {"check_in_at": None}}).modified_count:
            continue
        out.append({"case_id": case["_id"], "user_id": u["_id"], "email": u["email"]})
    return out


def private_terms(db, user_id) -> list[str]:
    """What an outbound message must never contain for this user: the names they gave for the people who died
    (UC-CASE-19 privacy rule). The circumstance is a fixed enum, and outbound copy is fixed text."""
    ids = [c["_id"] for c in db.cases.find({"created_by": user_id}, {"_id": 1})]
    return [a["value"] for a in db.case_intake_answers.find(
        {"case_id": {"$in": ids}, "field_key": "display_name", "answer_state": "answered"}, {"value": 1})]


# The jobs that need only the database, by the name the Worker posts (cloudflare/src/index.ts).
JOBS: dict[str, Callable] = {
    "purge_held_cases": purge_held_cases,
    "purge_inactive_drafts": purge_inactive_drafts,
    "expire_trials": expire_trials,
    "settle_trial_clocks": settle_trial_clocks,
}
