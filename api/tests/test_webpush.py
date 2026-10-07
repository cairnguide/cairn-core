"""Browser notifications (UC-REG-15 browser channel): an empty Web Push message signed with a VAPID key (RFC 8292).
No database, no network: an httpx mock transport stands in for the push service."""
from __future__ import annotations

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from cairn_api import jobs
from cairn_api.webpush import PushError, WebPushSender, public_key_from_pem


def _key() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def _sender(handler, pem=None) -> WebPushSender:
    pem = pem or _key()
    return WebPushSender(private_key_pem=pem, public_key=public_key_from_pem(pem), subject="mailto:ops@example.test",
                         http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_a_push_is_empty_and_signed_for_the_push_service():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(201)

    pem = _key()
    _sender(handler, pem).send("https://push.example.test/send/abc123")
    request = seen[0]
    assert request.content == b""  # nothing about the user travels through the push service
    assert request.headers["TTL"] == "86400"
    scheme, _, rest = request.headers["Authorization"].partition(" ")
    assert scheme == "vapid"
    parts = dict(p.strip().split("=", 1) for p in rest.split(","))
    public = serialization.load_pem_private_key(pem.encode(), None).public_key()
    claims = jwt.decode(parts["t"], public, algorithms=["ES256"], audience="https://push.example.test")
    assert claims["sub"] == "mailto:ops@example.test"
    assert parts["k"] == public_key_from_pem(pem) and len(parts["k"]) == 87  # 65 bytes, base64url


@pytest.mark.parametrize("status,gone", [(404, True), (410, True), (500, False), (429, False)])
def test_a_refused_push_raises_with_its_status(status, gone):
    with pytest.raises(PushError) as e:
        _sender(lambda r: httpx.Response(status)).send("https://push.example.test/send/x")
    assert e.value.status == status and ("gone" in str(e.value)) is gone


def test_without_a_vapid_key_browser_notifications_are_skipped(monkeypatch):
    monkeypatch.delenv("CAIRN_VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("CAIRN_VAPID_PRIVATE_KEY_FILE", raising=False)
    assert jobs.push_from_env() is None


def test_with_a_vapid_key_the_sender_is_built(monkeypatch):
    monkeypatch.setenv("CAIRN_VAPID_PRIVATE_KEY", _key())
    monkeypatch.setenv("CAIRN_VAPID_SUBJECT", "mailto:ops@example.test")
    assert isinstance(jobs.push_from_env(), WebPushSender)
