"""Test fixtures.

Contract tests run with no database. Integration tests need a scratch MongoDB
replica set (7.0 or newer) and are skipped unless CAIRN_TEST_MONGODB_URI is set:

  CAIRN_TEST_MONGODB_URI='mongodb://admin:pw@localhost:27017/?replicaSet=rs0' pytest

The URI must belong to an administrator that can create databases, roles, and
users. Each test session gets its own database, which is dropped at the end,
with its users and roles. The API connects as a user that holds only the
cairnApp role, exactly as in production. Never point it at real data.
"""
from __future__ import annotations

import os
import pathlib
import sys
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import Header
from fastapi.testclient import TestClient

from cairn_api.auth import Identity, verified_identity
from cairn_api.config import Settings
from cairn_api.main import create_app
from cairn_api.schemas import SignInMethod

REPO = pathlib.Path(__file__).resolve().parents[2]
DB_DIR = REPO / "database"

SETTINGS = Settings(  # app_url and the Stripe values are fake, for the mocked Stripe client
    mongodb_uri="mongodb://unused", mongodb_db="unused", pool_min_size=1, pool_max_size=4,
    auth0_domain="cairn-test.example.test", auth0_audience="https://api.cairn.example.test",
    claim_namespace="https://cairn.invalid/", email_connection="email",
    terms_version="terms-v1", privacy_version="privacy-v1",
    privacy_policy_url="https://cairn.example.test/privacy", terms_url="https://cairn.example.test/terms",
    journey_map_url="https://cairn.example.test/first-weeks", support_url="https://cairn.example.test/support",
    ai_provider_name="Example AI Provider",
    stripe_webhook_secret="whsec_test_secret_for_tests_only", stripe_product_id="prod_TestCairn",
    app_url="https://app.cairn.example.test", vapid_public_key="BTestPublicKeyForTestsOnly",
)


def fake_identity(x_test_subject: str | None = Header(default=None),
                  x_test_email: str | None = Header(default=None),
                  x_test_email_verified: str = Header(default="true"),
                  x_test_method: str = Header(default="email"),
                  x_test_issued_at: str | None = Header(default=None)) -> Identity:
    """Stands in for Auth0 token verification in tests only. test_auth.py covers the real path. The session rules
    (D-20) still run, in get_identity. X-Test-Issued-At (ISO 8601) is the token's issue time."""
    from cairn_api.errors import ApiError
    if not x_test_subject:
        raise ApiError(401, "not_signed_in", "Please sign in to continue.")
    return Identity(subject=x_test_subject, email=x_test_email,
                    email_verified=x_test_email_verified == "true",
                    sign_in_method=SignInMethod(x_test_method),
                    issued_at=datetime.fromisoformat(x_test_issued_at) if x_test_issued_at else None)


class FakeStripe:
    """A mocked Stripe client (subscription spec test_scope). Records every request Cairn makes, and answers with
    the subscription state a test sets. Tests check Cairn's state and requests, never money or email (SUB-D-09)."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.subscriptions: dict[str, dict] = {}
        self.events: dict[str, dict] = {}
        self.fail: set[str] = set()

    def _call(self, name: str, **kw):
        from cairn_api.stripe_client import StripeError
        self.calls.append((name, kw))
        if name in self.fail:
            raise StripeError("api_connection_error")

    def create_checkout_session(self, **kw):
        self._call("create_checkout_session", **kw)
        sid = f"cs_test_{uuid.uuid4().hex[:12]}"
        return {"id": sid, "url": f"https://checkout.stripe.test/{sid}"}

    def expire_checkout_session(self, session_id):
        self._call("expire_checkout_session", session_id=session_id)

    def create_portal_session(self, customer_id, return_url, *, update_payment):
        self._call("create_portal_session", customer_id=customer_id, return_url=return_url,
                   update_payment=update_payment)
        return {"url": "https://billing.stripe.test/session"}

    def retrieve_subscription(self, subscription_id):
        self._call("retrieve_subscription", subscription_id=subscription_id)
        return self.subscriptions[subscription_id]

    def set_cancel_at_period_end(self, subscription_id, cancel):
        self._call("set_cancel_at_period_end", subscription_id=subscription_id, cancel=cancel)
        self.subscriptions.setdefault(subscription_id, {"id": subscription_id})["cancel_at_period_end"] = cancel
        return self.subscriptions[subscription_id]

    def cancel_now(self, subscription_id):
        self._call("cancel_now", subscription_id=subscription_id)
        return {"id": subscription_id, "status": "canceled"}

    def set_trial_end(self, subscription_id, when):
        self._call("set_trial_end", subscription_id=subscription_id, when=when)
        self.subscriptions[subscription_id]["trial_end"] = int(when.timestamp())
        return self.subscriptions[subscription_id]

    def latest_invoice_url(self, subscription_id):
        self._call("latest_invoice_url", subscription_id=subscription_id)
        return "https://invoice.stripe.test/i/1"

    def retrieve_event(self, event_id):
        self._call("retrieve_event", event_id=event_id)
        return self.events[event_id]

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


class NoDatabase:
    def open(self): pass
    def close(self): pass
    def session(self, *a, **k):
        raise AssertionError("contract tests must not reach the database")


@pytest.fixture
def contract_client():
    app = create_app(settings=SETTINGS, database=NoDatabase(), verifier=object())
    app.dependency_overrides[verified_identity] = fake_identity
    with TestClient(app) as client:
        yield client


# ------------------------------------------------------------------ integration

def _import_database_tools():
    for folder in (DB_DIR / "db", DB_DIR / "tools"):
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
    import apply
    import create_login_user
    import load_templates
    return apply, create_login_user, load_templates


class Scratch:
    """A scratch Cairn database: the admin handle for setup and assertions, and each role's login URI."""

    def __init__(self, admin_uri: str):
        from pymongo import MongoClient
        self.name = f"cairn_api_test_{uuid.uuid4().hex[:8]}"
        self.admin_client = MongoClient(admin_uri, uuidRepresentation="standard", tz_aware=True)
        self.db = self.admin_client[self.name]
        self.admin_uri = admin_uri
        self.uris: dict[str, str] = {}

    def login(self, role: str) -> str:
        _, create_login_user, _ = _import_database_tools()
        user, password = f"{role}_{uuid.uuid4().hex[:6]}", uuid.uuid4().hex
        self.db.command("createUser", user, pwd=password, roles=[role])
        self.uris[role] = create_login_user.user_uri(self.admin_uri, user, password, self.name)
        return self.uris[role]

    def drop(self):
        self.db.command("dropAllUsersFromDatabase")
        self.db.command("dropAllRolesFromDatabase")
        self.admin_client.drop_database(self.name)
        self.admin_client.close()


