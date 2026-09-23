"""UC-9 (journey), UC-12 (step back from tasks), and UC-13 (status across the case)."""
from datetime import date, datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from .. import intake, journey, messages
from ..auth import Identity, get_identity
from ..db import Session
from ..schemas import (
    CaseStatusResponse,
    CategoryStatus,
    CheckIn,
    JourneyResponse,
    NextStep,
    PauseRequest,
    StatusCounts,
    TaskCategory,
    WeekOut,
)

router = APIRouter(prefix="/v1/cases/{case_id}", tags=["Journey"])

_DENIED = {403: {"description": "Not a member of this case, not an owner, or the case does not exist."}}


def _journey_view(s: Session, case_id: UUID, notes=None) -> JourneyResponse:
    case = intake.load_case(s, case_id)
    rows = journey.load_tasks(s, case_id)
    base = dict(case_id=case_id, journey_started_on=case["journey_started_on"],
                current_week=journey.current_week(case["journey_started_on"]), notes=notes or [])

    if journey.is_paused(case):
        # Step back from task mode. Progress is untouched. Tasks are not listed on purpose.
        return JourneyResponse(
            **base, mode="paused", paused_until=case["tasks_paused_until"], weeks=[],
            check_in=CheckIn(message=messages.CHECK_IN_MESSAGE, options=messages.CHECK_IN_OPTIONS),
            next_step=NextStep(action="check_in", prompt=messages.CHECK_IN_MESSAGE),
        )

    if not rows:
        deceased = intake.load_deceased(s, case_id)
        status = intake.intake_status(deceased)
        next_step = (messages.missing_for_journey(status.missing_required) if status.missing_required
                     else messages.READY_FOR_JOURNEY)
        return JourneyResponse(**base, mode="not_started", weeks=[], next_step=next_step)

    weeks = []
    for week in range(1, 5):
        in_week = [r for r in rows if r["journey_week"] == week]
        weeks.append(WeekOut(week=week, total=len(in_week),
                             done=sum(1 for r in in_week if r["status"] == "done"),
                             tasks=[journey.task_summary(r) for r in in_week]))
    nxt = journey.pick_next_action(rows)
    return JourneyResponse(
        **base, mode="tasks", weeks=weeks,
        next_action=journey.task_summary(nxt) if nxt else None,
        next_step=messages.TASK_NEXT if nxt else messages.ALL_DONE,
    )


@router.post(
    "/journey",
    response_model=JourneyResponse,
    summary="Build the four-week journey",
    description=(
        "UC-9. Matches the deceased's details to the current task templates and creates the case's tasks. "
        "Returns 409 with the missing fields until the date and state of death are recorded. Safe to repeat."
    ),
    responses={**_DENIED, 409: {"description": "Required intake fields are missing."}},
)
def build_journey(case_id: UUID, request: Request, identity: Identity = Depends(get_identity)) -> JourneyResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        intake.generate_journey(s, case_id)
        view = _journey_view(s, case_id)
        if view.mode == "tasks" or view.mode == "paused":
            return view
        return view.model_copy(update={"notes": [messages.JOURNEY_EMPTY]})


@router.get(
    "/journey",
    response_model=JourneyResponse,
    summary="The journey, with one next action in front",
    description="UC-9 and UC-12. While paused, returns a calm check-in and no task list.",
    responses=_DENIED,
)
def get_journey(case_id: UUID, request: Request, identity: Identity = Depends(get_identity)) -> JourneyResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        return _journey_view(s, case_id)


@router.post(
    "/journey/pause",
    response_model=JourneyResponse,
    summary="Step back from tasks for a while",
    description=(
        "UC-12. Holds all progress exactly as it is. Stores only the time the pause ends. "
        "No reason or feeling is collected or stored."
    ),
    responses=_DENIED,
)
def pause_journey(case_id: UUID, request: Request, req: PauseRequest | None = None,
                  identity: Identity = Depends(get_identity)) -> JourneyResponse:
    days = (req or PauseRequest()).pause_days
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        row = s.one("UPDATE cairn.cases SET tasks_paused_until = now() + make_interval(days => %s) "
                    "WHERE id = %s RETURNING id", (days, case_id))
        if row is None:
            raise intake.case_access_denied()
        s.audit("journey_paused", case_id, "case", case_id)
        return _journey_view(s, case_id)


@router.post("/journey/resume", response_model=JourneyResponse, summary="Pick the tasks back up",
             description="UC-12. Returns the journey with the same single next action as before the pause.",
             responses=_DENIED)
def resume_journey(case_id: UUID, request: Request, identity: Identity = Depends(get_identity)) -> JourneyResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        row = s.one("UPDATE cairn.cases SET tasks_paused_until = NULL WHERE id = %s RETURNING id", (case_id,))
        if row is None:
            raise intake.case_access_denied()
        s.audit("journey_resumed", case_id, "case", case_id)
        return _journey_view(s, case_id, notes=[messages.RESUMED])


@router.get(
    "/status",
    response_model=CaseStatusResponse,
    summary="Everything done, in progress, and next, in one view",
    description="UC-13. Grouped by certificates, agencies, financial institutions, and other areas.",
    responses=_DENIED,
)
def case_status(case_id: UUID, request: Request, identity: Identity = Depends(get_identity)) -> CaseStatusResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        case = intake.load_case(s, case_id)
        deceased = intake.load_deceased(s, case_id)
        rows = journey.load_tasks(s, case_id)
        today = date.today()

        by_category: dict[TaskCategory, CategoryStatus] = {}
        for r in rows:
            t = journey.task_summary(r)
            group = by_category.setdefault(t.category, CategoryStatus(
                category=t.category, label=messages.CATEGORY_LABELS[t.category.value],
                done=[], in_progress=[], up_next=[], set_aside=[]))
            {"done": group.done, "in_progress": group.in_progress, "not_started": group.up_next}.get(
                r["status"], group.set_aside).append(t)

        counts = StatusCounts(
            total=len(rows),
            done=sum(r["status"] == "done" for r in rows),
            in_progress=sum(r["status"] == "in_progress" for r in rows),
            not_started=sum(r["status"] == "not_started" for r in rows),
            set_aside=sum(r["status"] in journey.SET_ASIDE_STATUSES for r in rows),
            overdue=sum(r["status"] in journey.OPEN_STATUSES and r["due_on"] is not None and r["due_on"] < today
                        for r in rows),
        )
        nxt = journey.pick_next_action(rows)
        order = list(TaskCategory)
        s.audit("case_status_viewed", case_id, "case", case_id)
        return CaseStatusResponse(
            case_id=case_id,
            as_of=datetime.now(timezone.utc),
            deceased_name=f"{deceased['legal_first_name']} {deceased['legal_last_name']}",
            journey_paused=journey.is_paused(case),
            paused_until=case["tasks_paused_until"] if journey.is_paused(case) else None,
            counts=counts,
            next_action=journey.task_summary(nxt) if nxt else None,
            categories=sorted(by_category.values(), key=lambda c: order.index(c.category)),
            certificate_order=journey.certificate_order(s, case_id),
            institution_notices=journey.institution_notices(s, case_id),
        )
