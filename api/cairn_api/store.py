"""The data-access layer. Every read and write the API makes goes through a Session here.

MongoDB has no row-level security, so this module is where the case boundary
lives. It takes the place of the PostgreSQL policies, SECURITY DEFINER
functions, and triggers that held it until 2026-10-01. The rules:

* The case is the security boundary. A case is visible to its creator and its
  active members. Case data (answers, tasks, the deceased, context, notification
  preferences) is reached only through a case id that passed that check. A case
  that isn't visible looks exactly like one that doesn't exist.
* Writes to case data need an owner or co_executor membership, and also:
  an account that can write (setup complete, adult confirmed, access full), or
  for a draft, an account that can edit drafts (read-only included, UC-CASE-18).
  Deleting and notification preferences never check either (D-2026-09-25-F1).
* Account fields the user can't set (onboarding_step, status, access, the
  trial, the subscription) are written only by the methods that own them:
  advance_onboarding, start_journey, the break methods, and the Stripe event
  methods. trial_started_at is written once, with a filter that only matches
  while it is unset. No break ever changes a subscription field (BRK-D-08).
* consents and audit_events are append-only. There is no method that updates
  them, and only account deletion removes consents.
* Values are checked here first so the user gets a plain answer. The collection
  validators in database/db/schema.py are the backstop.

One Session is one multi-document transaction, opened by Database.session in
db.py. Session.now is the transaction's clock, like now() in PostgreSQL.

Nothing here logs a value. Errors carry a rule name only (errors.RuleViolation).
"""
from __future__ import annotations

import functools
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from pymongo.errors import OperationFailure

from .errors import UNAUTHORIZED, ApiError, RuleViolation

# ------------------------------------------------------------------ conventions

TRIAL = timedelta(hours=672)  # exactly 28 days (D-02), in hours so daylight saving time never shifts it
WRITE_ROLES = ("owner", "co_executor")
OPEN_TASK_STATUSES = ("not_started", "check_on_this", "in_progress", "not_today")
STEPS = ("account_created", "adult_confirmed", "privacy_terms_accepted", "trial_terms_accepted",
         "ai_notice_accepted", "preferred_name_saved", "voice_saved", "complete")
CONSENT_FOR_STEP = {"privacy_terms_accepted": "privacy_terms", "trial_terms_accepted": "trial_terms",
                    "ai_notice_accepted": "ai_notice"}
# The account fields the app may set directly (Settings and onboarding screens).
ACCOUNT_FIELDS = {"preferred_name", "name_pronunciation", "voice", "time_zone"}
# The case fields the app may set directly. status and the journey start belong to start_journey,
# deletion_requested_at to request_case_deletion, tasks_paused_until to the break methods (BRK-D-06).
# journey_template_key can change on an active case when an answer changes the base path (UC-CASE-09).
CASE_FIELDS = {"purge_after", "last_intake_step", "death_not_yet_occurred", "skip_explainers",
               "name_fallback", "attorney_triggers", "shown_notices", "journey_template_key",
               "loss_survivor_resources", "secure_now_first"}
# "Until I come back" (UC-BRK-05): a break with no end. users.break_until stays null. Each active case's
# tasks_paused_until gets this far date, which only means "until the user returns", so existing reads still work.
REST_UNTIL_RETURN = timedelta(days=365)
# The subscription fields only Stripe events and the subscription routes change (cairn-subscription-use-cases-v33).
SUBSCRIPTION_FIELDS = {"subscription_status", "access", "stripe_customer_id", "stripe_subscription_id",
                       "stripe_checkout_session_id", "current_period_end", "cancel_at_period_end", "billing_notice",
                       "subscribed_at", "annual_reminder_due_at", "cancel_requested_at"}
# Notification choices for the whole account (account D-13). in_app is always on.
NOTIFICATION_FIELDS = {"channels", "frequency", "quiet_hours_start", "quiet_hours_end", "browser_push_endpoint",
                       "due_date_lead", "inactivity_after", "journey_confirmed_at"}
DEFAULT_NOTIFICATIONS = {"channels": ["email", "in_app"], "frequency": "due_only", "quiet_hours_start": "21:00",
                         "quiet_hours_end": "08:00", "browser_push_endpoint": None, "due_date_lead": "three_days",
                         "inactivity_after": "off", "journey_confirmed_at": None}
JURISDICTIONS = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR "
    "PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR GU VI AS MP".split())
DECEASED_FIELDS = {"legal_first_name", "legal_middle_name", "legal_last_name", "date_of_birth", "ssn_last4",
                   "domicile_state", "veteran_status", "has_will", "date_of_death", "place_type", "facility_name",
                   "city", "county", "death_state"}
CASE_SCOPED = ("deceased", "case_intake_answers", "case_tasks", "notification_log", "context_items")


class _Unset:
    pass


UNSET: Any = _Unset()


def utcnow() -> datetime:
    """Now, at the millisecond precision BSON dates keep, so a stored time reads back equal."""
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def to_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def from_date(value: date | None) -> str | None:
    return value.isoformat() if value else None


def local_date(now: datetime, time_zone: str | None) -> date:
    return now.astimezone(ZoneInfo(time_zone or "UTC")).date()


def free_days_running(u: dict, now: datetime) -> bool:
    """The 28 free days have started and not ended. A care rest that stopped them keeps them running."""
    if u.get("trial_started_at") is None:
        return False
    return u.get("trial_clock_paused_at") is not None or now < u["trial_ends_at"]


def effective_access(u: dict, now: datetime) -> str:
    """D-19. read_only once the free days have ended, unless a subscription is active. Derived on every read, so
    enforcement never waits for the expire_trials job. While a care rest has stopped the free days, the account
    never becomes read-only (DEC-26-01). Before the first journey there are no free days to end."""
    if u.get("subscription_status") == "active":
        return "full"
    if u.get("trial_started_at") is None:
        return "read_only" if u.get("subscription_status") == "lapsed" else "full"
    return "full" if free_days_running(u, now) else "read_only"


def on_break(u: dict, now: datetime) -> bool:
    """A break is running (UC-BRK-08): started, and with no end or an end still ahead."""
    return u.get("break_started_at") is not None and (u.get("break_until") is None or u["break_until"] > now)


def denied() -> RuleViolation:
    return RuleViolation("insufficient_privilege")


class _Bound:
    """A collection whose every call runs in the session's transaction."""

    def __init__(self, coll, cs):
        self._coll, self._cs = coll, cs

    def __getattr__(self, name):
        return functools.partial(getattr(self._coll, name), session=self._cs)


def delete_case(db, cs, case_id: UUID) -> bool:
    """Deletes a case and everything in it. Audit rows are kept. Shared with the jobs (maintenance.py).
    A check-in about this case is cancelled (crisis plan follow_up rules). When the owner has no active journey
    left, an unsent break-ending notice is cancelled too (UC-BRK-09)."""
    case = db.cases.find_one_and_delete({"_id": case_id}, {"created_by": 1}, session=cs)
    for name in CASE_SCOPED:
        db[name].delete_many({"case_id": case_id}, session=cs)
    db.users.update_many({"check_in_case_id": case_id}, {"$set": {
        "check_in_at": None, "check_in_case_id": None, "check_in_sent_at": None}}, session=cs)
    if case is not None and not db.cases.find_one({"created_by": case["created_by"], "status": {"$ne": "draft"}},
                                                  {"_id": 1}, session=cs):
        db.users.update_one({"_id": case["created_by"], "break_notice_sent_at": None},
                            {"$set": {"break_notice_at": None}}, session=cs)
    return case is not None


def audit_doc(actor_id, action: str, case_id=None, object_type=None, object_id=None, *, now: datetime) -> dict:
    return {"_id": uuid.uuid4(), "actor_id": actor_id, "case_id": case_id, "action": action,
            "object_type": object_type, "object_id": object_id, "occurred_at": now}


def queue_confirmation(db, cs, action_type: str, email: str, user_id: UUID | None, now: datetime) -> None:
    """The one confirmation for a deletion the user asked for (UC-CASE-21). The address is purged once sent."""
    db.action_confirmation_outbox.insert_one(
        {"_id": uuid.uuid4(), "action_type": action_type, "email": email, "user_id": user_id, "queued_at": now,
         "claimed_at": None, "attempts": 0, "last_error": None}, session=cs)


