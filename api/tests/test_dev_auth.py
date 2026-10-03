"""Development sign-in (cairn_api/dev_auth.py), the test login seed, and CORS.

The contract tests need no database. The last test needs CAIRN_TEST_MONGODB_URI.
"""
import dataclasses
import sys
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from cairn_api.config import Settings
from cairn_api.dev_auth import DEV_ISSUER, DevTokenVerifier, issue_token, verify_password
from cairn_api.errors import ApiError
from cairn_api.main import create_app

from .conftest import DB_DIR, SETTINGS, NoDatabase

sys.path.insert(0, str(DB_DIR / "tools"))
import seed_test_db  # noqa: E402

SECRET = "s" * 40
NS = SETTINGS.claim_namespace
AUD = SETTINGS.auth0_audience


class Auth0Stub:
    def verify(self, token):
        return "auth0-verified"


VERIFIER = DevTokenVerifier(Auth0Stub(), SECRET, AUD, NS)


def test_dev_token_endpoint_is_off_without_the_secret(contract_client):
    r = contract_client.post("/v1/dev/token", json={"username": "test.user", "password": "x"})
    assert r.status_code == 404


def test_dev_route_is_not_in_the_contract():
    assert not any(p.startswith("/v1/dev") for p in create_app(settings=SETTINGS).openapi()["paths"])


def test_dev_token_round_trip():
    token = issue_token(SECRET, AUD, NS, "email|cairn-dev-x", "x@example.test", "email")
    ident = VERIFIER.verify(token)
    assert ident.subject == "email|cairn-dev-x" and ident.email == "x@example.test"
    assert ident.email_verified and ident.sign_in_method.value == "email"


@pytest.mark.parametrize("claims,key", [
    ({}, "t" * 40),                                   # signed with another secret
    ({"iss": "https://cairn-test.example.test/"}, SECRET),  # not the dev issuer
    ({"aud": "https://other.example.test"}, SECRET),  # another audience
    ({"exp": int(time.time()) - 10}, SECRET),         # expired
])
def test_dev_verifier_refuses_bad_tokens(claims, key):
    now = int(time.time())
    body = {"sub": "email|x", "iss": DEV_ISSUER, "aud": AUD, "iat": now, "exp": now + 60, **claims}
    with pytest.raises(ApiError) as err:
        VERIFIER.verify(jwt.encode(body, key, algorithm="HS256"))
    assert err.value.status == 401


def test_non_dev_tokens_go_to_auth0():
    # Only HS256 reaches the dev secret. Anything else is Auth0's to accept or refuse.
    rs256 = jwt.encode({"sub": "auth0|x"}, rsa.generate_private_key(public_exponent=65537, key_size=2048),
                       algorithm="RS256")
    assert VERIFIER.verify(rs256) == "auth0-verified"
    with pytest.raises(ApiError):
        VERIFIER.verify("not a token")


def test_seed_hash_matches_api_check():
    stored = seed_test_db.hash_password("correct horse")
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong horse", stored)
    assert not verify_password("correct horse", "md5$1$abc$def")


def test_settings_refuse_weak_dev_secret_and_wildcard_cors():
    with pytest.raises(RuntimeError):
        dataclasses.replace(SETTINGS, dev_auth_secret="short")
    with pytest.raises(RuntimeError):
        dataclasses.replace(SETTINGS, cors_origins=("*",))


def test_seed_refuses_remote_hosts(monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_ADMIN_MONGODB_URI", "mongodb://admin:pw@db.example.test:27017/?replicaSet=rs0")
    monkeypatch.setattr(sys, "argv", ["seed_test_db.py"])
    assert seed_test_db.main() == 2
    assert "Refusing" in capsys.readouterr().err
    monkeypatch.setenv("CAIRN_ADMIN_MONGODB_URI", "mongodb+srv://admin:pw@cluster0.example.test/")
    assert seed_test_db.main() == 2


def _cors_client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings=settings, database=NoDatabase(), verifier=object()))


def test_cors_allows_only_listed_origins():
    preflight = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization"}
    with _cors_client(dataclasses.replace(SETTINGS, cors_origins=("https://app.cairn.example.test",))) as c:
        ok = c.options("/v1/registrations", headers={"Origin": "https://app.cairn.example.test", **preflight})
        assert ok.headers["access-control-allow-origin"] == "https://app.cairn.example.test"
        other = c.options("/v1/registrations", headers={"Origin": "https://evil.example.test", **preflight})
        assert "access-control-allow-origin" not in other.headers
    with _cors_client(SETTINGS) as c:
        r = c.get("/v1/welcome", headers={"Origin": "https://app.cairn.example.test"})
        assert "access-control-allow-origin" not in r.headers


# ------------------------------------------------------------------ integration

def test_sign_up_with_a_test_login(scratch_db):
    """The documented local flow: seed, get a dev token, register, and resume."""
    from urllib.parse import urlsplit

    from cairn_api.db import Database

    settings = dataclasses.replace(SETTINGS, dev_auth_secret=SECRET)
    api_user = urlsplit(scratch_db.uris["cairnApp"]).username
    db = Database(scratch_db.uris["cairnApp"], scratch_db.name, min_size=1, max_size=2)
    login = {"username": "new.user", "password": "test-login-password"}
    try:
        with TestClient(create_app(settings=settings, database=db, verifier=Auth0Stub())) as c:
            # Before the seed, the API's user can't read the logins and says how to add them.
            assert c.post("/v1/dev/token", json=login).json()["code"] == "test_logins_missing"
            seed_test_db.seed(scratch_db.admin_uri, scratch_db.name, login["password"], reset=True,
                              api_user=api_user)

            assert c.post("/v1/dev/token", json={**login, "password": "wrong"}).status_code == 401
            assert c.post("/v1/dev/token", json={**login, "username": "nobody"}).status_code == 401
            token = c.post("/v1/dev/token", json=login).json()["access_token"]
            auth = {"Authorization": f"Bearer {token}"}

            r = c.post("/v1/registrations", json={"time_zone": "America/New_York"}, headers=auth)
            assert r.status_code == 201, r.text
            assert r.json()["account"]["email"] == "new.user@example.test"
            assert c.post("/v1/registrations", json={}, headers=auth).status_code == 200

            # test.user was created by the seed, so it resumes rather than creating an account.
            token = c.post("/v1/dev/token", json={**login, "username": "Test.User"}).json()["access_token"]
            r = c.post("/v1/registrations", json={}, headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 200, r.text
            assert r.json()["account"]["onboarding_step"] == "account_created"

            # --reset puts new.user back, so sign-up can be tried again.
            seed_test_db.seed(scratch_db.admin_uri, scratch_db.name, login["password"], reset=True,
                              api_user=api_user)
            assert c.post("/v1/registrations", json={}, headers=auth).status_code == 201

            # The extra role reads the logins and nothing else in the dev database.
            from pymongo import MongoClient
            from pymongo.errors import OperationFailure
            with MongoClient(scratch_db.uris["cairnApp"]) as app_client:
                dev = app_client[seed_test_db.dev_db_name(scratch_db.name)]
                with pytest.raises(OperationFailure):
                    dev.test_logins.insert_one({"_id": "x"})
    finally:
        dev = scratch_db.admin_client[seed_test_db.dev_db_name(scratch_db.name)]
        dev.command("dropAllRolesFromDatabase")
        scratch_db.admin_client.drop_database(dev.name)
