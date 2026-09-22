"""Journey reads and task rules shared by the journey, task, and status endpoints."""
from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID

from psycopg.types.json import Jsonb

from .db import Session
from .schemas import (CertificateOrderRecord, InstitutionNotice, TaskCategory, TaskKind,
                      TaskStatus, TaskSummary)

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
    "confirm_pronouncement": TaskCategory.certificates,
    "choose_funeral_provider": TaskCategory.funeral,
    "notify_social_security": TaskCategory.agencies,
    "notify_va_if_veteran": TaskCategory.agencies,
    "notify_employer_and_pension": TaskCategory.agencies,
    "notify_banks": TaskCategory.financial_institutions,
    "notify_life_insurers": TaskCategory.financial_institutions,
    "notify_credit_bureaus": TaskCategory.financial_institutions,
    "secure_home_and_pets": TaskCategory.home_and_personal,
    "forward_mail": TaskCategory.home_and_personal,
    "cancel_subscriptions": TaskCategory.home_and_personal,
}

OPEN_STATUSES = {TaskStatus.not_started.value, TaskStatus.in_progress.value}
SET_ASIDE_STATUSES = {TaskStatus.skipped.value, TaskStatus.not_applicable.value}

# Context item keys from db/optional/context_items_jsonb.sql.
CERT_ORDER = "CERT_ORDER"
BANK_NOTICES = "BANK_NOTICES"
MAX_NOTICES = 100  # keeps the payload well under the 32 KB column limit

_TASK_SELECT = """
SELECT ct.id, ct.status, ct.due_on, ct.snoozed_until, ct.completed_at,
       t.task_key, t.version AS template_version, t.title, t.plain_summary,
       t.journey_week, t.sort_order, t.attorney_referral, t.attorney_referral_note,
       t.counsel_reviewed_at, t.jurisdiction, t.id AS template_id
FROM cairn.case_tasks ct
JOIN cairn.task_templates t ON t.id = ct.template_id
"""
_TASK_ORDER = " ORDER BY t.journey_week, t.sort_order, ct.due_on NULLS LAST, t.task_key"


def task_summary(row: dict) -> TaskSummary:
    return TaskSummary(
        id=row["id"], task_key=row["task_key"], template_version=row["template_version"],
        title=row["title"], plain_summary=row["plain_summary"], journey_week=row["journey_week"],
        sort_order=row["sort_order"],
        category=TASK_CATEGORIES.get(row["task_key"], TaskCategory.other),
        kind=TASK_KINDS.get(row["task_key"], TaskKind.general),
        status=row["status"], due_on=row["due_on"], snoozed_until=row["snoozed_until"],
        completed_at=row["completed_at"], attorney_referral=row["attorney_referral"],
    )


def load_tasks(s: Session, case_id: UUID) -> list[dict]:
    return s.all(_TASK_SELECT + " WHERE ct.case_id = %s" + _TASK_ORDER, (case_id,))


def load_task(s: Session, case_id: UUID, task_id: UUID) -> dict | None:
    return s.one(_TASK_SELECT + " WHERE ct.case_id = %s AND ct.id = %s", (case_id, task_id))


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


def current_week(journey_started_on: date, today: date | None = None) -> int:
    days = ((today or date.today()) - journey_started_on).days
    return max(1, min(4, days // 7 + 1))


def is_paused(case_row: dict, now: datetime | None = None) -> bool:
    until = case_row["tasks_paused_until"]
    return until is not None and until > (now or datetime.now(timezone.utc))


# ------------------------------------------------------------------ context records

def read_context(s: Session, case_id: UUID, key: str) -> dict | None:
    row = s.one("SELECT payload FROM cairn.context_items WHERE case_id = %s AND item_key = %s", (case_id, key))
    return row["payload"] if row else None


def write_context(s: Session, case_id: UUID, key: str, payload: dict) -> None:
    s.conn.execute(
        "INSERT INTO cairn.context_items (case_id, item_key, payload) VALUES (%s, %s, %s) "
        "ON CONFLICT (case_id, item_key) DO UPDATE SET payload = EXCLUDED.payload",
        (case_id, key, Jsonb(payload)),
    )


def lock_context(s: Session, case_id: UUID, key: str, empty: dict) -> dict:
    """Read a context item for update, creating it first so concurrent writers serialize."""
    s.conn.execute(
        "INSERT INTO cairn.context_items (case_id, item_key, payload) VALUES (%s, %s, %s) "
        "ON CONFLICT (case_id, item_key) DO NOTHING",
        (case_id, key, Jsonb(empty)),
    )
    row = s.one("SELECT payload FROM cairn.context_items WHERE case_id = %s AND item_key = %s FOR UPDATE",
                (case_id, key))
    return row["payload"] if row else dict(empty)


def certificate_order(s: Session, case_id: UUID) -> CertificateOrderRecord | None:
    payload = read_context(s, case_id, CERT_ORDER)
    return CertificateOrderRecord.model_validate(payload) if payload else None


def institution_notices(s: Session, case_id: UUID) -> list[InstitutionNotice]:
    payload = read_context(s, case_id, BANK_NOTICES) or {}
    return [InstitutionNotice.model_validate(n) for n in payload.get("institutions", [])]
