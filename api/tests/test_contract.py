"""Request validation and error shape. No database needed."""
import json
from datetime import date, timedelta

from cairn_api.main import create_app
from cairn_api.schemas import CreateCaseRequest, DeathEventIn, EstateFlagsIn

from .conftest import SETTINGS, as_user

CASE = "/v1/cases"
SECRET_NAME = "Zebulon-Fakename"


def test_openapi_contract_is_generated():
    spec = create_app(settings=SETTINGS).openapi()
    assert spec["openapi"].startswith("3.")
    for path in ("/v1/registrations", "/v1/cases", "/v1/cases/{case_id}/death-event",
                 "/v1/cases/{case_id}/estate-flags", "/v1/cases/{case_id}/journey",
                 "/v1/cases/{case_id}/journey/pause", "/v1/cases/{case_id}/status",
                 "/v1/cases/{case_id}/tasks/{task_id}/certificate-order",
                 "/v1/cases/{case_id}/tasks/{task_id}/institution-notices"):
        assert path in spec["paths"], path


def test_missing_identity_is_401_problem_json(contract_client):
    r = contract_client.post(CASE, json={})
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "not_signed_in"


def test_validation_errors_never_echo_input(contract_client):
    body = {"relationship": "spouse",
            "deceased": {"legal_first_name": SECRET_NAME, "legal_last_name": "",
                         "date_of_birth": (date.today() + timedelta(days=1)).isoformat()}}
    r = contract_client.post(CASE, json=body, headers=as_user("u1"))
    assert r.status_code == 422
    assert r.json()["code"] == "validation_failed"
    assert SECRET_NAME not in r.text
    fields = {e["field"] for e in r.json()["errors"]}
    assert {"deceased.legal_last_name", "deceased.date_of_birth"} <= fields


def test_unknown_fields_are_rejected(contract_client):
    body = {"relationship": "spouse", "deceased": {"legal_first_name": "A", "legal_last_name": "B",
                                                   "cause_of_death": "not collected"}}
    r = contract_client.post(CASE, json=body, headers=as_user("u1"))
    assert r.status_code == 422
    assert "not collected" not in r.text


def test_pause_has_no_reason_field(contract_client):
    r = contract_client.post(f"{CASE}/00000000-0000-0000-0000-000000000000/journey/pause",
                             json={"pause_days": 7, "reason": "overwhelmed"}, headers=as_user("u1"))
    assert r.status_code == 422
    assert "overwhelmed" not in r.text


def test_state_codes_are_normalized_and_checked():
    e = DeathEventIn(date_of_death=date.today(), death_state=" nh ")
    assert e.death_state == "NH"
    for bad in ("ZZ", "New Hampshire", "N1"):
        try:
            DeathEventIn(date_of_death=date.today(), death_state=bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} accepted")


def test_death_before_birth_rejected_in_composed_request():
    try:
        CreateCaseRequest(relationship="fiduciary",
                          deceased={"legal_first_name": "A", "legal_last_name": "B", "date_of_birth": "2000-01-02"},
                          death_event={"date_of_death": "2000-01-01", "death_state": "NH"})
    except ValueError as exc:
        assert "earlier than the date of birth" in str(exc)
    else:
        raise AssertionError("accepted death before birth")


def test_estate_flags_need_an_answer_and_accept_skip():
    assert EstateFlagsIn(veteran_status="skip").veteran_status.value == "skip"
    try:
        EstateFlagsIn()
    except ValueError:
        pass
    else:
        raise AssertionError("empty estate flags accepted")
    try:
        EstateFlagsIn(has_will="maybe")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid tri-state accepted")


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
