"""UC-CASE-01, UC-CASE-10, UC-CASE-18: start a case, find it again, and pick up where you left off."""
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Request
from psycopg import sql

from .. import account as acct
from .. import intake
from ..auth import Identity, get_identity
from ..errors import ApiError, case_access_denied
from ..schemas import (
    AnswerState,
    CaseListItem,
    CaseListResponse,
    CaseResponse,
    DeceasedIdentityPatch,
    DeceasedOut,
    FieldKey,
    IntakeSession,
    IntakeTurnResponse,
    NextStep,
    Option,
    ReadAloud,
    StartCaseRequest,
    UserRole,
)

router = APIRouter(prefix="/v1/cases", tags=["Case creation"])

_DENIED = {403: {"description": "Not a member of this case, the case does not exist, or (for an active case) "
                                "the account is read-only (code account_read_only)."},
           409: {"description": "Onboarding isn't finished, or a changed acknowledgment needs to be agreed to."}}


@router.post(
    "",
    response_model=IntakeTurnResponse,
    status_code=201,
    summary="Start a new case",
    description=(
        "UC-CASE-01 and UC-CASE-18. Creates a draft case right away and acknowledges the loss before asking "
        "anything. Offers two ways in: one question at a time, or in your own words. Creating or editing a "
        "draft never starts the free period (DEC-01). A read-only account can create a draft. Nothing about "
        "the person who died is collected here."
    ),
    responses=_DENIED,
)
def create_case(request: Request, req: StartCaseRequest | None = Body(default=None),
                identity: Identity = Depends(get_identity)) -> IntakeTurnResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        ready = acct.require_ready(s, request, write=False)
        another = acct.has_case(s)
        case_id = intake.create_draft(s)
        if req and req.user_role:
            intake.save_answer(s, case_id, FieldKey.user_role, AnswerState.answered, req.user_role.value, None)
        c = intake.ctx(s, request, ready.account)
        case, answers = intake.load_case(s, case_id), intake.load_answers(s, case_id)
        step = NextStep(action="choose_intake_mode", prompt=c.copy["intake_mode_question"],
                        options=[Option(value="one_question_at_a_time", label=c.copy["one_question_at_a_time"]),
                                 Option(value="own_words", label=c.copy["own_words"])])
        notes = intake.poa_note_once(c, case) if req and req.user_role == UserRole.power_of_attorney else []
        return intake.turn(c, case, answers, IntakeSession(),
                           acknowledgment=c.copy["ack_another_case" if another else "ack_first_case"],
                           body=[c.copy["intro"]], notes=notes, next_step=step)


@router.get("", response_model=CaseListResponse, summary="Your cases, drafts included")
def list_cases(request: Request, identity: Identity = Depends(get_identity)) -> CaseListResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        ready = acct.require_ready(s, request, write=False)
        c = intake.ctx(s, request, ready.account)
        ids = [r["id"] for r in s.all("SELECT id FROM cairn.cases WHERE created_by = cairn.current_user_id() "
                                      "ORDER BY last_activity_at DESC")]
        items = []
        for case_id in ids:
            case, answers = intake.load_case(s, case_id), intake.load_answers(s, case_id)
            out = intake.case_out(c, case, answers)
            items.append(CaseListItem(id=out.id, status=out.status, display_name=out.display_name,
                                      last_activity_at=out.last_activity_at, draft_expires_at=out.draft_expires_at))
        return CaseListResponse(cases=items)


@router.get(
    "/{case_id}",
    response_model=CaseResponse,
    summary="Open a case",
    description=(
        "UC-CASE-10. Opening a draft counts as activity and resets its 28 days. The response greets the user, "
        "says in one line where they left off, and asks whether to continue."
    ),
    responses=_DENIED,
)
def get_case(case_id: UUID, request: Request, identity: Identity = Depends(get_identity)) -> CaseResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        ready = acct.require_ready(s, request, write=False)
        c = intake.ctx(s, request, ready.account)
        case = intake.load_case(s, case_id)
        if case["status"] == "draft":
            intake.touch(s, case_id)
            case = intake.load_case(s, case_id)
        answers = intake.load_answers(s, case_id)
        deceased = intake.load_deceased(s, case_id)
        if deceased:
            s.audit("deceased_viewed", case_id, "deceased", deceased["id"])

        if case["status"] == "draft":
            step_key = case["last_intake_step"]
            where = (c.copy["resume_where"].format(step=c.copy[f"step_{step_key}"]) if step_key
                     else c.copy["resume_done"] if answers else c.copy["resume_start"])
            ack, body = c.copy["resume_greeting"], [where]
            step = NextStep(action="resume", prompt=c.copy["resume_question"],
                            options=[Option(value="keep_going", label=c.copy["keep_going"]),
                                     Option(value="something_else", label=c.copy["look_at_something_else"])])
        else:
            ack, body = None, []
            step = NextStep(action="view_journey", prompt=c.copy["journey_preview_intro"])
        text = " ".join(x for x in [ack, *body, step.prompt] if x)
        return CaseResponse(case=intake.case_out(c, case, answers),
                            deceased=DeceasedOut.model_validate(deceased) if deceased else None,
                            acknowledgment=ack, body=body, notes=ready.notes, next_step=step,
                            read_aloud=ReadAloud(label=c.copy["read_this_to_me"], text=text))


@router.patch(
    "/{case_id}/deceased",
    response_model=CaseResponse,
    summary="Record the deceased's legal identity, inside the task that needs it",
    description=(
        "Just in time only. full_legal_name and date_of_birth are never part of case creation "
        "(never_collect_at_case_creation). Refused while the case is a draft. The database refuses it too."
    ),
    responses={**_DENIED, 409: {"description": "The case is still a draft."}},
)
def patch_deceased(case_id: UUID, req: DeceasedIdentityPatch, request: Request,
                   identity: Identity = Depends(get_identity)) -> CaseResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        ready = acct.require_ready(s, request, write=True)
        c = intake.ctx(s, request, ready.account)
        case = intake.load_case(s, case_id)
        if case["status"] == "draft":
            raise ApiError(409, "journey_not_started", "This is only needed once the journey has started.")
        intake.require_case_write(c, case)
        fields = {f: getattr(req, f) for f in req.model_fields_set}
        row = s.one("INSERT INTO cairn.deceased (case_id) VALUES (%s) ON CONFLICT (case_id) DO NOTHING RETURNING id",
                    (case_id,))
        if row:
            s.audit("deceased_added", case_id, "deceased", row["id"])
        assignments = sql.SQL(", ").join(
            sql.SQL("{} = {}").format(sql.Identifier(f), sql.Placeholder(f)) for f in sorted(fields))
        updated = s.one(sql.SQL("UPDATE cairn.deceased SET {} WHERE case_id = {} RETURNING id").format(
            assignments, sql.Placeholder("case_id")), {**fields, "case_id": case_id})
        if updated is None:
            raise case_access_denied()
        s.audit("deceased_updated", case_id, "deceased", updated["id"])
        answers = intake.load_answers(s, case_id)
        step = NextStep(action="view_journey", prompt=c.copy["readback_saved"])
        return CaseResponse(case=intake.case_out(c, case, answers),
                            deceased=DeceasedOut.model_validate(intake.load_deceased(s, case_id)),
                            notes=[], next_step=step,
                            read_aloud=ReadAloud(label=c.copy["read_this_to_me"], text=step.prompt))
