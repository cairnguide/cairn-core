"""UC-CASE-01 to UC-CASE-11, UC-CASE-14 to UC-CASE-17, and UC-CASE-22 to UC-CASE-24: the intake conversation.

Each call is one turn: acknowledge, ask at most one thing, give one next action.
The client keeps the session (IntakeSession) and sends it back. The server
never stores it, so nothing about the user's distress is persisted. Crisis
levels and replies follow the Support and Crisis Plan (cairn-support-crisis-plan.json).
"""
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from .. import account as acct
from .. import intake, safety
from ..auth import Identity, get_identity
from ..errors import ApiError
from ..extraction import extract
from ..schemas import (
    AnswerIn,
    AnswerState,
    AttorneyReferralIn,
    CheckInIn,
    ConfirmationIn,
    DeathNotYetIn,
    FieldKey,
    IntakeMessageIn,
    IntakePreferencesIn,
    IntakeSession,
    IntakeTurnResponse,
    Level2ChoiceIn,
    NextStep,
    Note,
    Option,
    ReadAloud,
    ReviewResponse,
    SafetyMode,
    SessionIn,
    SupportResource,
    TranscriptIn,
)

router = APIRouter(prefix="/v1/cases/{case_id}", tags=["Case creation"])

_DENIED = {403: {"description": "Not the owner of this case, the case does not exist, or an active case on a "
                                "read-only account."}}
AFSP_URL = "https://afsp.org/ive-lost-someone/"


def _open(s, request, case_id: UUID):
    ready = acct.require_ready(s, request, write=False)
    c = intake.ctx(s, request, ready.account)
    case = intake.load_case(s, case_id)
    intake.require_case_write(c, case)
    return c, case


def _open_for_safety(s, request, case_id: UUID):
    """Safety replies never depend on a writable account, acknowledgments, or onboarding: a crisis reply
    must always work. Membership only."""
    s.require_user()
    c = intake.ctx(s, request, acct.load_account(s))
    case = intake.load_case(s, case_id)
    if not s.is_case_member(case_id, ("owner", "co_executor")):
        raise intake.case_access_denied()
    return c, case


def _reload(c: intake.Ctx, case_id: UUID) -> tuple[dict, dict]:
    return intake.load_case(c.s, case_id), intake.load_answers(c.s, case_id)


def _save_pending_step(c: intake.Ctx, case: dict, answers: dict, session: IntakeSession) -> None:
    """last_intake_step is the question the user is on, so a return can say where they left off."""
    step = intake.next_field(answers, session)
    intake.update_case(c.s, case["id"], last_intake_step=step.value if step else None)


def _field_notes(c: intake.Ctx, case: dict, field: FieldKey, state: AnswerState, value, before, after,
                 edited: bool) -> tuple[str, list[str], list[Note]]:
    """Acknowledgment, statements, and notes for one saved answer."""
    copy, body, notes = c.copy, [], []
    ack = {AnswerState.skipped: copy["answer_skipped"], AnswerState.unsure: copy["answer_unsure"]}.get(
        state, copy["answer_saved"])
    if state == AnswerState.answered:
        if field == FieldKey.circumstance:
            ack = copy["circumstance_confirmed"]  # confirm without repeating anything painful
        elif field == FieldKey.display_name:
            ack = copy["display_name_ack"].format(display_name=value)
        elif field == FieldKey.user_role and value == "power_of_attorney":
            notes += intake.poa_note_once(c, case)
        elif field == FieldKey.place_of_death and value["outside_us"]:
            body.append(copy["outside_us_ack"])  # UC-CASE-04, out of scope for MVP: explain and keep the case
        elif field == FieldKey.place_of_death and value["jurisdiction"] is None:
            body.append(copy["place_unknown"])
    if field == FieldKey.estate_plan_status and (state != AnswerState.answered or value in ("no", "unknown")):
        body.append(copy["no_will_reassure"])  # UC-CASE-07. Reassure, don't explain inheritance rules
    if field == FieldKey.veteran_status and value == "yes" and not edited:
        body.append(copy["veterans_crisis_line_added"])
    definition = intake.journey_definition(c.s, case["journey_template_version"])
    if edited:
        body.append(intake.change_line(c, definition, before, after))
    else:
        field_notes = intake.field_step_notes(c, definition, field, after)
        shown = {n.text for n in field_notes}
        notes += field_notes + [n for n in intake.added_step_notes(c, before, after) if n.text not in shown]
    return ack, body, notes


