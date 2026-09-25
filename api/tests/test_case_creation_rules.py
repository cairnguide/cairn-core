"""Case creation rules that need no database: journey selection, redaction, safety, extraction, and copy.

Spec: database/docs/cairn-case-creation-use-cases.json. Each test names the
use case and acceptance criterion it covers. [SAFETY] and [PRIVACY] criteria
are release blockers. The database-backed checks are in test_case_creation.py.
"""
from __future__ import annotations

import ast
import json
import pathlib
import re
from datetime import date

import pytest

from cairn_api import journey_selection as js
from cairn_api import safety
from cairn_api.copy_store import load_case_copy
from cairn_api.extraction import extract
from cairn_api.intake import Ctx, question
from cairn_api.redaction import REMOVED, luhn_valid, redact
from cairn_api.schemas import FieldKey, IntakeMessageIn, IntakeSession, SafetyMode

from .conftest import REPO

SPEC = json.loads((REPO / "database" / "docs" / "cairn-case-creation-use-cases.json").read_text())
# Draft 0.4 changes to the 0.3.0 spec (UC-CASE-19 to UC-CASE-21 and changes to UC-CASE-10, 12, 18).
CHANGES = json.loads((REPO / "database" / "docs" / "cairn-case-creation-use-cases-2026-09-25.json").read_text())
CHANGED = {c["id"]: c for c in CHANGES["changes_to_existing"]}
DEFINITION = json.loads((REPO / "database" / "content" / "journeys" / "journey-selection.json").read_text())
COPY = load_case_copy()
UC = {u["id"]: u for u in SPEC["use_cases"]}


def facts(**answers) -> dict:
    rows = {k: {"answer_state": "answered", "value": v, "own_words": None} for k, v in answers.items()}
    return js.facts_from(rows, [], DEFINITION)


def pick(**answers) -> js.Selection:
    return js.select(DEFINITION, facts(**answers))


# ------------------------------------------------------------------ copy layer

def test_spec_copy_is_verbatim():
    """instructions_for_claude_code: copy lives in the copy layer, and the spec's copy is not reworded."""
    expected = {f"question_{f['key']}": f["question_copy"] for f in SPEC["data_fields"]}
    expected["pre_question_circumstance"] = next(f for f in SPEC["data_fields"]
                                                 if f["key"] == "circumstance")["pre_question_copy"]
    expected.update({
        "intro": UC["UC-CASE-01"]["copy"]["intro"],
        "poa_authority_note": UC["UC-CASE-02"]["alternate_flows"][0]["copy"],
        "place_unknown": UC["UC-CASE-04"]["alternate_flows"][0]["copy"],
        "pause": UC["UC-CASE-10"]["copy"]["pause"],
        "resume_question": UC["UC-CASE-10"]["copy"]["resume_question"],
        # Draft 0.4: the new pre-button notice, and the reminder sentence that follows the user's channel.
        "pre_button_notice": CHANGED["UC-CASE-12"]["copy"]["before_button"],
        "confirmation_first_case": UC["UC-CASE-12"]["copy"]["confirmation_first_case"].replace(
            "We'll remind you a few days before your free time ends.", "{reminder_sentence}"),
        "reminder_sentence_with_channel": CHANGED["UC-CASE-12"]["copy"]["reminder_sentence_with_channel"],
        "reminder_sentence_in_app_only": CHANGED["UC-CASE-12"]["copy"]["reminder_sentence_in_app_only"],
        "not_today": UC["UC-CASE-13"]["alternate_flows"][0]["copy"],
        "small_task_example": UC["UC-CASE-13"]["copy"]["small_task_example"],
        "steady_care_example": UC["UC-CASE-14"]["copy"]["steady_care_example"],
        "redaction_explanation": UC["UC-CASE-15"]["copy"]["explanation"],
        "draft_notice": UC["UC-CASE-17"]["copy"]["draft_notice"],
        "confirmation_existing_trial": UC["UC-CASE-18"]["copy"]["confirmation_existing_trial"],
    })
    expected.update({f"circumstance_question_{v}": t for v, t in SPEC["voice_samples_uc_case_05"].items()})
    expected["notification_example"] = "You have a step coming up in Cairn."
    uc19 = next(u for u in CHANGES["new_use_cases"] if u["id"] == "UC-CASE-19")
    assert f"Example: '{expected['notification_example']}'" in uc19["rules"][0]
    assert COPY.spec == expected
    assert COPY["confirmation_first_case"].endswith("{reminder_sentence}")
    assert COPY["keep_it_simple"] == uc19["shortcut"]["label"]
    assert COPY.version == "0.4.0" and CHANGES["spec"].endswith("draft 0.4")


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


