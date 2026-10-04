"""UC-CASE-12 and UC-CASE-13 (see the journey that fits, choose how Cairn keeps in touch, start it, choose a
first task), plus stepping back from tasks (UC-12) and status across the case (UC-13).
"""
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from .. import account as acct
from .. import intake, journey, messages, safety
from .. import notifications as nt
from ..auth import Identity, get_identity
from ..db import Session
from ..errors import ApiError, case_access_denied
from ..schemas import (
    CaseStatusResponse,
    CategoryStatus,
    CheckIn,
    CitationOut,
    FirstTaskChoice,
    FirstTaskIn,
    FirstTaskResponse,
    IntakeSession,
    IntakeTurnResponse,
    JourneyPreviewResponse,
    JourneyResponse,
    NextStep,
    Note,
    Option,
    PauseRequest,
    PreButtonNotice,
    PreviewTask,
    PreviewWeek,
    ReadAloud,
    StartJourneyIn,
    StartJourneyResponse,
    StatusCounts,
    TakeABreakIn,
    TaskCategory,
    WeekOut,
    journey_status,
)

router = APIRouter(prefix="/v1/cases/{case_id}", tags=["Journey"])

_DENIED = {403: {"description": "Not a member of this case, not an owner, or the case does not exist."}}
STATUS_LABELS = {"done": "done_label", "handled_elsewhere": "handled_elsewhere"}


def _journey_view(s: Session, request: Request, account: dict, case_id: UUID, notes=None) -> JourneyResponse:
    c = intake.ctx(s, request, account)
    case = intake.load_case(s, case_id)
    base = dict(case_id=case_id, journey_started_on=case["journey_started_on"],
                current_week=journey.current_week(case["journey_started_on"]) if case["journey_started_on"] else 1,
                notes=notes or [])

    if case["status"] == "draft":
        return JourneyResponse(**base, mode="not_started", weeks=[],
                               next_step=NextStep(action="preview_journey", prompt=c.copy["journey_preview_intro"]))

    if s.take_due_check_in(case_id):
        # DEC-26-04. The check-in the user said yes to, shown once, because it isn't going by email.
        base["notes"] = [Note(kind="crisis", text=c.copy["check_in_in_cairn"]), *base["notes"]]

    if journey.is_paused(case):
        # Step back from task mode. Progress is untouched. Tasks are not listed on purpose.
        return JourneyResponse(
            **base, mode="paused", paused_until=case["tasks_paused_until"], weeks=[],
            check_in=CheckIn(message=messages.CHECK_IN_MESSAGE, options=messages.CHECK_IN_OPTIONS),
            next_step=NextStep(action="check_in", prompt=messages.CHECK_IN_MESSAGE),
        )

    answers = intake.load_answers(s, case_id)
    rows = journey.load_tasks(s, case_id)
    context = journey.task_context(c, case, answers)
    weeks = []
    for week in range(1, 5):
        in_week = [r for r in rows if r["journey_week"] == week]
        weeks.append(WeekOut(week=week, total=len(in_week),
                             done=sum(1 for r in in_week if r["status"] == "done"),
                             tasks=[journey.task_summary(r, context) for r in in_week]))
    nxt = journey.pick_next_action(rows, recommended=context.recommended)
    base["notes"] = [*base["notes"], *context.loose_notes]
    return JourneyResponse(
        **base, mode="tasks", weeks=weeks, support=context.support,
        next_action=journey.task_summary(nxt, context) if nxt else None,
        next_step=messages.TASK_NEXT if nxt else messages.ALL_DONE,
    )


def _notice(c: intake.Ctx) -> PreButtonNotice | None:
    """UC-CASE-12 and UC-CASE-18. Before the button: when the free days begin, or that they don't change.
    A subscribed account sees no trial wording."""
    account = c.account
    if account["status"] == "subscribed":
        text = c.copy["pre_button_notice_subscribed"]
    elif account["trial_started_at"] is None:
        text = c.copy["pre_button_notice"]
    else:
        text = c.copy["pre_button_notice_existing_trial"].format(
            trial_end_date=acct.format_date(acct.local_trial_end(account)))
    return PreButtonNotice(text=text, version=c.copy.version_of_text(text))