def _apply_answer(c: intake.Ctx, case: dict, answers: dict, field: FieldKey, state: AnswerState, value,
                  own_words, session: IntakeSession):
    """Save one answer and recompute the journey. Returns the ack parts and the updated case and answers."""
    edited = field.value in answers
    _, before = intake.selection(c.s, case, answers)
    intake.save_answer(c.s, case["id"], field, state, value, own_words)
    if field == FieldKey.date_of_death and state == AnswerState.answered and case["death_not_yet_occurred"]:
        intake.update_case(c.s, case["id"], death_not_yet_occurred=False)
    case, answers = _reload(c, case["id"])
    _, after = intake.selection(c.s, case, answers)
    if case["status"] != "draft":
        intake.sync_tasks(c.s, case, answers, after)
    return _field_notes(c, case, field, state, value, before, after, edited), case, answers


@router.put(
    "/intake/answers/{field}",
    response_model=IntakeTurnResponse,
    summary="Answer, skip, or say I'm not sure",
    description=(
        "UC-CASE-02 to UC-CASE-09. Saves one data_fields answer with its answer_state (answered, skipped, "
        "unsure). A question with any answer is not asked again. The third Skip for now in a row starts level 2, "
        "and I'm not sure doesn't count (OPEN-07). Changing an answer recomputes the journey and says what "
        "changed in one line. On an active case it updates add-on tasks without touching progress elsewhere."
    ),
    responses=_DENIED,
)
def put_answer(case_id: UUID, field: FieldKey, req: AnswerIn, request: Request,
               identity: Identity = Depends(get_identity)) -> IntakeTurnResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        c, case = _open(s, request, case_id)
        answers = intake.load_answers(s, case_id)
        if field == FieldKey.circumstance and req.own_words:
            raise ApiError(422, "validation_failed", "Some answers need another look.",
                           errors=[{"field": "own_words", "message": "not kept for this question",
                                    "type": "value_error"}])
        value = intake.parse_value(field, req.value, c.today) if req.state == AnswerState.answered else None
        session = req.session or IntakeSession()
        if field == FieldKey.place_of_death and req.away_from_home:
            session = session.model_copy(update={"ask_residence": True})
        settings = request.app.state.settings
        session = safety.after_answer(session, req.state.value, settings.overwhelm_skip_threshold,
                                      settings.unsure_counts_as_skip)
        (ack, body, notes), case, answers = _apply_answer(c, case, answers, field, req.state, value,
                                                          req.own_words, session)
        _save_pending_step(c, case, answers, session)
        case = intake.load_case(s, case_id)

        next_step = None
        if field == FieldKey.user_role and value == "professional_fiduciary" and not case["skip_explainers"]:
            body.append(c.copy["fiduciary_offer"])
            next_step = NextStep(action="choose_pace", prompt=c.copy["fiduciary_offer_question"],
                                 options=[Option(value="skip_explainers", label=c.copy["fiduciary_offer_yes"]),
                                          Option(value="keep_explainers", label=c.copy["fiduciary_offer_no"])])
        elif field == FieldKey.display_name and req.state == AnswerState.skipped and c.voice == "plain_practical":
            body.append(c.copy["name_fallback_offer"])
            next_step = NextStep(action="choose_name_fallback", prompt=c.copy["name_fallback_question"],
                                 options=[Option(value="your_loved_one", label=c.copy["your_loved_one"]),
                                          Option(value="the_person_who_died", label=c.copy["the_person_who_died"])])
        if safety.blocks_questions(session) and next_step is not None:
            next_step = None  # a safety level wins over any offer
        # UC-CASE-23: answering how it happened is a heavy moment, so a rest is offered once.
        heavy = field == FieldKey.circumstance and req.state == AnswerState.answered
        return intake.turn(c, case, answers, session, acknowledgment=ack, body=body, notes=notes,
                           next_step=next_step, heavy=heavy)


