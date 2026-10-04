"""UC-CASE-19 and UC-CASE-20: choose, and change, how and when Cairn keeps in touch.

Always free, on every account status, and never gated on acknowledgments: a
request to stop messages must always work (D-2026-09-25-F1). Turning
notifications off never changes the journey itself.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from .. import account as acct
from .. import intake
from .. import notifications as nt
from ..auth import Identity, get_identity
from ..db import Session
from ..errors import ApiError, case_access_denied
from ..schemas import (
    AccountNotificationsResponse,
    JourneyNotifications,
    NextStep,
    NotificationChangeIn,
    NotificationChangeResponse,
    NotificationChannel,
    NotificationChoice,
    NotificationPreset,
    NotificationReadbackResponse,
    NotificationSavedResponse,
    NotificationSetIn,
    NotificationSetupResponse,
    Option,
    PushPermissionIn,
    ReadAloud,
)

router = APIRouter(tags=["Keeping in touch"])

_DENIED = {403: {"description": "Not a member of this case, or the case does not exist."}}


def _ctx(s: Session, request: Request) -> intake.Ctx:
    return intake.ctx(s, request, acct.load_account(s))


def _owned_case(c: intake.Ctx, case_id: UUID) -> dict:
    case = intake.load_case(c.s, case_id)
    if not c.s.is_case_member(case_id, ("owner", "co_executor")):
        raise case_access_denied()
    return case


def _resolve(c: intake.Ctx, case_id: UUID, req: NotificationSetIn) -> NotificationChoice:
    if req.choice is not None:
        return req.choice
    if req.preset == NotificationPreset.keep_it_simple:
        return nt.KEEP_IT_SIMPLE
    if req.preset == NotificationPreset.skip:
        return nt.IN_APP_ONLY
    # same_as: another of this user's journeys. The case boundary hides anyone else's.
    if req.same_as_case_id == case_id:
        raise ApiError(422, "validation_failed", "Choose a different journey to copy.")
    row = nt.load(c.s, req.same_as_case_id)
    if row is None:
        raise case_access_denied()
    return nt.choice_of(row)


def _after_save(c: intake.Ctx, case: dict, row: dict) -> NextStep:
    if NotificationChannel.push.value in row["channels"] and not row["push_permission_granted"]:
        # The OS prompt is shown only now, after the user chose push.
        return NextStep(action="request_push_permission", prompt=c.copy["notifications_push_permission"])
    if case["status"] == "draft":
        return NextStep(action="preview_journey", prompt=c.copy["journey_preview_intro"])
    return NextStep(action="done", prompt=c.copy["notifications_saved"])


@router.get(
    "/v1/cases/{case_id}/notification-preferences",
    response_model=NotificationSetupResponse,
    summary="How Cairn keeps in touch for this journey",
    description=(
        "UC-CASE-19. Explains that the user decides and can change it any time, then offers shortcuts (the same "
        "as another journey first, then Keep it simple for me) and the questions in order: channel, reasons, "
        "timing, frequency. With no choice stored, the effective choice is in_app_only."
    ),
    responses=_DENIED,
)
def get_setup(case_id: UUID, request: Request, identity: Identity = Depends(get_identity)) -> NotificationSetupResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        c = _ctx(s, request)
        _owned_case(c, case_id)
        names = nt.display_names(s, c.copy)
        masked = nt.masked_account_email(c.account)
        row = nt.load(s, case_id)
        # UC-CASE-19 voice samples: how to keep in touch, asked in the user's voice.
        step = NextStep(action="choose_notifications", prompt=c.copy[f"notifications_question_{c.voice}"])
        explanation = c.copy["notifications_intro"]
        return NotificationSetupResponse(
            case_id=case_id, display_name=names.get(case_id, c.copy["your_loved_one"]), explanation=explanation,
            preferences=nt.out(case_id, row, c.copy), masked_email=masked,
            shortcuts=nt.shortcuts(s, c.copy, case_id, names), questions=nt.questions(c.copy, masked),
            next_step=step, read_aloud=ReadAloud(label=c.copy["read_this_to_me"], text=f"{explanation} {step.prompt}"))


@router.post(
    "/v1/cases/{case_id}/notification-preferences/readback",
    response_model=NotificationReadbackResponse,
    summary="Read a choice back before saving it",
    description="UC-CASE-19 step 6. Says the choice in plain language and asks to confirm. Nothing is stored.",
    responses=_DENIED,
)
def readback(case_id: UUID, req: NotificationSetIn, request: Request,
             identity: Identity = Depends(get_identity)) -> NotificationReadbackResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        c = _ctx(s, request)
        _owned_case(c, case_id)
        choice = _resolve(c, case_id, req)
        prefs = nt.out(case_id, None, c.copy, choice)
        return NotificationReadbackResponse(preferences=prefs, next_step=NextStep(
            action="confirm_notifications", prompt=f"{prefs.readback} {c.copy['notifications_readback_question']}",
            options=[Option(value="yes", label=c.copy["notifications_yes"]),
                     Option(value="change", label=c.copy["notifications_change"])]))


@router.put(
    "/v1/cases/{case_id}/notification-preferences",
    response_model=NotificationSavedResponse,
    summary="Save how Cairn keeps in touch for this journey",
    description=(
        "UC-CASE-19 and UC-CASE-20. Send the confirmed choice or a shortcut: keep_it_simple, skip (in_app_only), "
        "or same_as another journey. Each journey keeps its own choice. Works on drafts, active, paused, closed, "
        "and read-only cases, always free."
    ),
    responses=_DENIED,
)
def save(case_id: UUID, req: NotificationSetIn, request: Request,
         identity: Identity = Depends(get_identity)) -> NotificationSavedResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        c = _ctx(s, request)
        case = _owned_case(c, case_id)
        row = nt.save(s, case_id, _resolve(c, case_id, req))
        return NotificationSavedResponse(preferences=nt.out(case_id, row, c.copy),
                                         acknowledgment=c.copy["notifications_saved"],
                                         next_step=_after_save(c, case, row))


@router.post(
    "/v1/cases/{case_id}/notification-preferences/push-permission",
    response_model=NotificationSavedResponse,
    summary="What the OS said to the push permission prompt",
    description=(
        "UC-CASE-19. Push is never required. If the user declines, push is taken off this journey's channels "
        "(and in_app_only is used when nothing else is left), so the readback never promises a message that "
        "can't arrive."
    ),
    responses=_DENIED,
)
def push_permission(case_id: UUID, req: PushPermissionIn, request: Request,
                    identity: Identity = Depends(get_identity)) -> NotificationSavedResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        c = _ctx(s, request)
        case = _owned_case(c, case_id)
        choice = nt.choice_of(nt.load(s, case_id))
        if NotificationChannel.push not in choice.channels:
            raise ApiError(409, "push_not_chosen", "Notifications on this phone aren't chosen for this journey.")
        if req.granted:
            row = nt.save(s, case_id, choice, push_granted=True)
            ack = c.copy["notifications_saved"]
        else:
            rest = [ch for ch in choice.channels if ch != NotificationChannel.push]
            choice = choice.model_copy(update={"channels": rest}) if rest else nt.IN_APP_ONLY
            row = nt.save(s, case_id, choice, push_granted=False)
            ack = c.copy["notifications_push_declined"]
        return NotificationSavedResponse(preferences=nt.out(case_id, row, c.copy), acknowledgment=ack,
                                         next_step=_after_save(c, case, row))


# ------------------------------------------------------------------ account-wide (UC-CASE-20)

def _journeys(c: intake.Ctx, ids: list[UUID] | None = None) -> list[JourneyNotifications]:
    names = nt.display_names(c.s, c.copy)
    out = []
    for case in nt.owned_cases(c.s):
        if ids is not None and case["id"] not in ids:
            continue
        status = intake.effective_status(case)
        out.append(JourneyNotifications(case_id=case["id"], display_name=names[case["id"]], status=status,
                                        preferences=nt.out(case["id"], nt.load(c.s, case["id"]), c.copy)))
    return out


def _which_journey(c: intake.Ctx, journeys: list[JourneyNotifications]) -> NextStep:
    """Ask which journey by the display name of each person, and offer All of them."""
    return NextStep(action="choose_journeys", prompt=c.copy["notifications_which_journey"],
                    options=[*(Option(value=str(j.case_id), label=j.display_name) for j in journeys),
                             Option(value="all", label=c.copy["all_of_them"])])


@router.get(
    "/v1/me/notification-preferences",
    response_model=AccountNotificationsResponse,
    summary="How Cairn keeps in touch, for every journey",
    description="UC-CASE-20 from Settings. One entry per journey, named by the person's display name.",
)
def list_all(request: Request, identity: Identity = Depends(get_identity)) -> AccountNotificationsResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        c = _ctx(s, request)
        journeys = _journeys(c)
        step = (_which_journey(c, journeys) if len(journeys) > 1
                else NextStep(action="change_notifications", prompt=c.copy["notifications_intro"]))
        return AccountNotificationsResponse(journeys=journeys, next_step=step)


def apply_change(c: intake.Ctx, req: NotificationChangeIn) -> NotificationChangeResponse:
    """Shared by Settings and chat. stop_everything is one step. A choice is read back, then applied on yes."""
    owned = [r["id"] for r in nt.owned_cases(c.s)]
    if not owned:
        return NotificationChangeResponse(applied=False, acknowledgment=c.copy["notifications_no_journey"],
                                          readback=None, journeys=[],
                                          next_step=NextStep(action="done", prompt=c.copy["notifications_no_journey"]))
    if req.scope == "all" or (req.scope is None and (req.stop_everything or len(owned) == 1)):
        targets = owned
    elif req.scope is None:
        return NotificationChangeResponse(applied=False, acknowledgment=None, readback=None,
                                          journeys=_journeys(c), next_step=_which_journey(c, _journeys(c)))
    else:
        if not set(req.scope) <= set(owned):
            raise case_access_denied()
        targets = list(dict.fromkeys(req.scope))

    if req.stop_everything:
        # No persuasion and no follow-up question.
        for case_id in targets:
            nt.save(c.s, case_id, nt.IN_APP_ONLY, push_granted=False)
        return NotificationChangeResponse(
            applied=True, acknowledgment=c.copy["notifications_stopped"], readback=c.copy["readback_in_app_only"],
            journeys=_journeys(c, targets), next_step=NextStep(action="done", prompt=c.copy["notifications_stopped"]))

    line = nt.readback(c.copy, req.choice)
    if not req.confirm:
        return NotificationChangeResponse(
            applied=False, acknowledgment=None, readback=line, journeys=_journeys(c, targets),
            next_step=NextStep(action="confirm_notification_change",
                               prompt=f"{line} {c.copy['notifications_change_question']}",
                               options=[Option(value="yes", label=c.copy["notifications_change_yes"]),
                                        Option(value="no", label=c.copy["notifications_change_no"])]))
    for case_id in targets:
        nt.save(c.s, case_id, req.choice)
    needs_push = NotificationChannel.push in req.choice.channels and not all(
        (nt.load(c.s, t) or {}).get("push_permission_granted") for t in targets)
    step = (NextStep(action="request_push_permission", prompt=c.copy["notifications_push_permission"]) if needs_push
            else NextStep(action="done", prompt=c.copy["notifications_changed"]))
    return NotificationChangeResponse(applied=True, acknowledgment=c.copy["notifications_changed"], readback=line,
                                      journeys=_journeys(c, targets), next_step=step)


@router.post(
    "/v1/me/notification-preferences/changes",
    response_model=NotificationChangeResponse,
    summary="Change how Cairn keeps in touch",
    description=(
        "UC-CASE-20. stop_everything sets in_app_only in one step, on every journey unless a scope is given. "
        "Any other change is read back in one line and applied only with confirm=true. With more than one "
        "journey and no scope, asks which journey by the person's display name and offers All of them."
    ),
    responses={403: {"description": "A journey in scope isn't one of the user's."}},
)
def change(req: NotificationChangeIn, request: Request,
           identity: Identity = Depends(get_identity)) -> NotificationChangeResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        return apply_change(_ctx(s, request), req)