@router.get(
    "/journey/preview",
    response_model=JourneyPreviewResponse,
    summary="See the journey that fits",
    description=(
        "UC-CASE-12. Runs journey selection on the answers so far and explains in a sentence or two why the "
        "journey fits. Shows the first four weeks by week: done items checked, open items labeled When you're "
        "ready. Missing answers never block it. Shows the pre-button notice, then Start journey and Not yet. "
        "Viewing it never starts the free period."
    ),
    responses=_DENIED,
)
def preview(case_id: UUID, request: Request, identity: Identity = Depends(get_identity),
            care_level: int = Query(default=1, ge=1, le=4, description="The session's care level. At levels 3 "
                                    "and 4 nothing about the free days, the price, or subscribing is shown.")
            ) -> JourneyPreviewResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        ready = acct.require_ready(s, request, write=False)
        c = intake.ctx(s, request, ready.account)
        case = intake.load_case(s, case_id)
        if case["status"] == "draft":
            intake.touch(s, case_id)
            case = intake.load_case(s, case_id)
        answers = intake.load_answers(s, case_id)
        definition, sel = intake.selection(s, case, answers)
        tmpl = intake.templates(s, sel.task_keys)
        loose, attached = intake.notes_by_task(c, definition, sel)

        weeks = []
        for week in range(1, 5):
            keys = intake.ordered([k for k in sel.task_keys if tmpl[k]["journey_week"] == week], tmpl)
            # Recommended first. Probably-not-applicable last, never hidden (UC-CASE-06, UC-CASE-12).
            keys.sort(key=lambda k: (k in sel.probably_not_applicable, k not in sel.recommended))
            tasks = []
            for k in keys:
                t, status = tmpl[k], sel.initial_status[k]
                if k in sel.probably_not_applicable and status == "not_started":
                    label = c.copy["probably_not_applicable_label"]
                elif k in sel.needs_check:
                    label = c.copy["check_on_this_label"]
                else:
                    label = c.copy[STATUS_LABELS.get(status, "when_youre_ready")]
                tasks.append(PreviewTask(
                    task_key=k, title=t["title"], plain_summary=t["plain_summary"], journey_week=week,
                    waypoint=definition["task_waypoints"].get(k), status=status,
                    journey_status=journey_status(status), needs_check=k in sel.needs_check,
                    probably_not_applicable=k in sel.probably_not_applicable, recommended=k in sel.recommended,
                    status_label=label,
                    attorney_referral=t["attorney_referral"], attorney_line=intake.attorney_line(c, t),
                    citations=[CitationOut.model_validate(x) for x in t["citations"]], notes=attached.get(k, [])))
            weeks.append(PreviewWeek(week=week, label=c.copy["week_label"].format(week=week), tasks=tasks))

        path = definition["paths"][sel.path_key]
        explanation = c.copy[path["why_copy_key"]]
        is_draft = case["status"] == "draft"
        chosen = nt.load(s, case_id) is not None
        if not is_draft:
            notice, available = None, False
            step = NextStep(action="view_journey", prompt=c.copy["journey_preview_intro"])
        elif care_level >= 3:
            # UC-CASE-12 care_level_3_or_4: no notice, no trial or price wording. Setup waits until level 1.
            notice, available = None, False
            step = intake.safety_step(c, IntakeSession(safety_mode=safety.SEVERITY[care_level - 1]))
        elif case["death_not_yet_occurred"]:
            # UC-CASE-17. Start journey is not offered.
            notice, available = None, False
            step = NextStep(action="death_not_yet", prompt=c.copy["death_not_yet_explain"],
                            options=[Option(value="save_draft", label=c.copy["save_draft_now"]),
                                     Option(value="has_happened", label=c.copy["death_has_happened"])])
        elif ready.account["status"] == "read_only":
            # UC-CASE-18. The only place outside day 29 where the subscription prompt is shown.
            notice, available = None, False
            step = NextStep(action="choose_subscription", prompt=c.copy["subscription_needed_new_journey"],
                            options=[Option(value="subscribe", label=request.app.state.copy["subscribe_button"])])
        elif not chosen:
            # UC-CASE-12 step 3 (UC-CASE-19): how Cairn keeps in touch, before the notice and the buttons.
            notice, available = _notice(c), True
            names = nt.display_names(s, c.copy)
            step = NextStep(action="choose_notifications", prompt=c.copy["notifications_intro"],
                            options=nt.shortcuts(s, c.copy, case_id, names))
        else:
            notice, available = _notice(c), True
            step = NextStep(action="start_journey", prompt=notice.text,
                            options=[Option(value="start_journey", label=c.copy["start_journey"]),
                                     Option(value="not_yet", label=c.copy["not_yet"])])
        text = " ".join([c.copy["journey_preview_intro"], explanation, step.prompt])
        return JourneyPreviewResponse(
            case=intake.case_out(c, case, answers), journey_template_key=sel.path_key,
            journey_template_version=sel.template_version, explanation=explanation, weeks=weeks, notes=loose,
            support=intake.support_resources(c, definition, sel), pre_button_notice=notice,
            start_available=available, care_level=care_level, controls=intake.controls(c.copy),
            notifications_chosen=chosen, next_step=step,
            read_aloud=ReadAloud(label=c.copy["read_this_to_me"], text=text))