def _message_turn(c: intake.Ctx, case: dict, text: str, session: IntakeSession, redactions: tuple[str, ...],
                  masked: str | None) -> IntakeTurnResponse:
    """One message, typed or a confirmed transcript. Crisis detection runs first, then everything else."""
    s = c.s
    case, answers = _reload(c, case["id"])
    privacy = dict(redactions=redactions, masked_text=masked)
    # UC-CASE-15. Explain kindly, and never echo the value or any part of it.
    lead = [c.copy["redaction_explanation"]] if redactions else []

    signals = safety.classify(text)
    extracted = extract(text, c.today)
    before_level = safety.care_level(session)
    if "continue" in extracted.intents and signals.mode is None and session.safety_mode != SafetyMode.normal:
        session = session.model_copy(update={"safety_mode": SafetyMode.normal, "consecutive_skips": 0,
                                             "overwhelm_signals": 0})
        before_level = 1
    session = safety.after_message(session, signals)
    if signals.volunteered_cause:
        session = session.model_copy(update={"sensitivity": "raised"})
    level = safety.care_level(session)

    # UC-CASE-14. Levels 3 and 4 leave task mode entirely. The tasks pause on their own (a care rest).
    if level >= 3:
        if before_level < 3:
            intake.begin_care_rest(c, case)
        if level == 4 and before_level < 4:
            s.count_level_4_referral()  # [LEGAL] anonymous monthly count for SB 243 reporting
        return intake.safety_turn(c, case, answers, session, ask_directly=signals.ask_directly,
                                  first=before_level < 3, **privacy)

    # UC-CASE-24. Distress came first, above. Then: say kindly that Cairn is for adults, offer 988, and stop.
    # Nothing about age is stored, and no proof is asked for.
    if signals.minor:
        session = session.model_copy(update={"intake_stopped": True})
        support = [SupportResource(id="lifeline_988", text=c.copy["support_988"], url="https://988lifeline.org")]
        return intake.turn(c, case, answers, session, acknowledgment=c.copy["under_18_message"], support=support,
                           next_step=NextStep(action="intake_stopped", prompt=c.copy["support_988"]), **privacy)

    body, notes = list(lead), []
    support: list[SupportResource] = []
    # UC-CASE-05. A volunteered cause gets a simple acknowledgment. No follow-up, and it is not stored.
    ack = c.copy["volunteered_cause_ack" if signals.volunteered_cause else "readback_ack"]
    if signals.suicide_loss and not case["loss_survivor_resources"]:
        # Set only because the user said it. Never shown back as a label. Loss survivor support is offered once
        # here and stays in the support resources (crisis plan).
        intake.update_case(s, case["id"], loss_survivor_resources=True)
        case = intake.load_case(s, case["id"])
        if "loss_survivor_resources" not in session.offered:
            support.append(SupportResource(id="loss_survivor_resources", text=c.copy["loss_survivor_offer"],
                                           url=AFSP_URL))
            session = session.model_copy(update={"offered": [*session.offered, "loss_survivor_resources"]})
    if extracted.pets_or_dependents and not case["secure_now_first"]:
        intake.update_case(s, case["id"], secure_now_first=True)  # UC-CASE-13
        case = intake.load_case(s, case["id"])

    if session.safety_mode == SafetyMode.overwhelm:
        return intake.turn(c, case, answers, session, body=body, support=support, **privacy)

    if "pause" in extracted.intents:
        _save_pending_step(c, case, answers, session)
        case = intake.load_case(s, case["id"])
        return _pause_turn(c, case, answers, session, body, support, privacy, ack)

    if "not_yet_died" in extracted.intents and not case["death_not_yet_occurred"] and case["status"] == "draft":
        intake.update_case(s, case["id"], death_not_yet_occurred=True)
        case = intake.load_case(s, case["id"])
        return _death_not_yet_turn(c, case, answers, session, body, privacy)

    for trigger in sorted(extracted.attorney_triggers):
        if intake.add_attorney_trigger(s, case, trigger):
            case = intake.load_case(s, case["id"])
            body.append(c.copy["family_disagreement_ack" if trigger == "family_disagreement"
                               else "attorney_task_added"])
            notes.append(Note(kind="legal", text=c.copy["attorney_referral_line"],
                              attorney_line=c.copy["attorney_referral_line"]))
            if case["status"] != "draft":
                _, sel = intake.selection(s, case, answers)
                intake.sync_tasks(s, case, answers, sel)

    if extracted.away_from_home:
        session = session.model_copy(update={"ask_residence": True})

    if "done" in extracted.intents and not extracted.proposals:
        return intake.turn(c, case, answers, session, acknowledgment=ack, body=body,
                           notes=notes, support=support, ask=False, **privacy)

    if extracted.proposals and not session.intake_stopped:
        proposals = [intake.proposal(c, f, v, w) for f, (v, w) in extracted.proposals.items()]
        step = NextStep(action="confirm_readback", prompt=c.copy["did_i_get_that_right"],
                        options=[Option(value="yes", label=c.copy["yes_thats_right"]),
                                 Option(value="fix", label=c.copy["fix_it"])])
        body.append(c.copy["readback_intro"])
        return intake.turn(c, case, answers, session, acknowledgment=ack, body=body, notes=notes,
                           support=support, proposals=proposals, next_step=step, **privacy)
    if not signals.volunteered_cause and not notes:
        ack = c.copy["readback_nothing"]
    return intake.turn(c, case, answers, session, acknowledgment=ack, body=body, notes=notes, support=support,
                       **privacy)


