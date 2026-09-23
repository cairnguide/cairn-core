"""End-to-end checks for UC-1 to UC-13 against a real database with row-level security.

Skipped unless CAIRN_TEST_ADMIN_URL points at a scratch PostgreSQL 15+ server.
Fake data only (example.test addresses, obviously fake names).
"""
from datetime import date, timedelta

import pytest

from .conftest import as_user

REG = {"first_name": "Pat", "last_name": "Testperson",
       "accepted_terms_version": "terms-v1", "accepted_privacy_version": "privacy-v1"}


def register(api, subject, relationship=None):
    body = dict(REG, relationship_to_deceased=relationship) if relationship else REG
    r = api.post("/v1/registrations", json=body, headers=as_user(subject))
    assert r.status_code in (200, 201), r.text
    return r.json()


def new_case(api, subject, relationship="spouse", **extra):
    body = {"relationship": relationship,
            "deceased": {"legal_first_name": "Dan", "legal_last_name": "Fakerson",
                         "date_of_birth": "1950-01-01", "domicile_state": "NH"}, **extra}
    r = api.post("/v1/cases", json=body, headers=as_user(subject))
    assert r.status_code == 201, r.text
    return r.json()


def ready_case(api, subject, relationship="child"):
    case = new_case(api, subject, relationship)
    cid = case["case"]["id"]
    r = api.put(f"/v1/cases/{cid}/death-event", headers=as_user(subject),
                json={"date_of_death": (date.today() - timedelta(days=2)).isoformat(),
                      "death_state": "NH", "place_type": "hospital"})
    assert r.status_code == 200, r.text
    r = api.post(f"/v1/cases/{cid}/journey", headers=as_user(subject))
    assert r.status_code == 200, r.text
    return cid, r.json()


def find_task(journey, key):
    return next(t for w in journey["weeks"] for t in w["tasks"] if t["task_key"] == key)


# ------------------------------------------------------------------ registration

def test_uc1_spouse_registers_and_is_acknowledged_first(api):
    body = register(api, "uc1-spouse", "spouse")
    assert body["notes"][0]["kind"] == "acknowledgment"
    assert "sorry for your loss" in body["notes"][0]["text"]
    assert body["next_step"]["action"] == "start_case"
    assert {c["purpose"] for c in body["consents"]} == {"terms", "privacy"}
    assert body["language_profile"] == "family"


def test_uc1_requires_confirmed_email_and_current_terms(api):
    r = api.post("/v1/registrations", json=REG, headers=as_user("uc1-unverified", verified=False))
    assert r.status_code == 403 and r.json()["code"] == "email_not_verified"
    r = api.post("/v1/registrations", json=dict(REG, accepted_terms_version="old"), headers=as_user("uc1-old"))
    assert r.status_code == 422 and r.json()["code"] == "consent_required"


def test_registration_is_idempotent(api):
    register(api, "uc1-repeat")
    r = api.post("/v1/registrations", json=REG, headers=as_user("uc1-repeat"))
    assert r.status_code == 200
    assert len(r.json()["consents"]) == 2


def test_uc2_child_is_asked_new_or_existing(api):
    step = register(api, "uc2-child", "child")["next_step"]
    assert step["action"] == "choose_case_start"
    join = next(o for o in step["options"] if o["value"] == "join_existing_case")
    assert join["available"] is False


def test_uc3_poa_is_told_authority_ends_at_death(api):
    body = register(api, "uc3-poa", "power_of_attorney")
    legal = [n for n in body["notes"] if n["kind"] == "legal"]
    assert legal and legal[0]["legal_review_required"]
    assert body["next_step"]["action"] == "confirm_current_role"


def test_uc4_fiduciary_gets_professional_language(api):
    body = register(api, "uc4-fid", "fiduciary")
    assert body["language_profile"] == "professional"
    assert body["next_step"]["action"] == "confirm_fiduciary"
    assert "sorry" not in body["notes"][0]["text"]


# ------------------------------------------------------------------ case creation

