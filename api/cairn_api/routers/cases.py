"""UC-5 to UC-8: create a case and record the deceased, the death, and estate facts."""
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from .. import account as acct
from .. import intake, messages
from ..auth import Identity, get_identity
from ..schemas import CaseResponse, CreateCaseRequest, DeathEventIn, DeceasedIdentityPatch, EstateFlagsIn

router = APIRouter(prefix="/v1/cases", tags=["Cases"])

_DENIED = {403: {"description": "Not a member of this case, not an owner, the case does not exist, or the "
                                "account is read-only (code account_read_only)."},
           409: {"description": "Onboarding isn't finished, or a changed acknowledgment needs to be agreed to."}}


@router.post(
    "",
    response_model=CaseResponse,
    status_code=201,
    summary="Start a case and record who died",
    description=(
        "UC-5. Creates the case, the caller's owner membership, and the deceased record in one transaction. "
        "The account's first case also starts the 28-day free trial in that transaction (UC-REG-08), and the "
        "response carries a note with the local end date. Needs finished onboarding and a writable account. "
        "UC-8: a fiduciary can include death_event and estate_flags in the same request, and set "
        "start_journey to build the journey once the minimum fields are present."
    ),
    responses=_DENIED,
)
def create_case(req: CreateCaseRequest, request: Request,
                identity: Identity = Depends(get_identity)) -> CaseResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        acct.require_ready(s, request, write=True)
        case_id, notes, next_step = intake.create_case_with_intake(s, req)
        # The first case starts the 28-day trial in this same transaction (D-03).
        if (started := acct.trial_started_now(s)) is not None:
            notes.insert(0, acct.trial_start_note(started, request.app.state.copy))
        return intake.case_response(s, case_id, next_step, notes)


@router.get("/{case_id}", response_model=CaseResponse, summary="Case, deceased, and intake progress",
            responses=_DENIED)
def get_case(case_id: UUID, request: Request, identity: Identity = Depends(get_identity)) -> CaseResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        ready = acct.require_ready(s, request, write=False)
        result = intake.case_response(s, case_id, notes=ready.notes)
        s.audit("deceased_viewed", case_id, "deceased", result.deceased.id)
        return result


@router.patch("/{case_id}/deceased", response_model=CaseResponse,
              summary="Correct the deceased's identifying details", responses=_DENIED)
def patch_deceased(case_id: UUID, req: DeceasedIdentityPatch, request: Request,
                   identity: Identity = Depends(get_identity)) -> CaseResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        acct.require_ready(s, request, write=True)
        intake.update_identity(s, case_id, req)
        return intake.case_response(s, case_id)


@router.put(
    "/{case_id}/death-event",
    response_model=CaseResponse,
    summary="Record the death event",
    description="UC-6. Replaces all death-event fields. Omitted optional fields are cleared.",
    responses=_DENIED,
)
def put_death_event(case_id: UUID, req: DeathEventIn, request: Request,
                    identity: Identity = Depends(get_identity)) -> CaseResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        acct.require_ready(s, request, write=True)
        intake.record_death_event(s, case_id, req)
        started = intake.refresh_journey_if_started(s, case_id)
        return intake.case_response(s, case_id, None if started else messages.ASK_ESTATE_QUESTIONS)


@router.patch(
    "/{case_id}/estate-flags",
    response_model=CaseResponse,
    summary="Answer the veteran and will questions",
    description="UC-7. Send one answer at a time. Omitted answers are unchanged. 'skip' saves 'unknown'.",
    responses=_DENIED,
)
def patch_estate_flags(case_id: UUID, req: EstateFlagsIn, request: Request,
                       identity: Identity = Depends(get_identity)) -> CaseResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        acct.require_ready(s, request, write=True)
        notes = intake.update_estate_flags(s, case_id, req)
        intake.refresh_journey_if_started(s, case_id)
        next_step = messages.ASK_HAS_WILL if req.has_will is None else None
        return intake.case_response(s, case_id, next_step, notes)