@router.post(
    "/intake/messages",
    response_model=IntakeTurnResponse,
    summary="Say something in your own words",
    description=(
        "UC-CASE-01 own words, UC-CASE-05, UC-CASE-07, UC-CASE-10, UC-CASE-13 to UC-CASE-17, UC-CASE-22, and "
        "UC-CASE-24. Typed text, or a speech transcript the user confirmed. Redacted as it is received, spoken "
        "digits included, and never stored. Crisis detection runs first. Cairn reads back only the data_fields "
        "it found and asks Did I get that right? Nothing is saved until the user confirms."
    ),
    responses=_DENIED,
)
def post_message(case_id: UUID, req: IntakeMessageIn, request: Request,
                 identity: Identity = Depends(get_identity)) -> IntakeTurnResponse:
    with request.app.state.db.session(identity.subject) as s:
        c, case = _open_for_safety(s, request, case_id)
        intake.touch(s, case_id)
        redactions = getattr(req.text, "redactions", ())
        return _message_turn(c, case, str(req.text), req.session or IntakeSession(), redactions,
                             str(req.text) if redactions else None)


@router.post(
    "/intake/transcripts",
    response_model=IntakeTurnResponse,
    summary="Show back what Cairn heard",
    description=(
        "UC-CASE-22. Takes the transcript from the speech to text step. Audio never reaches this API and is never "
        "stored anywhere. The transcript is redacted first (spoken digits too), and crisis detection runs on it "
        "exactly as on typed text. Otherwise the transcript is shown back with Did I hear that right? and Edit. "
        "Send the confirmed text to intake/messages with input_mode speech. An unclear transcript asks once to "
        "try again or type it, and never guesses an answer."
    ),
    responses=_DENIED,
)
def post_transcript(case_id: UUID, req: TranscriptIn, request: Request,
                    identity: Identity = Depends(get_identity)) -> IntakeTurnResponse:
    with request.app.state.db.session(identity.subject) as s:
        c, case = _open_for_safety(s, request, case_id)
        intake.touch(s, case_id)
        case, answers = _reload(c, case_id)
        session = req.session or IntakeSession()
        if req.unclear or req.transcript is None:
            step = NextStep(action="speak_again", prompt=c.copy["speech_unclear"],
                            options=[Option(value="speak", label=c.copy["speak"]),
                                     Option(value="type", label=c.copy["free_text_label"])])
            return intake.turn(c, case, answers, session, next_step=step)
        text = str(req.transcript)
        redactions = getattr(req.transcript, "redactions", ())
        signals = safety.classify(text)
        if signals.mode is not None or (signals.overwhelm_signal and session.overwhelm_signals >= 1):
            # Distress in speech is handled now, not after the user confirms what was heard.
            return _message_turn(c, case, text, session, redactions, text if redactions else None)
        step = NextStep(action="confirm_transcript", prompt=c.copy["did_i_hear_that_right"],
                        options=[Option(value="yes", label=c.copy["yes_thats_right"]),
                                 Option(value="edit", label=c.copy["edit"])])
        body = [c.copy["redaction_explanation"]] if redactions else []
        return intake.turn(c, case, answers, session, body=body, next_step=step, redactions=redactions,
                           masked_text=text)


