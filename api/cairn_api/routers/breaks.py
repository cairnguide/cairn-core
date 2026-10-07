"""Take a break, signed in (database/docs/cairn-take-a-break-use-cases-v32.json, UC-BRK-03 to UC-BRK-12).

One select, no confirmation (UC-BRK-01). Never needs a writable account,
finished acknowledgments, or a subscription: a break is a safety feature. The
reason, the kind of break, and the care level are never stored. No break of
any kind calls Stripe or changes a subscription field (BRK-D-08, AC-BRK-09).
Before sign-in, GET /v1/break opens S-01.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request

from .. import account as acct
from .. import breaks, journey, safety, subscription
from .. import notifications as nt
from ..auth import Identity, get_identity
from ..copy_store import Copy
from ..db import Session
from ..errors import ApiError
from ..schemas import BreakChangeIn, BreakIn, BreakResponse

router = APIRouter(prefix="/v1/me/break", tags=["Take a break"])


def care_level_of(req: BreakIn) -> int:
    return max(req.care_level, safety.care_level(req.session) if req.session else 1)


def _open_screen(s: Session, request: Request, account: dict, *, care_level: int) -> BreakResponse:
    """The screen Take a break opens from where the user is."""
    copy: Copy = request.app.state.break_copy
    if account["onboarding_step"] != "complete":
        return breaks.setup_pause(copy, has_setup_left=True)
    if breaks.has_active_journey(s):
        return breaks.rest_choices(copy, account, care_level=care_level, current=breaks.choice_of(account))
    if any(c["status"] == "draft" for c in s.owned_cases()):
        return breaks.draft_pause(copy)
    return breaks.setup_pause(copy, has_setup_left=False)


def _start(s: Session, request: Request, choice: str, care_level: int) -> BreakResponse:
    copy: Copy = request.app.state.break_copy
    settings = request.app.state.settings
    account = acct.load_account(s)
    started = account["break_started_at"] if account["on_break"] else s.now
    until = breaks.break_until(choice, started, s.now, account["time_zone"])
    if until is not None and until <= s.now:
        until = breaks.break_until("today", started, s.now, account["time_zone"])
    care = care_level >= 2
    notice = breaks.notice_at(started, until, care=care, tz=account["time_zone"], prefs=nt.load(s),
                              send_without_reminders=settings.break_notice_without_reminders)
    s.begin_break(until, care=care, notice_at=notice)
    s.audit("break_started", object_type="user", object_id=account["id"])
    return breaks.started(copy, acct.load_account(s), nt.load(s), care_level=care_level)


@router.get("", response_model=BreakResponse, summary="The break screen for where the user is",
            description="During a break, the resting screen S-06, shown first whenever Cairn is opened (UC-BRK-08). "
                        "Otherwise the screen Take a break opens: S-02 in setup, S-02b with no case, S-03 in a "
                        "draft, S-04 on an active journey.")
def get_break(request: Request, identity: Identity = Depends(get_identity), care_level: int = 1) -> BreakResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        account = acct.load_account(s)
        if account["on_break"]:
            return breaks.resting(request.app.state.break_copy, account, nt.load(s))
        return _open_screen(s, request, account, care_level=care_level)


@router.post(
    "",
    response_model=BreakResponse,
    summary="Take a break",
    description=(
        "UC-BRK-03 to UC-BRK-07. Without choice, opens the screen for where the user is: S-02 during setup (progress "
        "is already saved), S-02b after setup with no case, S-03 for a draft (use the case's take-a-break to save "
        "where they left off), or S-04's four rest choices on an active journey. With a choice on an active journey, "
        "starts the break on the account and every active journey (BRK-D-06): the rest of today ends at 11:59 PM "
        "local time, a few days after 3 days, a week after 7, and Until I come back has no end (BRK-D-07). A break "
        "longer than 24 hours at care level 1 gets one break-ending notice 24 hours before (UC-BRK-09). At care "
        "levels 2 to 4 it is a care rest: the free days stop while they are running (DEC-26-01) and no notice goes "
        "outside Cairn (BRK-D-03). Never touches a subscription."
    ),
)
def take_a_break(request: Request, req: BreakIn | None = None,
                 identity: Identity = Depends(get_identity)) -> BreakResponse:
    req = req or BreakIn()
    level = care_level_of(req)
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        account = acct.load_account(s)
        if req.choice is None or account["onboarding_step"] != "complete" or not breaks.has_active_journey(s):
            return _open_screen(s, request, account, care_level=level)
        return _start(s, request, req.choice, level)


@router.put(
    "",
    response_model=BreakResponse,
    summary="Change how long",
    description="UC-BRK-11. A few days and a week count from the break's original start, the rest of today from "
                "now. The notice is moved or cancelled, and a changed break never sends two notices.",
    responses={409: {"description": "There is no break to change."}},
)
def change_break(req: BreakChangeIn, request: Request, identity: Identity = Depends(get_identity)) -> BreakResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        if not acct.load_account(s)["on_break"]:
            raise ApiError(409, "not_on_break", request.app.state.break_copy["signed_in_nothing_to_pause"])
        return _start(s, request, req.choice, req.care_level)


@router.delete(
    "",
    response_model=BreakResponse,
    summary="I'm back",
    description=(
        "UC-BRK-10. Clears the break on the account and every journey and cancels an unsent notice. If a care rest "
        "stopped the free days, they start again and trial_ends_at moves later by exactly the paused time "
        "(AC-26-04), said once on S-07 at care level 1. An early subscriber's first charge moves with it "
        "(UC-SUB-06). Never asks the user to explain the gap. Reminders held during the break resume on their "
        "normal schedule, with no catch-up burst."
    ),
)
def end_break(request: Request, identity: Identity = Depends(get_identity), care_level: int = 1) -> BreakResponse:
    copy: Copy = request.app.state.break_copy
    with request.app.state.db.session(identity.subject) as s:
        uid = s.require_user()
        paused = s.end_break()
        s.audit("break_ended", object_type="user", object_id=uid)
        account = acct.load_account(s)
        response = breaks.back(copy, account, preferred_name=account["preferred_name"],
                               last_step=_last_step(s, copy), paused=paused, care_level=care_level)
    if paused:
        subscription.sync_first_charge(request.app.state.stripe, account, datetime.now(timezone.utc))
    return response


def _last_step(s: Session, copy: Copy) -> str:
    """Where the user left off, in one line: the next task on the most recent journey."""
    for case in s.owned_cases(newest_activity_first=True):
        if case["status"] != "draft":
            nxt = journey.pick_next_action(journey.load_tasks(s, case["id"]))
            if nxt:
                return nxt["title"]
    return copy["back_nothing_in_progress"]