def test_veteran_add_ons():
    assert "notify_va_if_veteran" in pick(veteran_status="yes").task_keys
    assert "notify_va_if_veteran" in pick(veteran_status="unknown").task_keys
    assert "notify_va_if_veteran" not in pick(veteran_status="no").task_keys
    assert "veterans_crisis_line" in pick(veteran_status="yes").support_resources
    assert "veterans_crisis_line" not in pick(veteran_status="unknown").support_resources


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
        place_of_death={"state": None, "county_or_city": None, "outside_us": False}).notes
    abroad = pick(place_of_death={"state": None, "county_or_city": None, "outside_us": True})
    assert "certificate_task_waiting_on_place_note" not in abroad.notes
    assert "outside_us_not_covered" in abroad.notes and "talk_to_estate_attorney" in abroad.task_keys


def test_uc04_certificate_office_uses_place_of_death_only():
    rows = {"place_of_death": {"answer_state": "answered", "value": {"state": "NV", "county_or_city": None,
                                                                    "outside_us": False}, "own_words": None},
            "residence_state": {"answer_state": "answered", "value": {"choice": "different", "state": "CA"},
                                "own_words": None}}
    assert js.certificate_office_state(rows) == "NV"
    assert js.certificate_office_state({"residence_state": rows["residence_state"]}) is None


def test_uc08_funeral_home_reports_to_ssa():
    sel = pick(completed_items=["funeral_provider_chosen"])
    assert "confirm_funeral_home_reported_death" in sel.task_keys and "notify_social_security" not in sel.task_keys
    both = pick(completed_items=["funeral_provider_chosen", "ssa_notified"])
    assert "confirm_funeral_home_reported_death" not in both.task_keys
    assert both.initial_status["notify_social_security"] == "done"


def test_uc08_completed_items_mark_done_and_unsure_is_check_on_this():
    sel = pick(completed_items=["death_pronounced", "certificates_ordered", "bank_notified"])
    for key in ("confirm_pronouncement", "order_death_certificates", "notify_banks"):
        assert sel.initial_status[key] == "done"
    unsure = pick(completed_items=["none_or_unsure"])
    for key in DEFINITION["check_on_this_when_unsure"]:
        if key in unsure.task_keys:
            assert unsure.initial_status[key] == "check_on_this"
    assert unsure.initial_status["notify_life_insurers"] == "not_started"


def test_uc16_attorney_triggers_and_unverified_states():
    sel = js.select(DEFINITION, js.facts_from({}, ["family_disagreement"], DEFINITION))
    assert "talk_to_estate_attorney" in sel.task_keys
    placed = pick(place_of_death={"state": "NH", "county_or_city": None, "outside_us": False})
    assert "ask_state_vital_records_office" in placed.task_keys  # no state is counsel-verified yet
    verified = dict(DEFINITION, verified_states=["NH"])
    f = js.facts_from({"place_of_death": {"answer_state": "answered", "own_words": None,
                                         "value": {"state": "NH", "county_or_city": None, "outside_us": False}}},
                      [], verified)
    assert "ask_state_vital_records_office" not in js.select(verified, f).task_keys


def test_mvp_window_is_the_first_four_weeks():
    """out_of_scope_mvp: journey beyond week 4. Template weeks are 1 to 4 by schema and database check."""
    schema = json.loads((REPO / "database" / "content" / "schema" / "task-template.schema.json").read_text())
    assert schema["properties"]["journey_week"]["maximum"] == 4
    assert DEFINITION["mvp_window"] == "first_4_weeks"
    assert DEFINITION["waypoints"] == SPEC["journey_selection"]["mvp_waypoints"]


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
    for voice, sample in SPEC["voice_samples_uc_case_05"].items():
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
    models = [schemas.IntakeMessageIn, schemas.AnswerIn, schemas.ProposedAnswerIn, schemas.PlaceOfDeath]
    samples = {schemas.IntakeMessageIn: {"text": "x 123-45-6789"},
               schemas.AnswerIn: {"state": "answered", "value": 1, "own_words": "x 123-45-6789"},
               schemas.ProposedAnswerIn: {"field": "display_name", "value": 1, "own_words": "x 123-45-6789"},
               schemas.PlaceOfDeath: {"county_or_city": "x 123-45-6789"}}
    for model in models:
        obj: BaseModel = model(**samples[model])
        texts = [v for v in obj.__dict__.values() if isinstance(v, str) and not isinstance(v, (bytes,))
                 and not hasattr(v, "value")]
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


@pytest.mark.parametrize("text,mode", [("I can't do this", SafetyMode.acute_distress),
                                       ("I'm panicking", SafetyMode.acute_distress),
                                       ("This is too much", SafetyMode.overwhelm),
                                       ("I'm so overwhelmed", SafetyMode.overwhelm)])