@router.post(
    "/intake/confirmations",
    response_model=IntakeTurnResponse,
    summary="Confirm what Cairn read back",
    description=(
        "UC-CASE-01 own words. Saves the confirmed values. Only data_fields keys are accepted, each checked "
        "like a button answer, so nothing else from free text or a transcript can be stored. Then asks only "
        "about fields still missing."
    ),
    responses=_DENIED,
)
def confirm(case_id: UUID, req: ConfirmationIn, request: Request,
            identity: Identity = Depends(get_identity)) -> IntakeTurnResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        c, case = _open(s, request, case_id)
        answers = intake.load_answers(s, case_id)
        session = req.session or IntakeSession()
        if len({a.field for a in req.answers}) != len(req.answers):
            raise ApiError(422, "validation_failed", "Some answers need another look.",
                           errors=[{"field": "answers", "message": "each field once", "type": "value_error"}])
        parsed = [(a.field, intake.parse_value(a.field, a.value, c.today), a.own_words) for a in req.answers]
        _, before = intake.selection(s, case, answers)
        for field, value, words in parsed:
            intake.save_answer(s, case_id, field, AnswerState.answered, value, words)
            if field == FieldKey.date_of_death and case["death_not_yet_occurred"]:
                intake.update_case(s, case_id, death_not_yet_occurred=False)
        case, answers = _reload(c, case_id)
        _, after = intake.selection(s, case, answers)
        if case["status"] != "draft":
            intake.sync_tasks(s, case, answers, after)
        definition = intake.journey_definition(s, case["journey_template_version"])
        notes = []
        for field, _, _ in parsed:
            notes += [n for n in intake.field_step_notes(c, definition, field, after) if n not in notes]
        notes += [n for n in intake.added_step_notes(c, before, after) if n not in notes]
        body = []
        roles = {f: v for f, v, _ in parsed}
        if roles.get(FieldKey.user_role) == "power_of_attorney":
            notes = intake.poa_note_once(c, case) + notes
        if roles.get(FieldKey.estate_plan_status) in ("no", "unknown"):
            body.append(c.copy["no_will_reassure"])
        if (place := roles.get(FieldKey.place_of_death)) and place["outside_us"]:
            body.append(c.copy["outside_us_ack"])
        _save_pending_step(c, case, answers, session)
        case = intake.load_case(s, case_id)
        return intake.turn(c, case, answers, session, acknowledgment=c.copy["readback_saved"], body=body,
                           notes=notes, heavy=FieldKey.circumstance in roles)


