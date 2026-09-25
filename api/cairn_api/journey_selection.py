"""Journey selection (the case creation spec's journey_selection, UC-CASE-12).

The rules are template data, not code: database/content/journeys/journey-selection.json,
loaded read-only into cairn.journey_templates. This module only evaluates them.

Facts come from the intake answers with the spec's defaults, so a journey is
always produced and missing answers never block it:
- veteran_status and estate_plan_status count as unknown when skipped, unsure, or unanswered.
- circumstance maps to a base path. Skipped, unsure, or unanswered is skipped_or_unknown.
- user_role defaults to family_member.
- the certificate office comes from place_of_death.state only, never residence_state.
"""
from __future__ import annotations

from dataclasses import dataclass, field

UNKNOWN_DEFAULTS = {"veteran_status": "unknown", "estate_plan_status": "unknown"}
DEFAULT_USER_ROLE = "family_member"


def answered_value(answers: dict[str, dict], key: str):
    row = answers.get(key)
    return row["value"] if row and row["answer_state"] == "answered" else None


def facts_from(answers: dict[str, dict], attorney_triggers: list[str], definition: dict) -> dict:
    """The facts the add-on conditions read. answers maps field key to its row."""
    place = answered_value(answers, "place_of_death") or {}
    date_of_death = answered_value(answers, "date_of_death") or {}
    residence = answered_value(answers, "residence_state") or {}
    circumstance = answered_value(answers, "circumstance")
    state = place.get("state")
    facts = {
        "user_role": answered_value(answers, "user_role") or DEFAULT_USER_ROLE,
        "date_of_death.precision": date_of_death.get("precision"),
        "place_of_death.state": state,
        "place_of_death.outside_us": bool(place.get("outside_us", False)),
        "place_state_verified": state is not None and state in definition["verified_states"],
        "residence_state": residence.get("choice"),
        "circumstance": circumstance,
        "completed_items": list(answered_value(answers, "completed_items") or []),
        "attorney_triggers": list(attorney_triggers),
    }
    for key, default in UNKNOWN_DEFAULTS.items():
        facts[key] = answered_value(answers, key) or default
    facts["base_path"] = definition["base_path_by_circumstance"][circumstance or "skipped_or_unknown"]
    return facts


def certificate_office_state(answers: dict[str, dict]) -> str | None:
    """UC-CASE-04 and DEC-05. The vital records office is keyed on where the death happened."""
    return (answered_value(answers, "place_of_death") or {}).get("state")


def evaluate(cond: dict, facts: dict) -> bool:
    if "all" in cond:
        return all(evaluate(c, facts) for c in cond["all"])
    if "any" in cond:
        return any(evaluate(c, facts) for c in cond["any"])
    if "not" in cond:
        return not evaluate(cond["not"], facts)
    value, op, target = facts[cond["fact"]], cond["op"], cond.get("value")
    match op:
        case "eq":
            return value == target
        case "ne":
            return value != target
        case "in":
            return value in target
        case "not_in":
            return value not in target
        case "contains":
            return target in value
        case "not_contains":
            return target not in value
        case "is_null":
            return value is None
        case "not_null":
            return value is not None
        case "empty":
            return not value
        case "not_empty":
            return bool(value)
    raise ValueError(f"unknown operator {op}")


@dataclass
class Selection:
    template_version: int
    path_key: str
    facts: dict
    task_keys: list[str]                       # in rule order. Sort by template week for display.
    notes: list[str]                           # note ids from the definition
    support_resources: list[str]
    add_ons: list[str]                         # ids of the add-ons that applied
    initial_status: dict[str, str] = field(default_factory=dict)


def select(definition: dict, facts: dict) -> Selection:
    """Always returns a journey. Never raises for missing answers."""
    path_key = facts["base_path"]
    tasks = list(definition["paths"][path_key]["tasks"])
    notes: list[str] = []
    resources = [k for k, r in definition["support_resources"].items() if r["always"]]
    applied: list[str] = []
    replacements: dict[str, str] = {}

    for add_on in definition["add_ons"]:
        if not evaluate(add_on["when"], facts):
            continue
        applied.append(add_on["id"])
        tasks += [t for t in add_on.get("add_tasks", []) if t not in tasks]
        notes += [n for n in add_on.get("add_notes", []) if n not in notes]
        resources += [r for r in add_on.get("add_support_resources", []) if r not in resources]
        replacements.update(add_on.get("replace_tasks", {}))

    replaced: list[str] = []
    for t in tasks:
        t = replacements.get(t, t)
        if t not in replaced:
            replaced.append(t)

    done = {t for item in facts["completed_items"] for t in definition["completed_items"].get(item, [])}
    unsure = "none_or_unsure" in facts["completed_items"]
    status = {}
    for t in replaced:
        if t in done:
            status[t] = "done"
        elif unsure and t in definition["check_on_this_when_unsure"]:
            status[t] = "check_on_this"
        else:
            status[t] = "not_started"

    return Selection(template_version=definition["version"], path_key=path_key, facts=facts, task_keys=replaced,
                     notes=notes, support_resources=resources, add_ons=applied, initial_status=status)
