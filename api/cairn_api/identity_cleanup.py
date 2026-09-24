"""Identity provider cleanup after account deletion (UC-ACCT-01).

cairn.delete_my_account removes the account and its data in the database and
queues a row in cairn.identity_deletion_requests. This worker, run on a
schedule as the owner role, finishes the job outside the database:

1. For Apple accounts, revoke the Apple refresh token through the Sign in with
   Apple REST API (https://appleid.apple.com/auth/revoke), as App Review
   requires. The token is read from Auth0's stored identity (needs the
   read:user_idp_tokens scope on the Management API client).
2. Delete the Auth0 user, so the person can't sign back in to an empty account.

A row is deleted once both steps succeed. Failures keep the row with a short
error code and an attempt count for the next run. Nothing personal is logged.

[VERIFY] Confirm against the real Auth0 tenant that the Apple identity exposes
refresh_token. If it doesn't, Apple tokens need another source before launch.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx
import jwt
import psycopg

log = logging.getLogger("cairn_api.identity_cleanup")

APPLE_REVOKE_URL = "https://appleid.apple.com/auth/revoke"
APPLE_AUDIENCE = "https://appleid.apple.com"


@dataclass(frozen=True)
class CleanupSettings:
    auth0_domain: str
    auth0_client_id: str          # Management API machine-to-machine client
    auth0_client_secret: str
    apple_client_id: str          # the Services ID used by the Auth0 Apple connection
    apple_team_id: str
    apple_key_id: str
    apple_private_key: str        # PEM, ES256. Load from a secret manager, never from the repo.


class CleanupError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def apple_client_secret(cfg: CleanupSettings, now: int | None = None) -> str:
    """The short-lived ES256 client secret Apple requires on its REST endpoints."""
    now = now or int(time.time())
    return jwt.encode({"iss": cfg.apple_team_id, "iat": now, "exp": now + 300, "aud": APPLE_AUDIENCE,
                       "sub": cfg.apple_client_id},
                      cfg.apple_private_key, algorithm="ES256", headers={"kid": cfg.apple_key_id})


class IdentityCleanup:
    def __init__(self, cfg: CleanupSettings, http: httpx.Client):
        self._cfg = cfg
        self._http = http
        self._token: str | None = None

    def _mgmt_token(self) -> str:
        if self._token is None:
            r = self._http.post(f"https://{self._cfg.auth0_domain}/oauth/token", json={
                "grant_type": "client_credentials", "client_id": self._cfg.auth0_client_id,
                "client_secret": self._cfg.auth0_client_secret,
                "audience": f"https://{self._cfg.auth0_domain}/api/v2/"})
            if r.status_code != 200:
                raise CleanupError("auth0_token_failed")
            self._token = r.json()["access_token"]
        return self._token

    def _user_url(self, subject: str) -> str:
        return f"https://{self._cfg.auth0_domain}/api/v2/users/{quote(subject, safe='')}"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._mgmt_token()}"}

    def revoke_apple(self, subject: str) -> None:
        r = self._http.get(self._user_url(subject), params={"fields": "identities", "include_fields": "true"},
                           headers=self._headers())
        if r.status_code == 404:
            return  # already gone from Auth0, nothing left to revoke through it
        if r.status_code != 200:
            raise CleanupError("auth0_lookup_failed")
        tokens = [i.get("refresh_token") for i in r.json().get("identities", []) if i.get("provider") == "apple"]
        token = next((t for t in tokens if t), None)
        if token is None:
            raise CleanupError("apple_token_missing")
        r = self._http.post(APPLE_REVOKE_URL, data={
            "client_id": self._cfg.apple_client_id, "client_secret": apple_client_secret(self._cfg),
            "token": token, "token_type_hint": "refresh_token"})
        if r.status_code != 200:
            raise CleanupError("apple_revoke_failed")

    def delete_auth0_user(self, subject: str) -> None:
        r = self._http.delete(self._user_url(subject), headers=self._headers())
        if r.status_code not in (204, 404):
            raise CleanupError("auth0_delete_failed")

    def process(self, subject: str, provider: str | None) -> None:
        if provider == "apple":
            self.revoke_apple(subject)
        self.delete_auth0_user(subject)


def run_once(owner_database_url: str, cleanup: IdentityCleanup, limit: int = 50) -> tuple[int, int]:
    """Works through queued requests. Returns (done, failed). Must connect as the owner role."""
    done = failed = 0
    with psycopg.connect(owner_database_url, autocommit=True) as conn:
        rows = conn.execute("SELECT id, idp_subject, provider FROM cairn.identity_deletion_requests "
                            "ORDER BY requested_at LIMIT %s", (limit,)).fetchall()
        for rid, subject, provider in rows:
            try:
                cleanup.process(subject, provider)
            except (CleanupError, httpx.HTTPError) as exc:
                code = exc.code if isinstance(exc, CleanupError) else "network_error"
                conn.execute("UPDATE cairn.identity_deletion_requests SET attempts = attempts + 1, "
                             "last_error = %s WHERE id = %s", (code, rid))
                log.warning("identity cleanup failed request=%s code=%s", rid, code)
                failed += 1
                continue
            conn.execute("DELETE FROM cairn.identity_deletion_requests WHERE id = %s", (rid,))
            done += 1
    return done, failed
