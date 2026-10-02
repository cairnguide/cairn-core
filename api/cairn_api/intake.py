"""Case creation (UC-CASE-01 to UC-CASE-18): drafts, intake answers, and the journey that fits.

Spec: database/docs/cairn-case-creation-use-cases.json. The schema is
migration 0010. Journey rules are template data (content/journeys), evaluated
by journey_selection. Copy comes from content/case-creation-copy.json.

Pacing rules followed by every turn built here:
- Acknowledge before asking. At most one question per turn. One next action.
- No timers and no required order. Every question offers Skip for now and I'm not sure.
- A question that has any answer row (answered, skipped, or unsure) is not asked again.
- Missing answers never block the journey. They become open items.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import Request
from pydantic import TypeAdapter, ValidationError

from . import account as acct
from . import journey_selection as js
from . import safety
from .copy_store import Copy
from .db import Session
from .errors import ApiError, case_access_denied
from .extraction import STATE_NAMES
from .schemas import (
    FIELD_VALUE_TYPES,
    AnswerOption,
    AnswerState,
    CaseOut,
    CompletedItem,
    FieldKey,
    IntakeSession,
    IntakeTurnResponse,
    NextStep,
    Note,
    Option,
    ProposedAnswer,
    Question,
    ReadAloud,
    ReviewLine,
    SafetyMode,
    SupportResource,
)

FIELD_ORDER = list(FieldKey)
STATE_LABELS = {code: name.title() for name, code in reversed(list(STATE_NAMES.items()))}
STATE_LABELS["DC"] = "Washington, DC"

# ------------------------------------------------------------------ context

@dataclass
class Ctx:
    s: Session
    request: Request
    copy: Copy
    account: dict

    @property
    def voice(self) -> str:
        return self.account["voice"]

    @property
    def today(self) -> date:
        return datetime.now(ZoneInfo(self.account.get("time_zone") or "UTC")).date()


def ctx(s: Session, request: Request, account: dict) -> Ctx:
    return Ctx(s=s, request=request, copy=request.app.state.case_copy, account=account)


# ------------------------------------------------------------------ reads

def load_case(s: Session, case_id: UUID) -> dict:
    row = s.load_case(case_id)
    if row is None:
        raise case_access_denied()
    return row


def load_answers(s: Session, case_id: UUID) -> dict[str, dict]:
    return s.load_answers(case_id)


def load_deceased(s: Session, case_id: UUID) -> dict | None:
    return s.load_deceased(case_id)


def display_name(case: dict, answers: dict, copy: Copy) -> str:
    """What to call the person who died. Conversation only. Never a legal name (UC-CASE-03)."""
    return js.answered_value(answers, "display_name") or copy[case["name_fallback"]]


def effective_status(case: dict, account: dict) -> str:
    return "read_only" if case["status"] == "active" and account["status"] == "read_only" else case["status"]


def case_out(c: Ctx, case: dict, answers: dict) -> CaseOut:
    return CaseOut(
        **{k: case[k] for k in ("id", "journey_template_key", "journey_template_version", "journey_started_at",
                                "journey_started_on", "last_intake_step", "last_activity_at", "draft_expires_at",
                                "death_not_yet_occurred", "skip_explainers", "tasks_paused_until",
                                "deletion_scheduled_for", "created_at")},
        status=effective_status(case, c.account),
        display_name=display_name(case, answers, c.copy),
    )


# ------------------------------------------------------------------ write gates

def require_case_write(c: Ctx, case: dict) -> None:
    """A draft stays editable on a read-only account (UC-CASE-18). An active case needs a writable account."""
    if case["status"] != "draft":
        acct.require_ready(c.s, c.request, write=True)
    if not c.s.is_case_member(case["id"], ("owner", "co_executor")):
        raise case_access_denied()


# ------------------------------------------------------------------ writes

def create_draft(s: Session) -> UUID:
    """UC-CASE-01. Case and owner membership in one transaction. Never starts the trial (DEC-01)."""
    case_id = s.create_draft()
    s.audit("case_created", case_id, "case", case_id)
    return case_id


def touch(s: Session, case_id: UUID) -> None:
    """Any open of a draft resets its 28 days (DEC-07)."""
    s.touch(case_id)


def update_case(s: Session, case_id: UUID, **cols) -> None:
    """Fields the app may set, and the activity clock. A case the caller can't change is refused."""
    s.update_case(case_id, **cols)


