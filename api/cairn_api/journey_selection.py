"""Journey selection (the case creation spec's journey_selection, UC-CASE-12).

The rules are template data, not code: database/content/journeys/journey-selection.json,
loaded read-only into the journey_templates collection. This module only evaluates them.

Facts come from the intake answers with the spec's defaults, so a journey is
always produced and missing answers never block it:
- veteran_status and estate_plan_status count as unknown when skipped, unsure, or unanswered.
- circumstance maps to a base path. Skipped, unsure, or unanswered is skipped_or_unknown.
- user_role defaults to family_member.
- the certificate office comes from place_of_death.jurisdiction only (DEC-05).
- the DMV step follows where they lived: residence_jurisdiction when it is a different place, otherwise
  the place of death, because residence defaults to same_as_place_of_death when it wasn't asked.

A started journey stays pinned to the rules version it began with, so the v1 fact names
(place_of_death.state, place_state_verified, residence_state) are still provided.
"""
from __future__ import annotations

from dataclasses import dataclass, field

UNKNOWN_DEFAULTS = {"veteran_status": "unknown", "estate_plan_status": "unknown"}
DEFAULT_USER_ROLE = "family_member"


def answered_value(answers: dict[str, dict], key: str):
    row = answers.get(key)
    return row["value"] if row and row["answer_state"] == "answered" else None


def completed_entries(answers: dict[str, dict]) -> tuple[list[str], dict[str, str | None]]:
    """The checklist (UC-CASE-08): items marked done, and items someone else is handling with their optional name."""
    done, handled = [], {}
    for entry in answered_value(answers, "completed_items") or []:
        if isinstance(entry, str):
            done.append(entry)
        else:
            handled[entry["item"]] = entry.get("handled_by")
    return done, handled


def certificate_office_jurisdiction(answers: dict[str, dict]) -> str | None:
    """UC-CASE-04 and DEC-05. The vital records office is keyed on where the death happened."""
    return (answered_value(answers, "place_of_death") or {}).get("jurisdiction")


def dmv_jurisdiction(answers: dict[str, dict]) -> str | None:
    """UC-CASE-04 and DEC-05. The DMV follows where they lived. Not asked means the same as the place of death."""
    residence = answered_value(answers, "residence_jurisdiction") or {}
    if residence.get("choice") == "different":
        return residence.get("jurisdiction")
    if residence.get("choice") == "unknown":
        return None
    return certificate_office_jurisdiction(answers)


def facts_from(answers: dict[str, dict], attorney_triggers: list[str], definition: dict,
               case: dict | None = None) -> dict:
    """The facts the add-on conditions read. answers maps field key to its row."""
    place = answered_value(answers, "place_of_death") or {}
    date_of_death = answered_value(answers, "date_of_death") or {}
    residence = answered_value(answers, "residence_jurisdiction") or {}
    circumstance = answered_value(answers, "circumstance")
    jurisdiction = place.get("jurisdiction")
    done, handled = completed_entries(answers)
    verified = jurisdiction is not None and jurisdiction in definition["verified_states"]
    facts = {
        "user_role": answered_value(answers, "user_role") or DEFAULT_USER_ROLE,
        "date_of_death.precision": date_of_death.get("precision"),
        "place_of_death.jurisdiction": jurisdiction,
        "place_of_death.outside_us": bool(place.get("outside_us", False)),
        "place_jurisdiction_verified": verified,
        "residence_jurisdiction": residence.get("choice"),
        "dmv_jurisdiction": dmv_jurisdiction(answers),
        "secure_now_first": bool((case or {}).get("secure_now_first", False)),
        "loss_survivor_resources": bool((case or {}).get("loss_survivor_resources", False)),
        "circumstance": circumstance,
        # Handled by someone else counts as taken care of for rules like the SSA wording change.
        "completed_items": done + list(handled),
        "handled_elsewhere": handled,
        "attorney_triggers": list(attorney_triggers),
        # v1 names, for journeys pinned to rules version 1.
        "place_of_death.state": jurisdiction,
        "place_state_verified": verified,
        "residence_state": residence.get("choice"),
    }
    for key, default in UNKNOWN_DEFAULTS.items():
        facts[key] = answered_value(answers, key) or default
    facts["base_path"] = definition["base_path_by_circumstance"][circumstance or "skipped_or_unknown"]
    return facts


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
    # done, handled_elsewhere, or not_started (open)
    initial_status: dict[str, str] = field(default_factory=dict)
    needs_check: set[str] = field(default_factory=set)
    probably_not_applicable: set[str] = field(default_factory=set)
    recommended: list[str] = field(default_factory=list)
    handled_by: dict[str, str | None] = field(default_factory=dict)


def select(definition: dict, facts: dict) -> Selection:
    """Always returns a journey. Never raises for missing answers."""
    path_key = facts["base_path"]
    tasks = list(definition["paths"][path_key]["tasks"])
    notes: list[str] = []
    resources = [k for k, r in definition["support_resources"].items() if r["always"]]
    applied: list[str] = []
    replacements: dict[str, str] = {}
    recommended: list[str] = []
    not_applicable: set[str] = set()

    for add_on in definition["add_ons"]:
        if not evaluate(add_on["when"], facts):
            continue
        applied.append(add_on["id"])
        tasks += [t for t in add_on.get("add_tasks", []) if t not in tasks]
        notes += [n for n in add_on.get("add_notes", []) if n not in notes]
        resources += [r for r in add_on.get("add_support_resources", []) if r not in resources]
        replacements.update(add_on.get("replace_tasks", {}))
        recommended += [t for t in add_on.get("recommend_tasks", []) if t not in recommended]
        not_applicable.update(add_on.get("mark_probably_not_applicable", []))

    replaced: list[str] = []
    for t in tasks:
        t = replacements.get(t, t)
        if t not in replaced:
            replaced.append(t)

    items = definition["completed_items"]
    done = {t for item in facts["completed_items"] if item not in facts.get("handled_elsewhere", {})
            for t in items.get(item, [])}
    handled_by = {t: name for item, name in facts.get("handled_elsewhere", {}).items() for t in items.get(item, [])}
    unsure = "none_or_unsure" in facts["completed_items"]
    status, needs_check = {}, set()
    for t in replaced:
        if t in handled_by:
            status[t] = "handled_elsewhere"
        elif t in done:
            status[t] = "done"
        else:
            status[t] = "not_started"
            if unsure and t in definition["check_on_this_when_unsure"]:
                needs_check.add(t)

    return Selection(template_version=definition["version"], path_key=path_key, facts=facts, task_keys=replaced,
                     notes=notes, support_resources=resources, add_ons=applied, initial_status=status,
                     needs_check=needs_check, probably_not_applicable=not_applicable & set(replaced),
                     recommended=[t for t in recommended if t in replaced],
                     handled_by={t: n for t, n in handled_by.items() if t in replaced})
