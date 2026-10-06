"""Account state: onboarding progress, the adult check, trial dates, access, the subscription, and acknowledgment
versions (account spec 3.2.0, D-19).

store.Session is the enforced backstop (account_can_write and
account_can_edit_drafts on every case write). The checks here exist so the user
gets a plain answer and a next step instead of a bare 403.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Query, Request

from .copy_store import Copy, consent_versions
from .db import Session
from .errors import ApiError
from .schemas import AccountOut, ConsentType, NextStep, Note, OnboardingStep, Option, SessionPolicy

# A read route's ?care_level=. The level is the client's, never stored (decision 7).
CareLevel = Annotated[int, Query(ge=1, le=4, description="The session's care level. At levels 2 to 4, no read-only "
                                                         "banner or billing notice (UC-SUB-01, UC-SUB-21).")]

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
    s.require_user()
    return s.load_account()


def has_case(s: Session) -> bool:
    return s.has_case()


def accepted_versions(s: Session) -> dict[str, set[str]]:
    return s.accepted_versions()


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


def adult_needed(account: dict) -> bool:
    """UC-REG-06. Asked of every account that hasn't answered yes, including accounts made before the question
    existed. An account that answered no stays stopped."""
    return account.get("adult_attested") is not True


# ------------------------------------------------------------------ state (D-19)

def is_read_only(account: dict) -> bool:
    return account["access"] == "read_only"


def has_subscription(account: dict) -> bool:
    return account["subscription_status"] == "active"


def on_free_days(account: dict) -> bool:
    """The 28 free days are running (a care rest that stopped them keeps them running)."""
    return bool(account.get("free_days_running"))


def can_write(account: dict) -> bool:
    return (account["onboarding_step"] == OnboardingStep.complete.value and account["status"] == "setup_complete"
            and not adult_needed(account) and not is_read_only(account))


# ------------------------------------------------------------------ dates

def local_trial_end(account: dict) -> date | None:
    ends = account.get("trial_ends_at")
    if ends is None:
        return None
    return ends.astimezone(ZoneInfo(account.get("time_zone") or "UTC")).date()


def local_date(account: dict, when) -> date | None:
    if when is None:
        return None
    return when.astimezone(ZoneInfo(account.get("time_zone") or "UTC")).date()


def format_date(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def mask_email(email: str) -> str:
    """Where a confirmation or email will go, shown masked (UC-CASE-19, UC-CASE-21).

    The domain stays whole so the user can recognize it, including an Apple
    private relay address (privaterelay.appleid.com).
    """
    local, _, domain = email.rpartition("@")
    if not local:
        return "•••"
    return f"{local[0]}•••{local[-1] if len(local) > 3 else ''}@{domain}"


# ------------------------------------------------------------------ output

def session_policy(request: Request) -> SessionPolicy:
    st = request.app.state.settings
    copy: Copy = request.app.state.copy
    return SessionPolicy(inactivity_timeout_seconds=st.inactivity_timeout_seconds,
                         warning_before_timeout_seconds=st.warning_before_timeout_seconds,
                         overall_session_days=st.overall_session_days, timeout_warning=copy["timeout_warning"],
                         timeout_warning_button=copy["timeout_warning_button"],
                         session_timed_out=copy["session_timed_out"], signed_out=copy["signed_out"])


def account_out(account: dict, copy: Copy) -> AccountOut:
    ai_ack = step_reached(account, OnboardingStep.ai_notice_accepted)
    return AccountOut(
        **{k: account[k] for k in ("id", "email", "sign_in_method", "preferred_name", "name_pronunciation",
                                   "voice", "status", "access", "subscription_status", "adult_attested",
                                   "onboarding_step", "trial_started_at", "trial_ends_at", "time_zone",
                                   "free_days_running", "on_break")},
        linked_sign_in_methods=account.get("linked_sign_in_methods", []),
        trial_end_date=local_trial_end(account),
        ai_label=copy["ai_persistent_label"] if ai_ack else None,
    )


def account_notes(s: Session, account: dict, request: Request, *, care_level: int = 1) -> list[Note]:
    """What to show inside Cairn about the account: the read-only banner, a payment problem, or the trial-ending
    note when it is due. Nothing about billing or the trial at care levels 2 to 4 (UC-SUB-21, AC-26-03), and no
    payment notice during a break (UC-SUB-09)."""
    if care_level >= 2:
        return []
    copy: Copy = request.app.state.copy
    sub: Copy = request.app.state.subscription_copy
    notes: list[Note] = []
    if is_read_only(account):
        notes.append(Note(kind="account", text=sub["read_only_banner"]))
    elif has_subscription(account) and not account["on_break"] and account["billing_notice"] != "none":
        key = "payment_failed_inapp" if account["billing_notice"] == "payment_failed" else "action_required_inapp"
        notes.append(Note(kind="account", text=sub[key]))
    if on_free_days(account) and s.latest_due_reminder_kind() is not None:
        notes.append(Note(kind="reminder", text=reminder_text(account, copy)))
    if (change := price_change_note(request, account)) is not None:
        notes.append(change)
    return notes


def price_change_note(request: Request, account: dict) -> Note | None:
    """UC-SUB-18. A scheduled new price, shown to a subscriber at care level 1 from 30 days before it applies, and
    not during a break (Cairn's own billing wording waits, SUB-D-08)."""
    st = request.app.state.settings
    if not st.price_change_effective_date or not has_subscription(account) or account["on_break"]:
        return None
    effective = datetime.fromisoformat(st.price_change_effective_date).replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if not effective - timedelta(days=30) <= now < effective:
        return None
    sub: Copy = request.app.state.subscription_copy
    return Note(kind="account", text=sub["price_change_body"].format(
        effective_date=format_date(effective.date()), new_price=st.price_change_new_price),
        legal_review_required=True)


def reminder_text(account: dict, copy: Copy) -> str:
    """The trial-ending note (account D-14), or for an early subscriber, that the first payment is coming
    (UC-SUB-06). The same text in Cairn and by email."""
    key = "trial_reminder_first_payment" if has_subscription(account) else "trial_reminder_ends_soon"
    return copy[key].format(trial_end_date=format_date(local_trial_end(account)))


# ------------------------------------------------------------------ gates for case, journey, and task routes

@dataclass
class Ready:
    account: dict
    notes: list[Note]


def require_ready(s: Session, request: Request, *, write: bool) -> Ready:
    """Setup finished, adult confirmed, acknowledgments current, and (for writes) not read-only."""
    copy: Copy = request.app.state.copy
    # A care rest that ended on its own starts the free days again before anything reads them (DEC-26-01).
    s.settle_trial_clock()
    account = load_account(s)
    resume = NextStep(action="resume_onboarding", prompt=copy["resume_onboarding"]).model_dump()
    if account["onboarding_step"] != OnboardingStep.complete.value:
        raise ApiError(409, "onboarding_incomplete", copy["onboarding_incomplete"], next_step=resume)
    if adult_needed(account):
        raise ApiError(409, "acknowledgment_required", copy["age_question"], acknowledgment="adult",
                       next_step=resume)
    stale = stale_acknowledgment(s, request, account)
    if stale is not None:
        raise ApiError(409, "acknowledgment_required", copy["acknowledgment_version_changed"],
                       acknowledgment=stale.value, next_step=resume)
    if write and is_read_only(account):
        sub: Copy = request.app.state.subscription_copy
        raise ApiError(403, "account_read_only", sub["read_only_banner"], next_step=subscribe_step(sub).model_dump())
    return Ready(account=account, notes=account_notes(s, account, request, care_level=care_level_of(request)))


def care_level_of(request: Request) -> int:
    """The care level a read route was given (?care_level=), so the read-only banner and billing notices are left
    out at levels 2 to 4 (UC-SUB-01, UC-SUB-21). The level is the client's, never stored (decision 7)."""
    raw = request.query_params.get("care_level", "1")
    return int(raw) if raw in ("1", "2", "3", "4") else 1


def subscribe_step(sub: Copy, *, again: bool = False) -> NextStep:
    """UC-SUB-01. The way to the subscription terms, and Not now."""
    return NextStep(action="choose_subscription", prompt=sub["read_only_banner"],
                    options=[Option(value="subscribe", label=sub["subscribe_again" if again else "subscribe"]),
                             Option(value="not_now", label=sub["subscribe_prompt_not_now"])])