def parse_value(field: FieldKey, value: object, today: date) -> object:
    """Validate a value against its field's type and return what is stored."""
    adapter = TypeAdapter(FIELD_VALUE_TYPES[field])
    try:
        parsed = adapter.validate_python(value)
    except ValidationError as exc:
        errors = [{"field": ".".join(["value", *(str(p) for p in e["loc"])]), "message": e["msg"],
                   "type": e["type"]} for e in exc.errors()]
        raise ApiError(422, "validation_failed", "Some answers need another look.", errors=errors) from None
    stored = adapter.dump_python(parsed, mode="json")
    if field == FieldKey.date_of_death and stored["precision"] == "today":
        stored["date"] = today.isoformat()
    return stored


def save_answer(s: Session, case_id: UUID, field: FieldKey, state: AnswerState, value: object | None,
                own_words: str | None) -> None:
    if field == FieldKey.circumstance:
        own_words = None  # only the enum is ever stored for circumstance
    s.save_answer(case_id, field.value, state.value, value, str(own_words) if own_words else None)
    # Opaque ids only. Never the field value.
    s.audit(f"intake_{field.value}_saved", case_id, "case", case_id)


def add_attorney_trigger(s: Session, case: dict, trigger: str) -> bool:
    if trigger in case["attorney_triggers"]:
        return False
    update_case(s, case["id"], attorney_triggers=[*case["attorney_triggers"], trigger])
    s.audit("attorney_referral_added", case["id"], "case", case["id"])
    return True


# ------------------------------------------------------------------ questions

def next_field(answers: dict, session: IntakeSession) -> FieldKey | None:
    """The first field with no answer row. residence_state only when the death was away from home,
    and then right away (UC-CASE-04)."""
    if session.ask_residence and FieldKey.residence_state.value not in answers:
        return FieldKey.residence_state
    for f in FIELD_ORDER:
        if f == FieldKey.residence_state and not session.ask_residence:
            continue
        if f.value not in answers:
            return f
    return None


def _labels(copy: Copy, field: str, values: list[str]) -> list[AnswerOption]:
    return [AnswerOption(value=v, label=copy[f"label_{field}_{v}"]) for v in values]


def question(c: Ctx, field: FieldKey, case: dict, session: IntakeSession) -> Question:
    copy = c.copy
    prompt, pre, kind, options = copy[f"question_{field.value}"], None, "choice", []
    if field == FieldKey.user_role:
        options = _labels(copy, "user_role", [v.value for v in FIELD_VALUE_TYPES[field]])
    elif field == FieldKey.display_name:
        kind = "text"
    elif field == FieldKey.date_of_death:
        kind, options = "date_of_death", _labels(copy, "date_of_death", ["today", "this_week", "exact"])
    elif field == FieldKey.place_of_death:
        kind, options = "place_of_death", _labels(copy, "place_of_death", ["state", "outside_us", "away_from_home"])
    elif field == FieldKey.residence_state:
        kind = "residence_state"
        options = _labels(copy, "residence_state", ["same_as_place_of_death", "different", "unknown"])
    elif field == FieldKey.circumstance:
        options = _labels(copy, "circumstance", [v.value for v in FIELD_VALUE_TYPES[field]])
        if case["skip_explainers"]:
            pass  # the plain question, with no explainer (UC-CASE-02 professional fiduciary)
        elif copy.has(f"circumstance_question_{c.voice}"):
            prompt = copy[f"circumstance_question_{c.voice}"]  # explains why, then asks, in the user's voice
        else:
            pre = copy["pre_question_circumstance"]
    elif field == FieldKey.veteran_status:
        options = _labels(copy, "veteran_status", ["yes", "no", "unknown"])
    elif field == FieldKey.estate_plan_status:
        options = _labels(copy, "estate_plan_status", [v.value for v in FIELD_VALUE_TYPES[field]])
    elif field == FieldKey.completed_items:
        kind = "multi_choice"
        options = _labels(copy, "completed_items", [v.value for v in CompletedItem])
    return Question(field=field, pre_question=pre, prompt=prompt, input=kind, options=options,
                    skip=AnswerOption(value="skipped", label=copy["skip_for_now"]),
                    not_sure=AnswerOption(value="unsure", label=copy["not_sure"]),
                    free_text_label=copy["free_text_label"])


def question_step(q: Question) -> NextStep:
    return NextStep(action="answer_question", prompt=q.prompt,
                    options=[Option(value=o.value, label=o.label) for o in [*q.options, q.skip, q.not_sure]])


# ------------------------------------------------------------------ labels (readback and review)

