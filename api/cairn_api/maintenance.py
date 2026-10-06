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

from . import notifications as nt
from .store import (
    OPEN_TASK_STATUSES,
    Session,
    audit_doc,
    delete_case,
    effective_access,
    free_days_running,
    from_date,
    identity_deletions,
    local_date,
    on_break,
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
        who.append({"status": "setup_complete", "trial_started_at": None,
                    "created_at": {"$lt": now - no_case_older_than}})
    n = 0
    for user in db.users.find({"$or": who}, {"idp_subject": 1, "sign_in_method": 1, "linked_identities": 1}):
        def run(cs, user=user):
            if db.cases.find_one({"$or": [{"created_by": user["_id"]}, {"members.user_id": user["_id"]}]},
                                 {"_id": 1}, session=cs):
                return 0
            db.identity_deletion_requests.insert_many(identity_deletions(user, now), session=cs)
            db.consents.delete_many({"user_id": user["_id"]}, session=cs)
            db.trial_reminders.delete_many({"user_id": user["_id"]}, session=cs)
            db.notification_preferences.delete_one({"_id": user["_id"]}, session=cs)
            db.users.delete_one({"_id": user["_id"]}, session=cs)
            db.audit_events.insert_one(audit_doc(None, "stale_account_purged", None, "user", user["_id"], now=now),
                                       session=cs)
            return 1
        n += _in_transaction(db, run)
    return n


def expire_trials(db) -> int:
    """Keeps users.access current, for reporting: read_only once the free days have ended without an active
    subscription (account D-19). Enforcement doesn't wait for this: store.effective_access is derived on every
    read. A trial stopped by a care rest never expires while it is stopped (DEC-26-01). Deletes nothing."""
    settle_trial_clocks(db)
    now = utcnow()
    n = 0
    for u in db.users.find({"$or": [{"trial_started_at": {"$ne": None}}, {"subscription_status": "lapsed"}]},
                           {"trial_started_at": 1, "trial_ends_at": 1, "trial_clock_paused_at": 1, "access": 1,
                            "subscription_status": 1}):
        want = effective_access(u, now)
        if u.get("access") != want:
            n += db.users.update_one({"_id": u["_id"], "access": u.get("access")},
                                     {"$set": {"access": want}}).modified_count
    return n


def settle_trial_clocks(db) -> int:
    """Starts the free days again for care rests whose break ended on its own (the end the user chose passed), and
    moves trial_ends_at and an unsent note later by exactly the paused time. The same rule as
    store.Session.settle_trial_clock, for users who haven't come back yet."""
    now = utcnow()
    n = 0
    for u in db.users.find({"trial_clock_paused_at": {"$ne": None}}):
        if on_break(u, now):
            continue
        def run(cs, u=u):
            paused_at = u["trial_clock_paused_at"]
            ended = u["break_until"] if u.get("break_until") and u["break_until"] >= paused_at else now
            paused = max(min(ended, now) - paused_at, timedelta(0))
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
    """The user is on a break (UC-BRK-08). While they are, nothing goes outside Cairn except a sign-in link they ask
    for, a confirmation of something they did, an opted-in check-in, the break-ending notice, and Stripe's own
    billing emails (AC-BRK-06)."""
    u = db.users.find_one({"_id": user_id}, {"break_started_at": 1, "break_until": 1})
    return bool(u) and on_break(u, now)


def outside_channels(prefs: dict | None) -> list[str]:
    """The channels that reach outside Cairn. In-app always shows everything anyway."""
    return [c for c in ("email", "browser") if prefs and c in prefs["channels"]]


# ------------------------------------------------------------------ confirmations (UC-CASE-21, UC-REG-15)

def claim_action_confirmations(db, limit: int = 50, stale: timedelta = timedelta(minutes=15)) -> list[dict]:
    """Claims confirmations to send. A claim older than stale is claimed again, so a sender that crashed
    mid-send is retried. That can repeat a message in that one rare case, which is safer than never
    sending it. Each claim is one atomic update, so two senders never claim the same row. The welcome
    confirmation carries the preferred name and nothing else personal (UC-REG-16)."""
    now = utcnow()
    out = []
    for _ in range(limit):
        row = db.action_confirmation_outbox.find_one_and_update(
            {"$or": [{"claimed_at": None}, {"claimed_at": {"$lt": now - stale}}]},
            {"$set": {"claimed_at": now}, "$inc": {"attempts": 1}},
            sort=[("queued_at", 1)], return_document=ReturnDocument.AFTER)
        if row is None:
            break
        name = None
        if row["action_type"] == "setup_complete" and row["user_id"] is not None:
            u = db.users.find_one({"_id": row["user_id"]}, {"preferred_name": 1})
            name = u.get("preferred_name") if u else None
        out.append({"confirmation_id": row["_id"], "action_type": row["action_type"], "email": row["email"],
                    "user_id": row["user_id"], "preferred_name": name})
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
    """The trial-ending note (account D-14): due a week before trial_ends_at, emailed to the sign-in email whatever
    the notification choices (a service notice), and shown in Cairn. During a break it shows only in Cairn, and is
    marked skipped so it is never emailed late (UC-BRK-08). An early subscriber gets the first payment note instead,
    on the same timing (UC-SUB-06). Each is claimed at most once. A note more than 48 hours overdue is not sent late
    with a date that no longer fits. Skips trials that have ended or are stopped by a care rest."""
    now = utcnow()
    out = []
    due = db.trial_reminders.find({"email_sent_at": None, "skipped_at": {"$in": [None]},
                                   "due_at": {"$lte": now, "$gt": now - timedelta(hours=48)}},
                                  sort=[("due_at", 1)])
    for r in due:
        if len(out) >= limit:
            break
        u = db.users.find_one({"_id": r["user_id"]})
        if (u is None or u["status"] == "pending_deletion" or u["trial_ends_at"] is None
                or now >= u["trial_ends_at"] or u.get("trial_clock_paused_at") is not None):
            continue
        if on_break(u, now):
            db.trial_reminders.update_one({"_id": r["_id"], "email_sent_at": None}, {"$set": {"skipped_at": now}})
            continue
        if not db.trial_reminders.update_one({"_id": r["_id"], "email_sent_at": None},
                                             {"$set": {"email_sent_at": now}}).modified_count:
            continue  # another sender got it first
        out.append({"reminder_id": r["_id"], "kind": r["kind"], "email": u["email"],
                    "preferred_name": u.get("preferred_name"), "trial_ends_at": u["trial_ends_at"],
                    "time_zone": u.get("time_zone"), "subscription_status": u.get("subscription_status", "none")})
    return out


# ------------------------------------------------------------------ notifications (account D-13, UC-CASE-19)

LOCK_NOTIFICATIONS = "claim_due_notifications"
FREQUENCY_WINDOW = {"daily": timedelta(hours=24), "weekly": timedelta(days=7), "due_only": timedelta(0)}


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
    """Picks the accounts due a reminder, logs each pick, and returns it. They follow the account's channels,
    frequency, and quiet hours exactly (account notification_preferences rules). At most one message per account
    per run, and none more often than the frequency allows. A step coming up is sent once per task and comes
    before inactivity. Inactivity is sent once per quiet stretch. Nothing is sent with frequency none, during a
    break (UC-BRK-08), in quiet hours, for a draft, a case set to be deleted, or a read-only account. Logging at
    claim time means a failed send is not retried: a missed nudge is better than a repeated one."""
    now = utcnow()
    if not _take_lock(db, LOCK_NOTIFICATIONS, now, timedelta(minutes=10)):
        return []  # one sender at a time, so two runs never pick the same account
    try:
        return _claim_notifications(db, now, limit)
    finally:
        _release_lock(db, LOCK_NOTIFICATIONS, now)


def _claim_notifications(db, now: datetime, limit: int) -> list[dict]:
    out = []
    for p in db.notification_preferences.find({"frequency": {"$ne": "none"},
                                               "channels": {"$in": ["email", "browser"]}}):
        if len(out) >= limit:
            break
        u = db.users.find_one({"_id": p["_id"]})
        if (u is None or u["status"] != "setup_complete" or effective_access(u, now) != "full"
                or on_break(u, now) or nt.in_quiet_hours(now, u.get("time_zone"), p)):
            continue
        cases = list(db.cases.find({"created_by": u["_id"], "status": "active", "deletion_requested_at": None,
                                    "$or": [{"tasks_paused_until": None}, {"tasks_paused_until": {"$lte": now}}]}))
        if not cases:
            continue
        window = FREQUENCY_WINDOW[p["frequency"]]
        if window and db.notification_log.find_one({"case_id": {"$in": [c["_id"] for c in cases]},
                                                    "sent_at": {"$gt": now - window}}, {"_id": 1}):
            continue
        pick = None
        for case in cases:
            today = local_date(now, u.get("time_zone"))
            sent = {r["case_task_id"] for r in db.notification_log.find(
                {"case_id": case["_id"], "case_task_id": {"$ne": None}}, {"case_task_id": 1})}
            soon = [t for t in db.case_tasks.find({
                "case_id": case["_id"], "selected": True, "status": {"$in": list(OPEN_TASK_STATUSES)},
                "$or": [{"snoozed_until": None}, {"snoozed_until": {"$lte": now}}],
                "due_on": {"$gte": from_date(today),
                           "$lte": from_date(today + timedelta(days=nt.LEAD_DAYS[p["due_date_lead"]]))}})
                if t["_id"] not in sent]
            if soon:
                task = min(soon, key=lambda t: (t["due_on"], str(t["_id"])))
                pick = (case, "due_date_upcoming", task["_id"])
                break
        if pick is None and (days := nt.INACTIVITY_DAYS[p["inactivity_after"]]) is not None:
            for case in cases:
                quiet = case["last_activity_at"] <= now - timedelta(days=days)
                if quiet and not db.notification_log.find_one({"case_id": case["_id"], "reason": "inactivity",
                                                               "sent_at": {"$gt": case["last_activity_at"]}},
                                                              {"_id": 1}):
                    pick = (case, "inactivity", None)
                    break
        if pick is None:
            continue
        case, reason, task_id = pick
        channels = outside_channels(p)
        log_ids = []
        for channel in channels:
            log_id = uuid.uuid4()
            db.notification_log.insert_one({"_id": log_id, "case_id": case["_id"], "case_task_id": task_id,
                                            "reason": reason, "channel": channel, "sent_at": now})
            log_ids.append(log_id)
        out.append({"notification_id": log_ids[0], "reason": reason, "email": u["email"], "user_id": u["_id"],
                    "channels": channels, "push_endpoint": p.get("browser_push_endpoint")})
    return out


# ------------------------------------------------------------------ check-ins (DEC-26-04)

def claim_due_check_ins(db, limit: int = 100) -> list[dict]:
    """The one check-in a user said yes to after a hard moment, through the account's channels (account D-13). It is
    the only message sent outside the user's frequency choice: it ignores frequency, including No reminders, and
    respects quiet hours. It goes even during a break. Before setup chose channels, it goes by email only if the user
    agreed to that in the same question (crisis plan context account_setup). It also shows in Cairn the next time
    the user opens it, which clears it. Marked sent when claimed, so it goes outside Cairn once."""
    now = utcnow()
    out = []
    for u in db.users.find({"check_in_at": {"$ne": None, "$lte": now}, "check_in_sent_at": None}):
        if len(out) >= limit:
            break
        prefs = db.notification_preferences.find_one({"_id": u["_id"]})
        channels = outside_channels(prefs) if prefs else (["email"] if u.get("check_in_by_email") else [])
        if not channels or nt.in_quiet_hours(now, u.get("time_zone"), prefs):
            continue
        if not db.users.update_one({"_id": u["_id"], "check_in_at": u["check_in_at"], "check_in_sent_at": None},
                                   {"$set": {"check_in_sent_at": now}}).modified_count:
            continue
        out.append({"user_id": u["_id"], "email": u["email"], "channels": channels,
                    "push_endpoint": prefs.get("browser_push_endpoint") if prefs else None})
    return out


# ------------------------------------------------------------------ the break-ending notice (UC-BRK-09)

def claim_break_notices(db, limit: int = 100) -> list[dict]:
    """One notice 24 hours before a break ends, through every channel the user chose and no other. Sent exactly
    once per break: marked sent when claimed. With only in-app, it shows in Cairn and nothing goes out. Never for a
    care rest, a draft, or a setup break (break_notice_at is null then). A break that already ended, or a notice
    that would fall in quiet hours now, waits or lapses."""
    now = utcnow()
    out = []
    for u in db.users.find({"break_notice_at": {"$ne": None, "$lte": now}, "break_notice_sent_at": None}):
        if len(out) >= limit:
            break
        if not on_break(u, now):
            continue
        prefs = db.notification_preferences.find_one({"_id": u["_id"]})
        if nt.in_quiet_hours(now, u.get("time_zone"), prefs):
            continue
        if not db.users.update_one({"_id": u["_id"], "break_notice_sent_at": None},
                                   {"$set": {"break_notice_sent_at": now}}).modified_count:
            continue
        channels = outside_channels(prefs)
        if channels:
            out.append({"user_id": u["_id"], "email": u["email"], "channels": channels,
                        "push_endpoint": prefs.get("browser_push_endpoint")})
    return out


# ------------------------------------------------------------------ the yearly renewal reminder (UC-SUB-17)

YEAR = timedelta(days=365)


def claim_annual_reminders(db, limit: int = 100) -> list[dict]:
    """The yearly reminder that the subscription renews every month, to the sign-in email, the same medium used to
    subscribe. Cairn's own billing email waits for a break to end, but no longer than 30 days (UC-SUB-17,
    SUB-D-08). Then the next one is set a year on."""
    now = utcnow()
    out = []
    for u in db.users.find({"subscription_status": "active", "annual_reminder_due_at": {"$ne": None, "$lte": now}}):
        if len(out) >= limit:
            break
        if on_break(u, now) and now - u["annual_reminder_due_at"] < timedelta(days=30):
            continue
        if not db.users.update_one({"_id": u["_id"], "annual_reminder_due_at": u["annual_reminder_due_at"]},
                                   {"$set": {"annual_reminder_due_at": u["annual_reminder_due_at"] + YEAR}}
                                   ).modified_count:
            continue
        out.append({"user_id": u["_id"], "email": u["email"]})
    return out


# ------------------------------------------------------------------ a price change (UC-SUB-18)

PRICE_NOTICE_EARLIEST = timedelta(days=30)
PRICE_NOTICE_LATEST = timedelta(days=7)


def claim_price_change_notices(db, effective: datetime, limit: int = 100) -> tuple[list[dict], list]:
    """The notice of a new price, to every active subscriber, between 30 and 7 days before it applies (California
    automatic renewal law), once each. A break may delay Cairn's own billing email only inside that window
    (SUB-D-08). A subscriber still on a break when the window closes isn't emailed, and is returned in the second
    list so the owner moves the effective date for that account. Applying the price is done in Stripe, from the
    first renewal on or after the effective date."""
    now = utcnow()
    day = from_date(effective.date())
    if now < effective - PRICE_NOTICE_EARLIEST:
        return [], []
    too_late = now > effective - PRICE_NOTICE_LATEST
    out, missed = [], []
    for u in db.users.find({"subscription_status": "active", "price_notice_sent_for": {"$ne": day}}):
        if len(out) >= limit:
            break
        if too_late:
            missed.append(u["_id"])  # never sent inside the last 7 days: the effective date moves instead
            continue
        if on_break(u, now):
            continue  # waits for the break, while the window is open
        if not db.users.update_one({"_id": u["_id"], "price_notice_sent_for": {"$ne": day}},
                                   {"$set": {"price_notice_sent_for": day}}).modified_count:
            continue
        out.append({"user_id": u["_id"], "email": u["email"]})
    return out, missed


def drop_browser_endpoint(db, user_id) -> None:
    """The browser said the push subscription is gone (permission revoked): browser comes off the channels and the
    endpoint is cleared (account notification_preferences rules)."""
    db.notification_preferences.update_one({"_id": user_id}, {"$pull": {"channels": "browser"},
                                                               "$set": {"browser_push_endpoint": None}})


# ------------------------------------------------------------------ Stripe (UC-SUB-06, UC-SUB-13, UC-SUB-22)

def process_stripe_events(db, stripe, limit: int = 50, settle: timedelta = timedelta(minutes=5)) -> tuple[int, int]:
    """Events the webhook recorded but couldn't finish, fetched again from Stripe by id (the payload is never
    stored) and applied the same way. Returns (applied, failed)."""
    from .stripe_client import StripeError
    from .subscription import process_event
    now = utcnow()
    done = failed = 0
    for row in db.stripe_events.find({"processed_at": None, "received_at": {"$lte": now - settle},
                                      "attempts": {"$lt": 10}}, limit=limit):
        try:
            event = stripe.retrieve_event(row["_id"])
            def run(cs, event=event):
                s = Session(db, cs)
                process_event(s, stripe, event)
                s.finish_stripe_event(event["id"], processed=True)
            _in_transaction(db, run)
            done += 1
        except StripeError:
            db.stripe_events.update_one({"_id": row["_id"]}, {"$inc": {"attempts": 1}})
            failed += 1
    return done, failed


def retry_cancellations(db, stripe) -> tuple[int, int]:
    """UC-SUB-13. A cancel request recorded while Stripe couldn't be reached is sent again, then confirmed."""
    from .stripe_client import StripeError
    done = failed = 0
    for u in db.users.find({"subscription_status": "active", "cancel_requested_at": {"$ne": None},
                            "cancel_at_period_end": False, "stripe_subscription_id": {"$ne": None}}):
        try:
            stripe.set_cancel_at_period_end(u["stripe_subscription_id"], True)
        except StripeError:
            failed += 1
            continue
        def run(cs, u=u):
            db.users.update_one({"_id": u["_id"]}, {"$set": {"cancel_at_period_end": True}}, session=cs)
            queue_confirmation(db, cs, "subscription_canceled", u["email"], u["_id"], utcnow())
        _in_transaction(db, run)
        done += 1
    return done, failed


def sync_early_subscriptions(db, stripe) -> int:
    """UC-SUB-06. An early subscriber's first charge stays at the free days' end after a care rest moves it, and
    while one has them stopped. Best effort each hour."""
    from .subscription import sync_first_charge
    now = utcnow()
    n = 0
    for u in db.users.find({"subscription_status": "active", "stripe_subscription_id": {"$ne": None},
                            "trial_started_at": {"$ne": None}}):
        if not free_days_running(u, now):
            continue
        account = {**u, "free_days_running": True}
        n += sync_first_charge(stripe, account, now)
    return n


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