def test_uc5_case_created_with_owner_and_identity(api):
    register(api, "uc5")
    body = new_case(api, "uc5")
    assert body["case"]["relationship"] == "spouse"
    assert body["deceased"]["veteran_status"] == "unknown"
    assert body["deceased"]["has_will"] == "unknown"
    assert body["intake"]["missing_required"] == ["date_of_death", "death_state"]
    assert body["next_step"]["action"] == "record_death_event"


def test_uc5_unregistered_user_cannot_create_case(api):
    r = api.post("/v1/cases", headers=as_user("nobody"),
                 json={"relationship": "spouse", "deceased": {"legal_first_name": "A", "legal_last_name": "B"}})
    assert r.status_code == 403 and r.json()["code"] == "registration_required"


def test_uc5_other_users_cannot_see_or_edit_the_case(api):
    register(api, "uc5-owner")
    register(api, "uc5-stranger")
    cid = new_case(api, "uc5-owner")["case"]["id"]
    stranger = as_user("uc5-stranger")
    assert api.get(f"/v1/cases/{cid}", headers=stranger).status_code == 403
    r = api.patch(f"/v1/cases/{cid}/deceased", json={"legal_first_name": "Hacked"}, headers=stranger)
    assert r.status_code == 403
    r = api.put(f"/v1/cases/{cid}/death-event", headers=stranger,
                json={"date_of_death": "2026-01-01", "death_state": "CA"})
    assert r.status_code == 403
    assert api.get(f"/v1/cases/{cid}", headers=as_user("uc5-owner")).json()["deceased"]["legal_first_name"] == "Dan"


