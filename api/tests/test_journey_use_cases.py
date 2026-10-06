"""Base journeys J-EXPECTED-FACILITY and J-SUDDEN-UNEXPECTED (database/docs/cairn-journey-*-use-cases-v11.json).

Each spec's test_how: run journeys/tools/resolve.py on the fixture and compare to expected, after
journeys/tools/validate.py reports 0 errors. The expected values come from the spec files themselves, so a
template change that moves a step fails here until the spec is regenerated. No database.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from .conftest import REPO

TOOLS = REPO / "journeys" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
import resolve  # noqa: E402

SPECS = {
    "J-EXPECTED-FACILITY": json.loads((REPO / "database" / "docs" /
                                       "cairn-journey-J-EXPECTED-FACILITY-use-cases-v11.json").read_text()),
    "J-SUDDEN-UNEXPECTED": json.loads((REPO / "database" / "docs" /
                                       "cairn-journey-J-SUDDEN-UNEXPECTED-use-cases-v11.json").read_text()),
}
JOURNEYS = list(SPECS)


def ucs(journey: str) -> list[dict]:
    return SPECS[journey]["use_cases"]


def steps(plan: dict) -> dict[str, dict]:
    return {s["step_id"]: s for p in plan["phases"] for s in p["steps"]}


def juris(step: dict) -> list[dict]:
    return [{k: j.get(k) for k in ("key", "state", "overlay", "status", "legal_review")} for j in step["jurisdiction"]]


def test_the_templates_validate_with_no_errors():
    """test_how: validate.py must report 0 errors first."""
    run = subprocess.run([sys.executable, str(TOOLS / "validate.py")], capture_output=True, text=True,
                         cwd=REPO / "journeys")
    assert run.returncode == 0 and "0 errors" in run.stdout, run.stdout + run.stderr


@pytest.mark.parametrize("journey", JOURNEYS)
def test_01_the_case_gets_this_journey(journey):
    uc = ucs(journey)[0]
    plan = resolve.resolve(uc["fixture"])
    assert (plan["journey"]["id"], plan["journey"]["version"]) == (uc["expected"]["journey_id"],
                                                                   uc["expected"]["journey_version"])
    journeys = [resolve.load(p) for p in resolve.load("manifest.json")["files"]["journeys"]]
    matches = [j["id"] for j in journeys if resolve.evaluate(j["applies_when"], uc["fixture"]) is True]
    assert matches == [uc["expected"]["journey_id"]]  # exactly one base journey


@pytest.mark.parametrize("journey", JOURNEYS)
def test_02_the_journey_with_no_extra_facts(journey):
    uc = ucs(journey)[1]
    plan = resolve.resolve(uc["fixture"])
    expected = uc["expected"]
    assert plan["step_count"] == expected["full_step_count"]
    assert resolve.resolve(uc["fixture"], mvp_only=True)["step_count"] == expected["first_28_days_step_count"]
    assert {p["phase"]: [s["step_id"] for s in p["steps"]] for p in plan["phases"]} == expected["steps_by_phase"]
    assert plan["modules_applied"] == expected["modules_applied"]
    assert [q["question"] for q in plan["qualifying_questions"]] == expected["questions_queued"]
    assert plan["estimated_certified_copies"] == expected["estimated_certified_copies"]


@pytest.mark.parametrize("journey", JOURNEYS)
@pytest.mark.parametrize("answer", ["yes", "no"])
def test_03_the_employed_question_is_answered_once(journey, answer):
    """A yes adds S-W4-EMPLOYER and one more certified copy. A no sets was_employed and had_employer_plan false, so
    M-EMPLOYED is never queued again (change_needed_in_cairn_core)."""
    uc = ucs(journey)[2]
    base = SPECS[journey]["fixture_base"]
    facts = resolve.answer_question(base, "M-EMPLOYED", answer)
    assert facts == uc["fixtures"][answer]
    plan = resolve.resolve(facts)
    expected = uc["expected"][answer]
    assert plan["step_count"] == expected["full_step_count"]
    queued = [q["module"] for q in plan["qualifying_questions"]]
    assert ("M-EMPLOYED" in queued) is expected["employment_question_still_queued"]
    ids = [s["step_id"] for p in plan["phases"] for s in p["steps"]]
    if expected["added_step"]:
        assert ids.index(expected["added_step"]) == ids.index(expected["added_after"]) + 1
        assert resolve.resolve(facts, mvp_only=True)["step_count"] == expected["first_28_days_step_count"]
        assert plan["estimated_certified_copies"] == expected["estimated_certified_copies"]
    else:
        assert "S-W4-EMPLOYER" not in ids


@pytest.mark.parametrize("journey", JOURNEYS)
def test_03_skip_or_not_sure_sets_nothing(journey):
    base = SPECS[journey]["fixture_base"]
    for answer in ("skip", "unsure"):
        facts = resolve.answer_question(base, "M-EMPLOYED", answer)
        assert facts == base
        assert "M-EMPLOYED" in [q["module"] for q in resolve.resolve(facts)["qualifying_questions"]]


@pytest.mark.parametrize("journey", JOURNEYS)
def test_04_state_coverage(journey):
    """Death certificates follow where the death happened. Probate follows where the person lived. Every overlay is
    legal review pending, and a not_verified one says it has no verified steps for that state."""
    uc = ucs(journey)[3]
    base = SPECS[journey]["fixture_base"]
    for state, expected in uc["expected"]["by_state"].items():
        plan = steps(resolve.resolve({**base, "death_state": state, "residence_state": state}))
        for step_id, want in expected.items():
            assert juris(plan[step_id]) == want, (state, step_id)
    split = steps(resolve.resolve({**base, "death_state": "NH", "residence_state": "FL"}))
    for step_id, want in uc["expected"]["died_NH_lived_FL"].items():
        assert juris(split[step_id]) == want
    ny = steps(resolve.resolve({**base, "death_state": "NY", "residence_state": "NY"}))
    probate = ny["S-W5-PROBATE-NEEDED"]["jurisdiction"][0]
    assert probate["status"] == "not_verified" and probate.get("assistant_must_say_unverified") is True


@pytest.mark.parametrize("journey", JOURNEYS)
def test_05_order_certified_copies(journey):
    uc = ucs(journey)[4]
    plan = resolve.resolve(uc["fixture"])
    step = steps(plan)[uc["step_id"]]
    expected = uc["expected"]
    assert (step["target_date"], step["depends_on"], step["mvp_first_28_days"]) == (
        expected["target_date"], expected["depends_on"], expected["in_first_28_days"])
    assert step["copy"]["ask"] == expected["copy_ask"]
    assert plan["estimated_certified_copies"] == expected["estimated_certified_copies"]
    how = steps(plan)["S-W1-HOW-DC-IS-MADE"]
    assert [n for n in how["notes"]] == expected["notes_on_how_dc_is_made"]
    # Estimate = steps needing a certified copy, plus 2.
    assert plan["estimated_certified_copies"] == sum(s["certified_copy_needed"] for s in steps(plan).values()) + 2


def test_06_expected_tell_close_family_and_friends():
    uc = ucs("J-EXPECTED-FACILITY")[5]
    plan = resolve.resolve(uc["fixture"])
    w0 = [s["step_id"] for p in plan["phases"] if p["phase"] == "W0" for s in p["steps"]]
    assert w0 == uc["expected"]["w0_steps"] and w0.index(uc["step_id"]) + 1 == uc["expected"]["position"]
    step = steps(plan)[uc["step_id"]]
    assert (step["target_date"], step["mvp_first_28_days"]) == (uc["expected"]["target_date"],
                                                                uc["expected"]["in_first_28_days"])
    assert step["copy"]["ask"] == uc["expected"]["copy_ask"]
    assert step["sensitive_data"]["fields"] == []  # never asks for names or contact details to be stored


def test_06_sudden_the_medical_examiner_or_coroner():
    uc = ucs("J-SUDDEN-UNEXPECTED")[5]
    plan = resolve.resolve(uc["fixture"])
    w0 = [s["step_id"] for p in plan["phases"] if p["phase"] == "W0" for s in p["steps"]]
    assert w0 == uc["expected"]["w0_steps"]
    step = steps(plan)[uc["step_id"]]
    assert step["target_date"] == uc["expected"]["target_date"]
    assert step["copy"]["ask"] == uc["expected"]["copy_ask"]
    assert juris(step) == uc["expected"]["jurisdiction"]
    text = json.dumps(step["copy"]).lower()
    assert "autopsy will" not in text and "will not be an autopsy" not in text  # never says whether one happens


@pytest.mark.parametrize("journey", JOURNEYS)
def test_every_user_facing_string_is_house_style(journey):
    plan = resolve.resolve(SPECS[journey]["fixture_base"])
    for step in steps(plan).values():
        for text in step["copy"].values():
            assert "—" not in text and ";" not in text, text