@router.post(
    "/intake/continue",
    response_model=IntakeTurnResponse,
    summary="Keep going",
    description=(
        "The user asked to continue: after choosing one question at a time, after resuming, or after stepping "
        "away during distress (UC-CASE-14). Returns the next question still missing. On an active journey this "
        "is coming back to tasks, so a care rest ends and the free days count again. After level 3 or 4, asks "
        "once whether Cairn may check in tomorrow."
    ),
    responses=_DENIED,
)
def continue_intake(case_id: UUID, request: Request, req: SessionIn | None = None,
                    identity: Identity = Depends(get_identity)) -> IntakeTurnResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        c, case = _open(s, request, case_id)
        intake.touch(s, case_id)
        case, answers = _reload(c, case_id)
        session = (req.session if req and req.session else IntakeSession())
        was_level = safety.care_level(session)
        session = session.model_copy(update={"safety_mode": SafetyMode.normal, "consecutive_skips": 0,
                                             "overwhelm_signals": 0})
        body = []
        if case["status"] != "draft" and c.account["break_started_at"] is not None:
            # Coming back to tasks ends the break for the person (UC-BRK-10).
            paused = s.end_rest(case_id)
            case = intake.load_case(s, case_id)
            account = acct.load_account(s)
            if paused:
                body.append(c.brk["trial_resumed_after_care_rest"].format(
                    trial_end_date=acct.format_date(acct.local_trial_end(account))))
        response = intake.turn(c, case, answers, session, body=body,
                               acknowledgment=c.copy["continue_ack"] if was_level > 1 else None)
        if was_level >= 3 and not response.session.check_in_asked:
            response.announcements.append(intake.check_in_offer(c))
            response.session = response.session.model_copy(update={"check_in_asked": True})
        return response


@router.post(
    "/intake/level-2-choice",
    response_model=IntakeTurnResponse,
    summary="Rest, one small thing, or just talk",
    description=(
        "UC-CASE-14 level 2. rest is a care rest: in a draft it saves and pauses, on an active journey it opens "
        "the rest choices and the free days pause while resting (DEC-26-01). small_thing offers one small task. "
        "talk stays with the user and asks nothing."
    ),
    responses=_DENIED,
)
def level_2_choice(case_id: UUID, req: Level2ChoiceIn, request: Request,
                   identity: Identity = Depends(get_identity)) -> IntakeTurnResponse:
    with request.app.state.db.session(identity.subject) as s:
        c, case = _open_for_safety(s, request, case_id)
        case, answers = _reload(c, case_id)
        session = (req.session or IntakeSession()).model_copy(update={"safety_mode": SafetyMode.overwhelm})
        if req.choice == "rest":
            if case["status"] == "draft":
                _save_pending_step(c, case, answers, session)
                case = intake.load_case(s, case_id)
                return _pause_turn(c, case, answers, session, [], [], {})
            # UC-BRK-07 at level 2: the care note once, only while the free days run. Nothing about a subscription.
            step = NextStep(action="choose_rest", prompt=c.brk["rest_choices_question"],
                            options=intake.rest_options(c))
            body = [c.brk["rest_care_clock_note"]] if acct.on_free_days(c.account) and not safety.no_billing(
                session) else []
            return intake.turn(c, case, answers, session, body=body, next_step=step)
        if req.choice == "small_thing":
            step = NextStep(action="small_task", prompt=c.copy["small_task_example"])
            return intake.turn(c, case, answers, session, acknowledgment=c.copy["small_task_open"], next_step=step)
        step = NextStep(action="just_talk", prompt=c.copy["just_talk"],
                        options=[Option(value="continue", label=c.copy["ready_to_continue"])])
        return intake.turn(c, case, answers, session, next_step=step)