def subject_filter(idp_subject: str) -> dict:
    """Matches the account a sign-in belongs to, whether it created the account or was linked later."""
    return {"$or": [{"idp_subject": idp_subject}, {"linked_identities.idp_subject": idp_subject}]}


def identity_deletion(idp_subject: str, provider: str | None, now: datetime) -> dict:
    return {"_id": uuid.uuid4(), "idp_subject": idp_subject, "provider": provider, "requested_at": now,
            "attempts": 0, "last_error": None}


def identity_deletions(user: dict, now: datetime) -> list[dict]:
    """Identity provider cleanup for every sign-in on an account: the one it was created with and each linked one
    (Auth0 user deletion, and Apple token revocation, TN3194)."""
    return [identity_deletion(user["idp_subject"], user.get("sign_in_method"), now),
            *(identity_deletion(i["idp_subject"], i["sign_in_method"], now)
              for i in user.get("linked_identities", []))]


def setting_int(db, cs, key: str) -> int:
    row = db.app_settings.find_one({"_id": key}, session=cs)
    if row is None:
        raise RuntimeError(f"app setting {key} is missing. Run database/db/apply.py.")
    return int(row["value"])


# ------------------------------------------------------------------ value rules

_DATE_CHARS = set("0123456789-")


def _iso_date(v) -> bool:
    return isinstance(v, str) and len(v) == 10 and set(v) <= _DATE_CHARS and v[4] == v[7] == "-"


def _clean_text(v, lo: int, hi: int) -> bool:
    return isinstance(v, str) and lo <= len(v) <= hi and not any(ord(ch) < 32 or ord(ch) == 127 for ch in v)


INTAKE_ENUMS = {
    "user_role": {"spouse_partner", "child", "other_family", "named_executor", "power_of_attorney",
                  "professional_fiduciary", "friend", "other", "prefer_not_to_say"},
    "circumstance": {"expected_illness_or_hospice", "sudden_natural", "accident_or_unexpected",
                     "under_investigation", "prefer_not_to_say"},
    "veteran_status": {"yes", "no", "unknown"},
    "estate_plan_status": {"yes_location_known", "yes_location_unknown", "no", "unknown"},
}
COMPLETED_ITEMS = {"death_pronounced", "home_pets_vehicles_secured", "funeral_provider_chosen", "funeral_home_has_ssn",
                   "certificates_ordered", "ssa_notified", "bank_insurer_or_employer_notified", "other"}