def _first_task_step(c: intake.Ctx, recommended: list[FirstTaskChoice]) -> NextStep:
    options = [Option(value=str(r.task.id), label=r.task.title) for r in recommended[:1]]
    options += [Option(value="small_task", label=c.copy["small_task_example"]),
                Option(value="not_today", label=c.copy["not_today_button"])]
    return NextStep(action="choose_first_task", prompt=c.copy["what_feels_doable"], options=options)


@router.post(
    "/journey/start",
    response_model=StartJourneyResponse,
    summary="Start journey",
    description=(
        "UC-CASE-12 and UC-CASE-18. In one transaction: the case becomes active, the journey template key and "
        "version are pinned, journey_started_at is set, the tasks are created, and on the account's first "
        "journey the 28-day free period starts (DEC-01). A second journey never restarts or extends it "
        "(DEC-04). No payment information is asked for or stored (DEC-02). Send back the pre-button notice "
        "version to show the notice was on screen first. A read-only account gets a subscription prompt."
    ),
    responses={**_DENIED, 409: {"description": "Already started, the death hasn't happened, or the notice "
                                              "changed since it was shown."}},
)
def start(case_id: UUID, req: StartJourneyIn, request: Request,
          identity: Identity = Depends(get_identity)) -> StartJourneyResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        ready = acct.require_ready(s, request, write=False)
        c = intake.ctx(s, request, ready.account)
        case = intake.load_case(s, case_id)
        if not s.is_case_member(case_id, ("owner",)):
            raise case_access_denied()
        if req.session is not None and safety.no_billing(req.session):
            # UC-CASE-12 care_level_3_or_4. Nothing about billing. Setup waits until level 1.
            raise ApiError(409, "not_now", c.copy["safety_next"])
        if ready.account["status"] == "read_only":
            raise ApiError(403, "account_read_only", c.copy["subscription_needed_new_journey"],
                           next_step=acct.subscribe_step(request.app.state.copy).model_dump())
        if case["status"] != "draft":
            raise ApiError(409, "journey_already_started", "This journey has already started.")
        if case["death_not_yet_occurred"]:
            raise ApiError(409, "death_not_yet_occurred", c.copy["death_not_yet_explain"])
        notice = _notice(c)
        if req.pre_button_notice_version != notice.version:
            raise ApiError(409, "notice_changed", notice.text, pre_button_notice=notice.model_dump())
        acct.require_ready(s, request, write=True)

        answers = intake.load_answers(s, case_id)
        _, sel = intake.selection(s, case, answers)
        trial_started_now = s.start_journey(case_id, sel.template_version, sel.path_key)
        case = intake.load_case(s, case_id)
        intake.sync_tasks(s, case, answers, sel)
        # No choice made at step 3 means in_app_only (UC-CASE-19).
        nt.ensure_default(s, case_id)

        account = acct.load_account(s)
        c = intake.ctx(s, request, account)
        end_date = acct.local_trial_end(account)
        end = acct.format_date(end_date) if end_date else None
        if account["status"] == "subscribed":
            key = "confirmation_subscribed"  # UC-CASE-18: no trial wording
        else:
            key = "confirmation_first_case" if trial_started_now else "confirmation_existing_trial"
        # The trial reminder always shows in Cairn. It also goes out only when the user chose a channel (OPEN-02).
        clause = (c.copy["reminder_channel_clause_channel_chosen"].format(channel=c.copy["reminder_channel_email"])
                  if nt.any_email_choice(s) else c.copy["reminder_channel_clause_in_cairn_only"])
        confirmation = c.copy[key].format(trial_end_date=end, reminder_channel_clause=clause)

        rows = journey.load_tasks(s, case_id)
        context = journey.task_context(c, case, answers)
        recommended = [FirstTaskChoice(task=journey.task_summary(r, context), why_now=r["why_now"])
                       for r in journey.time_sensitive(rows, recommended=context.recommended)]
        step = _first_task_step(c, recommended)
        text = " ".join([confirmation, c.copy["time_sensitive_intro"],
                         *(f"{r.task.title}. {r.why_now or ''}" for r in recommended), step.prompt])
        return StartJourneyResponse(
            case=intake.case_out(c, case, answers), confirmation=confirmation,
            legal_review_required=key in c.copy.legal_review, trial_started_now=trial_started_now,
            trial_end_date=end_date, recommended=recommended,
            small_task=c.copy["small_task_example"], next_step=step, controls=intake.controls(c.copy),
            read_aloud=ReadAloud(label=c.copy["read_this_to_me"], text=text))