def value_label(copy: Copy, field: str, value: object) -> str:
    if field == "display_name":
        return str(value)
    if field == "date_of_death":
        if value["precision"] in ("exact", "today") and value["date"]:
            return acct.format_date(date.fromisoformat(value["date"]))
        return copy[f"label_date_of_death_{value['precision']}"]
    if field == "place_of_death":
        if value["outside_us"]:
            return copy["label_place_of_death_outside_us"]
        if value["state"] is None:
            return value["county_or_city"] or copy["state_unsure"]
        state = STATE_LABELS.get(value["state"], value["state"])
        return f"{value['county_or_city']}, {state}" if value["county_or_city"] else state
    if field == "residence_state":
        label = copy[f"label_residence_state_{value['choice']}"]
        return f"{label}: {STATE_LABELS.get(value['state'], value['state'])}" if value.get("state") else label
    if field == "completed_items":
        return ", ".join(copy[f"label_completed_items_{v}"] for v in value)
    return copy[f"label_{field}_{value}"]


def answer_label(copy: Copy, field: str, row: dict | None) -> str:
    if row is None:
        return copy["state_unanswered"]
    if row["answer_state"] != "answered":
        return copy[f"state_{row['answer_state']}"]
    if row["own_words"] and field != "circumstance":
        return row["own_words"]
    return value_label(copy, field, row["value"])


# ------------------------------------------------------------------ journey selection

def journey_definition(s: Session, version: int | None = None) -> dict:
    """The pinned rules for a started case, or the newest active rules for a draft."""
    definition = s.journey_definition(version)
    if definition is None:
        raise ApiError(503, "journey_unavailable", "The journey steps aren't available right now. "
                                                   "Everything you've shared is saved.")
    return definition


def selection(s: Session, case: dict, answers: dict) -> tuple[dict, js.Selection]:
    definition = journey_definition(s, case["journey_template_version"])
    facts = js.facts_from(answers, case["attorney_triggers"], definition)
    return definition, js.select(definition, facts)


def templates(s: Session, keys: list[str]) -> dict[str, dict]:
    """The newest active version of each task template, with citations."""
    out = s.templates(keys)
    missing = set(keys) - set(out)
    if missing:
        raise ApiError(503, "journey_unavailable", "The journey steps aren't available right now. "
                                                   "Everything you've shared is saved.")
    return out


def ordered(keys: list[str], tmpl: dict[str, dict]) -> list[str]:
    return sorted(keys, key=lambda k: (tmpl[k]["journey_week"], tmpl[k]["sort_order"], k))


def journey_notes(c: Ctx, definition: dict, sel: js.Selection) -> list[Note]:
    notes = []
    for note_id in sel.notes:
        n = definition["notes"][note_id]
        notes.append(Note(kind="legal" if n["attorney_flag"] else "info", text=c.copy[n["copy_key"]],
                          legal_review_required=n["legal_review"], source_urls=n["source_urls"],
                          attorney_line=c.copy["attorney_referral_line"] if n["attorney_flag"] else None))
    return notes


def notes_by_task(c: Ctx, definition: dict, sel: js.Selection) -> tuple[list[Note], dict[str, list[Note]]]:
    """Journey-level notes, and notes attached to one task (the certificate notes)."""
    loose, attached = [], {}
    for note_id, note in zip(sel.notes, journey_notes(c, definition, sel), strict=True):
        target = definition["notes"][note_id]["attach_to_task"]
        if target and target in sel.task_keys:
            attached.setdefault(target, []).append(note)
        else:
            loose.append(note)
    return loose, attached


def support_resources(c: Ctx, definition: dict, sel: js.Selection) -> list[SupportResource]:
    return [SupportResource(id=r, text=c.copy[definition["support_resources"][r]["copy_key"]],
                            url=definition["support_resources"][r]["url"]) for r in sel.support_resources]


def attorney_line(c: Ctx, tmpl: dict) -> str | None:
    return (tmpl["attorney_referral_note"] or c.copy["attorney_referral_line"]) if tmpl["attorney_referral"] else None


def change_line(c: Ctx, definition: dict, before: js.Selection, after: js.Selection) -> str:
    """UC-CASE-09. What an answer changed in the journey, in one line."""
    tmpl = templates(c.s, list({*before.task_keys, *after.task_keys}))
    parts = []
    if before.path_key != after.path_key:
        parts.append(c.copy["changed_path"].format(
            path_title=c.copy[definition["paths"][after.path_key]["title_copy_key"]]))
    added = [tmpl[k]["title"] for k in ordered([k for k in after.task_keys if k not in before.task_keys], tmpl)]
    removed = [tmpl[k]["title"] for k in ordered([k for k in before.task_keys if k not in after.task_keys], tmpl)]
    if added:
        parts.append(c.copy["changed_added"].format(tasks=_join(added)))
    if removed:
        parts.append(c.copy["changed_removed"].format(tasks=_join(removed)))
    return " ".join(parts) or c.copy["changed_nothing"]