def test_uc14_distress_modes(text, mode):
    assert safety.classify(text).mode == mode


def test_uc14_repeated_skips_signal_overwhelm_and_sessions_only_escalate():
    s = IntakeSession()
    for _ in range(2):
        s = safety.after_skip(s, True, 3)
    assert s.safety_mode == SafetyMode.normal
    s = safety.after_skip(s, True, 3)
    assert s.safety_mode == SafetyMode.overwhelm
    raised = IntakeSession(sensitivity="raised")
    for _ in range(2):
        raised = safety.after_skip(raised, True, 3)
    assert raised.safety_mode == SafetyMode.overwhelm  # raised sensitivity lowers the bar
    assert safety.escalate(IntakeSession(safety_mode=SafetyMode.risk_of_harm), SafetyMode.overwhelm).safety_mode \
        == SafetyMode.risk_of_harm


def test_uc14_steady_care_in_acute_distress_and_risk_of_harm():
    for mode in (SafetyMode.acute_distress, SafetyMode.risk_of_harm):
        assert safety.uses_steady_care(IntakeSession(safety_mode=mode))
    assert not safety.uses_steady_care(IntakeSession(safety_mode=SafetyMode.overwhelm))


def test_uc14_crisis_copy_makes_no_promises():
    """rules: never make promises about confidentiality or what a crisis line will do."""
    for key in ("support_988", "support_veterans_crisis_line", "risk_ack", "emergency_911", "steady_care_example",
                "safety_next"):
        assert not re.search(r"\b(confidential|anonymous|will (help|answer|listen|call you))\b", COPY[key], re.I)


# ------------------------------------------------------------------ UC-CASE-01 free text extraction

TODAY = date(2026, 9, 25)


def test_uc01_extracts_only_data_fields():
    e = extract("My mom died yesterday in Manchester, NH after a long illness. She was a Navy veteran. "
                "She had a will, it's in the safe. Her favorite color was blue and she had cancer.", TODAY)
    assert set(e.proposals) <= set(FieldKey)
    assert e.proposals[FieldKey.circumstance] == ("expected_illness_or_hospice", None)
    assert e.proposals[FieldKey.place_of_death][0] == {"state": "NH", "county_or_city": "Manchester",
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
    assert e.proposals[FieldKey.residence_state][0] == {"choice": "different", "state": "NH"}
    assert e.proposals[FieldKey.place_of_death][0]["state"] == "FL"
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
    assert SETTINGS.estate_plan_mode == "add_on"         # OPEN-DECISION-01
    assert SETTINGS.pre_need_path == "not_built"         # OPEN-DECISION-05
    for bad in ({"estate_plan_mode": "trailhead"}, {"pre_need_path": "built"}):
        with pytest.raises(RuntimeError):
            Settings(**{**SETTINGS.__dict__, **bad})
    migration = (REPO / "database" / "db" / "migrations" / "0010_case_creation.sql").read_text()
    assert "('trial_reminder_days_before', '3'" in migration   # OPEN-DECISION-02
    assert "('draft_retention_days', '28'" in migration        # DEC-07


def test_no_payment_code_in_the_api():
    """[PRIVACY] DEC-02: no payment form, field, or SDK call anywhere in case creation or Start journey."""
    # redaction.py names card_number as something it removes, so it is the one module left out.
    source = "\n".join(p.read_text() for p in (REPO / "api" / "cairn_api").rglob("*.py") if p.name != "redaction.py")
    assert not re.search(r"\b(stripe|braintree|paypal|square|adyen|storekit|revenuecat|payment_method|"
                         r"card_number|cvv|cvc|billing_address)\b", source, re.I)
    deps = (REPO / "api" / "pyproject.toml").read_text()
    assert not re.search(r"stripe|braintree|paypal|adyen|revenuecat", deps, re.I)


def test_no_document_upload():
    """DEC-06 and out_of_scope_mvp: no upload endpoint, no file field."""
    from cairn_api.main import create_app

    from .conftest import SETTINGS
    spec = json.dumps(create_app(settings=SETTINGS).openapi())
    assert "multipart/form-data" not in spec and '"format": "binary"' not in spec
    assert not any("upload" in path for path in json.loads(spec)["paths"])


def test_policy_update_is_tracked():
    assert SPEC["policy_updates_required"] == [
        "Add the 28-day draft deletion period to the retention schedule in CAIRN-POL-PRIV-01."]
    audit = (REPO / "database" / "docs" / "case-creation-gap-audit.md").read_text()
    assert "CAIRN-POL-PRIV-01" in audit


def test_spec_file_is_the_one_implemented():
    assert pathlib.Path(REPO / "database" / "docs" / "cairn-case-creation-use-cases.json").exists()
    assert SPEC["spec_version"] == "0.3.0"


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