@router.post(
    "/journey/not-yet",
    response_model=FirstTaskResponse,
    summary="Not yet",
    description="UC-CASE-12. The case stays a draft, the free period doesn't start, and everything is saved.",
    responses=_DENIED,
)
def not_yet(case_id: UUID, request: Request, identity: Identity = Depends(get_identity)) -> FirstTaskResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        ready = acct.require_ready(s, request, write=False)
        c = intake.ctx(s, request, ready.account)
        case = intake.load_case(s, case_id)
        intake.require_case_write(c, case)
        intake.touch(s, case_id)
        case, answers = intake.load_case(s, case_id), intake.load_answers(s, case_id)
        return FirstTaskResponse(case=intake.case_out(c, case, answers), acknowledgment=c.copy["not_yet_saved"],
                                 task=None, next_step=NextStep(action="paused", prompt=c.copy["pause"]))


@router.post(
    "/journey/first-task",
    response_model=FirstTaskResponse,
    summary="Choose what feels doable right now",
    description=(
        "UC-CASE-13. A task id opens that task (any task, without comment), small_task offers the small thing, "
        "and not_today leaves the case active with no task in progress. Cairn never starts a task the user "
        "didn't choose."
    ),
    responses=_DENIED,
)
def first_task(case_id: UUID, req: FirstTaskIn, request: Request,
               identity: Identity = Depends(get_identity)) -> FirstTaskResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        ready = acct.require_ready(s, request, write=True)
        c = intake.ctx(s, request, ready.account)
        case = intake.load_case(s, case_id)
        if case["status"] == "draft":
            raise ApiError(409, "journey_not_started", "The journey hasn't started yet.")
        answers = intake.load_answers(s, case_id)
        out = intake.case_out(c, case, answers)
        if req.choice == "not_today":
            # DEC-07: the free days keep counting while away. A subscribed account hears nothing about billing.
            key = "not_today" if c.account["status"] == "trial_active" else "not_today_subscribed"
            return FirstTaskResponse(case=out, acknowledgment=c.copy[key], task=None,
                                     next_step=NextStep(action="paused", prompt=c.copy[key]))
        if req.choice == "small_task":
            return FirstTaskResponse(case=out, acknowledgment=c.copy["small_task_open"], task=None,
                                     next_step=NextStep(action="small_task", prompt=c.copy["small_task_example"]))
        if s.update_task(case_id, req.choice, status="in_progress",
                         only_from=("not_started", "check_on_this", "not_today")):
            s.audit("task_status_changed", case_id, "case_task", req.choice)
        task = journey.load_task(s, case_id, req.choice)
        if task is None:
            raise case_access_denied()
        return FirstTaskResponse(case=out, acknowledgment=None,
                                 task=journey.task_summary(task, journey.task_context(c, case, answers)),
                                 next_step=NextStep(action="open_task", prompt=task["title"]))


