"""Browser notifications (account D-12 and UC-REG-15: "A notification in this browser").

Cairn sends an empty Web Push message, signed with a VAPID key (RFC 8292). The
push carries no text at all, so nothing about the user, the person who died, or
a task passes through the browser vendor's push service. The service worker
shows a fixed, private line from Cairn's own copy when it wakes. With no
payload, no message encryption is needed (RFC 8291 applies only to payloads).

The endpoint is the one the browser gave when the user said yes. A 404 or 410
means the subscription is gone (permission revoked): the caller takes browser
off the user's channels and clears the endpoint.
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import jwt

GONE = (404, 410)


class PushError(Exception):
    """A push wasn't delivered. str() is a short code, safe to log."""

    def __init__(self, code: str, status: int | None = None):
        super().__init__(f"push:{status or 'network'}:{code}"[:100])
        self.status = status


@dataclass(frozen=True)
class WebPushSender:
    private_key_pem: str     # the VAPID private key (P-256, PEM), from the secret manager
    public_key: str          # the matching public key, base64url, also given to the browser at subscribe time
    subject: str             # mailto: or https: contact for the push services
    http: httpx.Client | None = None
    ttl_seconds: int = 86400

    def _authorization(self, endpoint: str) -> str:
        parts = urlsplit(endpoint)
        claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(time.time()) + 12 * 3600,
                  "sub": self.subject}
        token = jwt.encode(claims, self.private_key_pem, algorithm="ES256")
        return f"vapid t={token}, k={self.public_key}"

    def send(self, endpoint: str) -> None:
        """One empty push. Raises PushError, with status 404 or 410 when the subscription is gone."""
        client = self.http or httpx.Client(timeout=10)
        try:
            r = client.post(endpoint, content=b"", headers={
                "Authorization": self._authorization(endpoint), "TTL": str(self.ttl_seconds), "Urgency": "normal"})
        except httpx.HTTPError as exc:
            raise PushError(type(exc).__name__) from None
        if r.status_code >= 300:
            raise PushError("gone" if r.status_code in GONE else "refused", r.status_code)


def public_key_from_pem(private_key_pem: str) -> str:
    """The base64url uncompressed public key for a VAPID private key."""
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    raw = key.public_key().public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
