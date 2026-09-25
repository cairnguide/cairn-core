"""Test fixtures.

Contract tests run with no database. Integration tests need a scratch
PostgreSQL 15+ server and are skipped unless CAIRN_TEST_ADMIN_URL is set:

  CAIRN_TEST_ADMIN_URL=postgresql://admin@localhost/postgres pytest

The admin URL must be able to create databases. Never point it at real data.
"""
from __future__ import annotations

import os
import pathlib
import sys
import uuid

import pytest
from fastapi import Header
from fastapi.testclient import TestClient

from cairn_api.auth import Identity, get_identity
from cairn_api.config import Settings
from cairn_api.main import create_app
from cairn_api.schemas import SignInMethod

REPO = pathlib.Path(__file__).resolve().parents[2]
DB_DIR = REPO / "database"

SETTINGS = Settings(
    database_url="postgresql://unused", db_session_role="cairn_app", pool_min_size=1, pool_max_size=4,
    auth0_domain="cairn-test.example.test", auth0_audience="https://api.cairn.example.test",
    claim_namespace="https://cairn.invalid/", email_connection="email",
    terms_version="terms-v1", privacy_version="privacy-v1",
    privacy_policy_url="https://cairn.example.test/privacy", terms_url="https://cairn.example.test/terms",
    journey_map_url="https://cairn.example.test/first-weeks", support_url="https://cairn.example.test/support",
    ai_provider_name="Example AI Provider",
)


def fake_identity(x_test_subject: str | None = Header(default=None),
                  x_test_email: str | None = Header(default=None),
                  x_test_email_verified: str = Header(default="true"),
                  x_test_method: str = Header(default="email")) -> Identity:
    """Stands in for Auth0 token verification in tests only. test_auth.py covers the real path."""
    from cairn_api.errors import ApiError
    if not x_test_subject:
        raise ApiError(401, "not_signed_in", "Please sign in to continue.")
    return Identity(subject=x_test_subject, email=x_test_email,
                    email_verified=x_test_email_verified == "true",
                    sign_in_method=SignInMethod(x_test_method))


class NoDatabase:
    def open(self): pass
    def close(self): pass
    def session(self, *a, **k):
        raise AssertionError("contract tests must not reach the database")


@pytest.fixture
def contract_client():
    app = create_app(settings=SETTINGS, database=NoDatabase(), verifier=object())
    app.dependency_overrides[get_identity] = fake_identity
    with TestClient(app) as client:
        yield client


# ------------------------------------------------------------------ integration

def _apply_sql(conn, path: pathlib.Path) -> None:
    conn.execute(path.read_text())


@pytest.fixture(scope="session")
def scratch_db_url():
    admin_url = os.environ.get("CAIRN_TEST_ADMIN_URL")
    if not admin_url:
        pytest.skip("Set CAIRN_TEST_ADMIN_URL to run integration tests against a scratch Postgres server.")
    from urllib.parse import urlsplit

    import psycopg

    name = f"cairn_api_test_{uuid.uuid4().hex[:8]}"
    u = urlsplit(admin_url)
    url = f"{u.scheme}://{u.netloc}/{name}" + (f"?{u.query}" if u.query else "")
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            for f in sorted((DB_DIR / "db" / "migrations").glob("*.sql")):
                _apply_sql(conn, f)
            _apply_sql(conn, DB_DIR / "db" / "optional" / "context_items_jsonb.sql")
            _apply_sql(conn, DB_DIR / "db" / "optional" / "context_items_read_only.sql")
        sys.path.insert(0, str(DB_DIR / "tools"))
        import load_templates
        templates, errors = load_templates.load_and_validate(DB_DIR / "content", allow_unreviewed=True)
        journeys, journey_errors = load_templates.load_and_validate_journeys(
            DB_DIR / "content", {doc["task_key"] for _, doc in templates}, allow_unreviewed=True)
        assert not errors and not journey_errors, errors + journey_errors
        load_templates.load_into_db(templates, "api-test", url, journeys)
        yield url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def api(scratch_db_url):
    from cairn_api.db import Database
    db = Database(scratch_db_url, session_role="cairn_app", min_size=1, max_size=4)
    app = create_app(settings=SETTINGS, database=db, verifier=object())
    app.dependency_overrides[get_identity] = fake_identity
    with TestClient(app) as client:
        client.scratch_url = scratch_db_url  # owner connection, for test setup and assertions only
        yield client