@pytest.fixture(scope="session")
def scratch_db():
    admin_uri = os.environ.get("CAIRN_TEST_MONGODB_URI")
    if not admin_uri:
        pytest.skip("Set CAIRN_TEST_MONGODB_URI to run integration tests against a scratch MongoDB replica set.")
    apply, _, load_templates = _import_database_tools()
    scratch = Scratch(admin_uri)
    try:
        apply.apply(scratch.db, log=lambda _: None)
        for role in ("cairnApp", "cairnJobs", "cairnLoader"):
            scratch.login(role)
        templates, errors = load_templates.load_and_validate(DB_DIR / "content", allow_unreviewed=True)
        journeys, journey_errors = load_templates.load_and_validate_journeys(
            DB_DIR / "content", {doc["task_key"] for _, doc in templates}, allow_unreviewed=True)
        assert not errors and not journey_errors, errors + journey_errors
        load_templates.load_into_db(templates, "api-test", scratch.uris["cairnLoader"], journeys, scratch.name)
        yield scratch
    finally:
        scratch.drop()


@pytest.fixture(scope="session")
def api(scratch_db):
    from cairn_api.db import Database, client_for
    db = Database(scratch_db.uris["cairnApp"], scratch_db.name, min_size=1, max_size=4)
    stripe = FakeStripe()
    app = create_app(settings=SETTINGS, database=db, verifier=object(), stripe=stripe)
    app.dependency_overrides[verified_identity] = fake_identity
    jobs_client = client_for(scratch_db.uris["cairnJobs"], app_name="cairn-test-jobs")
    with TestClient(app) as client:
        client.db = scratch_db.db                     # administrator, for test setup and assertions only
        client.jobs_db = jobs_client[scratch_db.name]  # the cairnJobs user, as the jobs container connects
        client.scratch = scratch_db
        client.stripe = stripe
        yield client
    jobs_client.close()


def user_id(api, subject: str):
    return api.db.users.find_one({"idp_subject": subject})["_id"]


def expire_trial(api, subject: str, days: int = 29) -> None:
    """Moves an account's trial into the past. Only a test does this: the API never rewrites the trial."""
    u = api.db.users.find_one({"idp_subject": subject})
    shift = timedelta(days=days)
    api.db.users.update_one({"_id": u["_id"]}, {"$set": {"trial_started_at": u["trial_started_at"] - shift,
                                                          "trial_ends_at": u["trial_ends_at"] - shift}})


def onboard(api, subject: str, method: str = "email", preferred_name: str = "Pat",
            voice: str = "choose_for_me", email: bool = True, frequency: str = "due_only") -> dict:
    """Walks an account through UC-REG-06 to UC-REG-16 with the real endpoints."""
    h = as_user(subject, method=method)
    r = api.get("/v1/onboarding", headers=h)
    if r.json()["account"]["onboarding_step"] == "complete":
        return r.json()
    r = api.post("/v1/onboarding/adult", json={"answer": "yes"}, headers=h)
    assert r.status_code == 200, r.text
    for consent_type in ("privacy_terms", "trial_terms", "ai_notice"):
        version = r.json()["screen"]["checkbox"]["document_version"]
        r = api.post(f"/v1/onboarding/acknowledgments/{consent_type}", headers=h,
                     json={"agreed": True, "document_version": version, "client": "test/1.0"})
        assert r.status_code == 200, r.text
    r = api.put("/v1/onboarding/preferred-name", json={"preferred_name": preferred_name}, headers=h)
    assert r.status_code == 200, r.text
    r = api.put("/v1/onboarding/personality", json={"choice": voice}, headers=h)
    assert r.status_code == 200, r.text
    r = api.put("/v1/onboarding/notification-channels", json={"email": email}, headers=h)
    assert r.status_code == 200, r.text
    r = api.put("/v1/onboarding/notification-frequency", json={"frequency": frequency}, headers=h)
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
    api.put(f"/v1/cases/{case_id}/keep-in-touch", json={"skip": True}, headers=as_user(subject))
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
    for field, value in (answers or {"place_of_death": {"jurisdiction": "NH"}}).items():
        answer(api, subject, case_id, field, value)
    start_journey(api, subject, case_id)
    r = api.get(f"/v1/cases/{case_id}/journey", headers=as_user(subject))
    assert r.status_code == 200, r.text
    return case_id, r.json()


def task_keys(journey: dict) -> set[str]:
    return {t["task_key"] for w in journey["weeks"] for t in w["tasks"]}


def find_task(journey: dict, key: str) -> dict:
    return next(t for w in journey["weeks"] for t in w["tasks"] if t["task_key"] == key)
