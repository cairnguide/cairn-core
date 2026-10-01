"""Runtime settings, read from the environment.

Credentials are never committed. MONGODB_URI must belong to a login user that
holds only the cairnApp role (database/db/schema.py). Never an administrator,
never the jobs or loader user, and never a user with bypassDocumentValidation.
"""
import os
import pathlib
from dataclasses import dataclass


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Set {name} in the environment.")
    return value


def secret_from_env(name: str) -> str | None:
    """A secret from NAME, or from the file named by NAME_FILE.

    Secret managers that mount files use NAME_FILE. Cloudflare secrets arrive as
    environment variables, so NAME works too. NAME wins when both are set.
    """
    value = os.environ.get(name)
    if value:
        return value
    path = os.environ.get(f"{name}_FILE")
    if path:
        return pathlib.Path(path).read_text().strip()
    return None


@dataclass(frozen=True)
class Settings:
    mongodb_uri: str
    # The Cairn database on that deployment. The cairnApp role is scoped to it.
    mongodb_db: str
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
    # The voices folder (manifest.yaml, core.md, one file per voice). None uses the repository's voices/.
    voices_dir: str | None = None
    # Path to a replacement case creation copy file after legal review. None uses the bundled one.
    case_copy_path: str | None = None
    # Case creation open decisions (spec open_decisions), with the spec's defaults.
    # Only the default of each is built. Anything else is refused at startup.
    estate_plan_mode: str = "add_on"        # OPEN-DECISION-01: estate plan as an add-on, not a starting trailhead
    pre_need_path: str = "not_built"        # OPEN-DECISION-05: pre-need planning path
    # UC-CASE-14 overwhelm signal: this many skips in a row slows the session down.
    # Thresholds come from Trello card 26, which is not written yet.
    overwhelm_skip_threshold: int = 3

    def __post_init__(self):
        if self.estate_plan_mode != "add_on":
            raise RuntimeError("CAIRN_ESTATE_PLAN_MODE: only add_on is built (OPEN-DECISION-01).")
        if self.pre_need_path != "not_built":
            raise RuntimeError("CAIRN_PRE_NEED_PATH: the pre-need path is not built (OPEN-DECISION-05).")
        if self.overwhelm_skip_threshold < 1:
            raise RuntimeError("CAIRN_OVERWHELM_SKIP_THRESHOLD must be at least 1.")

    @property
    def auth0_issuer(self) -> str:
        return f"https://{self.auth0_domain}/"

    @property
    def auth0_jwks_url(self) -> str:
        return f"https://{self.auth0_domain}/.well-known/jwks.json"


def load_settings() -> Settings:
    return Settings(
        mongodb_uri=_require("MONGODB_URI"),
        mongodb_db=os.environ.get("CAIRN_MONGODB_DB", "cairn"),
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
        voices_dir=os.environ.get("CAIRN_VOICES_DIR") or None,
        case_copy_path=os.environ.get("CAIRN_CASE_COPY") or None,
        estate_plan_mode=os.environ.get("CAIRN_ESTATE_PLAN_MODE", "add_on"),
        pre_need_path=os.environ.get("CAIRN_PRE_NEED_PATH", "not_built"),
        overwhelm_skip_threshold=int(os.environ.get("CAIRN_OVERWHELM_SKIP_THRESHOLD", "3")),
    )