def added_step_notes(c: Ctx, before: js.Selection, after: js.Selection) -> list[Note]:
    """A step an answer added, with a one-line description and its link."""
    return step_notes(c, [k for k in after.task_keys if k not in before.task_keys])


def poa_note_once(c: Ctx, case: dict) -> list[Note]:
    """UC-CASE-02. The power of attorney note, once per case, with its citation and the attorney line."""
    if "poa_authority_note" in case["shown_notices"]:
        return []
    n = journey_definition(c.s, case["journey_template_version"])["notes"]["poa_authority_note"]
    update_case(c.s, case["id"], shown_notices=[*case["shown_notices"], "poa_authority_note"])
    return [Note(kind="legal", text=c.copy[n["copy_key"]], legal_review_required=n["legal_review"],
                 source_urls=n["source_urls"], attorney_line=c.copy["attorney_referral_line"])]


def _facts_in(cond: dict) -> set[str]:
    if "all" in cond or "any" in cond:
        return set().union(*(_facts_in(c) for c in cond.get("all", cond.get("any"))))
    if "not" in cond:
        return _facts_in(cond["not"])
    return {cond["fact"]}


def field_step_notes(c: Ctx, definition: dict, field: FieldKey, after: js.Selection) -> list[Note]:
    """UC-CASE-06 and UC-CASE-07. The steps an answer brings into the journey, each with its one-line
    description and link, even when an unknown answer had already included them. Read from the add-on
    rules that look at this field, so the rules stay in template data."""
    keys = []
    for add_on in definition["add_ons"]:
        if add_on["id"] in after.add_ons and field.value in _facts_in(add_on["when"]):
            keys += [k for k in add_on.get("add_tasks", []) if k in after.task_keys and k not in keys]
    return step_notes(c, keys)


def step_notes(c: Ctx, keys: list[str]) -> list[Note]:
    if not keys:
        return []
    tmpl = templates(c.s, keys)
    notes = []
    for k in ordered(keys, tmpl):
        t = tmpl[k]
        notes.append(Note(kind="legal" if t["attorney_referral"] else "info",
                          text=f'{c.copy["step_added"].format(tasks=t["title"])} {t["plain_summary"]}',
                          source_urls=[x["url"] for x in t["citations"]], attorney_line=attorney_line(c, t)))
    return notes


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


# ------------------------------------------------------------------ tasks on an active case

def anchor_date(answers: dict, started_on: date) -> date:
    """Due dates count from the date of death when it is known, and never fall before the journey began."""
    d = js.answered_value(answers, "date_of_death") or {}
    return date.fromisoformat(d["date"]) if d.get("date") else started_on


def sync_tasks(s: Session, case: dict, answers: dict, sel: js.Selection) -> None:
    """Make the case's tasks match the selection without losing progress (UC-CASE-09).

    New tasks are added. Tasks the rules no longer include are unselected, not
    deleted, and a task already in progress or done stays visible. Newly
    checked completed_items mark their open tasks done.
    """
    existing = {r["task_key"]: r for r in s.task_states(case["id"])}
    missing = [k for k in sel.task_keys if k not in existing]
    tmpl = templates(s, missing) if missing else {}
    anchor = anchor_date(answers, case["journey_started_on"])
    for k in missing:
        due = max(anchor + timedelta(days=tmpl[k]["due_offset_days"]), case["journey_started_on"])
        s.add_task(case["id"], tmpl[k], sel.initial_status[k], due)
    for k, row in existing.items():
        want = k in sel.task_keys
        if row["selected"] != want:
            s.update_task(case["id"], row["id"], selected=want)
        if want and sel.initial_status[k] == "done" and row["status"] in ("not_started", "check_on_this"):
            s.update_task(case["id"], row["id"], status="done")
        elif want and sel.initial_status[k] == "check_on_this" and row["status"] == "not_started":
            s.update_task(case["id"], row["id"], status="check_on_this")
    if case["journey_template_key"] != sel.path_key:
        update_case(s, case["id"], journey_template_key=sel.path_key)
    s.audit("journey_tasks_synced", case["id"], "case", case["id"])


# ------------------------------------------------------------------ turns

def voice_for(c: Ctx, session: IntakeSession) -> str:
    return "steady_care" if safety.uses_steady_care(session) else c.voice


