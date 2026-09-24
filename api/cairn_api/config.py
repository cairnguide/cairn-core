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
    # Auth0 (open question 6 in database/CLAUDE.md, resolved). Auth0 owns
    # Google, Apple, and email sign-in, passwords, passkeys, and email
    # confirmation. Cairn only verifies Auth0's signed access tokens.
    auth0_domain: str            # tenant or custom domain, for example cairn.us.auth0.com
    auth0_audience: str          # the Cairn API identifier configured in Auth0
    claim_namespace: str         # prefix for the custom claims set by auth0/actions/cairn-claims.js
    email_connection: str        # Auth0 connection for email sign-up. Passwordless magic link by default (D-10).
    # Current policy versions. Registration is refused unless the client sends
    # back exactly these, which proves the user saw the current text.
    terms_version: str
    privacy_version: str
    # Where the welcome, decline, and age screens send people (UC-REG-01, 06, 07, 10).
    privacy_policy_url: str
    terms_url: str
    journey_map_url: str        # the public "what the first weeks look like" page
    support_url: str
    # The third-party AI provider, named on the privacy step (UC-REG-07). [LEGAL REVIEW REQUIRED]
    ai_provider_name: str
    # Path to a replacement copy file after legal review. None uses the bundled one.
    registration_copy_path: str | None = None

    @property
    def auth0_issuer(self) -> str:
        return f"https://{self.auth0_domain}/"

    @property
    def auth0_jwks_url(self) -> str:
        return f"https://{self.auth0_domain}/.well-known/jwks.json"


def load_settings() -> Settings:
    return Settings(
        database_url=_require("DATABASE_URL"),
        db_session_role=os.environ.get("CAIRN_DB_SESSION_ROLE", "cairn_app"),
        pool_min_size=int(os.environ.get("CAIRN_DB_POOL_MIN", "1")),
        pool_max_size=int(os.environ.get("CAIRN_DB_POOL_MAX", "10")),
        auth0_domain=_require("CAIRN_AUTH0_DOMAIN"),
        auth0_audience=_require("CAIRN_AUTH0_AUDIENCE"),
        claim_namespace=os.environ.get("CAIRN_CLAIM_NAMESPACE", "https://cairn.invalid/"),
        email_connection=os.environ.get("CAIRN_AUTH0_EMAIL_CONNECTION", "email"),
        terms_version=_require("CAIRN_TERMS_VERSION"),
        privacy_version=_require("CAIRN_PRIVACY_VERSION"),
        privacy_policy_url=_require("CAIRN_PRIVACY_POLICY_URL"),
        terms_url=_require("CAIRN_TERMS_URL"),
        journey_map_url=_require("CAIRN_JOURNEY_MAP_URL"),
        support_url=_require("CAIRN_SUPPORT_URL"),
        ai_provider_name=_require("CAIRN_AI_PROVIDER_NAME"),
        registration_copy_path=os.environ.get("CAIRN_REGISTRATION_COPY") or None,
    )