def completed_item_key(entry) -> str | None:
    """A checklist entry is an item name (done) or {item, handled_by} (someone else is handling it)."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and set(entry) == {"item", "handled_by"}:
        return entry["item"]
    return None


def intake_value_valid(key: str, v) -> bool:
    """The shape of each data_fields value. Anything not proven valid is invalid."""
    if key in INTAKE_ENUMS:
        return isinstance(v, str) and v in INTAKE_ENUMS[key]
    if key == "display_name":
        return _clean_text(v, 1, 60)
    if not isinstance(v, (dict, list)):
        return False
    if key == "date_of_death":
        if not isinstance(v, dict) or set(v) != {"date", "precision"}:
            return False
        if v["precision"] in ("exact", "today"):
            return _iso_date(v["date"])
        return v["precision"] in ("this_week", "unknown") and v["date"] is None
    if key == "place_of_death":
        if not isinstance(v, dict) or set(v) != {"county_or_city", "outside_us", "jurisdiction"}:
            return False
        return (isinstance(v["outside_us"], bool) and (v["jurisdiction"] is None or v["jurisdiction"] in JURISDICTIONS)
                and (v["county_or_city"] is None or (isinstance(v["county_or_city"], str)
                                                     and 1 <= len(v["county_or_city"]) <= 100))
                and not (v["outside_us"] and v["jurisdiction"] is not None))
    if key == "residence_jurisdiction":
        if not isinstance(v, dict) or set(v) != {"choice", "jurisdiction"}:
            return False
        return (v["choice"] in ("same_as_place_of_death", "different", "unknown")
                and (v["jurisdiction"] is None or (v["choice"] == "different" and v["jurisdiction"] in JURISDICTIONS)))
    if key == "completed_items":
        if not isinstance(v, list) or not v:
            return False
        if v == ["none_or_unsure"]:
            return True
        keys = [completed_item_key(e) for e in v]
        return (all(k in COMPLETED_ITEMS for k in keys) and len(set(keys)) == len(keys)
                and all(isinstance(e, str) or e["handled_by"] is None or _clean_text(e["handled_by"], 1, 60)
                        for e in v))
    return False


_HH_MM = frozenset(f"{h:02d}:{m:02d}" for h in range(24) for m in range(60))


def notification_values_valid(v: dict) -> bool:
    """Account notification choices (account D-13): in_app always, no sms, a push endpoint only with browser."""
    channels = v["channels"]
    return (isinstance(channels, list) and "in_app" in channels and set(channels) <= {"email", "in_app", "browser"}
            and len(set(channels)) == len(channels)
            and v["frequency"] in ("due_only", "daily", "weekly", "none")
            and v["quiet_hours_start"] in _HH_MM and v["quiet_hours_end"] in _HH_MM
            and v["due_date_lead"] in ("day_before", "three_days", "one_week")
            and v["inactivity_after"] in ("off", "three_days", "one_week", "two_weeks")
            and (v["browser_push_endpoint"] is None
                 or ("browser" in channels and isinstance(v["browser_push_endpoint"], str)
                     and v["browser_push_endpoint"].startswith("https://")
                     and 12 <= len(v["browser_push_endpoint"]) <= 2048)))


def session_activity(db, idp_subject: str, issued_at: datetime | None, now: datetime, *, timeout: timedelta,
                     every: timedelta) -> bool:
    """D-20 and UC-REG-19. False when the token belongs to a session that went quiet for longer than timeout: it was
    issued before the quiet stretch ended, so the user must sign in again. Otherwise records the activity, at most
    once per every, outside the request's transaction so it never conflicts with it. A token issued after the
    quiet stretch is a new sign-in. Without an issue time (tests, development) there is nothing to compare."""
    users = db.users
    u = users.find_one(subject_filter(idp_subject), {"last_active_at": 1})
    if u is None:
        return True
    last = u.get("last_active_at")
    if last is not None and issued_at is not None and now - last > timeout and issued_at <= last + timeout:
        return False
    if last is None or now - last >= every:
        users.update_one({"_id": u["_id"]}, {"$set": {"last_active_at": now}})
    return True


def end_session(db, idp_subject: str, quiet_since: datetime) -> None:
    """UC-REG-19 Sign out: the session counts as quiet since quiet_since, so the token that signed out stops working."""
    db.users.update_one(subject_filter(idp_subject), {"$set": {"last_active_at": quiet_since}})


# ------------------------------------------------------------------ the session

class Session:
    """One request's transaction, as one user. Every method enforces the rules in the module docstring."""

    rest_until_return = REST_UNTIL_RETURN

    def __init__(self, db, cs, user_id: UUID | None = None, cache: dict | None = None):
        self._db = db
        self._cs = cs
        self.user_id = user_id
        self.now = utcnow()
        self._cache = cache if cache is not None else {}

    def _c(self, name: str) -> _Bound:
        return _Bound(self._db[name], self._cs)

    def require_user(self) -> UUID:
        if self.user_id is None:
            raise ApiError(403, "registration_required", "Please finish creating your account first.")
        return self.user_id

    def setting_int(self, key: str) -> int:
        return setting_int(self._db, self._cs, key)

    # -------------------------------------------------------------- audit

    def audit(self, action: str, case_id: UUID | None = None,
              object_type: str | None = None, object_id: UUID | None = None) -> None:
        """Append an audit row as the acting user, in this transaction.

        The app role can insert audit rows and cannot read them, by design.
        Audit rows hold opaque ids only, never names or free text.
        """
        self._c("audit_events").insert_one(
            audit_doc(self.require_user(), action, case_id, object_type, object_id, now=self.now))

    # -------------------------------------------------------------- sign up and sign in

    def resolve_user(self, idp_subject: str) -> UUID | None:
        """The account for a sign-in: the one it was created with, or one the user linked later (UC-REG-05)."""
        row = self._c("users").find_one(subject_filter(idp_subject), {"_id": 1})
        return row["_id"] if row else None

    def sign_in_method_for_email(self, email: str) -> str | None:
        """Which method already owns an email address, or None. Called only with the caller's own verified
        email, so it answers "which method did I use", never "does this other person have an account"."""
        lower = email.lower()
        row = self._c("users").find_one({"$or": [{"email_lower": lower}, {"linked_identities.email_lower": lower}]},
                                        {"email_lower": 1, "sign_in_method": 1, "linked_identities": 1})
        if row is None:
            return None
        if row["email_lower"] == lower:
            return row.get("sign_in_method") or "unknown"
        return next(i["sign_in_method"] for i in row["linked_identities"] if i["email_lower"] == lower)

    def create_account(self, idp_subject: str, email: str, sign_in_method: str,
                       time_zone: str | None = None) -> UUID:
        """Creates the account in pending_onboarding, or returns the existing one for the same subject.
        No legal name is collected. A provider-shared name is kept only as a pre-fill (UC-REG-02 to 04)."""
        users = self._c("users")
        existing = users.find_one(subject_filter(idp_subject), {"_id": 1, "idp_subject": 1})
        if existing:
            # The account's email follows the sign-in it was created with, never a linked one.
            if existing["idp_subject"] == idp_subject:
                users.update_one({"_id": existing["_id"]}, {"$set": {"email": email, "email_lower": email.lower()}})
            return existing["_id"]
        uid = uuid.uuid4()
        users.insert_one({
            "_id": uid, "idp_subject": idp_subject, "email": email, "email_lower": email.lower(),
            "sign_in_method": sign_in_method, "preferred_name": None, "name_pronunciation": None,
            "name_prefill": None, "adult_attested": None, "adult_attested_at": None,
            "voice": "steady_direct", "time_zone": time_zone, "onboarding_step": "account_created",
            "status": "pending_onboarding", "access": "full", "subscription_status": "none",
            "trial_started_at": None, "trial_ends_at": None, "trial_clock_paused_at": None,
            "ai_reminder_shown_at": None, "ai_reminder_shown_on": None, "last_active_at": self.now,
            "check_in_at": None, "check_in_case_id": None, "check_in_by_email": False, "check_in_sent_at": None,
            "break_started_at": None, "break_until": None, "break_notice_at": None, "break_notice_sent_at": None,
            "subscribe_prompt_shown_on": None, "price_notice_sent_for": None, "stripe_customer_id": None,
            "stripe_subscription_id": None,
            "stripe_checkout_session_id": None, "current_period_end": None, "cancel_at_period_end": False,
            "billing_notice": "none", "subscribed_at": None, "annual_reminder_due_at": None,
            "cancel_requested_at": None, "created_at": self.now})
        return uid

    def linked_identities(self) -> list[dict]:
        u = self._c("users").find_one({"_id": self.require_user()}, {"linked_identities": 1})
        return u.get("linked_identities", []) if u else []

    def link_identity(self, idp_subject: str, sign_in_method: str, email: str) -> str:
        """UC-REG-05. Adds a second sign-in to the caller's account. The caller is signed in with a sign-in that
        already belongs to the account, and the router has verified the second one's token. Never automatic.
        Returns linked, already_linked, same_method, or in_use (that sign-in or its email is another account's)."""
        uid = self.require_user()
        users = self._c("users")
        u = users.find_one({"_id": uid}, {"idp_subject": 1, "sign_in_method": 1, "linked_identities": 1})
        owner = users.find_one(subject_filter(idp_subject), {"_id": 1})
        if owner is not None:
            return "already_linked" if owner["_id"] == uid else "in_use"
        linked = u.get("linked_identities", [])
        if sign_in_method == u.get("sign_in_method") or any(i["sign_in_method"] == sign_in_method for i in linked):
            return "same_method"
        lower = email.lower()
        if users.find_one({"_id": {"$ne": uid}, "$or": [{"email_lower": lower},
                                                         {"linked_identities.email_lower": lower}]}, {"_id": 1}):
            return "in_use"
        users.update_one({"_id": uid}, {"$push": {"linked_identities": {
            "idp_subject": idp_subject, "sign_in_method": sign_in_method, "email_lower": lower,
            "linked_at": self.now}}})
        return "linked"

    def unlink_identity(self, sign_in_method: str) -> bool:
        """Removes a linked sign-in and queues its identity provider cleanup. The sign-in the account was created
        with is never removed here, so the account always keeps one."""
        uid = self.require_user()
        found = next((i for i in self.linked_identities() if i["sign_in_method"] == sign_in_method), None)
        if found is None:
            return False
        self._c("users").update_one({"_id": uid},
                                    {"$pull": {"linked_identities": {"idp_subject": found["idp_subject"]}}})
        self._c("identity_deletion_requests").insert_one(identity_deletion(found["idp_subject"], sign_in_method,
                                                                           self.now))
        return True

    def test_login(self, username: str) -> tuple[bool, dict | None]:
        """Development only: a test login for POST /v1/dev/token, from the separate <db>_dev database that
        database/tools/seed_test_db.py creates. Returns (seeded, login). seeded is False when the API's
        user can't read the logins, which means the seed never ran here. Read outside the transaction,
        so a refusal can't abort it."""
        try:
            row = self._db.client[f"{self._db.name}_dev"].test_logins.find_one({"_id": username.lower()})
        except OperationFailure as exc:
            if exc.code == UNAUTHORIZED:
                return False, None
            raise
        return True, row

    # -------------------------------------------------------------- the account

    def _user(self) -> dict | None:
        if self.user_id is None:
            return None
        return self._c("users").find_one({"_id": self.user_id})

    ACCOUNT_OUT = ("email", "sign_in_method", "preferred_name", "name_pronunciation", "name_prefill",
                   "adult_attested", "adult_attested_at", "voice", "time_zone", "onboarding_step", "status",
                   "subscription_status", "trial_started_at", "trial_ends_at", "trial_clock_paused_at",
                   "ai_reminder_shown_at", "ai_reminder_shown_on", "check_in_at", "check_in_case_id",
                   "check_in_sent_at", "break_started_at", "break_until", "break_notice_at", "break_notice_sent_at",
                   "subscribe_prompt_shown_on", "stripe_customer_id", "stripe_subscription_id",
                   "stripe_checkout_session_id", "current_period_end", "cancel_at_period_end", "billing_notice",
                   "subscribed_at", "annual_reminder_due_at", "cancel_requested_at", "created_at")

    def load_account(self) -> dict | None:
        """The caller's account. access is the effective access (read_only once the free days end, D-19)."""
        u = self._user()
        if u is None:
            return None
        out = {k: u.get(k) for k in self.ACCOUNT_OUT}
        out["linked_sign_in_methods"] = [i["sign_in_method"] for i in u.get("linked_identities", [])]
        return {**out, "id": u["_id"], "stored_access": u.get("access"), "access": effective_access(u, self.now),
                "free_days_running": free_days_running(u, self.now), "on_break": on_break(u, self.now)}

    @staticmethod
    def _setup_done(a: dict | None) -> bool:
        return bool(a) and a["onboarding_step"] == "complete" and a["status"] == "setup_complete" and bool(
            a["adult_attested"])

    def account_can_write(self) -> bool:
        """Setup complete, adult confirmed, and access full (D-05, D-19). Deleting never depends on this."""
        a = self.load_account()
        return self._setup_done(a) and a["access"] == "full"

    def account_can_edit_drafts(self) -> bool:
        """UC-CASE-18. A read-only account can still create and edit drafts."""
        return self._setup_done(self.load_account())

    def record_adult_answer(self, adult: bool) -> bool:
        """UC-REG-06. Yes or no, with the time. Never a birthdate or an age. A yes moves onboarding past the
        question. A no stops onboarding: the account stays pending_onboarding, nothing more is collected, and
        purge_stale_accounts deletes it after the pending-account period. Returns whether the answer was saved.
        Once given, the answer is not changed here (OPEN-09)."""
        uid = self.require_user()
        users = self._c("users")
        u = users.find_one({"_id": uid}, {"adult_attested": 1, "onboarding_step": 1})
        if u.get("adult_attested") is False:
            return False
        if u.get("adult_attested") is not True:
            users.update_one({"_id": uid}, {"$set": {"adult_attested": adult, "adult_attested_at": self.now}})
        if adult and u["onboarding_step"] == "account_created":
            self.advance_onboarding("adult_confirmed")
        return True

    def update_account(self, **fields) -> None:
        """Settings and onboarding screens. Only ACCOUNT_FIELDS, never onboarding, status, or the trial."""
        uid = self.require_user()
        if not fields:
            return
        if not set(fields) <= ACCOUNT_FIELDS:
            raise denied()
        self._c("users").update_one({"_id": uid}, {"$set": fields})

    def accepted_versions(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for r in self._c("consents").find({"user_id": self.user_id}, {"purpose": 1, "policy_version": 1}):
            out.setdefault(r["purpose"], set()).add(r["policy_version"])
        return out

    def add_consent(self, purpose: str, policy_version: str, auth_provider: str | None, client: str | None) -> None:
        """Append-only. A consent row is never changed afterwards."""
        self._c("consents").insert_one({
            "_id": uuid.uuid4(), "user_id": self.require_user(), "purpose": purpose,
            "policy_version": policy_version, "granted_at": self.now, "auth_provider": auth_provider,
            "client": client})

    def consents(self) -> list[dict]:
        return list(self._c("consents").find(
            {"user_id": self.user_id},
            {"_id": 0, "purpose": 1, "policy_version": 1, "granted_at": 1, "auth_provider": 1, "client": 1},
            sort=[("granted_at", 1)]))

    def advance_onboarding(self, to: str) -> str:
        """Moves onboarding forward by exactly one step, in the spec's order. Repeating a step already passed
        is a no-op so clients can retry. Each acknowledgment step needs its consent, and the name step a name."""
        if self.user_id is None:
            raise denied()
        if to not in STEPS or to == STEPS[0]:
            raise RuleViolation("invalid_parameter")
        u = self._user()
        cur, want = STEPS.index(u["onboarding_step"]), STEPS.index(to)
        if cur >= want:
            return u["onboarding_step"]
        if cur != want - 1:
            raise RuleViolation("prerequisite")
        if to in CONSENT_FOR_STEP and not self._c("consents").find_one(
                {"user_id": self.user_id, "purpose": CONSENT_FOR_STEP[to]}, {"_id": 1}):
            raise RuleViolation("prerequisite")
        if to == "adult_confirmed" and u.get("adult_attested") is not True:
            raise RuleViolation("prerequisite")
        if to == "preferred_name_saved" and u.get("preferred_name") is None:
            raise RuleViolation("prerequisite")
        if to == "complete" and self.notification_preferences() is None:
            raise RuleViolation("prerequisite")
        changes: dict = {"onboarding_step": to}
        if to == "preferred_name_saved":
            changes["name_prefill"] = None
        if to == "complete" and u["status"] == "pending_onboarding":
            changes["status"] = "setup_complete"
        # The filter on the current step serializes two requests racing on the same account.
        if not self._c("users").update_one({"_id": self.user_id, "onboarding_step": u["onboarding_step"]},
                                           {"$set": changes}).matched_count:
            raise RuleViolation("prerequisite")
        return to

    def latest_due_reminder_kind(self) -> str | None:
        row = self._c("trial_reminders").find_one({"user_id": self.user_id, "due_at": {"$lte": self.now}},
                                                  sort=[("due_at", -1)])
        return row["kind"] if row else None

    def trial_reminders(self) -> list[dict]:
        return list(self._c("trial_reminders").find(
            {"user_id": self.user_id}, {"_id": 0, "kind": 1, "due_at": 1, "email_sent_at": 1}, sort=[("due_at", 1)]))

    def delete_my_account(self) -> None:
        """UC-REG-15 and UC-ACCT-01. Everything goes now, in this transaction: every case the user created in any
        status (holds included) with all of its data, their memberships, consents, reminders, notification
        choices and the browser push endpoint, any scheduled check-in or break notice (they live on the account),
        and the account. Pending confirmations are dropped so exactly one confirmation goes out, and it has no
        user id. The identity provider cleanup is queued (Apple token revocation, TN3194). Audit rows keep ids
        only. Never checks whether the account can write (D-2026-09-25-F1). A Stripe subscription is cancelled
        by the router before this runs (UC-SUB-16)."""
        u = self._user()
        if u is None:
            raise denied()
        uid = u["_id"]
        for case in self._c("cases").find({"created_by": uid}, {"_id": 1}):
            delete_case(self._db, self._cs, case["_id"])
            self._c("audit_events").insert_one(
                audit_doc(uid, "case_deleted_with_account", case["_id"], "case", case["_id"], now=self.now))
        self._c("cases").update_many({"members.user_id": uid}, {"$pull": {"members": {"user_id": uid}}})
        self._c("action_confirmation_outbox").delete_many({"user_id": uid})
        queue_confirmation(self._db, self._cs, "account_deleted", u["email"], None, self.now)
        self._c("identity_deletion_requests").insert_many(identity_deletions(u, self.now))
        self._c("consents").delete_many({"user_id": uid})
        self._c("trial_reminders").delete_many({"user_id": uid})
        self._c("notification_preferences").delete_one({"_id": uid})
        self._c("users").delete_one({"_id": uid})
        self._c("audit_events").insert_one(audit_doc(uid, "account_deleted", None, "user", uid, now=self.now))

    # -------------------------------------------------------------- the case boundary

    def _visible(self) -> dict:
        """The filter for cases the caller can see: their own, or one they are an active member of."""
        if self.user_id is None:
            return {"_id": {"$in": []}}
        return {"$or": [{"created_by": self.user_id},
                        {"members": {"$elemMatch": {"user_id": self.user_id, "status": "active"}}}]}

    def _member(self, case: dict, roles: tuple[str, ...] | None = None) -> bool:
        return self.user_id is not None and any(
            m["user_id"] == self.user_id and m["status"] == "active" and (roles is None or m["role"] in roles)
            for m in case.get("members", []))

    def _case(self, case_id: UUID) -> dict:
        """A visible case, or the same refusal as a case that doesn't exist."""
        case = self._c("cases").find_one({"_id": case_id, **self._visible()})
        if case is None:
            raise denied()
        return case

    def _case_for_write(self, case_id: UUID, *, drafts_on_read_only: bool = True) -> dict:
        """An owner or co_executor, on an account that may change this case (case_writable)."""
        case = self._case(case_id)
        if not self._member(case, WRITE_ROLES):
            raise denied()
        if not self._case_writable(case, drafts_on_read_only):
            raise denied()
        return case

    def _case_writable(self, case: dict, drafts_on_read_only: bool = True) -> bool:
        if self.account_can_write():
            return True
        return drafts_on_read_only and case["status"] == "draft" and self.account_can_edit_drafts()

    def is_case_member(self, case_id: UUID, roles: tuple[str, ...] | None = None) -> bool:
        case = self._c("cases").find_one({"_id": case_id}, {"members": 1})
        return case is not None and self._member(case, roles)

    def _activity(self, case_id: UUID) -> None:
        """Activity on a case (an answer, an edit, an open, a task change). Always the server's time."""
        self._c("cases").update_one({"_id": case_id}, {"$set": {"last_activity_at": self.now}})

    # -------------------------------------------------------------- cases

    def has_case(self) -> bool:
        return self._c("cases").find_one({"created_by": self.user_id}, {"_id": 1}) is not None

    def owned_cases(self, *, newest_activity_first: bool = False) -> list[dict]:
        sort = [("last_activity_at", -1)] if newest_activity_first else [("created_at", 1)]
        return [{"id": r["_id"], "status": r["status"], "created_at": r["created_at"]}
                for r in self._c("cases").find({"created_by": self.user_id},
                                               {"status": 1, "created_at": 1}, sort=sort)]

    def create_draft(self) -> UUID:
        """UC-CASE-01. The case and its owner membership in one write. Never starts the trial (DEC-01)."""
        uid = self.require_user()
        if not self.account_can_edit_drafts():
            raise denied()
        case_id = uuid.uuid4()
        self._c("cases").insert_one({
            "_id": case_id, "status": "draft", "created_by": uid,
            "members": [{"user_id": uid, "role": "owner", "relationship": None, "status": "active",
                         "created_at": self.now}],
            "created_at": self.now, "purge_after": None, "journey_template_key": None,
            "journey_template_version": None, "journey_started_at": None, "journey_started_on": None,
            "last_intake_step": None, "last_activity_at": self.now, "death_not_yet_occurred": False,
            "skip_explainers": False, "name_fallback": "your_loved_one", "attorney_triggers": [],
            "shown_notices": [], "tasks_paused_until": None,
            "deletion_requested_at": None, "loss_survivor_resources": False, "secure_now_first": False})
        return case_id

    def load_case(self, case_id: UUID) -> dict | None:
        case = self._c("cases").find_one({"_id": case_id, **self._visible()}, {"members": 0})
        if case is None:
            return None
        out = {**case, "id": case.pop("_id"), "journey_started_on": to_date(case["journey_started_on"])}
        out["draft_expires_at"] = (case["last_activity_at"] + timedelta(days=self.setting_int("draft_retention_days"))
                                   if case["status"] == "draft" else None)
        out["deletion_scheduled_for"] = (
            case["deletion_requested_at"] + timedelta(days=self.setting_int("case_deletion_hold_days"))
            if case["deletion_requested_at"] else None)
        return out

    def touch(self, case_id: UUID) -> None:
        """Opening a draft or coming back to a journey counts as activity. Like an UPDATE that row-level
        security filtered out, nothing happens when the caller may not change the case."""
        case = self._c("cases").find_one({"_id": case_id, **self._visible()})
        if case and self._member(case, WRITE_ROLES) and self._case_writable(case):
            self._activity(case_id)

    def update_case(self, case_id: UUID, *, activity: bool = True, started_only: bool = False, **fields) -> None:
        """Change the fields the app may set (CASE_FIELDS). started_only refuses a draft."""
        if not set(fields) <= CASE_FIELDS:
            raise denied()
        case = self._case_for_write(case_id)
        if started_only and case["status"] == "draft":
            raise denied()
        if case["status"] == "draft" and fields.get("journey_template_key") is not None:
            raise RuleViolation("check_violation", "draft_has_no_journey")
        changes = dict(fields)
        if activity:
            changes["last_activity_at"] = self.now
        if changes:
            self._c("cases").update_one({"_id": case_id}, {"$set": changes})

    def start_journey(self, case_id: UUID, template_version: int, template_key: str) -> bool:
        """UC-CASE-12. Moves a draft to active and, on the account's first journey, starts the 28-day trial,
        all in this transaction. Returns True when this call started the trial. trial_started_at is only
        ever written while it is unset. Refuses read-only accounts (UC-CASE-18) and cases where the person
        has not died yet (UC-CASE-17). No payment information is involved (DEC-02)."""
        uid = self.user_id
        case = self._c("cases").find_one({"_id": case_id})
        if uid is None or case is None or not self._member(case, ("owner",)):
            raise denied()
        if not self.account_can_write():
            raise denied()
        if case["status"] != "draft":
            raise RuleViolation("prerequisite")
        if case["death_not_yet_occurred"]:
            raise RuleViolation("prerequisite")
        template = self._c("journey_templates").find_one(
            {"version": template_version, "active": True, f"definition.paths.{template_key}": {"$exists": True}},
            {"_id": 1})
        if template is None:
            raise RuleViolation("invalid_parameter")

        user = self._user()
        moved = self._c("cases").update_one({"_id": case_id, "status": "draft"}, {"$set": {
            "status": "active", "journey_template_key": template_key, "journey_template_version": template_version,
            "journey_started_at": self.now, "journey_started_on": from_date(local_date(self.now, user["time_zone"])),
            "last_activity_at": self.now}})
        if not moved.modified_count:
            raise RuleViolation("prerequisite")

        ends = self.now + TRIAL
        started = self._c("users").update_one({"_id": uid, "trial_started_at": None}, {"$set": {
            "trial_started_at": self.now, "trial_ends_at": ends}})
        trial_started = bool(started.modified_count)
        if trial_started:
            # Account D-14: one note, a week before the free days end, by email and in Cairn.
            remind = timedelta(days=self.setting_int("trial_reminder_days_before"))
            self._c("trial_reminders").insert_one({"_id": uuid.uuid4(), "user_id": uid, "kind": "trial_ends_soon",
                                                   "due_at": ends - remind, "email_sent_at": None,
                                                   "skipped_at": None})
        if on_break(user, self.now):
            # A journey started during a break rests with the others (BRK-D-06).
            self._c("cases").update_one({"_id": case_id}, {"$set": {"tasks_paused_until": self._paused_until(
                user["break_until"])}})
        self.audit("journey_started", case_id, "case", case_id)
        return trial_started

    def request_case_deletion(self, case_id: UUID, mode: str) -> datetime | None:
        """Deletes a case now, or holds it for case_deletion_hold_days first. Owner only. Works on drafts,
        active, and read-only cases alike and never checks whether the account can write
        (D-2026-09-25-F1). Deleting now queues one confirmation. A hold queues it when the case is deleted.
        Returns when a held case will be deleted, or None when it was deleted now."""
        case = self._c("cases").find_one({"_id": case_id})
        if self.user_id is None or case is None or not self._member(case, ("owner",)):
            raise denied()
        if mode not in ("now", "hold"):
            raise RuleViolation("invalid_parameter")
        if mode == "now":
            delete_case(self._db, self._cs, case_id)
            self.audit("case_deleted_now", case_id, "case", case_id)
            queue_confirmation(self._db, self._cs, "case_deleted_now", self._user()["email"], self.user_id, self.now)
            return None
        requested = case["deletion_requested_at"]
        if requested is None:
            requested = self.now
            self._c("cases").update_one({"_id": case_id}, {"$set": {"deletion_requested_at": requested}})
            self.audit("case_deletion_scheduled", case_id, "case", case_id)
        return requested + timedelta(days=self.setting_int("case_deletion_hold_days"))

    def cancel_case_deletion(self, case_id: UUID) -> bool:
        """Keeps a case the user had set to delete with a hold. Owner only."""
        case = self._c("cases").find_one({"_id": case_id}, {"members": 1})
        if self.user_id is None or case is None or not self._member(case, ("owner",)):
            raise denied()
        if not self._c("cases").update_one({"_id": case_id, "deletion_requested_at": {"$ne": None}},
                                           {"$set": {"deletion_requested_at": None}}).modified_count:
            return False
        self.audit("case_deletion_cancelled", case_id, "case", case_id)
        return True

    # -------------------------------------------------------------- breaks and the free days (UC-BRK, DEC-26-01)

    def _rest_member(self, case_id: UUID) -> dict:
        """A rest is a safety feature, so it needs a membership and nothing else: never onboarding,
        acknowledgments, or a writable account."""
        case = self._case(case_id)
        if not self._member(case, WRITE_ROLES):
            raise denied()
        return case

    def _paused_until(self, break_until: datetime | None) -> datetime:
        return break_until or self.now + REST_UNTIL_RETURN

    def _pause_active_cases(self, uid: UUID, until: datetime | None) -> None:
        """BRK-D-06. Every active journey rests with the person. Drafts have no tasks to pause."""
        self._c("cases").update_many({"created_by": uid, "status": {"$ne": "draft"}},
                                     {"$set": {"tasks_paused_until": until}})

    def begin_break(self, until: datetime | None, *, care: bool, notice_at: datetime | None = None) -> bool:
        """UC-BRK-05 and UC-BRK-07. A break on the account: break_until is None for Until I come back. Every active
        journey's tasks_paused_until follows it. A care rest (levels 2 to 4) also stops the free days, only while
        they are running (DEC-26-01). A normal rest never does. Starting a break during one keeps the original
        start (UC-BRK-11). notice_at is the break-ending notice time, or None (UC-BRK-09, BRK-D-03). Never touches
        a subscription field (BRK-D-08). The reason and the kind of break are never stored. Returns True when this
        call stopped the free days."""
        uid = self.require_user()
        u = self._user()
        continuing = on_break(u, self.now)
        started = u["break_started_at"] if continuing else self.now
        if until is not None and until <= started:
            raise RuleViolation("invalid_parameter")
        # A changed break never sends a second notice (UC-BRK-11).
        sent = u["break_notice_sent_at"] if continuing else None
        self._c("users").update_one({"_id": uid}, {"$set": {
            "break_started_at": started, "break_until": until, "break_notice_at": None if sent else notice_at,
            "break_notice_sent_at": sent}})
        self._pause_active_cases(uid, self._paused_until(until))
        if not care or u["trial_started_at"] is None or u.get("trial_clock_paused_at") is not None:
            return False
        if not self._c("cases").find_one({"created_by": uid, "status": {"$ne": "draft"}}, {"_id": 1}):
            return False  # no journey yet: a care rest only stops the questions (UC-CASE-14)
        if not free_days_running(u, self.now):
            return False  # a subscriber whose free days have ended: nothing about billing changes (BRK-D-08)
        return bool(self._c("users").update_one({"_id": uid, "trial_clock_paused_at": None},
                                                {"$set": {"trial_clock_paused_at": self.now}}).modified_count)

    def end_break(self) -> timedelta | None:
        """UC-BRK-10. The user is back. Clears the break and every journey's tasks_paused_until, cancels an unsent
        notice, and starts the free days again (settle_trial_clock). Returns how long they were paused, or None."""
        uid = self.require_user()
        self._c("users").update_one({"_id": uid}, {"$set": {
            "break_started_at": None, "break_until": None, "break_notice_at": None, "break_notice_sent_at": None}})
        self._pause_active_cases(uid, None)
        self._c("cases").update_many({"created_by": uid, "status": {"$ne": "draft"}},
                                     {"$set": {"last_activity_at": self.now}})
        return self.settle_trial_clock()

    def begin_rest(self, case_id: UUID, until: datetime, *, care: bool) -> bool:
        """A rest started from a case (a level 3 or 4 pause, or a level 2 choice). Breaks are the account's, so
        this checks the membership and then starts a break for the person."""
        self._rest_member(case_id)
        return self.begin_break(None if until - self.now >= REST_UNTIL_RETURN else until, care=care)

    def end_rest(self, case_id: UUID) -> timedelta | None:
        """The user came back to tasks from a case."""
        self._rest_member(case_id)
        return self.end_break()

    def settle_trial_clock(self) -> timedelta | None:
        """Starts the free days again once no break is running, and moves trial_ends_at (and any note not yet
        sent) later by exactly the paused time (AC-26-04). A break that ended on its own ends at its end time.
        If the user subscribed early, the router moves the first charge date in Stripe (UC-SUB-06)."""
        u = self._user()
        if u is None or u.get("trial_clock_paused_at") is None or on_break(u, self.now):
            return None
        paused_at = u["trial_clock_paused_at"]
        ended = u["break_until"] if u.get("break_until") and u["break_until"] >= paused_at else self.now
        paused = max(min(ended, self.now) - paused_at, timedelta(0))
        moved = self._c("users").update_one(
            {"_id": u["_id"], "trial_clock_paused_at": paused_at},
            {"$set": {"trial_clock_paused_at": None, "trial_ends_at": u["trial_ends_at"] + paused}})
        if not moved.modified_count:
            return None
        if paused:
            # The app can't update reminders (only the job claims them), so an unsent one is replaced, later.
            reminders = self._c("trial_reminders")
            for r in list(reminders.find({"user_id": u["_id"], "email_sent_at": None, "skipped_at": None})):
                if reminders.delete_one({"_id": r["_id"], "email_sent_at": None}).deleted_count:
                    reminders.insert_one({**r, "due_at": r["due_at"] + paused})
        return paused

    # -------------------------------------------------------------- the AI reminder (UC-CASE-23)

    def ai_reminder_due(self, *, session_start: bool, every: timedelta) -> bool:
        """At the start of a session, at most once a day. During one, again after every 3 hours."""
        u = self._user()
        if u is None:
            return False
        last = u.get("ai_reminder_shown_at")
        if last is None:
            return True
        if session_start:
            return u.get("ai_reminder_shown_on") != from_date(local_date(self.now, u.get("time_zone")))
        return self.now - last >= every

    def mark_ai_reminder_shown(self) -> None:
        u = self._user()
        if u is not None:
            self._c("users").update_one({"_id": u["_id"]}, {"$set": {
                "ai_reminder_shown_at": self.now,
                "ai_reminder_shown_on": from_date(local_date(self.now, u.get("time_zone")))}})

    # -------------------------------------------------------------- check-in and the SB 243 count

    def set_check_in(self, when: datetime, case_id: UUID | None = None, *, by_email: bool = False) -> None:
        """Only on an explicit yes to "Would it be okay if I checked in with you tomorrow?" (DEC-26-04). Stored on
        the account, so it works during setup before any case exists (AC-26-12). case_id cancels it if that case
        is deleted. by_email is the yes to email asked during setup before notification choices exist."""
        uid = self.require_user()
        if case_id is not None:
            self._rest_member(case_id)
        self._c("users").update_one({"_id": uid}, {"$set": {
            "check_in_at": when, "check_in_case_id": case_id, "check_in_by_email": by_email,
            "check_in_sent_at": None}})

    def take_due_check_in(self) -> bool:
        """A check-in that is due shows once, the next time the user opens Cairn, whether or not it also went by
        email (in-app is always one of the channels). Showing it clears it."""
        u = self._user()
        if u is None or u.get("check_in_at") is None or u["check_in_at"] > self.now:
            return False
        return bool(self._c("users").update_one({"_id": u["_id"], "check_in_at": u["check_in_at"]}, {"$set": {
            "check_in_at": None, "check_in_case_id": None, "check_in_by_email": False,
            "check_in_sent_at": None}}).modified_count)

    def count_level_4_referral(self) -> None:
        """An anonymous monthly count for SB 243 reporting. No user, no case, no time of day."""
        self._c("safety_referral_counts").update_one(
            {"_id": self.now.strftime("%Y-%m")},
            {"$inc": {"level_4_referrals": 1}, "$set": {"updated_at": self.now}}, upsert=True)

    # -------------------------------------------------------------- the person who died

    def load_deceased(self, case_id: UUID, *, for_export: bool = False) -> dict | None:
        """Identity only, or for the download everything except the SSN digits. ssn_last4 is never read."""
        self._case(case_id)
        fields = (["legal_first_name", "legal_middle_name", "legal_last_name", "date_of_birth", "domicile_state",
                   "veteran_status", "has_will", "date_of_death", "place_type", "facility_name", "city", "county",
                   "death_state"] if for_export
                  else ["legal_first_name", "legal_middle_name", "legal_last_name", "date_of_birth"])
        row = self._c("deceased").find_one({"case_id": case_id}, {f: 1 for f in fields})
        if row is None:
            return None
        out = {"id": row.pop("_id"), **row}
        for f in ("date_of_birth", "date_of_death"):
            if f in out:
                out[f] = to_date(out[f])
        if for_export:
            out.pop("id")
        return out

    def save_deceased(self, case_id: UUID, fields: dict) -> tuple[UUID, bool]:
        """Just in time, inside the task that needs it. Never on a draft (never_collect_at_case_creation).
        Returns the row id and whether it was created."""
        if not set(fields) <= DECEASED_FIELDS:
            raise denied()
        case = self._case_for_write(case_id, drafts_on_read_only=False)
        if case["status"] == "draft":
            raise denied()
        coll = self._c("deceased")
        row = coll.find_one({"case_id": case_id})
        created = row is None
        if created:
            row = {"_id": uuid.uuid4(), "case_id": case_id, "veteran_status": "unknown", "has_will": "unknown",
                   **{f: None for f in DECEASED_FIELDS - {"veteran_status", "has_will"}}}
        merged = {**row, **{k: from_date(v) if isinstance(v, date) else v for k, v in fields.items()}}
        today = from_date(self.now.date())
        if merged["date_of_birth"] and merged["date_of_birth"] > today:
            raise RuleViolation("check_violation", "deceased_date_of_birth_check")
        if merged["date_of_death"] and merged["date_of_death"] > today:
            raise RuleViolation("check_violation", "deceased_date_of_death_check")
        if merged["date_of_birth"] and merged["date_of_death"] and merged["date_of_death"] < merged["date_of_birth"]:
            raise RuleViolation("check_violation", "death_not_before_birth")
        if created:
            coll.insert_one(merged)
        else:
            coll.replace_one({"_id": row["_id"]}, merged)
        return row["_id"], created

    # -------------------------------------------------------------- intake answers

    def load_answers(self, case_id: UUID) -> dict[str, dict]:
        if self._c("cases").find_one({"_id": case_id, **self._visible()}, {"_id": 1}) is None:
            return {}
        return {r["field_key"]: r for r in self._c("case_intake_answers").find(
            {"case_id": case_id}, {"_id": 0, "field_key": 1, "answer_state": 1, "value": 1, "own_words": 1})}

    def save_answer(self, case_id: UUID, field_key: str, answer_state: str, value, own_words: str | None) -> None:
        """One row per data_fields key. Only the spec's fields and shapes can be stored, and circumstance
        stores the enum only. Any answer counts as activity on the case (DEC-07)."""
        self._case_for_write(case_id)
        if answer_state not in ("answered", "skipped", "unsure") or (answer_state == "answered") != (value is not None):
            raise RuleViolation("check_violation", "value_only_when_answered")
        if value is not None and not intake_value_valid(field_key, value):
            raise RuleViolation("check_violation", "value_matches_field")
        if own_words is not None and (field_key == "circumstance" or answer_state != "answered"
                                      or not _clean_text(own_words, 1, 120)):
            raise RuleViolation("check_violation", "own_words_allowed")
        self._c("case_intake_answers").update_one(
            {"case_id": case_id, "field_key": field_key},
            {"$set": {"answer_state": answer_state, "value": value, "own_words": own_words,
                      "updated_at": self.now}}, upsert=True)
        self._activity(case_id)

    def display_names(self) -> list[dict]:
        """Each of the user's cases with its display_name answer, oldest first. Conversation only."""
        cases = self.owned_cases()
        names = {r["case_id"]: r["value"] for r in self._c("case_intake_answers").find(
            {"case_id": {"$in": [c["id"] for c in cases]}, "field_key": "display_name", "answer_state": "answered"},
            {"case_id": 1, "value": 1})}
        fallbacks = {r["_id"]: r["name_fallback"] for r in self._c("cases").find(
            {"created_by": self.user_id}, {"name_fallback": 1})}
        return [{"id": c["id"], "name_fallback": fallbacks[c["id"]], "display_name": names.get(c["id"])}
                for c in cases]

    def any_case_says_veteran(self) -> bool:
        ids = [c["id"] for c in self.owned_cases()]
        return self._c("case_intake_answers").find_one(
            {"case_id": {"$in": ids}, "field_key": "veteran_status", "answer_state": "answered", "value": "yes"},
            {"_id": 1}) is not None

    # -------------------------------------------------------------- content (templates)

    def journey_definition(self, version: int | None = None) -> dict | None:
        """The pinned rules for a started case, or the newest active rules for a draft."""
        if version is None:
            row = self._c("journey_templates").find_one({"active": True}, {"definition": 1}, sort=[("version", -1)])
        else:
            row = self._c("journey_templates").find_one({"version": version}, {"definition": 1})
        return row["definition"] if row else None

    def _template_out(self, t: dict) -> dict:
        cites = sorted(({**c, "last_verified_on": to_date(c["last_verified_on"])} for c in t["citations"]),
                       key=lambda c: c["authority_name"])
        return {**{k: v for k, v in t.items() if k not in ("_id", "citations")}, "id": t["_id"], "citations": cites}

    def templates(self, keys: list[str]) -> dict[str, dict]:
        """The newest active version of each task template, with citations."""
        out: dict[str, dict] = {}
        for t in self._c("task_templates").find({"active": True, "task_key": {"$in": list(keys)}}):
            if t["task_key"] not in out or t["version"] > out[t["task_key"]]["version"]:
                out[t["task_key"]] = self._template_out(t)
        return out

    def _templates_by_id(self, ids) -> dict[UUID, dict]:
        """Template rows are immutable apart from active, so they are cached by id."""
        cache = self._cache.setdefault("templates", {})
        missing = [i for i in set(ids) if i not in cache]
        if missing:
            for t in self._c("task_templates").find({"_id": {"$in": missing}}):
                cache[t["_id"]] = self._template_out(t)
        return {i: cache[i] for i in ids if i in cache}

    def template_citations(self, template_id: UUID) -> list[dict]:
        """Citations for a task's pinned template, the US authority first."""
        t = self._templates_by_id([template_id])[template_id]
        return sorted(t["citations"], key=lambda c: (c["jurisdiction"] != "US", c["authority_name"]))

    # -------------------------------------------------------------- tasks

    def _task_rows(self, case_id: UUID, query: dict) -> list[dict]:
        rows = list(self._c("case_tasks").find({"case_id": case_id, **query}))
        tmpl = self._templates_by_id([r["template_id"] for r in rows])
        out = []
        for r in rows:
            t = tmpl[r["template_id"]]
            out.append({
                "id": r["_id"], "status": r["status"], "due_on": to_date(r["due_on"]),
                "snoozed_until": r["snoozed_until"], "completed_at": r["completed_at"], "selected": r["selected"],
                "task_key": t["task_key"], "template_version": t["version"], "title": t["title"],
                "plain_summary": t["plain_summary"], "journey_week": t["journey_week"], "sort_order": t["sort_order"],
                "attorney_referral": t["attorney_referral"], "attorney_referral_note": t["attorney_referral_note"],
                "why_now": t["why_now"], "counsel_reviewed_at": t["counsel_reviewed_at"],
                "jurisdiction": t["jurisdiction"], "template_id": t["id"],
                "needs_check": r.get("needs_check", False), "probably_not_applicable": r.get("probably_not_applicable",
                                                                                            False),
                "handled_by": r.get("handled_by")})
        out.sort(key=lambda r: (r["journey_week"], r["sort_order"], r["due_on"] is None, r["due_on"] or date.min,
                                r["task_key"]))
        return out

    def load_tasks(self, case_id: UUID) -> list[dict]:
        """What the rules currently select, plus anything the user has already worked on, so a changed answer
        never hides progress (UC-CASE-09). In journey order."""
        if self._c("cases").find_one({"_id": case_id, **self._visible()}, {"_id": 1}) is None:
            return []
        return self._task_rows(case_id, {"$or": [{"selected": True}, {"status": {"$in": ["in_progress", "done"]}}]})

    def load_task(self, case_id: UUID, task_id: UUID) -> dict | None:
        if self._c("cases").find_one({"_id": case_id, **self._visible()}, {"_id": 1}) is None:
            return None
        rows = self._task_rows(case_id, {"_id": task_id})
        return rows[0] if rows else None

    def task_states(self, case_id: UUID) -> list[dict]:
        """Every task on the case with its key, for syncing with the journey rules."""
        self._case(case_id)
        return [{"id": r["_id"], "status": r["status"], "selected": r["selected"], "task_key": r["task_key"],
                 "needs_check": r.get("needs_check", False),
                 "probably_not_applicable": r.get("probably_not_applicable", False), "handled_by": r.get("handled_by")}
                for r in self._c("case_tasks").find({"case_id": case_id})]

    def add_task(self, case_id: UUID, template: dict, status: str, due_on: date | None, *, needs_check: bool = False,
                 probably_not_applicable: bool = False, handled_by: str | None = None) -> UUID:
        """A task pinned to one template version. Active cases on writable accounts only."""
        case = self._case_for_write(case_id, drafts_on_read_only=False)
        if case["status"] == "draft":
            raise denied()
        task_id = uuid.uuid4()
        self._c("case_tasks").insert_one({
            "_id": task_id, "case_id": case_id, "template_id": template["id"], "task_key": template["task_key"],
            "status": status, "due_on": from_date(due_on), "snoozed_until": None,
            "completed_at": self.now if status == "done" else None, "selected": True,
            "needs_check": needs_check, "probably_not_applicable": probably_not_applicable,
            "handled_by": handled_by if status == "handled_elsewhere" else None,
            "created_at": self.now, "updated_at": self.now})
        return task_id

    def update_task(self, case_id: UUID, task_id: UUID, *, status: str = UNSET, snoozed_until=UNSET,
                    selected: bool = UNSET, only_from: tuple[str, ...] | None = None, needs_check: bool = UNSET,
                    probably_not_applicable: bool = UNSET, handled_by: str | None = UNSET) -> bool:
        """Change a task's status, snooze, or selected flag. Returns False when nothing matched, like an
        UPDATE that row-level security filtered out. Done records the completion time once. A change to
        status or snooze is activity on the case. A change the rules make (selected) is not."""
        case = self._c("cases").find_one({"_id": case_id, **self._visible()})
        if case is None or not self._member(case, WRITE_ROLES) or not self.account_can_write():
            return False
        query: dict = {"_id": task_id, "case_id": case_id}
        if only_from is not None:
            query["status"] = {"$in": list(only_from)}
        row = self._c("case_tasks").find_one(query)
        if row is None:
            return False
        changes: dict = {"updated_at": self.now}
        if status is not UNSET:
            changes["status"] = status
            changes["completed_at"] = (row["completed_at"] or self.now) if status == "done" else None
            if status != "handled_elsewhere":
                changes["handled_by"] = None
            if status in ("done", "handled_elsewhere"):
                changes["needs_check"] = False
        if snoozed_until is not UNSET:
            changes["snoozed_until"] = snoozed_until
        if selected is not UNSET:
            changes["selected"] = selected
        if needs_check is not UNSET:
            changes["needs_check"] = needs_check
        if probably_not_applicable is not UNSET:
            changes["probably_not_applicable"] = probably_not_applicable
        if handled_by is not UNSET:
            changes["handled_by"] = handled_by
        self._c("case_tasks").update_one({"_id": task_id}, {"$set": changes})
        if (status is not UNSET and status != row["status"]) or (
                snoozed_until is not UNSET and snoozed_until != row["snoozed_until"]):
            self._activity(case_id)
        return True

    # -------------------------------------------------------------- context records (CONTEXT_ITEM)

    def read_context(self, case_id: UUID, key: str) -> dict | None:
        if self._c("cases").find_one({"_id": case_id, **self._visible()}, {"_id": 1}) is None:
            return None
        row = self._c("context_items").find_one({"case_id": case_id, "item_key": key}, {"payload": 1})
        return row["payload"] if row else None

    def write_context(self, case_id: UUID, key: str, payload: dict) -> None:
        case = self._case_for_write(case_id, drafts_on_read_only=False)
        if not self._member(case, WRITE_ROLES):
            raise denied()
        self._c("context_items").update_one(
            {"case_id": case_id, "item_key": key},
            {"$set": {"payload": payload, "updated_at": self.now}, "$setOnInsert": {"expires_at": None}}, upsert=True)

    def lock_context(self, case_id: UUID, key: str, empty: dict) -> dict:
        """Read a context item for update, creating it first. Writing it inside the transaction takes the
        document's write lock, so a concurrent writer gets a write conflict instead of a lost update."""
        self._case_for_write(case_id, drafts_on_read_only=False)
        row = self._c("context_items").find_one_and_update(
            {"case_id": case_id, "item_key": key},
            {"$set": {"updated_at": self.now}, "$setOnInsert": {"payload": empty, "expires_at": None}},
            upsert=True, return_document=True)
        return row["payload"]

    def context_items(self, case_id: UUID) -> list[dict]:
        self._case(case_id)
        return list(self._c("context_items").find({"case_id": case_id},
                                                  {"_id": 0, "item_key": 1, "payload": 1, "updated_at": 1},
                                                  sort=[("item_key", 1)]))

    # -------------------------------------------------------------- keeping in touch (account D-13)

    def notification_preferences(self) -> dict | None:
        """The account's notification choices, or None before UC-REG-15."""
        if self.user_id is None:
            return None
        return self._c("notification_preferences").find_one({"_id": self.user_id})

    def save_notification_preferences(self, values: dict) -> dict:
        """UC-REG-15, UC-REG-17, UC-CASE-19. Insert or change the account's choices. Always free: no write check
        on the account, never gated on onboarding past the account existing (D-2026-09-25-F1). in_app is always
        kept. A push endpoint is kept only while browser is chosen."""
        uid = self.require_user()
        if not set(values) <= NOTIFICATION_FIELDS:
            raise denied()
        current = self.notification_preferences() or DEFAULT_NOTIFICATIONS
        merged = {**{k: current[k] for k in NOTIFICATION_FIELDS}, **values}
        merged["channels"] = [c for c in ("email", "in_app", "browser") if c in merged["channels"] or c == "in_app"]
        if "browser" not in merged["channels"]:
            merged["browser_push_endpoint"] = None
        if not notification_values_valid(merged):
            raise RuleViolation("check_violation", "notification_choice_valid")
        self._c("notification_preferences").update_one({"_id": uid}, {"$set": {**merged, "updated_at": self.now}},
                                                       upsert=True)
        return self.notification_preferences()

    def any_email_choice(self) -> bool:
        p = self.notification_preferences()
        return bool(p) and "email" in p["channels"]

    def notifications_sent(self, case_id: UUID) -> list[dict]:
        self._case(case_id)
        return list(self._c("notification_log").find(
            {"case_id": case_id}, {"_id": 0, "reason": 1, "channel": 1, "sent_at": 1}, sort=[("sent_at", 1)]))

    # -------------------------------------------------------------- sessions (D-20)

    def mark_subscribe_prompt_shown(self) -> bool:
        """UC-SUB-01. At most once a day. Returns False when it was already shown today."""
        u = self._user()
        today = from_date(local_date(self.now, u.get("time_zone")))
        return bool(self._c("users").update_one({"_id": u["_id"], "subscribe_prompt_shown_on": {"$ne": today}},
                                                {"$set": {"subscribe_prompt_shown_on": today}}).modified_count)

    # -------------------------------------------------------------- the subscription (cairn-subscription-use-cases)

    def add_subscription_consent(self, policy_version: str, price_shown: str, auth_provider: str | None,
                                 client: str | None) -> None:
        """UC-SUB-02. Append-only, with the copy version and the price shown."""
        self._c("consents").insert_one({
            "_id": uuid.uuid4(), "user_id": self.require_user(), "purpose": "subscription_terms",
            "policy_version": policy_version, "granted_at": self.now, "auth_provider": auth_provider,
            "client": client, "price_shown": price_shown})

    def subscription_consent_since(self, since: datetime) -> bool:
        return self._c("consents").find_one({"user_id": self.require_user(), "purpose": "subscription_terms",
                                             "granted_at": {"$gte": since}}, {"_id": 1}) is not None

    def update_subscription(self, **fields) -> None:
        """The caller's own subscription fields, from the subscription routes (a Checkout Session id, a cancel
        request time). Nothing outside SUBSCRIPTION_FIELDS."""
        uid = self.require_user()
        if not set(fields) <= SUBSCRIPTION_FIELDS:
            raise denied()
        self._c("users").update_one({"_id": uid}, {"$set": fields})

    def queue_confirmation(self, action_type: str, *, replace_unsent: bool = False) -> None:
        """UC-CASE-21. One confirmation of something the user just did, to the sign-in email. Sent even during a
        break (AC-BRK-07). replace_unsent keeps only one of this type waiting, so several Settings changes in a row
        send one message."""
        u = self._user()
        if replace_unsent:
            self._c("action_confirmation_outbox").delete_many({"user_id": u["_id"], "action_type": action_type,
                                                               "claimed_at": None})
        queue_confirmation(self._db, self._cs, action_type, u["email"], u["_id"], self.now)

    # -------------------------------------------------------------- Stripe events (UC-SUB-22)
    # Called only by the webhook route, after the Stripe signature checked out. The session has no user: the event
    # names the account by its id (client_reference_id) or its Stripe customer id, never by email.

    def record_stripe_event(self, event_id: str, event_type: str, account_id: UUID | None) -> bool:
        """Records an event once, by Stripe's id. Returns False when it was already applied (a repeat delivery)."""
        events = self._c("stripe_events")
        row = events.find_one({"_id": event_id}, {"processed_at": 1})
        if row is not None:
            return row["processed_at"] is None
        events.insert_one({"_id": event_id, "type": event_type, "account_id": account_id, "received_at": self.now,
                           "processed_at": None, "attempts": 0})
        return True

    def finish_stripe_event(self, event_id: str, *, processed: bool, account_id: UUID | None = None) -> None:
        changes: dict = {"processed_at": self.now} if processed else {}
        if account_id is not None:
            changes["account_id"] = account_id
        self._c("stripe_events").update_one({"_id": event_id}, {"$set": changes, "$inc": {"attempts": 1}})

    def stripe_account(self, account_id: UUID | None = None, customer_id: str | None = None) -> dict | None:
        """The account a Stripe object belongs to. Never looked up by email (UC-SUB-22)."""
        users = self._c("users")
        row = users.find_one({"_id": account_id}) if account_id is not None else None
        if row is None and customer_id:
            row = users.find_one({"stripe_customer_id": customer_id})
        return row

    def queue_confirmation_for(self, user_id: UUID, email: str, action_type: str) -> None:
        """A confirmation from a Stripe event (the user subscribed). Queued once per subscription (UC-SUB-04)."""
        queue_confirmation(self._db, self._cs, action_type, email, user_id, self.now)

    def apply_stripe_state(self, user_id: UUID, fields: dict) -> None:
        """Sets subscription fields from what Stripe reports now (SUB-D-02). Nothing outside SUBSCRIPTION_FIELDS,
        so an event can never touch the trial, a break, or a case."""
        if not set(fields) <= SUBSCRIPTION_FIELDS:
            raise denied()
        self._c("users").update_one({"_id": user_id}, {"$set": fields})
