"""Case creation rules that need no database: journey selection, redaction, safety, extraction, and copy.

Spec: database/docs/cairn-case-creation-use-cases-v32.json (3.2.0), which defers to
database/docs/cairn-support-crisis-plan-v32.json for crisis levels and replies and to
database/docs/cairn-take-a-break-use-cases-v32.json for breaks. Each test names the
use case and acceptance criterion it covers. [SAFETY] and [PRIVACY] criteria
are release blockers, and so is [LEGAL]. The database-backed checks are in test_case_creation.py.
"""
from __future__ import annotations

import ast
import json
import re
from datetime import date

import pytest

from cairn_api import journey_selection as js
from cairn_api import safety
from cairn_api.copy_store import load_break_copy, load_case_copy
from cairn_api.extraction import extract
from cairn_api.intake import Ctx, question
from cairn_api.redaction import REMOVED, luhn_valid, redact
from cairn_api.schemas import FieldKey, IntakeMessageIn, IntakeSession, SafetyMode, TranscriptIn

from .conftest import REPO

SPEC = json.loads((REPO / "database" / "docs" / "cairn-case-creation-use-cases-v32.json").read_text())
PLAN = json.loads((REPO / "database" / "docs" / "cairn-support-crisis-plan-v32.json").read_text())
BREAKS = json.loads((REPO / "database" / "docs" / "cairn-take-a-break-use-cases-v32.json").read_text())
BREAK_COPY = load_break_copy()
DEFINITION = json.loads((REPO / "database" / "content" / "journeys" / "journey-selection.json").read_text())
COPY = load_case_copy()
UC = {u["id"]: u for u in SPEC["use_cases"]}
VA_STEPS = ("notify_va_if_veteran", "notify_military_retiree_benefits")


def facts(**answers) -> dict:
    rows = {k: {"answer_state": "answered", "value": v, "own_words": None} for k, v in answers.items()}
    return js.facts_from(rows, [], DEFINITION)


def pick(**answers) -> js.Selection:
    return js.select(DEFINITION, facts(**answers))


# ------------------------------------------------------------------ copy layer

def test_spec_copy_is_verbatim():
    """instructions_for_claude_code: copy lives in the copy layer, and the specs' copy is not reworded."""
    fields = {f["key"]: f for f in SPEC["data_fields"]}
    expected = {f"question_{k}": f["question_copy"] for k, f in fields.items()}
    expected["pre_question_circumstance"] = fields["circumstance"]["pre_question_copy"]
    expected.update({
        "intro": UC["UC-CASE-01"]["copy"]["intro"],
        "poa_authority_note": UC["UC-CASE-02"]["alternate_flows"][0]["copy"],
        "place_unknown": UC["UC-CASE-04"]["alternate_flows"][0]["copy"],
        "level_2_after_skips": UC["UC-CASE-09"]["copy"]["level_2_after_skips"],
        "pre_button_notice": UC["UC-CASE-12"]["copy"]["pre_button_notice"],
        "confirmation_first_case": UC["UC-CASE-12"]["copy"]["confirmation_first_case"],
        "not_today": UC["UC-CASE-13"]["alternate_flows"][0]["copy"],
        "steady_care_example": SPEC["voice_samples"]["uc_case_05"]["steady_care"],
        "redaction_explanation": UC["UC-CASE-15"]["copy"]["explanation"],
        "draft_notice": UC["UC-CASE-17"]["copy"]["draft_notice"],
        "confirmation_existing_trial": UC["UC-CASE-18"]["copy"]["confirmation_existing_trial"],
        "confirmation_subscribed": UC["UC-CASE-18"]["copy"]["confirmation_subscribed"],
        "notifications_intro": UC["UC-CASE-19"]["copy"]["opening"],
        "lead_time_question": UC["UC-CASE-19"]["copy"]["lead_time_question"],
        "inactivity_question": UC["UC-CASE-19"]["copy"]["inactivity_question"],
        "notification_example": UC["UC-CASE-19"]["copy"]["example_notification"],
        "speech_first_use": UC["UC-CASE-22"]["copy"]["first_use"],
        "ai_reminder": UC["UC-CASE-23"]["copy"]["ai_reminder"],
        "under_18_message": UC["UC-CASE-24"]["copy"]["message"],
        "empty_state": UC["UC-CASE-25"]["copy"]["empty_state"],
        "empty_state_button": UC["UC-CASE-25"]["copy"]["empty_state_button"],
        # The crisis plan's follow-up. It wins where the specs differ.
        "check_in_question": PLAN["follow_up"]["ask"],
        "check_in_outside_cairn": PLAN["follow_up"]["message_outside_cairn"],
        "check_in_in_cairn": PLAN["follow_up"]["message_in_cairn"],
    })
    expected.update({f"circumstance_question_{v}": t for v, t in SPEC["voice_samples"]["uc_case_05"].items()})
    expected.update({f"notifications_question_{v}": t for v, t in SPEC["voice_samples"]["uc_case_19"].items()})
    quoted = {"ask_about_suicide": next(b for b in PLAN["safety_modes"][3]["behavior"] if "directly" in b)}
    assert {k: v for k, v in COPY.spec.items() if k not in quoted} == expected
    for key, source in quoted.items():
        assert f"'{COPY[key]}'" in source
    assert COPY.version == SPEC["spec_version"]


