"""UC-10 and UC-11: open a task, follow its guidance, and record that it's done."""
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request

from .. import account as acct
from .. import intake, journey, journey_selection, messages
from ..auth import Identity, get_identity
from ..db import Session
from ..errors import ApiError, case_access_denied
from ..schemas import (
    CertificateOrderRequest,
    CitationOut,
    InstitutionNoticeRequest,
    TaskDetail,
    TaskKind,
    TaskResponse,
    TaskStatus,
    TaskUpdateRequest,
)

router = APIRouter(prefix="/v1/cases/{case_id}/tasks/{task_id}", tags=["Tasks"])

_DENIED = {403: {"description": "Not a member of this case, not an owner, or the task does not exist."}}


def _load(s: Session, case_id: UUID, task_id: UUID) -> dict:
    row = journey.load_task(s, case_id, task_id)
    if row is None:
        raise case_access_denied()
    return row


def _task_response(s: Session, case_id: UUID, task_id: UUID, notes=None) -> TaskResponse:
    row = _load(s, case_id, task_id)
    summary = journey.task_summary(row)
    citations = s.template_citations(row["template_id"])
    # UC-CASE-04 and DEC-05. The certificate office comes from where the death happened, never residence.
    death_state = journey_selection.certificate_office_jurisdiction(intake.load_answers(s, case_id))
    reviewed = row["counsel_reviewed_at"] is not None
    detail = TaskDetail(
        **summary.model_dump(),
        attorney_referral_note=row["attorney_referral_note"],
        content_reviewed_by_counsel=reviewed,
        jurisdiction=row["jurisdiction"],
        death_state=death_state,
        citations=[CitationOut.model_validate(c) for c in citations],
        certificate_order=(journey.certificate_order(s, case_id)
                           if summary.kind == TaskKind.certificate_order else None),
        institution_notices=(journey.institution_notices(s, case_id)
                             if summary.kind == TaskKind.institution_notice else None),
    )
    nxt = journey.pick_next_action(journey.load_tasks(s, case_id))
    all_notes = list(notes or [])
    if not reviewed:
        all_notes.append(messages.UNREVIEWED_CONTENT)
    return TaskResponse(task=detail, next_action=journey.task_summary(nxt) if nxt else None, notes=all_notes)


def _set_status(s: Session, case_id: UUID, task_id: UUID, status: TaskStatus) -> None:
    if not s.update_task(case_id, task_id, status=status.value):
        raise case_access_denied()
    s.audit("task_status_changed", case_id, "case_task", task_id)


def _require_kind(row: dict, kind: TaskKind) -> None:
    if journey.TASK_KINDS.get(row["task_key"]) != kind:
        raise ApiError(409, "wrong_task_kind", "This kind of record doesn't belong to this task.")


@router.get("", response_model=TaskResponse, summary="Open a task with its guidance and sources",
            description="Guidance, citations to the issuing authority, and anything already recorded for it.",
            responses=_DENIED)
def get_task(case_id: UUID, task_id: UUID, request: Request,
             identity: Identity = Depends(get_identity)) -> TaskResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        ready = acct.require_ready(s, request, write=False)
        return _task_response(s, case_id, task_id, ready.notes)


@router.patch("", response_model=TaskResponse, summary="Change a task's status or snooze it",
              description="Setting status to done records the completion time. The response carries the next action.",
              responses=_DENIED)
def update_task(case_id: UUID, task_id: UUID, req: TaskUpdateRequest, request: Request,
                identity: Identity = Depends(get_identity)) -> TaskResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        acct.require_ready(s, request, write=True)
        if req.status is not None:
            _set_status(s, case_id, task_id, req.status)
        if "snoozed_until" in req.model_fields_set:
            if not s.update_task(case_id, task_id, snoozed_until=req.snoozed_until):
                raise case_access_denied()
            s.audit("task_snoozed" if req.snoozed_until else "task_unsnoozed", case_id, "case_task", task_id)
        return _task_response(s, case_id, task_id)


@router.post(
    "/certificate-order",
    response_model=TaskResponse,
    summary="Record a death certificate order",
    description="UC-10. Saves copies and expected arrival, marks the task done, and returns the next task.",
    responses={**_DENIED, 409: {"description": "This task is not the death certificate task."}},
)
def record_certificate_order(case_id: UUID, task_id: UUID, req: CertificateOrderRequest, request: Request,
                             identity: Identity = Depends(get_identity)) -> TaskResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        acct.require_ready(s, request, write=True)
        _require_kind(_load(s, case_id, task_id), TaskKind.certificate_order)
        journey.write_context(s, case_id, journey.CERT_ORDER, req.model_dump(mode="json"))
        s.audit("certificate_order_recorded", case_id, "case_task", task_id)
        _set_status(s, case_id, task_id, TaskStatus.done)
        return _task_response(s, case_id, task_id, [messages.CERT_ORDER_SAVED])


@router.post(
    "/institution-notices",
    response_model=TaskResponse,
    summary="Record that an institution was notified",
    description=(
        "UC-11. Records the institution as notified and, by default, marks the task done so the journey "
        "moves on. Sending the same institution name again updates that entry."
    ),
    responses={**_DENIED, 409: {"description": "This task is not an institution notification task."}},
)
def record_institution_notice(case_id: UUID, task_id: UUID, req: InstitutionNoticeRequest, request: Request,
                              identity: Identity = Depends(get_identity)) -> TaskResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        acct.require_ready(s, request, write=True)
        _require_kind(_load(s, case_id, task_id), TaskKind.institution_notice)
        payload = journey.lock_context(s, case_id, journey.BANK_NOTICES, {"institutions": []})
        notices = [n for n in payload.get("institutions", [])
                   if n["institution_name"].casefold() != req.institution_name.casefold()]
        if len(notices) >= journey.MAX_NOTICES:
            raise ApiError(422, "too_many_institutions", "That's the most institutions we can track for one case.")
        notices.append({
            "id": str(uuid4()), "institution_name": req.institution_name,
            "institution_type": req.institution_type.value, "notified_on": req.notified_on.isoformat(),
            "method": req.method, "status": "notified",
        })
        journey.write_context(s, case_id, journey.BANK_NOTICES, {"institutions": notices})
        s.audit("institution_notice_recorded", case_id, "case_task", task_id)
        if req.complete_task:
            _set_status(s, case_id, task_id, TaskStatus.done)
        else:
            _set_status_if_open(s, case_id, task_id)
        return _task_response(s, case_id, task_id, [messages.INSTITUTION_SAVED])


def _set_status_if_open(s: Session, case_id: UUID, task_id: UUID) -> None:
    """Recording one institution without finishing the task moves it to in progress."""
    if s.update_task(case_id, task_id, status="in_progress", only_from=("not_started",)):
        s.audit("task_status_changed", case_id, "case_task", task_id)
