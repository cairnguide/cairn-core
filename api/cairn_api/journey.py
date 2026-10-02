"""Journey reads and task rules shared by the journey, task, and status endpoints."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID

from .db import Session
from .schemas import (
    CertificateOrderRecord,
    InstitutionNotice,
    Note,
    SupportResource,
    TaskCategory,
    TaskKind,
    TaskStatus,
    TaskSummary,
)

if TYPE_CHECKING:
    from .intake import Ctx

# Task keys with a structured completion record (UC-10 and UC-11).
TASK_KINDS: dict[str, TaskKind] = {
    "order_death_certificates": TaskKind.certificate_order,
    "notify_banks": TaskKind.institution_notice,
    "notify_life_insurers": TaskKind.institution_notice,
    "notify_credit_bureaus": TaskKind.institution_notice,
}

# Grouping for the status view (UC-13). Keys come from the illustrative week
# table in docs/cairn-mvp-data-model.md. Unknown keys fall into "other".
# Follow-up: this belongs in the template content schema as a category field,
# so authors set it alongside the wording rather than in application code.
TASK_CATEGORIES: dict[str, TaskCategory] = {
    "order_death_certificates": TaskCategory.certificates,
    "ask_state_vital_records_office": TaskCategory.certificates,
    "confirm_pronouncement": TaskCategory.certificates,
    "medical_examiner_or_coroner": TaskCategory.certificates,
    "choose_funeral_provider": TaskCategory.funeral,
    "notify_social_security": TaskCategory.agencies,
    "confirm_funeral_home_reported_death": TaskCategory.agencies,
    "notify_va_if_veteran": TaskCategory.agencies,
    "notify_employer_and_pension": TaskCategory.agencies,
    "notify_banks": TaskCategory.financial_institutions,
    "notify_life_insurers": TaskCategory.financial_institutions,
    "notify_credit_bureaus": TaskCategory.financial_institutions,
    "secure_home_and_pets": TaskCategory.home_and_personal,
    "forward_mail": TaskCategory.home_and_personal,
    "cancel_subscriptions": TaskCategory.home_and_personal,
}

OPEN_STATUSES = {TaskStatus.not_started.value, TaskStatus.check_on_this.value, TaskStatus.in_progress.value,
                 TaskStatus.not_today.value}
SET_ASIDE_STATUSES = {TaskStatus.skipped.value, TaskStatus.not_applicable.value}

# Context item keys (database/db/schema.py, context_items).
CERT_ORDER = "CERT_ORDER"
BANK_NOTICES = "BANK_NOTICES"
MAX_NOTICES = 100  # keeps the payload well under the 32 KB column limit

@dataclass
class TaskContext:
    """What a task needs from the case's pinned journey rules: its waypoint, notes, and the attorney line."""
    waypoints: dict[str, str | None] = field(default_factory=dict)
    attached: dict[str, list[Note]] = field(default_factory=dict)
    loose_notes: list[Note] = field(default_factory=list)
    support: list[SupportResource] = field(default_factory=list)
    attorney_line: str | None = None


def task_context(c: Ctx, case: dict, answers: dict) -> TaskContext:
    from . import intake
    definition, sel = intake.selection(c.s, case, answers)
    loose, attached = intake.notes_by_task(c, definition, sel)
    return TaskContext(waypoints=definition["task_waypoints"], attached=attached, loose_notes=loose,
                       support=intake.support_resources(c, definition, sel),
                       attorney_line=c.copy["attorney_referral_line"])


def task_summary(row: dict, context: TaskContext | None = None) -> TaskSummary:
    context = context or TaskContext()
    line = (row["attorney_referral_note"] or context.attorney_line) if row["attorney_referral"] else None
    return TaskSummary(
        id=row["id"], task_key=row["task_key"], template_version=row["template_version"],
        title=row["title"], plain_summary=row["plain_summary"], journey_week=row["journey_week"],
        sort_order=row["sort_order"],
        category=TASK_CATEGORIES.get(row["task_key"], TaskCategory.other),
        kind=TASK_KINDS.get(row["task_key"], TaskKind.general),
        status=row["status"], due_on=row["due_on"], snoozed_until=row["snoozed_until"],
        completed_at=row["completed_at"], attorney_referral=row["attorney_referral"], attorney_line=line,
        why_now=row["why_now"], waypoint=context.waypoints.get(row["task_key"]),
        notes=context.attached.get(row["task_key"], []),
    )


def load_tasks(s: Session, case_id: UUID) -> list[dict]:
    """In journey order. A changed answer never hides a task the user has worked on (UC-CASE-09)."""
    return s.load_tasks(case_id)


def load_task(s: Session, case_id: UUID, task_id: UUID) -> dict | None:
    return s.load_task(case_id, task_id)


def pick_next_action(rows: list[dict], now: datetime | None = None) -> dict | None:
    """The single next thing to do: the first open, unsnoozed task in journey order.

    A task already in progress comes before one not yet started, so the user
    finishes what they began before something new is put in front of them.
    """
    now = now or datetime.now(timezone.utc)
    open_rows = [r for r in rows if r["status"] in OPEN_STATUSES
                 and (r["snoozed_until"] is None or r["snoozed_until"] <= now)]
    for r in open_rows:
        if r["status"] == TaskStatus.in_progress.value:
            return r
    return open_rows[0] if open_rows else None


def time_sensitive(rows: list[dict], limit: int = 2) -> list[dict]:
    """UC-CASE-13. The one or two open tasks due soonest. Done and set-aside tasks never count."""
    open_rows = [r for r in rows if r["status"] in OPEN_STATUSES]
    return sorted(open_rows, key=lambda r: (r["due_on"] or date.max, r["journey_week"], r["sort_order"]))[:limit]


def current_week(journey_started_on: date, today: date | None = None) -> int:
    days = ((today or date.today()) - journey_started_on).days
    return max(1, min(4, days // 7 + 1))


def is_paused(case_row: dict, now: datetime | None = None) -> bool:
    until = case_row["tasks_paused_until"]
    return until is not None and until > (now or datetime.now(timezone.utc))


# ------------------------------------------------------------------ context records

def read_context(s: Session, case_id: UUID, key: str) -> dict | None:
    return s.read_context(case_id, key)


def write_context(s: Session, case_id: UUID, key: str, payload: dict) -> None:
    s.write_context(case_id, key, payload)


def lock_context(s: Session, case_id: UUID, key: str, empty: dict) -> dict:
    """Read a context item for update, creating it first. A concurrent writer gets a conflict, not a lost update."""
    return s.lock_context(case_id, key, empty)


def certificate_order(s: Session, case_id: UUID) -> CertificateOrderRecord | None:
    payload = read_context(s, case_id, CERT_ORDER)
    return CertificateOrderRecord.model_validate(payload) if payload else None


def institution_notices(s: Session, case_id: UUID) -> list[InstitutionNotice]:
    payload = read_context(s, case_id, BANK_NOTICES) or {}
    return [InstitutionNotice.model_validate(n) for n in payload.get("institutions", [])]
