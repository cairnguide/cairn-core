"""Runtime settings, read from the environment.

Credentials are never committed. DATABASE_URL must use a login role that is a
member of cairn_app. Never the owner role, and never a role with BYPASSRLS.
"""
import os
from dataclasses import dataclass


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Set {name} in the environment.")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str
    # Role the pooled connections switch to. Keeps the session to cairn_app
    # privileges even if the login role was granted anything extra.
    db_session_role: str
    pool_min_size: int
    pool_max_size: int
    # Identity provider (open question 6 in database/CLAUDE.md). Any OIDC
    # provider that publishes a JWKS works. Passwords, passkeys, and email
    # confirmation all live with the provider, never in Cairn.
    oidc_issuer: str
    oidc_audience: str
    oidc_jwks_url: str
    # Current policy versions. Registration is refused unless the client sends
    # back exactly these, which proves the user saw the current text.
    terms_version: str
    privacy_version: str


def load_settings() -> Settings:
    return Settings(
        database_url=_require("DATABASE_URL"),
        db_session_role=os.environ.get("CAIRN_DB_SESSION_ROLE", "cairn_app"),
        pool_min_size=int(os.environ.get("CAIRN_DB_POOL_MIN", "1")),
        pool_max_size=int(os.environ.get("CAIRN_DB_POOL_MAX", "10")),
        oidc_issuer=_require("CAIRN_OIDC_ISSUER"),
        oidc_audience=_require("CAIRN_OIDC_AUDIENCE"),
        oidc_jwks_url=_require("CAIRN_OIDC_JWKS_URL"),
        terms_version=_require("CAIRN_TERMS_VERSION"),
        privacy_version=_require("CAIRN_PRIVACY_VERSION"),
    )
