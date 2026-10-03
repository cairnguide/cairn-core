"""Development-only sign-in, so registration can be tried without an Auth0 tenant.

Off unless CAIRN_DEV_AUTH_SECRET is set. When it is on:

- POST /v1/dev/token checks a username and password against the dev-only
  cairn_dev database that database/tools/seed_test_db.py creates, and returns an
  access token signed with that secret (HS256).
- The API accepts those tokens alongside Auth0's. Auth0 tokens are RS256 and
  still go to the real verifier, so each algorithm only ever meets its own key.

The dev token carries the same claims as the Auth0 Action (auth0/actions/cairn-claims.js),
so everything after sign-in, starting with POST /v1/registrations, runs the real code.

cairn_dev is never created by database/db/apply.py, so a production deployment has no
test logins, and the Cloudflare Worker never forwards CAIRN_DEV_AUTH_SECRET to the container.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time

import jwt
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import Identity, identity_from_claims
from .errors import ApiError

log = logging.getLogger("cairn_api")

DEV_ISSUER = "cairn-dev-auth"
TOKEN_LIFETIME_SECONDS = 8 * 60 * 60


def verify_password(password: str, stored: str) -> bool:
    """Checks a password against "pbkdf2_sha256$<iterations>$<salt>$<hash>" (base64 salt and hash).

    database/tools/seed_test_db.py writes this format with hash_password.
    """
    try:
        scheme, iterations, salt, expected = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
    except ValueError:
        return False
    return hmac.compare_digest(digest, base64.b64decode(expected))


class DevTokenVerifier:
    """Accepts dev tokens (HS256, this issuer) and passes every other token to the Auth0 verifier."""

    def __init__(self, auth0_verifier, secret: str, audience: str, claim_namespace: str):
        self._auth0 = auth0_verifier
        self._secret = secret
        self._audience = audience
        self._namespace = claim_namespace

    def verify(self, token: str) -> Identity:
        try:
            alg = jwt.get_unverified_header(token).get("alg")
        except jwt.PyJWTError:
            raise ApiError(401, "invalid_token", "Please sign in again.") from None
        if alg != "HS256":
            return self._auth0.verify(token)
        try:
            claims = jwt.decode(token, self._secret, algorithms=["HS256"], audience=self._audience,
                                issuer=DEV_ISSUER, options={"require": ["sub", "exp", "iat"]})
        except jwt.PyJWTError:
            raise ApiError(401, "invalid_token", "Please sign in again.") from None
        return identity_from_claims(claims, self._namespace)


def issue_token(secret: str, audience: str, namespace: str, subject: str, email: str,
                strategy: str) -> str:
    now = int(time.time())
    return jwt.encode({
        "sub": subject, "iss": DEV_ISSUER, "aud": audience, "iat": now, "exp": now + TOKEN_LIFETIME_SECONDS,
        f"{namespace}email": email, f"{namespace}email_verified": True, f"{namespace}sign_in_method": strategy,
    }, secret, algorithm="HS256")


class DevLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class DevToken(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = TOKEN_LIFETIME_SECONDS


router = APIRouter(prefix="/v1/dev", tags=["Development"])


# Left out of the OpenAPI contract on purpose. It exists only in development.
@router.post("/token", response_model=DevToken, include_in_schema=False)
def dev_token(body: DevLogin, request: Request) -> DevToken:
    settings = request.app.state.settings
    if not settings.dev_auth_secret:
        raise ApiError(404, "not_found", "Not found.")
    with request.app.state.db.session() as s:
        seeded, row = s.test_login(body.username)
    if not seeded:
        raise ApiError(503, "test_logins_missing",
                       "There are no test logins in this database. Run: make seed-test-db")
    if row is None or not verify_password(body.password, row["password_hash"]):
        raise ApiError(401, "invalid_login", "That username and password don't match a test login.")
    return DevToken(access_token=issue_token(settings.dev_auth_secret, settings.auth0_audience,
                                             settings.claim_namespace, row["idp_subject"], row["email"],
                                             row["sign_in_strategy"]))
