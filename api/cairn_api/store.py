"""The data-access layer. Every read and write the API makes goes through a Session here.

MongoDB has no row-level security, so this module is where the case boundary
lives. It takes the place of the PostgreSQL policies, SECURITY DEFINER
functions, and triggers that held it until 2026-10-01. The rules:

* The case is the security boundary. A case is visible to its creator and its
  active members. Case data (answers, tasks, the deceased, context, notification
  preferences) is reached only through a case id that passed that check. A case
  that isn't visible looks exactly like one that doesn't exist.
* Writes to case data need an owner or co_executor membership, and also:
  an account that can write (onboarding complete, not read-only), or for a
  draft, an account that can edit drafts (read-only included, UC-CASE-18).
  Deleting and notification preferences never check either (D-2026-09-25-F1).
* Account fields the user can't set (onboarding_step, status, the trial) are
  written only by advance_onboarding and start_journey. trial_started_at is
  written once, with a filter that only matches while it is unset.
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
WRITABLE_STATUSES = ("active_no_case", "trial_active", "subscribed")
STEPS = ("account_created", "privacy_terms_accepted", "trial_terms_accepted", "ai_notice_accepted",
         "preferred_name_saved", "complete")
CONSENT_FOR_STEP = {"privacy_terms_accepted": "privacy_terms", "trial_terms_accepted": "trial_terms",
                    "ai_notice_accepted": "ai_notice"}
# The account fields the app may set directly (Settings and onboarding screens).
ACCOUNT_FIELDS = {"preferred_name", "name_pronunciation", "name_prefill", "voice", "time_zone"}
# The case fields the app may set directly. status and the journey start belong to start_journey,
# deletion_requested_at to request_case_deletion. journey_template_key can change on an active case
# when an answer changes the base path (UC-CASE-09).
CASE_FIELDS = {"tasks_paused_until", "purge_after", "last_intake_step", "death_not_yet_occurred", "skip_explainers",
               "name_fallback", "attorney_triggers", "shown_notices", "journey_template_key"}
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


def effective_account_status(status: str, trial_ends_at: datetime | None, now: datetime) -> str:
    """read_only is derived from trial_ends_at, so enforcement never waits for the expire_trials job."""
    if status in ("subscribed", "pending_deletion", "pending_onboarding"):
        return status
    if trial_ends_at is not None and now >= trial_ends_at:
        return "read_only"
    return status


def denied() -> RuleViolation:
    return RuleViolation("insufficient_privilege")


class _Bound:
    """A collection whose every call runs in the session's transaction."""

    def __init__(self, coll, cs):
        self._coll, self._cs = coll, cs

    def __getattr__(self, name):
        return functools.partial(getattr(self._coll, name), session=self._cs)


def delete_case(db, cs, case_id: UUID) -> bool:
    """Deletes a case and everything in it. Audit rows are kept. Shared with the jobs (maintenance.py)."""
    deleted = db.cases.delete_one({"_id": case_id}, session=cs).deleted_count
    db.notification_preferences.delete_one({"_id": case_id}, session=cs)
    for name in CASE_SCOPED:
        db[name].delete_many({"case_id": case_id}, session=cs)
    return bool(deleted)


def audit_doc(actor_id, action: str, case_id=None, object_type=None, object_id=None, *, now: datetime) -> dict:
    return {"_id": uuid.uuid4(), "actor_id": actor_id, "case_id": case_id, "action": action,
            "object_type": object_type, "object_id": object_id, "occurred_at": now}


def queue_confirmation(db, cs, action_type: str, email: str, user_id: UUID | None, now: datetime) -> None:
    """The one confirmation for a deletion the user asked for (UC-CASE-21). The address is purged once sent."""
    db.action_confirmation_outbox.insert_one(
        {"_id": uuid.uuid4(), "action_type": action_type, "email": email, "user_id": user_id, "queued_at": now,
         "claimed_at": None, "attempts": 0, "last_error": None}, session=cs)


def setting_int(db, cs, key: str) -> int:
    row = db.app_settings.find_one({"_id": key}, session=cs)
    if row is None:
        raise RuntimeError(f"app setting {key} is missing. Run database/db/apply.py.")
    return int(row["value"])


# ------------------------------------------------------------------ value rules

_DATE_CHARS = set("0123456789-")


def _iso_date(v) -> bool:
    return isinstance(v, str) and len(v) == 10 and set(v) <= _DATE_CHARS and v[4] == v[7] == "-"


