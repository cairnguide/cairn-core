"""Caller identity from an Auth0 access token.

Cairn never handles passwords or passkeys. Auth0 owns sign-in for all three
supported methods (Google, Apple, and email with a password or passkey),
along with email confirmation and multi-factor. This module verifies Auth0's
signed token and reads the claims added by the post-login Action in
auth0/actions/cairn-claims.js: email, email_verified, and sign_in_method.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .errors import ApiError
from .schemas import SignInMethod

_bearer = HTTPBearer(auto_error=False, description="Auth0 access token for the Cairn API audience.")


# Auth0 connection strategies accepted for account creation. Anything else
# (another social connection enabled by mistake, enterprise SSO) is refused.
STRATEGY_TO_METHOD: dict[str, SignInMethod] = {
    "google-oauth2": SignInMethod.google,
    "apple": SignInMethod.apple,
    "auth0": SignInMethod.email,   # email and password (or passkey) database connection
    "email": SignInMethod.email,   # passwordless email code or link
}


@dataclass(frozen=True)
class Identity:
    subject: str
    email: str | None
    email_verified: bool
    sign_in_method: SignInMethod


def identity_from_claims(claims: dict, namespace: str) -> Identity:
    # Auth0 subjects look like "google-oauth2|1234". The Action's strategy claim
    # is preferred. The subject prefix is the fallback if the claim is missing.
    subject = str(claims["sub"])
    strategy = claims.get(f"{namespace}sign_in_method") or subject.split("|", 1)[0]
    method = STRATEGY_TO_METHOD.get(strategy)
    if method is None:
        raise ApiError(403, "sign_in_method_not_supported",
                       "Please sign in with Google, Apple, or your email address.")
    verified = claims.get(f"{namespace}email_verified", False)
    return Identity(
        subject=subject,
        email=claims.get(f"{namespace}email"),
        # Tolerate the string form some providers use for this flag.
        email_verified=verified is True or verified == "true",
        sign_in_method=method,
    )


class TokenVerifier:
    def __init__(self, issuer: str, audience: str, jwks_url: str, claim_namespace: str,
                 key_resolver: Callable[[str], object] | None = None):
        self._issuer = issuer
        self._audience = audience
        self._namespace = claim_namespace
        if key_resolver is None:
            client = jwt.PyJWKClient(jwks_url, cache_keys=True)
            key_resolver = lambda token: client.get_signing_key_from_jwt(token).key  # noqa: E731
        self._key_for = key_resolver

    def verify(self, token: str) -> Identity:
        try:
            claims = jwt.decode(
                token, self._key_for(token),
                algorithms=["RS256"],  # Auth0 API default signing algorithm
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["sub", "exp", "iat"]},
            )
        except jwt.PyJWTError:
            raise ApiError(401, "invalid_token", "Please sign in again.") from None
        return identity_from_claims(claims, self._namespace)


def get_identity(request: Request,
                 creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Identity:
    if creds is None or creds.scheme.lower() != "bearer":
        raise ApiError(401, "not_signed_in", "Please sign in to continue.")
    verifier: TokenVerifier = request.app.state.token_verifier
    return verifier.verify(creds.credentials)
