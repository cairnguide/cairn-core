"""Auth0 token verification and sign-in method mapping, with a locally generated key. No network."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from cairn_api.auth import TokenVerifier
from cairn_api.errors import ApiError

NS = "https://cairn.invalid/"
ISSUER = "https://cairn-test.example.test/"
AUDIENCE = "https://api.cairn.example.test"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
VERIFIER = TokenVerifier(ISSUER, AUDIENCE, "unused", NS, key_resolver=lambda _t: KEY.public_key())


def token(sub="google-oauth2|123", strategy="google-oauth2", verified=True, email="pat@example.test",
          key=KEY, **overrides):
    now = int(time.time())
    claims = {"sub": sub, "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 300,
              f"{NS}email": email, f"{NS}email_verified": verified, f"{NS}sign_in_method": strategy}
    claims.update(overrides)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256")


@pytest.mark.parametrize("sub,strategy,method", [
    ("google-oauth2|1", "google-oauth2", "google"),
    ("apple|001.abc", "apple", "apple"),
    ("auth0|abc", "auth0", "email"),
    ("email|abc", "email", "email"),
])
def test_three_supported_methods(sub, strategy, method):
    ident = VERIFIER.verify(token(sub=sub, strategy=strategy))
    assert ident.sign_in_method.value == method
    assert ident.subject == sub
    assert ident.email == "pat@example.test" and ident.email_verified


def test_method_falls_back_to_subject_prefix():
    assert VERIFIER.verify(token(sub="apple|001.abc", strategy=None)).sign_in_method.value == "apple"


@pytest.mark.parametrize("strategy", ["facebook", "github", "samlp", "waad"])
def test_other_connections_are_refused(strategy):
    with pytest.raises(ApiError) as exc:
        VERIFIER.verify(token(sub=f"{strategy}|1", strategy=strategy))
    assert exc.value.status == 403 and exc.value.code == "sign_in_method_not_supported"


def test_unverified_email_is_reported_as_unverified():
    assert VERIFIER.verify(token(sub="auth0|x", strategy="auth0", verified=False)).email_verified is False
    assert VERIFIER.verify(token(verified="true")).email_verified is True
    assert VERIFIER.verify(token(verified=None)).email_verified is False


@pytest.mark.parametrize("bad", [
    {"key": OTHER_KEY},
    {"aud": "https://someone-else.example.test"},
    {"iss": "https://evil.example.test/"},
    {"exp": int(time.time()) - 10},
])
def test_bad_tokens_are_rejected(bad):
    with pytest.raises(ApiError) as exc:
        VERIFIER.verify(token(**bad))
    assert exc.value.status == 401