def test_uc6_death_event_recorded_on_same_row(api):
    register(api, "uc6")
    cid = new_case(api, "uc6", "child")["case"]["id"]
    r = api.put(f"/v1/cases/{cid}/death-event", headers=as_user("uc6"),
                json={"date_of_death": "2026-09-01", "death_state": "nh", "place_type": "hospice",
                      "county": "Fake County"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deceased"]["death_state"] == "NH"
    assert body["intake"]["ready_for_journey"] is True
    assert body["next_step"]["action"] == "answer_estate_questions"


def test_uc6_death_before_birth_gets_plain_language(api):
    register(api, "uc6b")
    cid = new_case(api, "uc6b")["case"]["id"]
    r = api.put(f"/v1/cases/{cid}/death-event", headers=as_user("uc6b"),
                json={"date_of_death": "1949-12-31", "death_state": "NH"})
    assert r.status_code == 422
    assert "earlier than the date of birth" in r.json()["detail"]
    # The database constraint catches the reverse edit too.
    api.put(f"/v1/cases/{cid}/death-event", headers=as_user("uc6b"),
            json={"date_of_death": "2026-01-01", "death_state": "NH"})
    r = api.patch(f"/v1/cases/{cid}/deceased", headers=as_user("uc6b"), json={"date_of_birth": "2026-02-01"})
    assert r.status_code == 422 and r.json().get("constraint") == "death_not_before_birth"


def test_uc7_estate_flags_one_at_a_time_with_skip(api):
    register(api, "uc7")
    cid = new_case(api, "uc7", "power_of_attorney")["case"]["id"]
    r = api.patch(f"/v1/cases/{cid}/estate-flags", json={"veteran_status": "skip"}, headers=as_user("uc7"))
    body = r.json()
    assert body["deceased"]["veteran_status"] == "unknown"
    assert any("fine answer" in n["text"] for n in body["notes"])
    assert body["next_step"]["action"] == "answer_has_will"
    r = api.patch(f"/v1/cases/{cid}/estate-flags", json={"has_will": "yes"}, headers=as_user("uc7"))
    body = r.json()
    assert body["deceased"]["has_will"] == "yes"
    assert body["deceased"]["veteran_status"] == "unknown"  # untouched by the second answer
    assert any("where the will is kept" in n["text"] for n in body["notes"])


def test_uc8_fiduciary_composed_session(api):
    register(api, "uc8", "fiduciary")
    body = new_case(api, "uc8", "fiduciary",
                    death_event={"date_of_death": "2026-09-10", "death_state": "NH"},
                    estate_flags={"veteran_status": "yes", "has_will": "unknown"},
                    start_journey=True)
    assert body["case"]["relationship"] == "fiduciary"
    assert body["journey_task_count"] > 0
    assert body["next_step"]["action"] == "view_journey"
    assert any("filled in later" in n["text"] for n in body["notes"])


def test_uc8_partial_save_then_journey_refused_until_ready(api):
    register(api, "uc8b", "fiduciary")
    cid = new_case(api, "uc8b", "fiduciary", start_journey=True)["case"]["id"]
    r = api.post(f"/v1/cases/{cid}/journey", headers=as_user("uc8b"))
    assert r.status_code == 409
    assert set(r.json()["missing_required"]) == {"date_of_death", "death_state"}


# ------------------------------------------------------------------ journey

def test_uc9_journey_puts_one_week_one_action_in_front(api):
    register(api, "uc9")
    _, journey = ready_case(api, "uc9", "spouse")
    assert journey["mode"] == "tasks"
    assert [w["week"] for w in journey["weeks"]] == [1, 2, 3, 4]
    assert journey["next_action"]["task_key"] == "order_death_certificates"
    week1 = [t["task_key"] for t in journey["weeks"][0]["tasks"]]
    assert week1[:2] == ["order_death_certificates", "choose_funeral_provider"]


def test_uc10_order_death_certificates(api):
    register(api, "uc10")
    cid, journey = ready_case(api, "uc10")
    task = find_task(journey, "order_death_certificates")
    detail = api.get(f"/v1/cases/{cid}/tasks/{task['id']}", headers=as_user("uc10")).json()
    assert detail["task"]["citations"][0]["url"].startswith("https://")
    assert detail["task"]["death_state"] == "NH"
    arrival = (date.today() + timedelta(days=10)).isoformat()
    r = api.post(f"/v1/cases/{cid}/tasks/{task['id']}/certificate-order", headers=as_user("uc10"),
                 json={"copies_requested": 8, "expected_by": arrival})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task"]["status"] == "done"
    assert body["task"]["certificate_order"]["copies_requested"] == 8
    assert body["task"]["certificate_order"]["expected_by"] == arrival
    assert body["next_action"]["task_key"] != "order_death_certificates"


def test_uc10_wrong_task_kind_is_refused(api):
    register(api, "uc10b")
    cid, journey = ready_case(api, "uc10b")
    task = find_task(journey, "choose_funeral_provider")
    r = api.post(f"/v1/cases/{cid}/tasks/{task['id']}/certificate-order", headers=as_user("uc10b"),
                 json={"copies_requested": 2})
    assert r.status_code == 409


def test_uc11_notify_bank_and_move_on(api):
    register(api, "uc11")
    cid, journey = ready_case(api, "uc11", "power_of_attorney")
    task = find_task(journey, "notify_banks")
    r = api.post(f"/v1/cases/{cid}/tasks/{task['id']}/institution-notices", headers=as_user("uc11"),
                 json={"institution_name": "First Example Bank", "method": "phone"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task"]["status"] == "done"
    assert body["task"]["institution_notices"][0]["institution_name"] == "First Example Bank"
    assert body["next_action"]["id"] != task["id"]
    # The same bank again updates rather than duplicates.
    api.post(f"/v1/cases/{cid}/tasks/{task['id']}/institution-notices", headers=as_user("uc11"),
             json={"institution_name": "first example bank", "method": "in_person"})
    status = api.get(f"/v1/cases/{cid}/status", headers=as_user("uc11")).json()
    assert len(status["institution_notices"]) == 1


def test_uc12_pause_holds_progress_and_resume_restores_it(api):
    register(api, "uc12")
    cid, journey = ready_case(api, "uc12")
    before = journey["next_action"]["id"]
    r = api.post(f"/v1/cases/{cid}/journey/pause", headers=as_user("uc12"), json={"pause_days": 7})
    paused = r.json()
    assert paused["mode"] == "paused" and paused["weeks"] == [] and paused["next_action"] is None
    assert paused["check_in"]["message"]
    assert api.get(f"/v1/cases/{cid}/journey", headers=as_user("uc12")).json()["mode"] == "paused"
    resumed = api.post(f"/v1/cases/{cid}/journey/resume", headers=as_user("uc12")).json()
    assert resumed["mode"] == "tasks"
    assert resumed["next_action"]["id"] == before


def test_uc13_status_across_the_case(api):
    register(api, "uc13", "fiduciary")
    cid, journey = ready_case(api, "uc13", "fiduciary")
    certs = find_task(journey, "order_death_certificates")
    api.post(f"/v1/cases/{cid}/tasks/{certs['id']}/certificate-order", headers=as_user("uc13"),
             json={"copies_requested": 5})
    funeral = find_task(journey, "choose_funeral_provider")
    api.patch(f"/v1/cases/{cid}/tasks/{funeral['id']}", headers=as_user("uc13"), json={"status": "in_progress"})
    body = api.get(f"/v1/cases/{cid}/status", headers=as_user("uc13")).json()
    cats = {c["category"]: c for c in body["categories"]}
    assert [t["task_key"] for t in cats["certificates"]["done"]] == ["order_death_certificates"]
    assert [t["task_key"] for t in cats["funeral"]["in_progress"]] == ["choose_funeral_provider"]
    assert "financial_institutions" in cats and "agencies" in cats
    assert body["counts"]["done"] == 1 and body["counts"]["in_progress"] == 1
    assert body["certificate_order"]["copies_requested"] == 5
    # In-progress work comes before starting something new.
    assert body["next_action"]["task_key"] == "choose_funeral_provider"


def test_late_veteran_answer_adds_va_task_without_losing_progress(api):
    register(api, "late-vet")
    cid, journey = ready_case(api, "late-vet")
    api.patch(f"/v1/cases/{cid}/estate-flags", json={"veteran_status": "no"}, headers=as_user("late-vet"))
    # generate_case_tasks never removes tasks, so the VA task from 'unknown' stays.
    keys = {t["task_key"] for w in api.get(f"/v1/cases/{cid}/journey", headers=as_user("late-vet")).json()["weeks"]
            for t in w["tasks"]}
    assert "notify_va_if_veteran" in keys


@pytest.mark.parametrize("path,method,body", [
    ("/journey", "post", None),
    ("/journey/pause", "post", {}),
    ("/status", "get", None),
])
def test_journey_endpoints_deny_non_members(api, path, method, body):
    register(api, "deny-owner")
    register(api, "deny-other")
    cid, _ = ready_case(api, "deny-owner")
    r = getattr(api, method)(f"/v1/cases/{cid}{path}", headers=as_user("deny-other"),
                             **({"json": body} if body is not None else {}))
    assert r.status_code == 403


# ------------------------------------------------------------------ sign-in methods

@pytest.mark.parametrize("method,subject", [
    ("google", "google-oauth2|uc1-g"), ("apple", "apple|001.uc1a"), ("email", "auth0|uc1-e")])
def test_account_created_with_each_method(api, method, subject):
    r = api.post("/v1/registrations", json=REG, headers=as_user(subject, method=method))
    assert r.status_code == 201, r.text
    assert r.json()["user"]["sign_in_method"] == method
    me = api.get("/v1/me", headers=as_user(subject, method=method)).json()
    assert me["sign_in_method"] == method


def test_same_email_other_method_is_told_which_to_use(api):
    email = "shared-address@example.test"
    r = api.post("/v1/registrations", json=REG, headers=as_user("google-oauth2|dup", email, method="google"))
    assert r.status_code == 201
    for subject, method in (("apple|dup", "apple"), ("auth0|dup", "email")):
        r = api.post("/v1/registrations", json=REG, headers=as_user(subject, email.upper(), method=method))
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "account_exists"
        assert r.json()["sign_in_method"] == "google"
        assert "sign in with Google" in r.json()["detail"]
    # The original method still signs in normally.
    r = api.post("/v1/registrations", json=REG, headers=as_user("google-oauth2|dup", email, method="google"))
    assert r.status_code == 200


def test_email_signup_waits_for_confirmation(api):
    r = api.post("/v1/registrations", json=REG, headers=as_user("auth0|unconfirmed", verified=False))
    assert r.status_code == 403 and r.json()["code"] == "email_not_verified"
    r = api.post("/v1/registrations", json=REG, headers=as_user("auth0|unconfirmed"))
    assert r.status_code == 201
