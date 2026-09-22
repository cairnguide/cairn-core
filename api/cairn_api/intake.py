"""Case and deceased intake (UC-5 to UC-8).

Each step is its own function with its own audit event, so the fiduciary's
single-session flow (UC-8) leaves the same audit trail as the step-by-step
flows (UC-5, UC-6, UC-7).
"""
from __future__ import annotations

from uuid import UUID

from psycopg import sql

from . import messages
from .db import Session
from .errors import ApiError, case_access_denied
from .journey import load_tasks
from .schemas import (CaseOut, CaseResponse, CreateCaseRequest, DeathEventIn, DeceasedIdentityIn,
                      DeceasedIdentityPatch, DeceasedOut, EstateFlagsIn, IntakeStatus, NextStep, Note,
                      Relationship)

# Minimum fields before cairn.generate_case_tasks can produce a meaningful journey.
REQUIRED_FOR_JOURNEY = ("legal_first_name", "legal_last_name", "date_of_death", "death_state")
OPTIONAL_FIELDS = ("date_of_birth", "domicile_state", "place_type", "county", "facility_name")
TRI_STATE_FIELDS = ("veteran_status", "has_will")

# ssn_last4 is deliberately not selected. It is out of scope for these flows.
_DECEASED_COLUMNS = """id, case_id, legal_first_name, legal_middle_name, legal_last_name, date_of_birth,
  domicile_state, veteran_status, has_will, date_of_death, place_type, death_state, county, city,
  facility_name"""


# ------------------------------------------------------------------ reads

def load_case(s: Session, case_id: UUID) -> dict:
    row = s.one(
        "SELECT c.id, c.status, c.journey_started_on, c.tasks_paused_until, c.created_at, m.relationship "
        "FROM cairn.cases c LEFT JOIN cairn.case_members m "
        "  ON m.case_id = c.id AND m.user_id = cairn.current_user_id() AND m.status = 'active' "
        "WHERE c.id = %s",
        (case_id,),
    )
    if row is None:
        raise case_access_denied()
    return row


def load_deceased(s: Session, case_id: UUID) -> dict:
    row = s.one(f"SELECT {_DECEASED_COLUMNS} FROM cairn.deceased WHERE case_id = %s", (case_id,))
    if row is None:
        raise case_access_denied()
    return row


def intake_status(deceased: dict) -> IntakeStatus:
    missing = [f for f in REQUIRED_FOR_JOURNEY if deceased.get(f) is None]
    unanswered = [f for f in OPTIONAL_FIELDS if deceased.get(f) is None]
    unanswered += [f for f in TRI_STATE_FIELDS if deceased.get(f) == "unknown"]
    return IntakeStatus(ready_for_journey=not missing, missing_required=missing, unanswered_optional=unanswered)


def default_next_step(case: dict, deceased: dict, task_count: int) -> NextStep:
    intake = intake_status(deceased)
    if intake.missing_required:
        return messages.missing_for_journey(intake.missing_required)
    if task_count:
        return messages.VIEW_JOURNEY
    return messages.READY_FOR_JOURNEY


def case_response(s: Session, case_id: UUID, next_step: NextStep | None = None,
                  notes: list[Note] | None = None) -> CaseResponse:
    case = load_case(s, case_id)
    deceased = load_deceased(s, case_id)
    task_count = len(load_tasks(s, case_id))
    return CaseResponse(
        case=CaseOut.model_validate(case),
        deceased=DeceasedOut.model_validate(deceased),
        intake=intake_status(deceased),
        journey_task_count=task_count,
        notes=notes or [],
        next_step=next_step or default_next_step(case, deceased, task_count),
    )


# ------------------------------------------------------------------ writes

def _require_owner(s: Session, case_id: UUID) -> None:
    ok = s.one("SELECT cairn.is_case_member(%s, ARRAY['owner', 'co_executor']) AS ok", (case_id,))
    if not ok or not ok["ok"]:
        raise case_access_denied()


def create_case(s: Session, relationship: Relationship) -> UUID:
    """UC-5 steps 1 and 2. Case and first owner in one transaction, so a case is never ownerless."""
    uid = s.require_user()
    case_id = s.one("INSERT INTO cairn.cases (created_by) VALUES (%s) RETURNING id", (uid,))["id"]
    s.conn.execute(
        "INSERT INTO cairn.case_members (case_id, user_id, relationship, role, status) "
        "VALUES (%s, %s, %s, 'owner', 'active')",
        (case_id, uid, relationship.value),
    )
    s.audit("case_created", case_id, "case", case_id)
    return case_id


def add_deceased(s: Session, case_id: UUID, d: DeceasedIdentityIn) -> UUID:
    """UC-5 step 4. veteran_status and has_will take their 'unknown' defaults."""
    row = s.one(
        "INSERT INTO cairn.deceased (case_id, legal_first_name, legal_middle_name, legal_last_name, "
        "  date_of_birth, domicile_state) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (case_id, d.legal_first_name, d.legal_middle_name, d.legal_last_name, d.date_of_birth, d.domicile_state),
    )
    s.audit("deceased_added", case_id, "deceased", row["id"])
    return row["id"]


