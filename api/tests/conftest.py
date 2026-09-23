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
from cairn_api.schemas import SignInMethod
from cairn_api.config import Settings
from cairn_api.main import create_app

REPO = pathlib.Path(__file__).resolve().parents[2]
DB_DIR = REPO / "database"

SETTINGS = Settings(
    database_url="postgresql://unused", db_session_role="cairn_app", pool_min_size=1, pool_max_size=4,
    auth0_domain="cairn-test.example.test", auth0_audience="https://api.cairn.example.test",
    claim_namespace="https://cairn.invalid/", email_connection="Username-Password-Authentication",
    terms_version="terms-v1", privacy_version="privacy-v1",
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
    import psycopg
    from urllib.parse import urlsplit

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
        sys.path.insert(0, str(DB_DIR / "tools"))
        import load_templates
        templates, errors = load_templates.load_and_validate(DB_DIR / "content", allow_unreviewed=True)
        assert not errors, errors
        load_templates.load_into_db(templates, "api-test", url)
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
        yield client


def as_user(subject: str, email: str | None = None, verified: bool = True, method: str = "email") -> dict:
    return {"X-Test-Subject": subject, "X-Test-Email": email or f"{subject}@example.test",
            "X-Test-Email-Verified": "true" if verified else "false", "X-Test-Method": method}