def crisis_support(c: Ctx, answers: dict, *, emergency: bool) -> tuple[list[str], list[SupportResource]]:
    """988 always. The Veterans Crisis Line for a veteran (UC-CASE-14). No promises about what a line will do."""
    body = [c.copy["support_988"]]
    support = [SupportResource(id="lifeline_988", text=c.copy["support_988"], url="https://988lifeline.org")]
    if js.answered_value(answers, "veteran_status") == "yes":
        body.append(c.copy["support_veterans_crisis_line"])
        support.append(SupportResource(id="veterans_crisis_line", text=c.copy["support_veterans_crisis_line"],
                                       url="https://www.veteranscrisisline.net"))
    if emergency:
        body.append(c.copy["emergency_911"])
    return body, support


def turn(c: Ctx, case: dict, answers: dict, session: IntakeSession, *, acknowledgment: str | None = None,
         body: list[str] | None = None, notes: list[Note] | None = None, ask: bool = True,
         next_step: NextStep | None = None, proposals: list[ProposedAnswer] | None = None,
         support: list[SupportResource] | None = None, redactions: tuple[str, ...] = (),
         masked_text: str | None = None) -> IntakeTurnResponse:
    """Assemble one turn. When the session is in a safety mode, no intake question is asked."""
    body = list(body or [])
    notes = list(notes or [])
    q = None
    if next_step is None:
        if safety.blocks_questions(session):
            next_step = safety_step(c, session)
        elif ask and (f := next_field(answers, session)) is not None:
            q = question(c, f, case, session)
            if q.pre_question:
                body.append(q.pre_question)
            next_step = question_step(q)
        else:
            next_step = review_step(c)
    text = " ".join(x for x in [acknowledgment, *body, next_step.prompt] if x)
    return IntakeTurnResponse(
        case=case_out(c, case, answers), voice=voice_for(c, session), safety_mode=session.safety_mode,
        acknowledgment=acknowledgment, body=body, notes=notes, question=q, proposals=proposals,
        support=support or [], redactions=list(redactions), masked_text=masked_text, next_step=next_step,
        session=session, read_aloud=ReadAloud(label=c.copy["read_this_to_me"], text=text))


def safety_step(c: Ctx, session: IntakeSession) -> NextStep:
    if session.safety_mode == SafetyMode.overwhelm:
        return NextStep(action="overwhelm_choice", prompt=c.copy["overwhelm_offer"],
                        options=[Option(value="pause", label=c.copy["overwhelm_pause"]),
                                 Option(value="small_thing", label=c.copy["overwhelm_small_thing"])])
    # acute_distress and risk_of_harm: stay present. Logistics come back only if the user asks.
    return NextStep(action="stay_with_user", prompt=c.copy["safety_next"],
                    options=[Option(value="continue", label=c.copy["ready_to_continue"]),
                             Option(value="stay", label=c.copy["stay_here"])])


def review_step(c: Ctx) -> NextStep:
    return NextStep(action="review", prompt=c.copy["all_questions_done"],
                    options=[Option(value="review", label=c.copy["does_this_look_right"])])


def safety_turn(c: Ctx, case: dict, answers: dict, session: IntakeSession, **extra) -> IntakeTurnResponse:
    """acute_distress or risk_of_harm. No task, no intake question, no deadlines. steady_care voice."""
    risk = session.safety_mode == SafetyMode.risk_of_harm
    body, support = crisis_support(c, answers, emergency=risk)
    ack = c.copy["risk_ack"] if risk else c.copy["steady_care_example"]
    return turn(c, case, answers, session, acknowledgment=ack, body=body, support=support,
                next_step=safety_step(c, session), **extra)


def review_lines(c: Ctx, answers: dict, session: IntakeSession) -> tuple[list[ReviewLine], list[ReviewLine]]:
    """UC-CASE-11. What you told me, and what we can figure out later. Each line has Edit."""
    told, later = [], []
    for f in FIELD_ORDER:
        row = answers.get(f.value)
        if f == FieldKey.residence_state and row is None and not session.ask_residence:
            continue
        line = ReviewLine(field=f, label=c.copy[f"field_{f.value}"], answer=answer_label(c.copy, f.value, row),
                          answer_state=AnswerState(row["answer_state"]) if row else None,
                          edit=NextStep(action="edit_answer", prompt=c.copy[f"question_{f.value}"],
                                        options=[Option(value=f.value, label=c.copy["edit"])]))
        (told if row and row["answer_state"] == "answered" else later).append(line)
    return told, later


def proposal(c: Ctx, field: FieldKey, value: object, own_words: str | None) -> ProposedAnswer:
    return ProposedAnswer(field=field, value=value, label=value_label(c.copy, field.value, value),
                          own_words=None if field == FieldKey.circumstance else own_words)