def test_take_a_break_copy_is_verbatim():
    """The take a break spec 3.2.0 owns every break screen's words (UC-BRK). rest_care_clock_note_condition is a rule,
    not copy."""
    expected = {k: v for k, v in BREAKS["copy"].items() if k != "rest_care_clock_note_condition"}
    assert BREAK_COPY.spec == expected and BREAK_COPY.version == BREAKS["spec"]["version"]
    assert [BREAK_COPY[k] for k in ("rest_choice_today", "rest_choice_3_days", "rest_choice_7_days",
                                    "rest_choice_open")] == ["For the rest of today", "A few days (3)", "A week (7)",
                                                             "Until I come back"]
    for text in BREAK_COPY.all_strings:
        assert "—" not in text and ";" not in text, text


def test_web_only_copy_never_says_tap_or_phone():
    """DEC-PLAT: the alpha is web browser only. 'tap' became 'select', phone notifications became browser ones."""
    for text in COPY.all_strings:
        assert not re.search(r"\btap\b|\bphone\b", text, re.I), text
    for text in BREAK_COPY.all_strings:
        assert not re.search(r"\btap\b|\bphone\b", text, re.I), text


def _grade(text: str) -> float:
    """Flesch-Kincaid grade level, with a simple syllable count."""
    words = re.findall(r"[A-Za-z']+", text)
    sentences = max(1, len(re.findall(r"[.!?]+", text)))
    syllables = sum(max(1, len(re.findall(r"[aeiouy]+", w.lower().rstrip("e")))) for w in words)
    return 0.39 * len(words) / sentences + 11.8 * syllables / max(1, len(words)) - 15.59


def test_copy_is_about_grade_8():
    """DEC-A11Y and UC-CASE-23: about a grade 8 reading level (Federal Plain Language Guidelines). Every reminder
    and offer is at grade 8 or below, and the copy as a whole averages grade 8 or below."""
    for key in ("ai_reminder", "check_in_question"):
        assert _grade(COPY[key]) <= 8, key
    for key in ("offer_after_time", "offer_after_task", "quiet_988_line"):  # UC-BRK-06
        assert _grade(BREAK_COPY[key]) <= 8, key
    sentences = [x for x in COPY.all_strings if len(x.split()) >= 6]
    assert sum(_grade(x) for x in sentences) / len(sentences) <= 8
    assert not [x for x in sentences if _grade(x) > 12]


def test_legal_wording_is_flagged_for_counsel():
    """[LEGAL] UC-CASE-12: price and subscription wording needs counsel approval before launch."""
    for key in ("confirmation_first_case", "confirmation_subscribed", "pre_button_notice"):
        assert key in COPY.legal_review
    assert "$14.99 a month" in COPY["confirmation_first_case"]
    from cairn_api.copy_store import load_subscription_copy
    sub = load_subscription_copy()
    assert "new_journey_needs_subscription" in sub.legal_review and "$14.99" in sub["new_journey_needs_subscription"]


def test_case_copy_follows_house_style():
    for text in COPY.all_strings:
        assert "—" not in text and ";" not in text, text


def test_every_copy_key_the_journey_rules_name_exists():
    keys = [p[k] for p in DEFINITION["paths"].values() for k in ("title_copy_key", "why_copy_key")]
    keys += [n["copy_key"] for n in DEFINITION["notes"].values()]
    keys += [r["copy_key"] for r in DEFINITION["support_resources"].values()]
    missing = [k for k in keys if not COPY.has(k)]
    assert not missing