@router.get(
    "/journey",
    response_model=JourneyResponse,
    summary="The journey, with one next action in front",
    description="UC-9 and UC-12. While paused, a calm check-in and no task list. A draft points to the preview.",
    responses=_DENIED,
)
def get_journey(case_id: UUID, request: Request, identity: Identity = Depends(get_identity)) -> JourneyResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        ready = acct.require_ready(s, request, write=False)
        if ready.account["status"] != "read_only":
            # Coming back to the journey is activity, for the inactivity reason (UC-CASE-19).
            intake.touch(s, case_id)
        return _journey_view(s, request, ready.account, case_id, notes=ready.notes)


@router.post(
    "/journey/pause",
    response_model=JourneyResponse,
    summary="Step back from tasks for a while",
    description=(
        "UC-12. Holds all progress exactly as it is. Stores only the time the pause ends. "
        "No reason or feeling is collected or stored. Pausing doesn't stop or extend the 28 free days, and "
        "the response says so (D-2026-09-25-P1). It also offers to change how Cairn keeps in touch (UC-CASE-20)."
    ),
    responses=_DENIED,
)
def pause_journey(case_id: UUID, request: Request, req: PauseRequest | None = None,
                  identity: Identity = Depends(get_identity)) -> JourneyResponse:
    days = (req or PauseRequest()).pause_days
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        ready = acct.require_ready(s, request, write=True)
        s.update_case(case_id, activity=False, started_only=True,
                      tasks_paused_until=s.now + timedelta(days=days))
        s.audit("journey_paused", case_id, "case", case_id)
        c = intake.ctx(s, request, ready.account)
        notes = [Note(kind="info", text=c.copy["pause_notifications_offer"])]
        if ready.account["status"] == "trial_active":
            notes.insert(0, Note(kind="account", text=c.copy["pause_trial_note"]))
        return _journey_view(s, request, ready.account, case_id, notes=notes)


@router.post("/journey/resume", response_model=JourneyResponse, summary="Pick the tasks back up",
             description="UC-12. Returns the journey with the same single next action as before the pause.",
             responses=_DENIED)
def resume_journey(case_id: UUID, request: Request, identity: Identity = Depends(get_identity)) -> JourneyResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        acct.require_ready(s, request, write=True)
        paused = s.end_rest(case_id)
        s.audit("journey_resumed", case_id, "case", case_id)
        notes = [messages.RESUMED]
        if paused:
            # Crisis plan on_return: said once, after a care rest stopped the free days.
            account = acct.load_account(s)
            c = intake.ctx(s, request, account)
            notes.append(Note(kind="account", text=c.copy["rest_return_paused_note"].format(
                trial_end_date=acct.format_date(acct.local_trial_end(account)))))
        return _journey_view(s, request, acct.load_account(s), case_id, notes=notes)


