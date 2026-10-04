"""Request validation and error shape. No database needed."""
import json
from datetime import date, timedelta

from cairn_api.main import create_app
from cairn_api.schemas import AnswerIn, DateOfDeath, PlaceOfDeath

from .conftest import SETTINGS, as_user

CASE = "/v1/cases"
SECRET_NAME = "Zebulon-Fakename"
ANY_CASE = f"{CASE}/00000000-0000-0000-0000-000000000000"


def test_openapi_contract_is_generated():
    spec = create_app(settings=SETTINGS).openapi()
    assert spec["openapi"].startswith("3.")
    for path in ("/v1/registrations", "/v1/cases", "/v1/cases/{case_id}",
                 "/v1/cases/{case_id}/intake/answers/{field}", "/v1/cases/{case_id}/intake/messages",
                 "/v1/cases/{case_id}/intake/confirmations", "/v1/cases/{case_id}/intake/pause",
                 "/v1/cases/{case_id}/review", "/v1/cases/{case_id}/journey/preview",
                 "/v1/cases/{case_id}/journey/start", "/v1/cases/{case_id}/journey/first-task",
                 "/v1/cases/{case_id}/journey", "/v1/cases/{case_id}/journey/pause", "/v1/cases/{case_id}/status",
                 "/v1/cases/{case_id}/tasks/{task_id}/certificate-order",
                 "/v1/cases/{case_id}/tasks/{task_id}/institution-notices"):
        assert path in spec["paths"], path


def test_missing_identity_is_401_problem_json(contract_client):
    r = contract_client.post(CASE, json={})
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "not_signed_in"


def test_validation_errors_never_echo_input(contract_client):
    r = contract_client.put(f"{ANY_CASE}/intake/answers/display_name", headers=as_user("u1"),
                            json={"state": "answered", "value": SECRET_NAME, "own_words": SECRET_NAME * 20})
    assert r.status_code == 422
    assert r.json()["code"] == "validation_failed"
    assert SECRET_NAME not in r.text
    assert "own_words" in {e["field"] for e in r.json()["errors"]}


def test_case_creation_refuses_never_collect_fields(contract_client):
    """never_collect_at_case_creation: there is no field for any of these, so they are refused, not dropped."""
    for field in ("legal_first_name", "full_legal_name", "date_of_birth", "ssn", "cause_of_death",
                  "medical_history", "card_number"):
        r = contract_client.post(CASE, json={field: "123-45-6789"}, headers=as_user("u1"))
        assert r.status_code == 422, field
        assert "123-45-6789" not in r.text


def test_unknown_intake_fields_are_rejected(contract_client):
    r = contract_client.put(f"{ANY_CASE}/intake/answers/cause_of_death", headers=as_user("u1"),
                            json={"state": "answered", "value": "not collected"})
    assert r.status_code == 422
    assert "not collected" not in r.text
    r = contract_client.post(f"{ANY_CASE}/intake/confirmations", headers=as_user("u1"),
                             json={"answers": [{"field": "cause_of_death", "value": "not collected"}]})
    assert r.status_code == 422
    assert "not collected" not in r.text


def test_pause_has_no_reason_field(contract_client):
    r = contract_client.post(f"{CASE}/00000000-0000-0000-0000-000000000000/journey/pause",
                             json={"pause_days": 7, "reason": "overwhelmed"}, headers=as_user("u1"))
    assert r.status_code == 422
    assert "overwhelmed" not in r.text


def test_state_codes_are_normalized_and_checked():
    """UC-CASE-04: the 50 states, DC, and the five permanently inhabited territories."""
    assert PlaceOfDeath(jurisdiction=" nh ").jurisdiction == "NH"
    for code in ("DC", "PR", "GU", "VI", "AS", "MP"):
        assert PlaceOfDeath(jurisdiction=code).jurisdiction == code
    for bad in ("ZZ", "New Hampshire", "N1", "FM"):
        try:
            PlaceOfDeath(jurisdiction=bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} accepted")


def test_date_of_death_accepts_approximate_dates_and_refuses_the_future():
    """UC-CASE-04: today, this_week, and unknown are accepted without a date."""
    for precision in ("today", "this_week", "unknown"):
        assert DateOfDeath(precision=precision).date is None
    assert DateOfDeath(precision="exact", date=date.today()).date == date.today()
    for bad in ({"precision": "exact"}, {"precision": "exact", "date": (date.today() + timedelta(days=1))},
                {"precision": "this_week", "date": date.today()}):
        try:
            DateOfDeath(**bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} accepted")


def test_every_answer_can_be_skipped_or_unsure_without_a_value():
    assert AnswerIn(state="skipped").value is None
    assert AnswerIn(state="unsure").value is None
    for bad in ({"state": "answered"}, {"state": "skipped", "value": "yes"}, {"state": "maybe"}):
        try:
            AnswerIn(**bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} accepted")


def test_user_copy_follows_house_style():
    """database/CLAUDE.md: no em dashes and no semicolons within a sentence."""
    from cairn_api import messages
    texts = []
    for name, value in vars(messages).items():
        if name.startswith("__"):
            continue
        if isinstance(value, str):
            texts.append(value)
        elif hasattr(value, "model_dump") and not isinstance(value, type):
            texts.append(json.dumps(value.model_dump()))
        elif isinstance(value, (list, dict)):
            texts.append(json.dumps(value, default=lambda o: o.model_dump()))
    for rel in (None, "spouse", "child", "power_of_attorney", "fiduciary"):
        texts.append(json.dumps(messages.registration_next_step(rel and messages.Relationship(rel)).model_dump()))
    for t in texts:
        assert "—" not in t and ";" not in t, t
