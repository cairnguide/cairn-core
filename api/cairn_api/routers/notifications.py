"""How Cairn keeps in touch: Settings (account UC-REG-17) and the journey confirmation (case UC-CASE-19).

One set of choices for the whole account (account D-13). Changing them is always
free, on every account status, and never gated on acknowledgments: a request to
stop reminders must always work (D-2026-09-25-F1). At care level 4 no Settings
change is made in the same turn (AC-26-11).
"""
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from .. import account as acct
from .. import intake
from .. import notifications as nt
from ..auth import Identity, get_identity
from ..copy_store import Copy
from ..db import Session
from ..errors import ApiError, case_access_denied
from ..schemas import (
    KeepInTouchIn,
    KeepInTouchQuestion,
    KeepInTouchResponse,
    NextStep,
    NotificationSettingsPatch,
    NotificationSettingsResponse,
    Option,
    ReadAloud,
)

router = APIRouter(tags=["Keeping in touch"])

_DENIED = {403: {"description": "Not a member of this case, or the case does not exist."}}
# Before any choice was made (accounts set up before account D-13), nothing goes outside Cairn.
NOTHING_OUTSIDE = {"channels": ["in_app"], "frequency": "none"}


def settings_saved(s: Session, request: Request, account: dict) -> str:
    """UC-REG-17. Saved, and where the confirmation was sent. Queues one confirmation, replacing one not yet sent,
    so several changes in a row send one message (UC-CASE-21)."""
    copy: Copy = request.app.state.copy
    s.queue_confirmation("settings_changed", replace_unsent=True)
    destination = copy["settings_confirmation_destination"].format(masked_email=acct.mask_email(account["email"]))
    return copy["settings_saved"].format(confirmation_destination=destination)


def apply_settings(s: Session, request: Request, req: NotificationSettingsPatch) -> NotificationSettingsResponse:
    """Shared by Settings and chat. Each choice saves on its own and is read back in one line."""
    copy: Copy = request.app.state.copy
    account = acct.load_account(s)
    if req.care_level == 4:
        # AC-26-11. Nothing changes in this turn. The same request works when the user asks again.
        return NotificationSettingsResponse(preferences=nt.out(nt.load(s), copy), acknowledgment=None,
                                            next_step=NextStep(action="not_now", prompt=copy["settings_not_now"]))
    values: dict = {}
    if req.stop_all_reminders:
        values["frequency"] = "none"  # one step, no persuasion
    else:
        if req.channels is not None:
            values["channels"], values["browser_push_endpoint"] = nt.channels_from(req.channels)
        for f in ("frequency", "due_date_lead", "inactivity_after"):
            if getattr(req, f) is not None:
                values[f] = getattr(req, f).value
        for f in ("quiet_hours_start", "quiet_hours_end"):
            if getattr(req, f) is not None:
                values[f] = getattr(req, f)
    if nt.load(s) is None:
        values = {**NOTHING_OUTSIDE, **values}
    row = s.save_notification_preferences(values)
    s.audit("notification_preferences_saved", object_type="user", object_id=account["id"])
    prefs = nt.out(row, copy)
    saved = settings_saved(s, request, account)
    ack = copy["notify_stopped"] if req.stop_all_reminders else saved
    notes_step = NextStep(action="done", prompt=f"{prefs.readback} {ack}")
    if req.channels is not None and req.channels.browser and req.channels.browser_permission == "denied":
        notes_step = NextStep(action="done", prompt=copy["notify_browser_denied"])
    return NotificationSettingsResponse(preferences=prefs, acknowledgment=ack, next_step=notes_step)


@router.get(
    "/v1/me/notification-preferences",
    response_model=NotificationSettingsResponse,
    summary="How Cairn keeps in touch",
    description="UC-REG-17. Channels, frequency, due date lead time, inactivity notices, and quiet hours, each "
                "editable on its own, with Stop all reminders. Available on a read-only account too.",
)
def get_preferences(request: Request, identity: Identity = Depends(get_identity)) -> NotificationSettingsResponse:
    copy: Copy = request.app.state.copy
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        prefs = nt.out(nt.load(s), copy)
        return NotificationSettingsResponse(preferences=prefs, acknowledgment=None, next_step=NextStep(
            action="change_notifications", prompt=prefs.readback,
            options=[Option(value="stop_all_reminders", label=copy["notify_stop_all"])]),
            push_public_key=request.app.state.settings.vapid_public_key)


@router.patch(
    "/v1/me/notification-preferences",
    response_model=NotificationSettingsResponse,
    summary="Change how Cairn keeps in touch",
    description=(
        "UC-REG-17. Send only the choice being changed. Each is read back in one line and saved separately, with "
        "copy.settings_saved saying where the confirmation went. stop_all_reminders sets frequency none in one "
        "step, with no persuasion. Turning browser off clears the push endpoint. Browser permission denied takes "
        "browser off. Same validation and data boundary as setup. Always free, on a read-only account too. At "
        "care level 4 nothing changes in that turn (AC-26-11)."
    ),
)
def change_preferences(req: NotificationSettingsPatch, request: Request,
                       identity: Identity = Depends(get_identity)) -> NotificationSettingsResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        return apply_settings(s, request, req)