@router.post(
    "/intake/check-in",
    response_model=IntakeTurnResponse,
    summary="May Cairn check in tomorrow?",
    description=(
        "DEC-26-04. Only on a yes, one private check-in tomorrow, stored on the account with this case's id: through "
        "the account's notification channels (account D-13, which resolves OPEN-08), and in Cairn the next time the "
        "user opens it. It ignores the frequency setting and respects quiet hours. It never names the person who "
        "died, the circumstance, or any crisis, and it is cancelled if this case or the account is deleted. A no "
        "stores nothing."
    ),
    responses=_DENIED,
)
def check_in(case_id: UUID, req: CheckInIn, request: Request,
             identity: Identity = Depends(get_identity)) -> IntakeTurnResponse:
    with request.app.state.db.session(identity.subject) as s:
        c, case = _open_for_safety(s, request, case_id)
        session = (req.session or IntakeSession()).model_copy(update={"check_in_asked": True})
        if req.answer == "yes":
            s.set_check_in(s.now + timedelta(days=1), case_id)
            s.audit("check_in_scheduled", case_id, "case", case_id)
        case, answers = _reload(c, case_id)
        ack = c.copy["check_in_yes_saved" if req.answer == "yes" else "check_in_no_saved"]
        return intake.turn(c, case, answers, session, acknowledgment=ack,
                           next_step=intake.safety_step(c, session))


def _pause_turn(c, case, answers, session, body, support, privacy, ack=None) -> IntakeTurnResponse:
    """UC-CASE-10. Plainly confirms everything is saved and that an untouched draft is deleted after 28 days."""
    step = NextStep(action="paused", prompt=c.brk["draft_pause"],
                    options=[Option(value="keep_going", label=c.copy["keep_going"])])
    return intake.turn(c, case, answers, session, acknowledgment=ack or c.copy["answer_saved"], body=body,
                       support=support, next_step=step, **privacy)


@router.post(
    "/intake/pause",
    response_model=IntakeTurnResponse,
    summary="Stop for now",
    description=(
        "UC-CASE-10. Everything is already saved with each answer. This records where the user left off and "
        "confirms it plainly, including that an untouched draft is deleted after 28 days. Pausing never starts "
        "the free period, and no notification about the draft is ever sent."
    ),
    responses=_DENIED,
)
def pause(case_id: UUID, request: Request, req: SessionIn | None = None,
          identity: Identity = Depends(get_identity)) -> IntakeTurnResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        c, case = _open(s, request, case_id)
        answers = intake.load_answers(s, case_id)
        session = req.session if req and req.session else IntakeSession()
        _save_pending_step(c, case, answers, session)
        case = intake.load_case(s, case_id)
        return _pause_turn(c, case, answers, session, [], [], {})


@router.put(
    "/intake/preferences",
    response_model=IntakeTurnResponse,
    summary="Choose a shorter pace, or what to call them when there's no name",
    description="UC-CASE-02 (professional fiduciary pace) and UC-CASE-03 (name fallback).",
    responses=_DENIED,
)
def preferences(case_id: UUID, req: IntakePreferencesIn, request: Request,
                identity: Identity = Depends(get_identity)) -> IntakeTurnResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        c, case = _open(s, request, case_id)
        changes = {k: v for k, v in (("skip_explainers", req.skip_explainers), ("name_fallback", req.name_fallback))
                   if v is not None}
        intake.update_case(s, case_id, **changes)
        case, answers = _reload(c, case_id)
        return intake.turn(c, case, answers, req.session or IntakeSession(),
                           acknowledgment=c.copy["preferences_saved"])


def _death_not_yet_turn(c, case, answers, session, body, privacy) -> IntakeTurnResponse:
    step = NextStep(action="death_not_yet", prompt=c.copy["death_not_yet_explain"],
                    options=[Option(value="save_draft", label=c.copy["save_draft_now"]),
                             Option(value="come_back_later", label=c.copy["come_back_later"])])
    return intake.turn(c, case, answers, session, acknowledgment=c.copy["death_not_yet_ack"], body=body,
                       next_step=step, **privacy)


