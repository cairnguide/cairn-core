"""Caller identity from an OIDC access or ID token issued by the identity provider.

Cairn never handles passwords or passkeys. The provider owns sign-in, email
confirmation, and multi-factor. This module only verifies the provider's
signed token and reads three claims: sub, email, and email_verified.
"""
from __future__ import annotations

from dataclasses import dataclass

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .errors import ApiError

_bearer = HTTPBearer(auto_error=False, description="OIDC token from the Cairn identity provider.")


@dataclass(frozen=True)
class Identity:
    subject: str
    email: str | None
    email_verified: bool


class TokenVerifier:
    def __init__(self, issuer: str, audience: str, jwks_url: str):
        self._issuer = issuer
        self._audience = audience
        self._jwks = jwt.PyJWKClient(jwks_url, cache_keys=True)

    def verify(self, token: str) -> Identity:
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token, key.key,
                algorithms=["RS256", "ES256", "EdDSA"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["sub", "exp", "iat"]},
            )
        except jwt.PyJWTError:
            raise ApiError(401, "invalid_token", "Please sign in again.") from None
        return Identity(
            subject=str(claims["sub"]),
            email=claims.get("email"),
            email_verified=bool(claims.get("email_verified", False)),
        )


def get_identity(request: Request,
                 creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Identity:
    if creds is None or creds.scheme.lower() != "bearer":
        raise ApiError(401, "not_signed_in", "Please sign in to continue.")
    verifier: TokenVerifier = request.app.state.token_verifier
    return verifier.verify(creds.credentials)