# ------------------------------------------------------------------ the journey confirmation (UC-CASE-19)

def _owned_case(c: intake.Ctx, case_id: UUID) -> dict:
    case = intake.load_case(c.s, case_id)
    if not c.s.is_case_member(case_id, ("owner", "co_executor")):
        raise case_access_denied()
    return case


def _questions(copy: Copy) -> list[KeepInTouchQuestion]:
    return [
        KeepInTouchQuestion(id="due_date_lead", prompt=copy["lead_time_question"], options=[
            Option(value=v, label=copy[f"lead_{v}"]) for v in ("day_before", "three_days", "one_week")]),
        KeepInTouchQuestion(id="inactivity_after", prompt=copy["inactivity_question"], options=[
            Option(value=v, label=copy[f"inactivity_{v}"]) for v in ("off", "three_days", "one_week", "two_weeks")]),
    ]


def _opening(c: intake.Ctx, row: dict | None) -> str:
    """Reads back the account choices in one line: channels and how often (UC-REG-15)."""
    reg: Copy = c.request.app.state.copy
    prefs = row or {**NOTHING_OUTSIDE}
    labels = {"email": reg["notify_channel_email_label"], "browser": reg["notify_channel_browser_label"],
              "in_app": reg["notify_channel_inapp_label"]}
    names = [labels[ch] for ch in ("email", "browser", "in_app") if ch in prefs["channels"]]
    channels = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    return c.copy["notifications_intro"].format(channels=channels,
                                                frequency=reg[f"notify_frequency_{prefs['frequency']}_label"])


@router.get(
    "/v1/cases/{case_id}/keep-in-touch",
    response_model=KeepInTouchResponse,
    summary="Confirm how Cairn keeps in touch",
    description=(
        "UC-CASE-19. Reads back the account's channels and frequency in one line, then asks only how early to hear "
        "about due dates and whether to check in after a while, one per screen. Channels and frequency are never "
        "asked from scratch here (account D-13). With No reminders, both questions are skipped and it says plainly "
        "that nothing is sent outside Cairn except confirmations."
    ),
    responses=_DENIED,
)
def get_keep_in_touch(case_id: UUID, request: Request,
                      identity: Identity = Depends(get_identity)) -> KeepInTouchResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        c = intake.ctx(s, request, acct.load_account(s))
        _owned_case(c, case_id)
        row = nt.load(s)
        opening = _opening(c, row)
        none = (row or NOTHING_OUTSIDE)["frequency"] == "none"
        questions = [] if none else _questions(c.copy)
        prompt = c.copy["keep_in_touch_none"] if none else questions[0].prompt
        step = NextStep(action="confirm_keep_in_touch", prompt=prompt,
                        options=questions[0].options if questions else [
                            Option(value="ok", label=request.app.state.copy["continue"])])
        return KeepInTouchResponse(
            opening=opening, preferences=nt.out(row, request.app.state.copy), questions=questions,
            change_link=Option(value="change_settings", label=c.copy["change_how_you_hear"]), next_step=step,
            read_aloud=ReadAloud(label=c.copy["read_this_to_me"], text=f"{opening} {prompt}"))


@router.put(
    "/v1/cases/{case_id}/keep-in-touch",
    response_model=NotificationSettingsResponse,
    summary="Save due date lead time and inactivity notices",
    description=(
        "UC-CASE-19. Saves due_date_lead and inactivity_after on the account's notification choices. skip keeps the "
        "account choices and the OPEN-03 defaults (3 days before, no inactivity notices). Skippable, and always "
        "free, on drafts and read-only accounts too."
    ),
    responses=_DENIED,
)
def save_keep_in_touch(case_id: UUID, req: KeepInTouchIn, request: Request,
                       identity: Identity = Depends(get_identity)) -> NotificationSettingsResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        c = intake.ctx(s, request, acct.load_account(s))
        case = _owned_case(c, case_id)
        values: dict = {"journey_confirmed_at": s.now}
        if not req.skip:
            if req.due_date_lead is not None:
                values["due_date_lead"] = req.due_date_lead.value
            if req.inactivity_after is not None:
                values["inactivity_after"] = req.inactivity_after.value
        if nt.load(s) is None:
            values = {**NOTHING_OUTSIDE, **values}
        row = s.save_notification_preferences(values)
        s.audit("notification_preferences_saved", case_id, "case", case_id)
        step = (NextStep(action="preview_journey", prompt=c.copy["journey_preview_intro"]) if case["status"] == "draft"
                else NextStep(action="done", prompt=c.copy["keep_in_touch_saved"]))
        return NotificationSettingsResponse(preferences=nt.out(row, request.app.state.copy),
                                            acknowledgment=c.copy["keep_in_touch_saved"], next_step=step)


def require_not_level_4(care_level: int, request: Request) -> None:
    if care_level == 4:
        raise ApiError(409, "not_now", request.app.state.copy["settings_not_now"])