def test_no_user_facing_copy_is_inline_in_case_creation_code():
    """instructions_for_claude_code: copy is never inline in components. Handlers read every string from copy."""
    for rel in ("cairn_api/intake.py", "cairn_api/routers/case_intake.py", "cairn_api/routers/cases.py"):
        tree = ast.parse((REPO / "api" / rel).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("NextStep", "Note", "SupportResource"):
                for kw in node.keywords:
                    if kw.arg in ("prompt", "text") and isinstance(kw.value, ast.Constant):
                        raise AssertionError(f"inline copy in {rel}:{node.lineno}")


# ------------------------------------------------------------------ journey selection (journey_selection)

@pytest.mark.parametrize("circumstance,path", [
    ("expected_illness_or_hospice", "expected_loss"), ("sudden_natural", "sudden_loss"),
    ("accident_or_unexpected", "sudden_loss_investigation"), ("under_investigation", "sudden_loss_investigation"),
    ("prefer_not_to_say", "general"), (None, "general"),
])
def test_base_path_by_circumstance(circumstance, path):
    assert pick(**({"circumstance": circumstance} if circumstance else {})).path_key == path


def test_always_produces_a_journey_with_no_answers():
    """journey_selection.rule: never block on missing answers."""
    sel = js.select(DEFINITION, js.facts_from({}, [], DEFINITION))
    assert sel.path_key == "general" and sel.task_keys
    assert "notify_va_if_veteran" in sel.task_keys          # veteran_status unknown
    assert "no_will_explainer" in sel.task_keys             # estate_plan_status unknown
    assert "certificate_task_waiting_on_place_note" in sel.notes


def test_uc02_skipped_user_role_uses_family_member():
    rows = {"user_role": {"answer_state": "skipped", "value": None, "own_words": None}}
    assert js.facts_from(rows, [], DEFINITION)["user_role"] == "family_member"


def test_skipped_and_unsure_count_as_unknown():
    for state in ("skipped", "unsure"):
        rows = {k: {"answer_state": state, "value": None, "own_words": None}
                for k in ("veteran_status", "estate_plan_status")}
        f = js.facts_from(rows, [], DEFINITION)
        assert f["veteran_status"] == "unknown" and f["estate_plan_status"] == "unknown"


def test_uc06_va_and_military_retiree_steps_are_never_removed():
    """UC-CASE-06: never removed. Yes recommends them and adds the Veterans Crisis Line. Unknown shows them with
    'Check if they served'. No flags them probably_not_applicable, still visible."""
    yes, unknown, no = pick(veteran_status="yes"), pick(veteran_status="unknown"), pick(veteran_status="no")
    for sel in (yes, unknown, no):
        assert set(VA_STEPS) <= set(sel.task_keys)
    assert set(yes.recommended) >= set(VA_STEPS) and "veterans_crisis_line" in yes.support_resources
    assert "check_if_they_served_note" in unknown.notes and "veterans_crisis_line" not in unknown.support_resources
    assert DEFINITION["notes"]["check_if_they_served_note"]["attach_to_task"] == list(VA_STEPS)
    assert COPY["check_if_they_served"] == "Check if they served"
    assert set(VA_STEPS) <= no.probably_not_applicable and not (set(VA_STEPS) & set(no.recommended))


def test_mvp_template_has_every_section_and_all_ten_agencies():
    """journey_selection.mvp_template_sections (card 6): first hours, the certificate, what to secure now, funeral
    research, all 10 government agencies always, and banks, insurers, and employers."""
    sections = {s["key"]: s for s in SPEC["journey_selection"]["mvp_template_sections"]}
    assert set(DEFINITION["waypoints"]) == set(sections)
    agencies = {"ssa_medicare": "notify_social_security", "va": "notify_va_if_veteran",
                "irs_what_to_know_now": "irs_what_to_know_now", "uscis": "uscis_pending_matters",
                "state_dmv": "notify_state_dmv", "us_passport": "return_passport",
                "voter_registration": "cancel_voter_registration",
                "state_social_services": "notify_state_social_services",
                "military_retiree_benefits": "notify_military_retiree_benefits",
                "federal_employee_benefits": "notify_federal_employee_benefits"}
    assert set(agencies) == set(sections["government_notifications"]["agencies"])
    financial = {"notify_banks", "notify_credit_card_companies", "notify_mortgage_and_loan_servicers",
                 "notify_life_insurers", "notify_employer_and_pension"}
    for answers in ({}, {"veteran_status": "no"}, {"circumstance": "under_investigation"},
                    {"place_of_death": {"jurisdiction": None, "county_or_city": None, "outside_us": True}}):
        keys = set(pick(**answers).task_keys)
        assert set(agencies.values()) <= keys, answers
        assert financial | {"secure_home_and_identity", "choose_funeral_provider", "order_death_certificates"} <= keys
    for key in agencies.values():
        assert DEFINITION["task_waypoints"][key] == "government_notifications"


def test_funeral_step_is_research_only():
    """Card 52: search and compare only. Cairn never schedules, books, contacts, or arranges."""
    doc = json.loads((REPO / "database" / "content" / "tasks" / "us" / "choose_funeral_provider.json").read_text())
    assert doc["title"].startswith("Research and compare")
    assert "never books or contacts anyone" in doc["plain_summary"]


def test_secure_now_step_has_the_attorney_trigger():
    """Card 59: protect home, vehicles, pets, mail, valuables, and identity, and don't sell, give away, or divide
    anything yet, with the attorney line."""
    doc = json.loads((REPO / "database" / "content" / "tasks" / "us" / "secure_home_and_identity.json").read_text())
    for word in ("home", "car", "pets", "mail", "valuables", "don't sell, give away, or divide anything yet"):
        assert word in doc["plain_summary"]
    assert doc["attorney_referral"] and doc["attorney_referral_note"]
    assert "early_property_disposal" in extract("We're thinking of selling his car next week", TODAY).attorney_triggers


@pytest.mark.parametrize("status,added,absent", [
    ("yes_location_known", {"confirm_named_executor"}, {"find_the_will", "no_will_explainer"}),
    ("yes_location_unknown", {"find_the_will", "confirm_named_executor"}, {"no_will_explainer"}),
    ("no", {"no_will_explainer"}, {"find_the_will", "confirm_named_executor"}),
    ("unknown", {"no_will_explainer"}, {"find_the_will", "confirm_named_executor"}),
])
def test_estate_plan_add_ons(status, added, absent):
    keys = set(pick(estate_plan_status=status).task_keys)
    assert added <= keys and not (absent & keys)


def test_power_of_attorney_note():
    assert "poa_authority_note" in pick(user_role="power_of_attorney").notes
    assert "poa_authority_note" not in pick(user_role="child").notes
    note = DEFINITION["notes"]["poa_authority_note"]
    assert note["attorney_flag"] and note["legal_review"] and note["source_urls"]


def test_certificate_may_be_pending_on_sudden_paths():
    for c in ("sudden_natural", "accident_or_unexpected", "under_investigation"):
        assert "certificate_may_be_pending_note" in pick(circumstance=c).notes
    assert "certificate_may_be_pending_note" not in pick(circumstance="expected_illness_or_hospice").notes


def test_uc04_place_unknown_and_outside_us():
    assert "certificate_task_waiting_on_place_note" in pick(
        place_of_death={"jurisdiction": None, "county_or_city": None, "outside_us": False}).notes
    abroad = pick(place_of_death={"jurisdiction": None, "county_or_city": None, "outside_us": True})
    assert "certificate_task_waiting_on_place_note" not in abroad.notes
    assert "outside_us_not_covered" in abroad.notes and "talk_to_estate_attorney" in abroad.task_keys


def test_uc04_certificate_office_uses_place_of_death_only():
    rows = {"place_of_death": {"answer_state": "answered", "value": {"jurisdiction": "NV", "county_or_city": None,
                                                                    "outside_us": False}, "own_words": None},
            "residence_jurisdiction": {"answer_state": "answered", "own_words": None,
                                       "value": {"choice": "different", "jurisdiction": "CA"}}}
    assert js.certificate_office_jurisdiction(rows) == "NV"
    assert js.certificate_office_jurisdiction({"residence_jurisdiction": rows["residence_jurisdiction"]}) is None


def test_uc08_funeral_home_reports_to_ssa():
    sel = pick(completed_items=["funeral_provider_chosen"])
    assert "confirm_funeral_home_reported_death" in sel.task_keys and "notify_social_security" not in sel.task_keys
    both = pick(completed_items=["funeral_provider_chosen", "ssa_notified"])
    assert "confirm_funeral_home_reported_death" not in both.task_keys
    assert both.initial_status["notify_social_security"] == "done"


def test_uc08_completed_items_mark_done_and_unsure_needs_check():
    sel = pick(completed_items=["death_pronounced", "certificates_ordered", "bank_insurer_or_employer_notified",
                                "home_pets_vehicles_secured"])
    for key in ("confirm_pronouncement", "order_death_certificates", "notify_banks", "secure_home_and_identity"):
        assert sel.initial_status[key] == "done"
    # The bank item marks only the bank step. Insurers and employers are independent tasks.
    for key in ("notify_life_insurers", "notify_employer_and_pension", "notify_credit_card_companies"):
        assert sel.initial_status[key] == "not_started"
    unsure = pick(completed_items=["none_or_unsure"])
    for key in DEFINITION["check_on_this_when_unsure"]:
        if key in unsure.task_keys:
            assert unsure.initial_status[key] == "not_started" and key in unsure.needs_check  # open, needs_check
    assert "notify_credit_card_companies" not in unsure.needs_check


def test_uc08_checklist_has_the_secure_now_and_bank_insurer_employer_items():
    q = question(_ctx(), FieldKey.completed_items, CASE, IntakeSession())
    values = [o.value for o in q.options]
    assert "home_pets_vehicles_secured" in values and "bank_insurer_or_employer_notified" in values
    assert q.handled_elsewhere_label == "Someone else is handling this"


def test_uc08_someone_else_handling_it():
    """handled_elsewhere, with an optional name that is never required."""
    sel = pick(completed_items=[{"item": "funeral_provider_chosen", "handled_by": "Fake Cousin"},
                                {"item": "certificates_ordered", "handled_by": None}])
    assert sel.initial_status["choose_funeral_provider"] == "handled_elsewhere"
    assert sel.handled_by == {"choose_funeral_provider": "Fake Cousin", "order_death_certificates": None}
    assert sel.initial_status["order_death_certificates"] == "handled_elsewhere"
    # Handled by someone counts as taken care of, so the SSA wording still changes.
    assert "confirm_funeral_home_reported_death" in sel.task_keys


def test_uc16_attorney_triggers_and_unverified_jurisdictions():
    """UC-CASE-16 and OPEN-06: a place of death without counsel-reviewed content keeps the certificate step and
    shows it with 'Please confirm with the office'. No step is ever removed."""
    sel = js.select(DEFINITION, js.facts_from({}, ["family_disagreement"], DEFINITION))
    assert "talk_to_estate_attorney" in sel.task_keys
    for code in ("NH", "PR", "GU", "MP"):
        placed = pick(place_of_death={"jurisdiction": code, "county_or_city": None, "outside_us": False})
        assert "confirm_with_the_office_note" in placed.notes and "order_death_certificates" in placed.task_keys
    verified = dict(DEFINITION, verified_states=["NH"])
    f = js.facts_from({"place_of_death": {"answer_state": "answered", "own_words": None,
                                         "value": {"jurisdiction": "NH", "county_or_city": None, "outside_us": False}}},
                      [], verified)
    assert "confirm_with_the_office_note" not in js.select(verified, f).notes
    assert COPY["confirm_with_the_office"] == "Please confirm with the office."


def test_uc04_dmv_follows_where_they_lived_and_defaults_to_the_place_of_death():
    place = {"answer_state": "answered", "own_words": None,
             "value": {"jurisdiction": "NV", "county_or_city": None, "outside_us": False}}
    assert js.dmv_jurisdiction({"place_of_death": place}) == "NV"  # not asked: same as the place of death
    lived = {"answer_state": "answered", "own_words": None, "value": {"choice": "different", "jurisdiction": "CA"}}
    assert js.dmv_jurisdiction({"place_of_death": place, "residence_jurisdiction": lived}) == "CA"
    assert js.certificate_office_jurisdiction({"place_of_death": place, "residence_jurisdiction": lived}) == "NV"
    # Both unknown: the DMV step asks where they lived, just in time.
    assert "dmv_where_they_lived_note" in js.select(DEFINITION, js.facts_from({}, [], DEFINITION)).notes


def test_uc04_picker_covers_all_56_jurisdictions_by_full_name():
    q = question(_ctx(), FieldKey.place_of_death, CASE, IntakeSession())
    codes = [o.value for o in q.jurisdictions]
    assert codes == next(f for f in SPEC["data_fields"] if f["key"] == "place_of_death")["jurisdictions"]
    assert len(codes) == 56 and q.picker_label == "State or territory"
    labels = {o.value: o.label for o in q.jurisdictions}
    assert labels["DC"] == "District of Columbia" and labels["VI"] == "U.S. Virgin Islands"
    assert labels["MP"] == "Northern Mariana Islands" and labels["AS"] == "American Samoa"


def test_mvp_window_is_the_first_four_weeks():
    """out_of_scope_mvp: journey beyond week 4. Template weeks are 1 to 4 by schema and database check."""
    schema = json.loads((REPO / "database" / "content" / "schema" / "task-template.schema.json").read_text())
    assert schema["properties"]["journey_week"]["maximum"] == 4
    assert DEFINITION["mvp_window"] == "first_4_weeks"


# ------------------------------------------------------------------ questions and pacing (global_rules)

def _ctx(voice="steady_direct") -> Ctx:
    return Ctx(s=None, request=None, copy=COPY, account={"voice": voice, "time_zone": "UTC"})


CASE = {"skip_explainers": False}


@pytest.mark.parametrize("field", list(FieldKey))
def test_every_question_offers_skip_and_not_sure_and_free_text(field):
    q = question(_ctx(), field, CASE, IntakeSession())
    assert q.skip.label == "Skip for now" and q.not_sure.label == "I'm not sure"
    assert q.free_text_allowed
    assert all(o.label for o in q.options)


@pytest.mark.parametrize("voice", ["steady_direct", "warm_patient", "brisk_businesslike", "plain_practical"])
@pytest.mark.parametrize("field", list(FieldKey))
def test_at_most_one_question_per_turn(voice, field):
    """comfort_first_turn_contract: ask at most one question per turn."""
    q = question(_ctx(voice), field, CASE, IntakeSession())
    assert q.prompt.count("?") <= 1
    assert "?" not in (q.pre_question or "")


def test_uc05_explains_before_asking_in_every_voice_and_fiduciary_can_skip_it():
    for voice, sample in SPEC["voice_samples"]["uc_case_05"].items():
        if voice != "steady_care":
            assert question(_ctx(voice), FieldKey.circumstance, CASE, IntakeSession()).prompt == sample
    brief = question(_ctx(), FieldKey.circumstance, {"skip_explainers": True}, IntakeSession())
    assert brief.prompt == COPY["question_circumstance"] and brief.pre_question is None


def test_uc05_circumstance_choices_are_the_five_enum_values():
    q = question(_ctx(), FieldKey.circumstance, CASE, IntakeSession())
    assert [o.value for o in q.options] == next(f for f in SPEC["data_fields"] if f["key"] == "circumstance")["enum"]


# ------------------------------------------------------------------ UC-CASE-15 redaction

@pytest.mark.parametrize("text", ["my SSN is 123-45-6789", "ssn 123456789", "it's 123 45 6789 ok"])
def test_uc15_ssn_with_and_without_dashes(text):
    r = redact(text)
    assert "ssn" in r.kinds and REMOVED in r.text
    assert not re.search(r"\d{4}", r.text)


@pytest.mark.parametrize("number", [
    "4111 1111 1111 1111", "4111-1111-1111-1111", "5500000000000004", "378282246310005",
    "4222222222222", "6011000990139424", "3530111333300000", "6304000000000000018",
])
def test_uc15_card_numbers_13_to_19_digits_with_luhn(number):
    assert luhn_valid(re.sub(r"\D", "", number))
    r = redact(f"card {number} thanks")
    assert r.kinds == ("card_number",) and r.text == f"card {REMOVED} thanks"


def test_uc15_non_luhn_digit_runs_are_still_removed_as_account_numbers():
    r = redact("4111111111111112")
    assert r.kinds == ("account_number",) and r.text == REMOVED


@pytest.mark.parametrize("text", [
    "account number 0012345678", "acct: 12-3456-78", "routing # 021000021", "checking 55512345",
    "policy no. 998877", "IBAN 12345678", "1234567890123",
])
def test_uc15_long_digit_runs_labeled_as_account_numbers(text):
    r = redact(text)
    assert r.kinds == ("account_number",)
    assert not re.search(r"\d{4}", r.text)


def test_uc15_ordinary_numbers_are_left_alone():
    for text in ("call 603-555-0123", "she died on 9/20/2026", "ZIP 03301-1234", "room 42", "in 2025"):
        assert redact(text).text == text


def test_never_collect_date_of_birth_is_redacted_when_labeled():
    assert redact("born on 3/4/1950").kinds == ("date_of_birth",)
    assert "1950" not in redact("DOB: March 4, 1950").text


def test_uc15_redaction_runs_during_request_parsing():
    """[PRIVACY] Handlers never see the raw value, so nothing downstream can store, log, or send it."""
    m = IntakeMessageIn(text="SSN 123-45-6789, card 4111 1111 1111 1111")
    assert "6789" not in m.text and "1111" not in m.text
    assert m.text.redactions == ("card_number", "ssn")


def test_uc15_every_free_text_request_field_is_redacted():
    """[PRIVACY] Every free-text string a case creation request accepts goes through redaction."""
    from pydantic import BaseModel

    from cairn_api import schemas
    from cairn_api.redaction import RedactedStr
    models = [schemas.IntakeMessageIn, schemas.AnswerIn, schemas.ProposedAnswerIn, schemas.PlaceOfDeath,
              schemas.TranscriptIn, schemas.HandledElsewhere]
    samples = {schemas.IntakeMessageIn: {"text": "x 123-45-6789"},
               schemas.TranscriptIn: {"transcript": "x 123-45-6789"},
               schemas.HandledElsewhere: {"item": "ssa_notified", "handled_by": "x 123-45-6789"},
               schemas.AnswerIn: {"state": "answered", "value": 1, "own_words": "x 123-45-6789"},
               schemas.ProposedAnswerIn: {"field": "display_name", "value": 1, "own_words": "x 123-45-6789"},
               schemas.PlaceOfDeath: {"county_or_city": "x 123-45-6789"}}
    for model in models:
        obj: BaseModel = model(**samples[model])
        # input_mode and item are fixed choices, not free text.
        texts = [v for k, v in obj.__dict__.items() if isinstance(v, str) and not hasattr(v, "value")
                 and k not in ("input_mode", "item")]
        assert texts and all(isinstance(t, RedactedStr) and "6789" not in t for t in texts), model
    from pydantic import TypeAdapter
    assert "6789" not in TypeAdapter(schemas.FIELD_VALUE_TYPES[FieldKey.display_name]).validate_python("123-45-6789")


# ------------------------------------------------------------------ UC-CASE-14 safety modes

@pytest.mark.parametrize("text", ["I want to die", "I don't want to be here anymore", "I might hurt myself",
                                  "I'm thinking about suicide", "sometimes I want to end my life"])
def test_uc14_risk_of_harm(text):
    assert safety.classify(text).mode == SafetyMode.risk_of_harm


def test_uc05_suicide_loss_is_a_volunteered_cause_not_a_risk_to_the_user():
    s = safety.classify("My brother died by suicide last week")
    assert s.mode is None and s.suicide_loss and s.volunteered_cause
    assert safety.classify("My dad took his own life and I want to die too").mode == SafetyMode.risk_of_harm


@pytest.mark.parametrize("text", ["I can't do this", "I'm panicking", "I can't breathe"])
def test_uc14_level_3_on_any_one_signal(text):
    assert safety.classify(text).mode == SafetyMode.acute_distress


@pytest.mark.parametrize("text", ["I'm done", "I can't do this anymore", "I can't go on"])
def test_uc14_unclear_ending_language_is_level_4_and_asks_directly(text):
    """DEC-26-05: ending language not clearly about the paperwork is risk of harm, with the direct question."""
    s = safety.classify(text)
    assert s.mode == SafetyMode.risk_of_harm and s.ask_directly


@pytest.mark.parametrize("text", ["I'm done with these questions", "I'm done for now", "I'm finished with the forms"])
def test_ending_language_about_the_paperwork_is_not_a_crisis(text):
    assert safety.classify(text).mode is None


def test_uc14_level_2_starts_on_the_second_overwhelm_signal_not_before():
    """[OVERWHELM] DEC-26-03: two signals in one conversation, or three skips in a row, not before."""
    one = safety.after_message(IntakeSession(), safety.classify("This is too much"))
    assert one.safety_mode == SafetyMode.normal and one.overwhelm_signals == 1
    two = safety.after_message(one, safety.classify("I keep getting this wrong"))
    assert two.safety_mode == SafetyMode.overwhelm


def test_uc09_third_skip_in_a_row_starts_level_2_and_unsure_does_not_count():
    """[SAFETY] The third consecutive skip triggers level 2. Two skips, or skips separated by an answer or an
    I'm not sure, do not (OPEN-07 default)."""
    s = IntakeSession()
    for _ in range(2):
        s = safety.after_answer(s, "skipped", 3, False)
    assert s.safety_mode == SafetyMode.normal
    assert safety.after_answer(s, "skipped", 3, False).safety_mode == SafetyMode.overwhelm
    for between in ("unsure", "answered"):
        s = IntakeSession()
        for state in ("skipped", "skipped", between, "skipped", "skipped"):
            s = safety.after_answer(s, state, 3, False)
        assert s.safety_mode == SafetyMode.normal, between
    raised = IntakeSession(sensitivity="raised")
    for _ in range(2):
        raised = safety.after_answer(raised, "skipped", 3, False)
    assert raised.safety_mode == SafetyMode.normal  # never earlier than the third, even with raised sensitivity
    assert safety.escalate(IntakeSession(safety_mode=SafetyMode.risk_of_harm), SafetyMode.overwhelm).safety_mode \
        == SafetyMode.risk_of_harm


def test_uc09_level_2_message_never_says_skipping_was_wrong():
    for key in ("level_2_after_skips", "level_2_signals"):
        assert not re.search(r"\b(skip|wrong|should|need to answer)\b", COPY[key], re.I), key


@pytest.mark.parametrize("text", ["I'm 15", "I am 16 years old", "im only 14", "I'm in high school", "I'm a minor"])
def test_uc24_under_18_statements(text):
    assert safety.classify(text).minor


@pytest.mark.parametrize("text", ["I'm 5 minutes away", "My son is 15", "I'm 45", "It was 15 years ago"])
def test_uc24_not_under_18(text):
    assert not safety.classify(text).minor


def test_uc14_steady_care_in_acute_distress_and_risk_of_harm():
    for mode in (SafetyMode.acute_distress, SafetyMode.risk_of_harm):
        assert safety.uses_steady_care(IntakeSession(safety_mode=mode))
    assert not safety.uses_steady_care(IntakeSession(safety_mode=SafetyMode.overwhelm))


def test_uc14_crisis_copy_makes_no_promises():
    """rules: never make promises about confidentiality or what a crisis line will do."""
    for key in ("support_988", "support_veterans_crisis_line", "risk_ack", "emergency_911", "steady_care_example",
                "safety_next", "support_grief", "support_crisis_text_line", "ask_about_suicide", "check_in_question"):
        assert not re.search(r"\b(confidential|anonymous|will (help|answer|listen|call you))\b", COPY[key], re.I)
        # [SAFETY] never ask for a promise to stay safe, never describe methods.
        assert not re.search(r"\b(promise|stay safe|pills?|gun|rope|overdose|method)\b", COPY[key], re.I), key
    assert "838255" in COPY["support_veterans_crisis_line"] and "pressing 1" in COPY["support_veterans_crisis_line"]


# ------------------------------------------------------------------ UC-CASE-01 free text extraction

TODAY = date(2026, 9, 25)


def test_uc01_extracts_only_data_fields():
    e = extract("My mom died yesterday in Manchester, NH after a long illness. She was a Navy veteran. "
                "She had a will, it's in the safe. Her favorite color was blue and she had cancer.", TODAY)
    assert set(e.proposals) <= set(FieldKey)
    assert e.proposals[FieldKey.circumstance] == ("expected_illness_or_hospice", None)
    assert e.proposals[FieldKey.place_of_death][0] == {"jurisdiction": "NH", "county_or_city": "Manchester",
                                                       "outside_us": False}
    assert e.proposals[FieldKey.date_of_death][0] == {"precision": "exact", "date": "2026-09-24"}
    flat = json.dumps({k.value: v for k, v in e.proposals.items()})
    assert "cancer" not in flat and "blue" not in flat


def test_uc05_medical_cause_never_becomes_a_circumstance():
    e = extract("He died of a heart attack.", TODAY)
    assert FieldKey.circumstance not in e.proposals


def test_uc04_away_from_home_and_residence():
    e = extract("Dad died in Florida while visiting my sister. He lived in New Hampshire.", TODAY)
    assert e.away_from_home
    assert e.proposals[FieldKey.residence_jurisdiction][0] == {"choice": "different", "jurisdiction": "NH"}
    assert e.proposals[FieldKey.place_of_death][0]["jurisdiction"] == "FL"
    assert FieldKey.user_role not in e.proposals  # "visiting my sister" is not who died


@pytest.mark.parametrize("text,precision", [("she died this morning", "today"), ("he died a few days ago", "this_week"),
                                            ("I don't know when he died", "unknown")])
def test_uc04_approximate_dates(text, precision):
    assert extract(text, TODAY).proposals[FieldKey.date_of_death][0]["precision"] == precision


def test_intents_and_attorney_triggers():
    assert "not_yet_died" in extract("She's in hospice now, not gone yet.", TODAY).intents
    assert "pause" in extract("I need a break", TODAY).intents
    assert "done" in extract("That's all I know right now", TODAY).intents
    assert extract("My brothers are contesting the will", TODAY).attorney_triggers == {"contested_will"}
    assert "family_disagreement" in extract("We can't agree on the funeral", TODAY).attorney_triggers
    assert "multi_state_property" in extract("She had a house in another state", TODAY).attorney_triggers


# ------------------------------------------------------------------ config and scope

def test_open_decisions_are_configuration_with_the_spec_defaults():
    from cairn_api.config import Settings

    from .conftest import SETTINGS
    assert SETTINGS.estate_plan_mode == "add_on"                         # OPEN-01
    assert SETTINGS.notification_scope == "account"                      # OPEN-04, resolved by account D-13
    assert SETTINGS.sms_enabled is False                                 # OPEN-05
    assert SETTINGS.state_content_approach == "verified_link_confirm"    # OPEN-06
    assert SETTINGS.unsure_counts_as_skip is False                       # OPEN-07
    assert SETTINGS.draft_check_in == "account_channels"                 # OPEN-08, resolved by account D-13
    assert SETTINGS.under_18_handling == "stop_intake"                   # OPEN-09
    assert SETTINGS.outside_us_handling == "out_of_scope_message"        # OPEN-10
    assert SETTINGS.pre_need_path == "not_built"
    assert SETTINGS.subscription_price_display == "$14.99 a month"
    for bad in ({"estate_plan_mode": "trailhead"}, {"pre_need_path": "built"}, {"sms_enabled": True},
                {"notification_scope": "per_journey"}, {"ai_reminder_every_hours": 4},
                {"break_notice_during_care_rest": True}, {"inactivity_timeout_seconds": 1800},
                {"warning_before_timeout_seconds": 10}, {"overall_session_days": 90}):
        with pytest.raises(RuntimeError):
            Settings(**{**SETTINGS.__dict__, **bad})
    schema = (REPO / "database" / "db" / "schema.py").read_text()
    assert re.search(r'"trial_reminder_days_before": \(7,', schema)   # OPEN-02, resolved by account D-14
    assert re.search(r'"draft_retention_days": \(28,', schema)        # DEC-06
    from cairn_api.store import DEFAULT_NOTIFICATIONS  # OPEN-03 and OPEN-11
    assert (DEFAULT_NOTIFICATIONS["channels"], DEFAULT_NOTIFICATIONS["due_date_lead"],
            DEFAULT_NOTIFICATIONS["inactivity_after"]) == (["email", "in_app"], "three_days", "off")
    assert SETTINGS.break_notice_during_care_rest is False              # OPEN-BRK-01
    assert SETTINGS.break_notice_without_reminders is True              # OPEN-BRK-02
    assert (SETTINGS.inactivity_timeout_seconds, SETTINGS.warning_before_timeout_seconds,
            SETTINGS.overall_session_days) == (300, 20, 30)             # D-20


CASE_CREATION_MODULES = ("intake.py", "extraction.py", "journey_selection.py", "journey.py", "messages.py",
                         "routers/case_intake.py", "routers/cases.py", "routers/tasks.py")


def test_no_payment_code_in_case_creation_and_no_payment_details_anywhere():
    """[PRIVACY] DEC-02 and SUB-D-01: no payment form, field, or SDK call in case creation or Start journey. Payments
    happen only on Stripe's own pages: no card, bank, or billing address field anywhere in the API."""
    api = REPO / "api" / "cairn_api"
    for rel in CASE_CREATION_MODULES:
        assert not re.search(r"\b(stripe|braintree|paypal|square|adyen|storekit|revenuecat|payment_method)\b",
                             (api / rel).read_text(), re.I), rel
    # redaction.py names card_number as something it removes, so it is the one module left out.
    source = "\n".join(p.read_text() for p in api.rglob("*.py") if p.name != "redaction.py")
    assert not re.search(r"\b(card_number|cvv|cvc|billing_address|iban|routing_number)\b", source, re.I)
    deps = (REPO / "api" / "pyproject.toml").read_text()
    assert not re.search(r"stripe|braintree|paypal|adyen|revenuecat", deps, re.I)  # Stripe through httpx only


def test_no_document_upload():
    """DEC-06 and out_of_scope_mvp: no upload endpoint, no file field."""
    from cairn_api.main import create_app

    from .conftest import SETTINGS
    spec = json.dumps(create_app(settings=SETTINGS).openapi())
    assert "multipart/form-data" not in spec and '"format": "binary"' not in spec
    assert not any("upload" in path for path in json.loads(spec)["paths"])


def test_policy_updates_are_tracked():
    audit = (REPO / "database" / "docs" / "use-cases-v3-gap-audit.md").read_text()
    for update in SPEC["policy_updates_required"]:
        assert update in audit


def test_spec_file_is_the_one_implemented():
    assert SPEC["spec_version"] == "3.2.0" and PLAN["version"] == "3.2.0" and BREAKS["spec"]["version"] == "3.2.0"


# ------------------------------------------------------------------ UC-CASE-15 and UC-CASE-22

@pytest.mark.parametrize("text", ["the last four of his social are 1234", "his social security number ends in 9876",
                                  "ssn last 4: 4321", "last 4 of her SSN is 5555"])
def test_uc15_partial_ssns(text):
    r = redact(text)
    assert r.kinds == ("ssn",) and not re.search(r"\d{4}", r.text)


@pytest.mark.parametrize("text", ["one two three four five six seven eight nine",
                                  "her social is oh seven eight, oh five, one one two oh",
                                  "card four one one one one one one one one one one one one one one one"])
def test_uc15_spoken_digit_sequences_are_normalized_before_matching(text):
    """[PRIVACY] UC-CASE-15 and UC-CASE-22."""
    r = redact(text)
    assert r.kinds and not re.search(r"\d{4}", r.text)
    for word in ("one one", "seven eight", "five six seven"):
        assert word not in r.text


def test_uc15_counts_in_words_are_left_alone():
    assert redact("I have two kids and three dogs").text == "I have two kids and three dogs"


def test_uc22_transcripts_are_redacted_as_they_are_parsed():
    """[PRIVACY] Transcripts are redacted before persistence or any model request. There is no audio field."""
    t = TranscriptIn(transcript="his social is one two three four five six seven eight nine")
    assert "ssn" in t.transcript.redactions and "one two" not in t.transcript
    from cairn_api import schemas
    assert set(schemas.TranscriptIn.model_fields) == {"transcript", "unclear", "session"}


# ------------------------------------------------------------------ loader cross-checks

def _validate_journey(tmp_path, change) -> list[str]:
    import shutil
    import sys
    sys.path.insert(0, str(REPO / "database" / "tools"))
    import load_templates
    content = tmp_path / "content"
    shutil.copytree(REPO / "database" / "content", content)
    doc = json.loads((content / "journeys" / "journey-selection.json").read_text())
    change(doc)
    (content / "journeys" / "journey-selection.json").write_text(json.dumps(doc))
    templates, errors = load_templates.load_and_validate(content, allow_unreviewed=True)
    _, journey_errors = load_templates.load_and_validate_journeys(
        content, {d["task_key"] for _, d in templates}, allow_unreviewed=True)
    return errors + journey_errors


def test_loader_accepts_the_journey_rules(tmp_path):
    assert _validate_journey(tmp_path, lambda d: None) == []


@pytest.mark.parametrize("change,message", [
    (lambda d: d["paths"]["general"]["tasks"].append("no_such_task"), "no_such_task has no template"),
    (lambda d: d["add_ons"][0].update(add_notes=["no_such_note"]), "no note named no_such_note"),
    (lambda d: d["base_path_by_circumstance"].update(sudden_natural="nowhere"), "no path named nowhere"),
    (lambda d: d["add_ons"][0].update(when={"fact": "veteran_status", "op": "contains", "value": "yes"}),
     "doesn't fit fact veteran_status"),
    (lambda d: d["add_ons"][0].update(when={"fact": "cause_of_death", "op": "eq", "value": "x"}), "cause_of_death"),
])
def test_loader_rejects_broken_journey_rules(tmp_path, change, message):
    assert any(message in e for e in _validate_journey(tmp_path, change))


def test_release_gate_needs_counsel_review_of_the_journey_rules(tmp_path):
    import sys
    sys.path.insert(0, str(REPO / "database" / "tools"))
    import load_templates
    _, errors = load_templates.load_and_validate_journeys(REPO / "database" / "content", set(), allow_unreviewed=False)
    assert any("counsel_reviewed_at is empty" in e for e in errors)


# ------------------------------------------------------------------ UC-CASE-19

def test_uc19_outbound_text_is_checked_for_names_and_circumstances():
    """[PRIVACY] Outbound text passes a check that it contains no display_name or circumstance."""
    from cairn_api import outbound
    from cairn_api.copy_store import load_copy
    terms = outbound.circumstance_terms(COPY)
    assert terms and all(terms)
    ok = outbound.check_in(COPY)
    assert outbound.private_enough(ok, ["Fakename", *terms])
    assert not outbound.private_enough(outbound.Message("Hi", "About Fakename"), ["fakename"])
    assert not outbound.private_enough(outbound.Message("Hi", f"Re: {terms[0]}"), terms)
    account_copy = load_copy()
    for reason in ("due_date_upcoming", "inactivity"):
        msg = outbound.notification(reason, account_copy, COPY)
        assert outbound.private_enough(msg, terms), reason
