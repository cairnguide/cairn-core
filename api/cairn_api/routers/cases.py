"""UC-CASE-01, UC-CASE-10, UC-CASE-18: start a case, find it again, and pick up where you left off.
Also deleting a case, now or with a 7-day hold (UC-END-13, UC-CASE-10 change), with its one confirmation (UC-CASE-21).
"""
from datetime import timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, Request

from .. import account as acct
from .. import intake
from ..auth import Identity, get_identity
from ..errors import ApiError, case_access_denied
from ..schemas import (
    Announcement,
    AnswerState,
    CaseDeletionIn,
    CaseDeletionInfo,
    CaseDeletionResponse,
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
        ids = [r["id"] for r in s.owned_cases(newest_activity_first=True)]
        items = []
        for case_id in ids:
            case, answers = intake.load_case(s, case_id), intake.load_answers(s, case_id)
            out = intake.case_out(c, case, answers)
            # UC-CASE-10: a user who closed the tab before the pause message still sees the 28-day notice here.
            notice = c.copy["draft_notice"] if out.status == "draft" else None
            items.append(CaseListItem(id=out.id, status=out.status, display_name=out.display_name,
                                      last_activity_at=out.last_activity_at, draft_expires_at=out.draft_expires_at,
                                      draft_notice=notice, deletion_scheduled_for=out.deletion_scheduled_for))
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
        announcements = []
        if s.take_due_check_in(case_id):
            # DEC-26-04 and OPEN-08. The check-in the user said yes to, shown once, because it isn't going by email.
            announcements.append(Announcement(kind="check_in", text=c.copy["check_in_in_cairn"]))
        every = timedelta(hours=request.app.state.settings.ai_reminder_every_hours)
        if s.ai_reminder_due(session_start=True, every=every):
            # UC-CASE-23. Opening a case starts a session: the AI reminder, at most once a day.
            s.mark_ai_reminder_shown()
            announcements.append(Announcement(kind="ai_reminder", text=c.copy["ai_reminder"]))
        text = " ".join(x for x in [ack, *body, step.prompt, *(a.text for a in announcements)] if x)
        return CaseResponse(case=intake.case_out(c, case, answers),
                            deceased=DeceasedOut.model_validate(deceased) if deceased else None,
                            acknowledgment=ack, body=body, notes=ready.notes, announcements=announcements,
                            next_step=step, controls=intake.controls(c.copy),
                            read_aloud=ReadAloud(label=c.copy["read_this_to_me"], text=text))


@router.patch(
    "/{case_id}/deceased",
    response_model=CaseResponse,
    summary="Record the deceased's legal identity, inside the task that needs it",
    description=(
        "Just in time only. full_legal_name and date_of_birth are never part of case creation "
        "(never_collect_at_case_creation). Refused while the case is a draft. The data layer refuses it too."
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
        deceased_id, created = s.save_deceased(case_id, fields)
        if created:
            s.audit("deceased_added", case_id, "deceased", deceased_id)
        s.audit("deceased_updated", case_id, "deceased", deceased_id)
        answers = intake.load_answers(s, case_id)
        step = NextStep(action="view_journey", prompt=c.copy["readback_saved"])
        return CaseResponse(case=intake.case_out(c, case, answers),
                            deceased=DeceasedOut.model_validate(intake.load_deceased(s, case_id)),
                            notes=[], next_step=step, controls=intake.controls(c.copy),
                            read_aloud=ReadAloud(label=c.copy["read_this_to_me"], text=step.prompt))


# ------------------------------------------------------------------ deleting a case (UC-END-13, UC-CASE-21)
# Always free, on drafts, active, and read-only cases (D-2026-09-25-F1). Only the
# owner can delete. The data layer does the deleting and queues the confirmation.

def _deletion_ctx(s, request: Request, case_id: UUID) -> tuple[intake.Ctx, dict]:
    c = intake.ctx(s, request, acct.load_account(s))
    case = intake.load_case(s, case_id)
    if not s.is_case_member(case_id, ("owner",)):
        raise case_access_denied()
    return c, case


@router.get(
    "/{case_id}/deletion",
    response_model=CaseDeletionInfo,
    summary="What deleting this case removes, and where the confirmation goes",
    description=(
        "UC-END-13 and UC-CASE-21. Explains what goes, shows the masked address the one confirmation will go "
        "to before anything is confirmed, and offers Delete it now or Delete it in 7 days. For a case already "
        "on hold, offers Keep this case."
    ),
    responses=_DENIED,
)
def deletion_info(case_id: UUID, request: Request, identity: Identity = Depends(get_identity)) -> CaseDeletionInfo:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        c, case = _deletion_ctx(s, request, case_id)
        masked = acct.mask_email(c.account["email"])
        options = [Option(value="now", label=c.copy["case_delete_now"])]
        if case["deletion_scheduled_for"] is None:
            options.append(Option(value="hold", label=c.copy["case_delete_hold"]))
        else:
            options.append(Option(value="keep", label=c.copy["case_keep"]))
        return CaseDeletionInfo(
            explanation=c.copy["case_deletion_explanation"], masked_email=masked,
            confirmation_destination=c.copy["case_deletion_confirmation_destination"].format(masked_email=masked),
            deletion_scheduled_for=case["deletion_scheduled_for"],
            next_step=NextStep(action="confirm_case_deletion", prompt=c.copy["case_deletion_question"],
                               options=options))


@router.post(
    "/{case_id}/deletion",
    response_model=CaseDeletionResponse,
    summary="Delete this case now, or in 7 days",
    description=(
        "now deletes the case and everything in it at once and queues one confirmation (case_deleted_now). "
        "hold schedules deletion in 7 days (case_deletion_hold_days). The case stays visible until then, is "
        "included in the data download, and the confirmation goes when it is deleted (case_deleted_after_hold). "
        "No reason is asked."
    ),
    responses=_DENIED,
)
def delete_case(case_id: UUID, req: CaseDeletionIn, request: Request,
                identity: Identity = Depends(get_identity)) -> CaseDeletionResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        c, _ = _deletion_ctx(s, request, case_id)
        when = s.request_case_deletion(case_id, req.mode)
        if req.mode == "now":
            ack = c.copy["case_deleted_now"]
            return CaseDeletionResponse(deleted=True, deletion_scheduled_for=None, acknowledgment=ack,
                                        next_step=NextStep(action="view_cases", prompt=ack))
        local = when.astimezone(ZoneInfo(c.account.get("time_zone") or "UTC")).date()
        ack = c.copy["case_deletion_scheduled"].format(date=acct.format_date(local))
        return CaseDeletionResponse(
            deleted=False, deletion_scheduled_for=when, acknowledgment=ack,
            next_step=NextStep(action="case_deletion_scheduled", prompt=ack,
                               options=[Option(value="keep", label=c.copy["case_keep"])]))


@router.delete(
    "/{case_id}/deletion",
    response_model=CaseDeletionResponse,
    summary="Keep a case that was set to be deleted",
    description="Cancels a 7-day hold. Nothing was deleted, and no confirmation is sent.",
    responses=_DENIED,
)
def keep_case(case_id: UUID, request: Request, identity: Identity = Depends(get_identity)) -> CaseDeletionResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        c, _ = _deletion_ctx(s, request, case_id)
        s.cancel_case_deletion(case_id)
        ack = c.copy["case_deletion_cancelled"]
        return CaseDeletionResponse(deleted=False, deletion_scheduled_for=None, acknowledgment=ack,
                                    next_step=NextStep(action="view_case", prompt=ack))