def _state(v) -> bool:
    return isinstance(v, str) and len(v) == 2 and v.isascii() and v.isupper() and v.isalpha()


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
COMPLETED_ITEMS = {"death_pronounced", "funeral_provider_chosen", "funeral_home_has_ssn", "certificates_ordered",
                   "ssa_notified", "bank_notified", "other", "none_or_unsure"}


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
        if not isinstance(v, dict) or set(v) != {"county_or_city", "outside_us", "state"}:
            return False
        return (isinstance(v["outside_us"], bool) and (v["state"] is None or _state(v["state"]))
                and (v["county_or_city"] is None or (isinstance(v["county_or_city"], str)
                                                     and 1 <= len(v["county_or_city"]) <= 100))
                and not (v["outside_us"] and v["state"] is not None))
    if key == "residence_state":
        if not isinstance(v, dict) or set(v) != {"choice", "state"}:
            return False
        return (v["choice"] in ("same_as_place_of_death", "different", "unknown")
                and (v["state"] is None or (v["choice"] == "different" and _state(v["state"]))))
    if key == "completed_items":
        return (isinstance(v, list) and len(v) >= 1 and all(isinstance(e, str) and e in COMPLETED_ITEMS for e in v)
                and len(set(v)) == len(v) and ("none_or_unsure" not in v or len(v) == 1))
    return False


def notification_choice_valid(channels: list, reasons: list, lead_days, inactivity_days) -> bool:
    """in_app_only stands alone with no reasons. Any other channel needs a reason, and each reason its timing."""
    return (len(channels) >= 1 and set(channels) <= {"email", "push", "in_app_only"}
            and len(set(channels)) == len(channels)
            and set(reasons) <= {"due_date_upcoming", "inactivity"} and len(set(reasons)) == len(reasons)
            and ((len(channels) == 1 and not reasons) if "in_app_only" in channels else len(reasons) >= 1)
            and (lead_days is not None) == ("due_date_upcoming" in reasons)
            and (inactivity_days is not None) == ("inactivity" in reasons)
            and lead_days in (None, 1, 3, 7) and inactivity_days in (None, 3, 7, 14))


# ------------------------------------------------------------------ the session