@router.post(
    "/intake/death-not-yet",
    response_model=IntakeTurnResponse,
    summary="The death has not happened yet",
    description=(
        "UC-CASE-17. not_yet records it, explains that journeys start after a death, and offers to save a draft "
        "or come back later. Start journey is not offered and the free period does not start. save_draft "
        "confirms the draft and its 28-day deletion. has_happened clears it. The pre-need path is post-alpha."
    ),
    responses=_DENIED,
)
def death_not_yet(case_id: UUID, req: DeathNotYetIn, request: Request,
                  identity: Identity = Depends(get_identity)) -> IntakeTurnResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        c, case = _open(s, request, case_id)
        session = req.session or IntakeSession()
        if case["status"] != "draft":
            raise ApiError(409, "journey_already_started", "This journey has already started.")
        if req.choice == "has_happened":
            intake.update_case(s, case_id, death_not_yet_occurred=False)
            case, answers = _reload(c, case_id)
            return intake.turn(c, case, answers, session, acknowledgment=c.copy["answer_saved"])
        intake.update_case(s, case_id, death_not_yet_occurred=True)
        case, answers = _reload(c, case_id)
        if req.choice == "not_yet":
            return _death_not_yet_turn(c, case, answers, session, [], {})
        if req.choice == "save_draft":
            step = NextStep(action="draft_saved", prompt=c.copy["draft_notice"],
                            options=[Option(value="has_happened", label=c.copy["death_has_happened"])])
            return intake.turn(c, case, answers, session, next_step=step)
        _save_pending_step(c, case, answers, session)
        case = intake.load_case(s, case_id)
        return _pause_turn(c, case, answers, session, [], [], {})


@router.post(
    "/intake/attorney-referrals",
    response_model=IntakeTurnResponse,
    summary="Something Cairn should not guide alone",
    description=(
        "UC-CASE-16 and UC-CASE-07. Records the reason and adds Talk to an estate attorney as a task, with the "
        "attorney referral line. The journey is never blocked. Cairn keeps showing every step it can help with. "
        "A death outside the US is handled from the answers."
    ),
    responses=_DENIED,
)
def attorney_referral(case_id: UUID, req: AttorneyReferralIn, request: Request,
                      identity: Identity = Depends(get_identity)) -> IntakeTurnResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        c, case = _open(s, request, case_id)
        intake.add_attorney_trigger(s, case, req.trigger.value)
        case, answers = _reload(c, case_id)
        if case["status"] != "draft":
            _, sel = intake.selection(s, case, answers)
            intake.sync_tasks(s, case, answers, sel)
        body = [c.copy["family_disagreement_ack" if req.trigger.value == "family_disagreement"
                       else "attorney_task_added"]]
        note = Note(kind="legal", text=c.copy["attorney_referral_line"], attorney_line=c.copy["attorney_referral_line"])
        return intake.turn(c, case, answers, req.session or IntakeSession(), body=body, notes=[note])


@router.get(
    "/review",
    response_model=ReviewResponse,
    summary="Review what you shared",
    description=(
        "UC-CASE-11. Two groups: what you told me, and what we can figure out later. Each line has Edit. The "
        "user's own words are shown where free text was given, except for how it happened, which shows the "
        "choice's label."
    ),
    responses=_DENIED,
)
def review(case_id: UUID, request: Request, identity: Identity = Depends(get_identity)) -> ReviewResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        ready = acct.require_ready(s, request, write=False)
        c = intake.ctx(s, request, ready.account)
        case = intake.load_case(s, case_id)
        if case["status"] == "draft":
            intake.touch(s, case_id)
            case = intake.load_case(s, case_id)
        answers = intake.load_answers(s, case_id)
        residence_asked = "residence_jurisdiction" in answers
        told, later = intake.review_lines(c, answers, IntakeSession(ask_residence=residence_asked))
        step = NextStep(action="confirm_review", prompt=c.copy["does_this_look_right"],
                        options=[Option(value="yes", label=c.copy["review_yes"]),
                                 Option(value="change", label=c.copy["review_change"])])
        text = " ".join([c.copy["review_intro"], *(f"{x.label}: {x.answer}." for x in told + later), step.prompt])
        return ReviewResponse(case=intake.case_out(c, case, answers), acknowledgment=c.copy["review_intro"],
                              told_me=told, told_me_heading=c.copy["what_you_told_me"], later=later,
                              later_heading=c.copy["figure_out_later"], next_step=step,
                              controls=intake.controls(c.copy),
                              read_aloud=ReadAloud(label=c.copy["read_this_to_me"], text=text))
