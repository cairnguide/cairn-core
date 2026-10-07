"""The home screen (case UC-CASE-25, proposed) and where a returning sign-in goes first (account UC-REG-18).

Take a break, Support resources, Settings, Sign out, the AI guide label, and
Read this to me are on it always. The page title never names the person who
died. Nothing about the trial, the price, or subscribing at care levels 3 and 4
(AC-26-03), and no subscribe prompt at level 2 or during a break (UC-SUB-01).
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request

from .. import account as acct
from .. import breaks, intake, journey, onboarding
from .. import notifications as nt
from ..auth import Identity, get_identity
from ..copy_store import Copy
from ..db import Session
from ..schemas import HomeCaseCard, HomeResponse, NextStep, Note, Option, SubscribePrompt

router = APIRouter(prefix="/v1/home", tags=["Home"])


def _cards(s: Session, request: Request, account: dict, *, care_level: int) -> list[HomeCaseCard]:
    c = intake.ctx(s, request, account)
    reg: Copy = request.app.state.copy
    brk: Copy = request.app.state.break_copy
    cards = []
    for row in s.owned_cases(newest_activity_first=True):
        case = intake.load_case(s, row["id"])
        answers = intake.load_answers(s, row["id"])
        name = intake.display_name(case, answers, c.copy)
        if case["status"] == "draft":
            step = case["last_intake_step"]
            cards.append(HomeCaseCard(
                id=case["id"], status="draft", display_name=name,
                where_left_off=reg["home_draft_where"].format(step=c.copy[f"field_{step}"]) if step else None,
                draft_notice=brk["draft_pause"], draft_expires_at=case["draft_expires_at"],
                actions=[Option(value="keep_going", label=reg["home_keep_going"]),
                         Option(value="delete", label=reg["home_delete"])]))
            continue
        nxt = journey.pick_next_action(journey.load_tasks(s, case["id"]))
        trial = None
        if care_level < 3 and acct.on_free_days(account) and acct.local_trial_end(account):
            trial = reg["home_trial_line"].format(trial_end_date=acct.format_date(acct.local_trial_end(account)))
        cards.append(HomeCaseCard(
            id=case["id"], status=intake.effective_status(case), display_name=name,
            next_task=reg["home_next_task"].format(task=nxt["title"]) if nxt else None, trial_line=trial,
            actions=[Option(value="open_journey", label=reg["home_open_journey"])]))
    return cards


def _subscribe_prompt(s: Session, request: Request, account: dict, *, care_level: int,
                      prompt_seen: bool) -> SubscribePrompt | None:
    """UC-SUB-01 and UC-SUB-11. Once a session (the client sends prompt_seen) and never more than once a day."""
    if (care_level != 1 or prompt_seen or account["on_break"] or not acct.is_read_only(account)
            or acct.has_subscription(account)):
        return None
    if not s.mark_subscribe_prompt_shown():
        return None
    sub: Copy = request.app.state.subscription_copy
    lapsed = account["subscription_status"] == "lapsed"
    return SubscribePrompt(
        title=sub["subscribe_prompt_title"] if not lapsed else sub["settings_status_none"],
        body=sub["ended_read_only"] if lapsed else sub["subscribe_prompt_body"],
        options=[Option(value="subscribe", label=sub["subscribe_again" if lapsed else "subscribe_prompt_button"]),
                 Option(value="not_now", label=sub["subscribe_prompt_not_now"])])


@router.get(
    "",
    response_model=HomeResponse,
    summary="The home screen",
    description=(
        "UC-CASE-25 and UC-REG-18. route says where to go: resume_setup (UC-REG-13), resting (the S-06 break screen "
        "first, UC-BRK-08), resume_draft (UC-BRK-04), or home. The home screen greets by the preferred name, shows "
        "one Start a case action when there are no cases, a draft's card with the 28-day draft notice, and an active "
        "journey's card with its next task and, on the free days, their end date (never at care levels 3 and 4). A "
        "read-only account gets the banner and, at level 1 only, the subscribe prompt once a session and once a day "
        "(UC-SUB-01). A check-in the user said yes to shows here once (DEC-26-04). With session_start, the "
        "session-start AI reminder shows when due (UC-CASE-23)."
    ),
)
def home(request: Request, identity: Identity = Depends(get_identity),
         care_level: int = Query(default=1, ge=1, le=4), session_start: bool = False,
         subscribe_prompt_seen: bool = False) -> HomeResponse:
    reg: Copy = request.app.state.copy
    case_copy: Copy = request.app.state.case_copy
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        s.settle_trial_clock()
        account = acct.load_account(s)
        support = onboarding.support(reg)
        always = [reg["take_a_break_control"], reg["support_resources_link"], reg["home_settings"],
                  reg["home_sign_out"], reg["ai_persistent_label"], reg["read_this_to_me"]]
        name = account["preferred_name"]
        greeting = reg["home_greeting"].format(preferred_name=name) if name else reg["home_greeting_no_name"]
        notes: list[Note] = []
        ai = None
        if session_start and acct.step_reached(account, acct.OnboardingStep.ai_notice_accepted) and \
                s.ai_reminder_due(session_start=True, every=timedelta(hours=3)):
            ai = case_copy["ai_reminder"]
            s.mark_ai_reminder_shown()
        base = dict(greeting=greeting, page_title=reg["home_page_title"], always_visible=always, support=support,
                    ai_reminder=ai)

        if account["onboarding_step"] != "complete" or acct.adult_needed(account):
            return HomeResponse(route="resume_setup", cases=[], notes=[], **{**base, "greeting": reg[
                "resume_onboarding"]}, next_step=NextStep(action="resume_onboarding",
                                                          prompt=reg["resume_onboarding"]))
        if account["on_break"]:
            # UC-BRK-08. The resting screen first. Nothing else asks for attention, and no prompt waits.
            resting = breaks.resting(request.app.state.break_copy, account, nt.load(s))
            return HomeResponse(route="resting", resting=resting, cases=[], notes=[], **base,
                                next_step=resting.next_step)
        if account["break_started_at"] is not None:
            # The break's end passed while the user was away (UC-BRK-10). No catch-up burst, no questions.
            s.end_break()
            account = acct.load_account(s)
        if s.take_due_check_in():
            notes.append(Note(kind="crisis", text=case_copy["check_in_in_cairn"]))
        if session_start and name:
            base["greeting"] = reg["signin_welcome_back"].format(preferred_name=name)
        notes += acct.account_notes(s, account, request, care_level=care_level)
        prompt = _subscribe_prompt(s, request, account, care_level=care_level, prompt_seen=subscribe_prompt_seen)
        cards = _cards(s, request, account, care_level=care_level)
        if not cards:
            step = NextStep(action="start_case", prompt=case_copy["empty_state"],
                            options=[Option(value="start_case", label=case_copy["empty_state_button"])])
            return HomeResponse(route="home", cases=[], notes=notes, subscribe_prompt=prompt, **base, next_step=step)
        drafts_only = all(card.status == "draft" for card in cards)
        route = "resume_draft" if drafts_only and session_start else "home"
        first = cards[0]
        step = NextStep(action="open_case", prompt=first.display_name, options=first.actions)
        return HomeResponse(route=route, cases=cards, notes=notes, subscribe_prompt=prompt, **base, next_step=step)
