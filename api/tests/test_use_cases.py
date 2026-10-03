"""End-to-end checks for UC-10 to UC-13 against a real MongoDB database, as the cairnApp user.

Case creation (UC-CASE-01 to UC-CASE-18) replaced UC-5 to UC-9 and is in
test_case_creation.py. Registration and onboarding (UC-REG-01 to UC-REG-14,
UC-ACCT-01) are in test_registration_onboarding.py.

Skipped unless CAIRN_TEST_MONGODB_URI points at a scratch MongoDB replica set.
Fake data only (example.test addresses, obviously fake names).
"""
from datetime import date, timedelta

import pytest

from .conftest import active_case, answer, as_user, find_task, register, task_keys


def ready_case(api, subject, relationship=None):
    """A started journey with the place of death in NH. relationship is kept for readability of old tests."""
    return active_case(api, subject, {"place_of_death": {"state": "NH"},
                                      "date_of_death": {"precision": "exact",
                                                        "date": (date.today() - timedelta(days=2)).isoformat()}})


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
    register(api, "uc13")
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
    cid, journey = active_case(api, "late-vet", {"place_of_death": {"state": "NH"}, "veteran_status": "no"})
    assert "notify_va_if_veteran" not in task_keys(journey)
    funeral = find_task(journey, "choose_funeral_provider")
    api.patch(f"/v1/cases/{cid}/tasks/{funeral['id']}", headers=as_user("late-vet"), json={"status": "in_progress"})
    answer(api, "late-vet", cid, "veteran_status", "yes")
    after = api.get(f"/v1/cases/{cid}/journey", headers=as_user("late-vet")).json()
    assert "notify_va_if_veteran" in task_keys(after)
    assert find_task(after, "choose_funeral_provider")["status"] == "in_progress"


@pytest.mark.parametrize("path,method,body", [
    ("/journey/preview", "get", None),
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
