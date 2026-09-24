"""Identity cleanup after account deletion (UC-ACCT-01). No network: every call goes to a mock transport.

The queue test needs CAIRN_TEST_ADMIN_URL (see conftest.py).
"""
import json

import httpx
import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from cairn_api.identity_cleanup import APPLE_REVOKE_URL, CleanupError, CleanupSettings, IdentityCleanup, run_once

from .conftest import as_user, onboard

KEY = ec.generate_private_key(ec.SECP256R1())
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption()).decode()
CFG = CleanupSettings(auth0_domain="cairn-test.example.test", auth0_client_id="mgmt", auth0_client_secret="s",
                      apple_client_id="test.example.cairn", apple_team_id="TEAM123", apple_key_id="KEY123",
                      apple_private_key=PEM)


def cleanup_with(handler):
    calls = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "mgmt-token"})
        return handler(request)

    return IdentityCleanup(CFG, httpx.Client(transport=httpx.MockTransport(record))), calls


def test_apple_account_is_revoked_then_deleted():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"identities": [{"provider": "apple", "refresh_token": "rt-1"}]})
        if request.url == APPLE_REVOKE_URL:
            return httpx.Response(200)
        return httpx.Response(204)

    cleanup, calls = cleanup_with(handler)
    cleanup.process("apple|001.abc", "apple")
    revoke = next(c for c in calls if str(c.url) == APPLE_REVOKE_URL)
    form = dict(x.split("=", 1) for x in revoke.content.decode().split("&"))
    assert form["token"] == "rt-1" and form["token_type_hint"] == "refresh_token"
    assert form["client_id"] == CFG.apple_client_id
    secret = jwt.decode(form["client_secret"], KEY.public_key(), algorithms=["ES256"],
                        audience="https://appleid.apple.com")
    assert secret["iss"] == "TEAM123" and secret["sub"] == CFG.apple_client_id
    assert calls[-1].method == "DELETE" and calls[-1].url.raw_path == b"/api/v2/users/apple%7C001.abc"
    assert calls[-1].headers["authorization"] == "Bearer mgmt-token"


def test_google_account_is_only_deleted_from_auth0():
    cleanup, calls = cleanup_with(lambda request: httpx.Response(204))
    cleanup.process("google-oauth2|1", "google")
    assert [c.method for c in calls if c.url.path != "/oauth/token"] == ["DELETE"]


def test_missing_apple_token_is_an_error_to_retry():
    cleanup, _ = cleanup_with(lambda r: httpx.Response(200, json={"identities": [{"provider": "apple"}]}))
    with pytest.raises(CleanupError) as exc:
        cleanup.process("apple|001.x", "apple")
    assert exc.value.code == "apple_token_missing"


def test_already_deleted_auth0_user_counts_as_done():
    cleanup, _ = cleanup_with(lambda r: httpx.Response(404, content=json.dumps({}).encode()))
    cleanup.process("apple|001.gone", "apple")


def test_queue_is_cleared_on_success_and_kept_on_failure(api):
    for subject in ("google-oauth2|cleanup-ok", "apple|001.cleanup-fail"):
        method = "apple" if subject.startswith("apple") else "google"
        api.post("/v1/registrations", json={}, headers=as_user(subject, method=method))
        onboard(api, subject, method=method)
        assert api.post("/v1/me/deletion", json={"confirm": True},
                        headers=as_user(subject, method=method)).status_code == 200

    def handler(request):
        if request.method == "GET":
            return httpx.Response(500)
        return httpx.Response(204)

    cleanup, _ = cleanup_with(handler)
    done, failed = run_once(api.scratch_url, cleanup)
    assert (done, failed) >= (1, 1)
    with psycopg.connect(api.scratch_url) as conn:
        rows = conn.execute("SELECT idp_subject, attempts, last_error FROM cairn.identity_deletion_requests "
                            "WHERE idp_subject LIKE '%cleanup%'").fetchall()
    assert rows == [("apple|001.cleanup-fail", 1, "auth0_lookup_failed")]