def update_identity(s: Session, case_id: UUID, patch: DeceasedIdentityPatch) -> None:
    fields = {f: getattr(patch, f) for f in patch.model_fields_set}
    assignments = sql.SQL(", ").join(
        sql.SQL("{} = {}").format(sql.Identifier(f), sql.Placeholder(f)) for f in sorted(fields))
    query = sql.SQL("UPDATE cairn.deceased SET {} WHERE case_id = {} RETURNING id").format(
        assignments, sql.Placeholder("case_id"))
    row = s.one(query, {**fields, "case_id": case_id})
    if row is None:
        raise case_access_denied()
    s.audit("deceased_updated", case_id, "deceased", row["id"])


def record_death_event(s: Session, case_id: UUID, e: DeathEventIn) -> None:
    """UC-6. An UPDATE on the row created in UC-5, since the two tables were merged."""
    _require_owner(s, case_id)
    prior = s.one("SELECT id, date_of_birth, date_of_death FROM cairn.deceased WHERE case_id = %s FOR UPDATE",
                  (case_id,))
    if prior is None:
        raise case_access_denied()
    if prior["date_of_birth"] and e.date_of_death < prior["date_of_birth"]:
        # The database enforces this too (death_not_before_birth). Checking here
        # gives the same plain message without relying on a constraint error.
        raise ApiError(422, "invalid_value",
                       "The date of death is earlier than the date of birth. Please check both dates.",
                       constraint="death_not_before_birth")
    s.conn.execute(
        "UPDATE cairn.deceased SET date_of_death = %s, death_state = %s, place_type = %s, county = %s, "
        "  city = %s, facility_name = %s WHERE case_id = %s",
        (e.date_of_death, e.death_state, e.place_type.value if e.place_type else None,
         e.county, e.city, e.facility_name, case_id),
    )
    action = "death_event_added" if prior["date_of_death"] is None else "death_event_updated"
    s.audit(action, case_id, "deceased", prior["id"])


def update_estate_flags(s: Session, case_id: UUID, flags: EstateFlagsIn) -> list[Note]:
    """UC-7. Omitted answers are unchanged. 'skip' is stored as 'unknown' so the server stays the source of truth."""
    values = {}
    for field in TRI_STATE_FIELDS:
        answer = getattr(flags, field)
        if answer is not None:
            values[field] = "unknown" if answer.value == "skip" else answer.value
    assignments = sql.SQL(", ").join(
        sql.SQL("{} = {}").format(sql.Identifier(f), sql.Placeholder(f)) for f in sorted(values))
    query = sql.SQL("UPDATE cairn.deceased SET {} WHERE case_id = {} RETURNING id").format(
        assignments, sql.Placeholder("case_id"))
    row = s.one(query, {**values, "case_id": case_id})
    if row is None:
        raise case_access_denied()
    s.audit("deceased_estate_flags_updated", case_id, "deceased", row["id"])

    notes = []
    if "unknown" in values.values():
        notes.append(messages.UNKNOWN_IS_FINE)
    if values.get("has_will") == "yes":
        notes.append(messages.WILL_LOCATION_LATER)
    return notes


def generate_journey(s: Session, case_id: UUID) -> int:
    """UC-9. Refuses until the minimum fields exist, so the first view is a populated one."""
    _require_owner(s, case_id)
    intake = intake_status(load_deceased(s, case_id))
    if intake.missing_required:
        raise ApiError(409, "intake_incomplete", "A few details are still needed before the journey can start.",
                       missing_required=intake.missing_required,
                       next_step=messages.missing_for_journey(intake.missing_required).model_dump())
    created = s.one("SELECT cairn.generate_case_tasks(%s) AS n", (case_id,))["n"]
    if created:
        s.audit("journey_generated", case_id, "case", case_id)
    return created


def refresh_journey_if_started(s: Session, case_id: UUID) -> bool:
    """After a late answer (for example, veteran status learned later), add any newly matching tasks.

    Existing tasks are never removed, so progress is kept.
    """
    started = s.one("SELECT EXISTS (SELECT 1 FROM cairn.case_tasks WHERE case_id = %s) AS started", (case_id,))
    if started["started"]:
        generate_journey(s, case_id)
    return started["started"]


def create_case_with_intake(s: Session, req: CreateCaseRequest) -> tuple[UUID, list[Note], NextStep | None]:
    """UC-5 alone, or UC-8's composed session when the optional sections are present."""
    case_id = create_case(s, req.relationship)
    add_deceased(s, case_id, req.deceased)
    notes: list[Note] = []
    professional = req.relationship == Relationship.fiduciary

    if req.death_event:
        record_death_event(s, case_id, req.death_event)
    if req.estate_flags:
        notes += update_estate_flags(s, case_id, req.estate_flags)
    if professional:
        notes.append(messages.FILL_IN_LATER)

    next_step: NextStep | None = None
    if req.start_journey and intake_status(load_deceased(s, case_id)).ready_for_journey:
        generate_journey(s, case_id)
        next_step = messages.VIEW_JOURNEY
    elif not req.death_event:
        next_step = messages.after_identity(professional)
        notes.append(messages.DEATH_STATE_HINT)
    elif not req.estate_flags:
        next_step = messages.ASK_ESTATE_QUESTIONS
    return case_id, notes, next_step