def onboard(api, subject: str, method: str = "email", preferred_name: str = "Pat",
            voice: str = "choose_for_me") -> dict:
    """Walks an account through UC-REG-07 to UC-REG-12 with the real endpoints."""
    h = as_user(subject, method=method)
    r = api.get("/v1/onboarding", headers=h)
    if r.json()["account"]["onboarding_step"] == "complete":
        return r.json()
    for consent_type in ("privacy_terms", "trial_terms", "ai_notice"):
        version = r.json()["screen"]["checkbox"]["document_version"]
        r = api.post(f"/v1/onboarding/acknowledgments/{consent_type}", headers=h,
                     json={"agreed": True, "document_version": version, "client": "test/1.0"})
        assert r.status_code == 200, r.text
    r = api.put("/v1/onboarding/preferred-name", json={"preferred_name": preferred_name}, headers=h)
    assert r.status_code == 200, r.text
    r = api.put("/v1/onboarding/personality", json={"choice": voice}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def as_user(subject: str, email: str | None = None, verified: bool = True, method: str = "email") -> dict:
    return {"X-Test-Subject": subject, "X-Test-Email": email or f"{subject}@example.test",
            "X-Test-Email-Verified": "true" if verified else "false", "X-Test-Method": method}


# ------------------------------------------------------------------ case creation helpers

def register(api, subject: str, time_zone: str = "America/New_York", voice: str = "choose_for_me") -> dict:
    """Create the account and finish onboarding, so the user can start a case."""
    r = api.post("/v1/registrations", json={"time_zone": time_zone}, headers=as_user(subject))
    assert r.status_code in (200, 201), r.text
    return onboard(api, subject, voice=voice)


def new_draft(api, subject: str, **body) -> dict:
    r = api.post("/v1/cases", json=body or None, headers=as_user(subject))
    assert r.status_code == 201, r.text
    return r.json()


def answer(api, subject: str, case_id: str, field: str, value=None, state: str = "answered", **extra) -> dict:
    body = {"state": state, **extra}
    if value is not None:
        body["value"] = value
    r = api.put(f"/v1/cases/{case_id}/intake/answers/{field}", json=body, headers=as_user(subject))
    assert r.status_code == 200, r.text
    return r.json()


def start_journey(api, subject: str, case_id: str) -> dict:
    preview = api.get(f"/v1/cases/{case_id}/journey/preview", headers=as_user(subject))
    assert preview.status_code == 200, preview.text
    version = preview.json()["pre_button_notice"]["version"]
    r = api.post(f"/v1/cases/{case_id}/journey/start", json={"pre_button_notice_version": version},
                 headers=as_user(subject))
    assert r.status_code == 200, r.text
    return r.json()


def active_case(api, subject: str, answers: dict | None = None) -> tuple[str, dict]:
    """A started journey. answers maps field to value. Returns the case id and GET /journey."""
    case_id = new_draft(api, subject)["case"]["id"]
    for field, value in (answers or {"place_of_death": {"state": "NH"}}).items():
        answer(api, subject, case_id, field, value)
    start_journey(api, subject, case_id)
    r = api.get(f"/v1/cases/{case_id}/journey", headers=as_user(subject))
    assert r.status_code == 200, r.text
    return case_id, r.json()


def task_keys(journey: dict) -> set[str]:
    return {t["task_key"] for w in journey["weeks"] for t in w["tasks"]}


def find_task(journey: dict, key: str) -> dict:
    return next(t for w in journey["weeks"] for t in w["tasks"] if t["task_key"] == key)