class Session:
    """One request's transaction, as one user. Every method enforces the rules in the module docstring."""

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
        row = self._c("users").find_one({"idp_subject": idp_subject}, {"_id": 1})
        return row["_id"] if row else None

    def sign_in_method_for_email(self, email: str) -> str | None:
        """Which method already owns an email address, or None. Called only with the caller's own verified
        email, so it answers "which method did I use", never "does this other person have an account"."""
        row = self._c("users").find_one({"email_lower": email.lower()}, {"sign_in_method": 1})
        return (row.get("sign_in_method") or "unknown") if row else None

    def create_account(self, idp_subject: str, email: str, sign_in_method: str,
                       name_prefill: str | None = None, time_zone: str | None = None) -> UUID:
        """Creates the account in pending_onboarding, or returns the existing one for the same subject.
        No legal name is collected. A provider-shared name is kept only as a pre-fill (UC-REG-02 to 04)."""
        users = self._c("users")
        existing = users.find_one({"idp_subject": idp_subject}, {"_id": 1})
        if existing:
            users.update_one({"_id": existing["_id"]}, {"$set": {"email": email, "email_lower": email.lower()}})
            return existing["_id"]
        uid = uuid.uuid4()
        users.insert_one({
            "_id": uid, "idp_subject": idp_subject, "email": email, "email_lower": email.lower(),
            "sign_in_method": sign_in_method, "preferred_name": None, "name_pronunciation": None,
            "name_prefill": name_prefill, "voice": "steady_direct", "time_zone": time_zone,
            "onboarding_step": "account_created", "status": "pending_onboarding",
            "trial_started_at": None, "trial_ends_at": None, "created_at": self.now})
        return uid

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

    def load_account(self) -> dict | None:
        """The caller's account, with status as the effective status (read_only after the trial)."""
        u = self._user()
        if u is None:
            return None
        out = {k: u.get(k) for k in ("email", "sign_in_method", "preferred_name", "name_pronunciation",
                                     "name_prefill", "voice", "time_zone", "onboarding_step", "trial_started_at",
                                     "trial_ends_at", "created_at")}
        return {**out, "id": u["_id"], "status": effective_account_status(u["status"], u["trial_ends_at"], self.now)}

    def account_can_write(self) -> bool:
        """Onboarding complete and not read-only (D-05). Deleting never depends on this."""
        a = self.load_account()
        return bool(a) and a["onboarding_step"] == "complete" and a["status"] in WRITABLE_STATUSES

    def account_can_edit_drafts(self) -> bool:
        """UC-CASE-18. A read-only account can still create and edit drafts."""
        a = self.load_account()
        return bool(a) and a["onboarding_step"] == "complete" and a["status"] in (*WRITABLE_STATUSES, "read_only")

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
        if to == "preferred_name_saved" and u.get("preferred_name") is None:
            raise RuleViolation("prerequisite")
        changes: dict = {"onboarding_step": to}
        if to == "preferred_name_saved":
            changes["name_prefill"] = None
        if to == "complete" and u["status"] == "pending_onboarding":
            changes["status"] = "active_no_case"
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
        """UC-REG-15. Everything goes now, in this transaction: every case the user created in any status
        (holds included) with all of its data, their memberships, consents, reminders, and the account.
        Pending case confirmations are dropped so exactly one confirmation goes out, and it has no user id.
        The identity provider cleanup is queued (Apple token revocation, TN3194). Audit rows keep ids only.
        Never checks whether the account can write (D-2026-09-25-F1)."""
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
        self._c("identity_deletion_requests").insert_one(
            {"_id": uuid.uuid4(), "idp_subject": u["idp_subject"], "provider": u.get("sign_in_method"),
             "requested_at": self.now, "attempts": 0, "last_error": None})
        self._c("consents").delete_many({"user_id": uid})
        self._c("trial_reminders").delete_many({"user_id": uid})
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
            "shown_notices": [], "tasks_paused_until": None, "deletion_requested_at": None})
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
        started = self._c("users").update_one({"_id": uid, "trial_started_at": None}, [{"$set": {
            "trial_started_at": self.now, "trial_ends_at": ends,
            "status": {"$cond": [{"$eq": ["$status", "active_no_case"]}, "trial_active", "$status"]}}}])
        trial_started = bool(started.modified_count)
        if trial_started:
            remind = timedelta(days=self.setting_int("trial_reminder_days_before"))
            self._c("trial_reminders").insert_many([
                {"_id": uuid.uuid4(), "user_id": uid, "kind": kind, "due_at": due, "email_sent_at": None}
                for kind, due in (("trial_day_21", self.now + timedelta(hours=504)),
                                  ("trial_day_27", self.now + timedelta(hours=648)),
                                  ("trial_ends_soon", ends - remind))])
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
                "jurisdiction": t["jurisdiction"], "template_id": t["id"]})
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
        return [{"id": r["_id"], "status": r["status"], "selected": r["selected"], "task_key": r["task_key"]}
                for r in self._c("case_tasks").find({"case_id": case_id})]

    def add_task(self, case_id: UUID, template: dict, status: str, due_on: date | None) -> UUID:
        """A task pinned to one template version. Active cases on writable accounts only."""
        case = self._case_for_write(case_id, drafts_on_read_only=False)
        if case["status"] == "draft":
            raise denied()
        task_id = uuid.uuid4()
        self._c("case_tasks").insert_one({
            "_id": task_id, "case_id": case_id, "template_id": template["id"], "task_key": template["task_key"],
            "status": status, "due_on": from_date(due_on), "snoozed_until": None,
            "completed_at": self.now if status == "done" else None, "selected": True,
            "created_at": self.now, "updated_at": self.now})
        return task_id

    def update_task(self, case_id: UUID, task_id: UUID, *, status: str = UNSET, snoozed_until=UNSET,
                    selected: bool = UNSET, only_from: tuple[str, ...] | None = None) -> bool:
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
        if snoozed_until is not UNSET:
            changes["snoozed_until"] = snoozed_until
        if selected is not UNSET:
            changes["selected"] = selected
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

    # -------------------------------------------------------------- keeping in touch (UC-CASE-19, UC-CASE-20)

    def notification_preferences(self, case_id: UUID) -> dict | None:
        if self._c("cases").find_one({"_id": case_id, **self._visible()}, {"_id": 1}) is None:
            return None
        row = self._c("notification_preferences").find_one({"_id": case_id})
        return {**row, "case_id": row.pop("_id")} if row else None

    def save_notification_preferences(self, case_id: UUID, values: dict) -> None:
        """Insert or replace one journey's choice. Always free: no write check on the account
        (D-2026-09-25-F1). Owner or co_executor only."""
        case = self._case(case_id)
        if not self._member(case, WRITE_ROLES):
            raise denied()
        if not notification_choice_valid(values["channels"], values["reasons"], values["due_date_lead_days"],
                                         values["inactivity_days"]):
            raise RuleViolation("check_violation", "notification_choice_valid")
        self._c("notification_preferences").update_one(
            {"_id": case_id}, {"$set": {**values, "updated_at": self.now}}, upsert=True)

    def ensure_default_preferences(self, case_id: UUID) -> None:
        """Every started journey has a row. Skipped or never asked means in_app_only."""
        case = self._case(case_id)
        if not self._member(case, WRITE_ROLES):
            raise denied()
        self._c("notification_preferences").update_one({"_id": case_id}, {"$setOnInsert": {
            "channels": ["in_app_only"], "reasons": [], "due_date_lead_days": None, "inactivity_days": None,
            "frequency": "daily_max", "push_permission_granted": False, "updated_at": self.now}}, upsert=True)

    def any_email_choice(self) -> bool:
        ids = [c["id"] for c in self.owned_cases()]
        return self._c("notification_preferences").find_one(
            {"_id": {"$in": ids}, "channels": "email"}, {"_id": 1}) is not None

    def same_as_candidate(self, case_id: UUID) -> UUID | None:
        """The user's first other journey that has a choice."""
        others = [c["id"] for c in self.owned_cases() if c["id"] != case_id]
        chosen = {r["_id"] for r in self._c("notification_preferences").find({"_id": {"$in": others}}, {"_id": 1})}
        return next((c for c in others if c in chosen), None)

    def notifications_sent(self, case_id: UUID) -> list[dict]:
        self._case(case_id)
        return list(self._c("notification_log").find(
            {"case_id": case_id}, {"_id": 0, "reason": 1, "channel": 1, "sent_at": 1}, sort=[("sent_at", 1)]))
