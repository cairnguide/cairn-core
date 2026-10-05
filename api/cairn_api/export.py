"""Download all my data (UC-REG-16).

Everything is read through store.Session as the user, so the case boundary decides what is theirs.
Always free, on every account status, and never gated on acknowledgments.
Cases in a 7-day deletion hold are included until they are deleted.

Never included: Social Security number digits, account numbers, or card
numbers. deceased.ssn_last4 is never read, and every free-text value is
put through the UC-CASE-15 redaction again on the way out, as defense in depth.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import intake, journey
from . import notifications as nt
from .db import Session
from .redaction import redact
from .schemas import (
    DataExport,
    ExportAnswer,
    ExportCase,
    ExportConsent,
    ExportNotificationSent,
    ExportReminder,
    ExportTask,
)


def _clean(value):
    """Redact every string in a value, however deeply nested."""
    if isinstance(value, str):
        return redact(value).text
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    return value


def _summary(c: intake.Ctx, case: dict, rows: list[dict]) -> dict:
    """Where the case stands. The hand-off summary for this download."""
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    nxt = journey.pick_next_action(rows) if case["status"] != "draft" else None
    return {"journey": case["journey_template_key"], "tasks_by_status": counts,
            "next_step": nxt["title"] if nxt else None,
            "journey_paused": journey.is_paused(case)}


def _case(c: intake.Ctx, case_id) -> ExportCase:
    s = c.s
    case, answers = intake.load_case(s, case_id), intake.load_answers(s, case_id)
    rows = journey.load_tasks(s, case_id)
    person = s.load_deceased(case_id, for_export=True)  # never the SSN digits
    conversation = s.context_items(case_id)
    sent = s.notifications_sent(case_id)
    return ExportCase(
        id=case["id"], status=intake.effective_status(case),
        display_name=_clean(intake.display_name(case, answers, c.copy)), created_at=case["created_at"],
        journey_template_key=case["journey_template_key"], journey_started_at=case["journey_started_at"],
        tasks_paused_until=case["tasks_paused_until"], deletion_scheduled_for=case["deletion_scheduled_for"],
        summary=_summary(c, case, rows),
        answers=[ExportAnswer(field=k, answer_state=a["answer_state"], value=_clean(a["value"]),
                              own_words=_clean(a["own_words"])) for k, a in sorted(answers.items())],
        person_who_died=_clean({k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in person.items()})
        if person else None,
        tasks=[ExportTask(title=r["title"], plain_summary=r["plain_summary"], journey_week=r["journey_week"],
                          status=r["status"], due_on=r["due_on"], completed_at=r["completed_at"]) for r in rows],
        notification_preferences=nt.out(case_id, nt.load(s, case_id), c.copy),
        notifications_sent=[ExportNotificationSent.model_validate(r) for r in sent],
        conversation=[_clean({"item_key": r["item_key"], "payload": r["payload"],
                              "updated_at": r["updated_at"].isoformat()}) for r in conversation],
    )


def build(c: intake.Ctx) -> DataExport:
    s: Session = c.s
    a = c.account
    profile = _clean({
        "email": a["email"], "sign_in_method": a["sign_in_method"],
        "linked_sign_in_methods": a.get("linked_sign_in_methods") or None, "preferred_name": a["preferred_name"],
        "name_pronunciation": a["name_pronunciation"], "voice": a["voice"], "time_zone": a["time_zone"],
        "status": a["status"], "onboarding_step": a["onboarding_step"],
        "trial_started_at": a["trial_started_at"].isoformat() if a["trial_started_at"] else None,
        "trial_ends_at": a["trial_ends_at"].isoformat() if a["trial_ends_at"] else None,
        "created_at": a["created_at"].isoformat(),
    })
    consents = s.consents()
    reminders = s.trial_reminders()
    case_ids = [r["id"] for r in s.owned_cases()]
    return DataExport(
        format="cairn-data-export", format_version=1, generated_at=datetime.now(timezone.utc), profile=profile,
        acknowledgments=[ExportConsent.model_validate(r) for r in consents],
        trial_reminders=[ExportReminder.model_validate(r) for r in reminders],
        cases=[_case(c, case_id) for case_id in case_ids],
    )
