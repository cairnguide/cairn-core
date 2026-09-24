"""Account state: onboarding progress, trial dates, read-only, and acknowledgment versions.

The database is the enforced backstop (restrictive row-level security in
migration 0008). The checks here exist so the user gets a plain answer and a
next step instead of a bare 403.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

from fastapi import Request

from .copy_store import Copy, consent_versions
from .db import Session
from .errors import ApiError
from .schemas import AccountOut, ConsentType, NextStep, Note, OnboardingStep, Option

ACCOUNT_COLUMNS = """id, email, sign_in_method, preferred_name, name_pronunciation, name_prefill, voice,
  time_zone, onboarding_step, trial_started_at, trial_ends_at, created_at,
  cairn.effective_account_status(status, trial_ends_at) AS status"""

# The onboarding step that records each acknowledgment.
CONSENT_STEP = {
    ConsentType.privacy_terms: OnboardingStep.privacy_terms_accepted,
    ConsentType.trial_terms: OnboardingStep.trial_terms_accepted,
    ConsentType.ai_notice: OnboardingStep.ai_notice_accepted,
}
STEP_ORDER = list(OnboardingStep)


def step_reached(account: dict, step: OnboardingStep) -> bool:
    return STEP_ORDER.index(OnboardingStep(account["onboarding_step"])) >= STEP_ORDER.index(step)


def load_account(s: Session) -> dict:
    uid = s.require_user()
    return s.one(f"SELECT {ACCOUNT_COLUMNS} FROM cairn.users WHERE id = %s", (uid,))


def has_case(s: Session) -> bool:
    return s.one("SELECT EXISTS (SELECT 1 FROM cairn.cases WHERE created_by = cairn.current_user_id()) AS x")["x"]


def accepted_versions(s: Session) -> dict[str, set[str]]:
    rows = s.all("SELECT purpose, policy_version FROM cairn.consents WHERE user_id = cairn.current_user_id()")
    out: dict[str, set[str]] = {}
    for r in rows:
        out.setdefault(r["purpose"], set()).add(r["policy_version"])
    return out


def current_versions(request: Request) -> dict[str, str]:
    st = request.app.state
    return consent_versions(st.copy, st.settings.privacy_version, st.settings.terms_version)


def stale_acknowledgment(s: Session, request: Request, account: dict) -> ConsentType | None:
    """The first acknowledgment already passed in onboarding whose current version the user hasn't agreed to."""
    current = current_versions(request)
    accepted = accepted_versions(s)
    for ctype, step in CONSENT_STEP.items():
        if step_reached(account, step) and current[ctype.value] not in accepted.get(ctype.value, set()):
            return ctype
    return None


# ------------------------------------------------------------------ dates

def local_trial_end(account: dict) -> date | None:
    ends = account.get("trial_ends_at")
    if ends is None:
        return None
    return ends.astimezone(ZoneInfo(account.get("time_zone") or "UTC")).date()


def format_date(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


# ------------------------------------------------------------------ output

def account_out(account: dict, copy: Copy) -> AccountOut:
    ai_ack = step_reached(account, OnboardingStep.ai_notice_accepted)
    return AccountOut(
        **{k: account[k] for k in ("id", "email", "sign_in_method", "preferred_name", "name_pronunciation",
                                   "voice", "status", "onboarding_step", "trial_started_at",
                                   "trial_ends_at", "time_zone")},
        trial_end_date=local_trial_end(account),
        ai_label=copy["ai_persistent_label"] if ai_ack else None,
    )


def account_notes(s: Session, account: dict, copy: Copy) -> list[Note]:
    """The read-only banner, or a trial reminder that is due, for in-app display."""
    if account["status"] == "read_only":
        return [Note(kind="account", text=copy["read_only_banner"])]
    if account["status"] != "trial_active":
        return []
    due = s.one("SELECT kind FROM cairn.trial_reminders WHERE user_id = cairn.current_user_id() "
                "AND due_at <= now() ORDER BY due_at DESC LIMIT 1")
    if due is None:
        return []
    return [Note(kind="reminder", text=reminder_text(due["kind"], account, copy))]


def reminder_text(kind: str, account: dict, copy: Copy) -> str:
    key = {"trial_day_21": "trial_reminder_day_21", "trial_day_27": "trial_reminder_day_27"}[kind]
    return copy[key].format(trial_end_date=format_date(local_trial_end(account)))


# ------------------------------------------------------------------ gates for case, journey, and task routes

@dataclass
class Ready:
    account: dict
    notes: list[Note]


def require_ready(s: Session, request: Request, *, write: bool) -> Ready:
    """Onboarding finished, acknowledgments current, and (for writes) not read-only."""
    copy: Copy = request.app.state.copy
    account = load_account(s)
    if account["onboarding_step"] != OnboardingStep.complete.value:
        raise ApiError(409, "onboarding_incomplete", copy["onboarding_incomplete"],
                       next_step=NextStep(action="resume_onboarding", prompt=copy["resume_onboarding"]).model_dump())
    stale = stale_acknowledgment(s, request, account)
    if stale is not None:
        raise ApiError(409, "acknowledgment_required", copy["acknowledgment_version_changed"],
                       acknowledgment=stale.value,
                       next_step=NextStep(action="resume_onboarding", prompt=copy["resume_onboarding"]).model_dump())
    if write and account["status"] not in ("active_no_case", "trial_active", "subscribed"):
        raise ApiError(403, "account_read_only", copy["read_only_banner"], next_step=subscribe_step(copy).model_dump())
    return Ready(account=account, notes=account_notes(s, account, copy))


def subscribe_step(copy: Copy) -> NextStep:
    return NextStep(action="choose_subscription", prompt=copy["read_only_banner"],
                    options=[Option(value="subscribe", label=copy["subscribe_button"])])


def trial_started_now(s: Session) -> dict | None:
    """The account row if this transaction just started the trial (the first case was created)."""
    return s.one(f"SELECT {ACCOUNT_COLUMNS} FROM cairn.users WHERE id = cairn.current_user_id() "
                 "AND trial_started_at = now()")


def trial_start_note(account: dict, copy: Copy) -> Note:
    return Note(kind="account", text=copy["case_created_trial_start"].format(
        trial_end_date=format_date(local_trial_end(account))))