@router.post(
    "/take-a-break",
    response_model=IntakeTurnResponse,
    summary="Take a break",
    description=(
        "Global rule take_a_break: one select, no confirmation. In a draft it saves and shows the pause message "
        "(UC-CASE-10). On a journey, without rest_choice it offers the rest choices, and with one it sets the tasks "
        "aside until then. A rest chosen at level 2 or above is a care rest and pauses the free days (DEC-26-01). "
        "A rest in normal mode keeps them counting, and Cairn says so before pausing. Never needs a writable "
        "account."
    ),
    responses=_DENIED,
)
def take_a_break(case_id: UUID, request: Request, req: TakeABreakIn | None = None,
                 identity: Identity = Depends(get_identity)) -> IntakeTurnResponse:
    req = req or TakeABreakIn()
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        c = intake.ctx(s, request, acct.load_account(s))
        case = intake.load_case(s, case_id)
        if not s.is_case_member(case_id, ("owner", "co_executor")):
            raise case_access_denied()
        answers = intake.load_answers(s, case_id)
        session = req.session or IntakeSession()
        care = safety.care_level(session) >= 2
        if case["status"] == "draft":
            if s.account_can_edit_drafts():
                step = intake.next_field(answers, session)
                s.update_case(case_id, last_intake_step=step.value if step else None)
                case = intake.load_case(s, case_id)
            return intake.turn(c, case, answers, session, acknowledgment=c.copy["answer_saved"],
                               next_step=NextStep(action="paused", prompt=c.copy["pause"],
                                                  options=[Option(value="keep_going", label=c.copy["keep_going"])]))
        billing_ok = c.account["status"] == "trial_active" and not safety.no_billing(session)
        if req.rest_choice is None:
            body = []
            if billing_ok:
                body.append(c.copy["rest_care_clock_note"] if care else c.copy["rest_normal_clock_note"].format(
                    trial_end_date=acct.format_date(acct.local_trial_end(c.account))))
            return intake.turn(c, case, answers, session, body=body, next_step=NextStep(
                action="choose_rest", prompt=c.copy["rest_question"], options=intake.rest_options(c)))
        s.begin_rest(case_id, intake.rest_until(c, req.rest_choice), care=care)
        s.audit("journey_rest_started", case_id, "case", case_id)
        case = intake.load_case(s, case_id)
        return intake.turn(c, case, answers, session, acknowledgment=c.copy["rest_saved"], ask=False,
                           next_step=NextStep(action="resting", prompt=c.copy["welcome_back_rest"],
                                              options=[Option(value="resume", label=c.copy["keep_going"])]))


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
        ready = acct.require_ready(s, request, write=False)
        c = intake.ctx(s, request, ready.account)
        case = intake.load_case(s, case_id)
        answers = intake.load_answers(s, case_id)
        rows = journey.load_tasks(s, case_id)
        context = journey.task_context(c, case, answers) if case["status"] != "draft" else None
        today = date.today()

        by_category: dict[TaskCategory, CategoryStatus] = {}
        for r in rows:
            t = journey.task_summary(r, context)
            group = by_category.setdefault(t.category, CategoryStatus(
                category=t.category, label=messages.CATEGORY_LABELS[t.category.value],
                done=[], in_progress=[], up_next=[], set_aside=[]))
            if r["status"] in ("done", "handled_elsewhere"):
                group.done.append(t)
            elif r["status"] == "in_progress":
                group.in_progress.append(t)
            elif r["status"] in journey.OPEN_STATUSES:
                group.up_next.append(t)
            else:
                group.set_aside.append(t)

        counts = StatusCounts(
            total=len(rows),
            done=sum(r["status"] in ("done", "handled_elsewhere") for r in rows),
            in_progress=sum(r["status"] == "in_progress" for r in rows),
            not_started=sum(r["status"] in journey.OPEN_STATUSES and r["status"] != "in_progress" for r in rows),
            set_aside=sum(r["status"] in journey.SET_ASIDE_STATUSES for r in rows),
            overdue=sum(r["status"] in journey.OPEN_STATUSES and r["due_on"] is not None and r["due_on"] < today
                        for r in rows),
        )
        nxt = journey.pick_next_action(rows, recommended=context.recommended if context else frozenset())
        order = list(TaskCategory)
        s.audit("case_status_viewed", case_id, "case", case_id)
        return CaseStatusResponse(
            case_id=case_id,
            as_of=datetime.now(timezone.utc),
            deceased_name=intake.display_name(case, answers, c.copy),
            journey_paused=journey.is_paused(case),
            paused_until=case["tasks_paused_until"] if journey.is_paused(case) else None,
            counts=counts,
            next_action=journey.task_summary(nxt, context) if nxt else None,
            categories=sorted(by_category.values(), key=lambda g: order.index(g.category)),
            certificate_order=journey.certificate_order(s, case_id),
            institution_notices=journey.institution_notices(s, case_id),
        )
