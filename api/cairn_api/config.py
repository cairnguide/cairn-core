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
    # UC-CASE-09 and DEC-26-03: the third Skip for now in a row starts level 2. Two never do.
    overwhelm_skip_threshold: int = 3
    # Case creation spec 2.0.0 open decisions, with their stated defaults. Only the defaults that need no other
    # build are allowed. Anything else is refused at startup, so a value can't silently do nothing.
    unsure_counts_as_skip: bool = False         # OPEN-07: "I'm not sure" does not count toward the three skips
    sms_enabled: bool = False                   # OPEN-05: text messages are off in the MVP
    notification_scope: str = "per_journey"     # OPEN-04: notification choices per journey
    draft_check_in: str = "next_open"           # OPEN-08: a draft's check-in shows the next time Cairn is opened
    under_18_handling: str = "stop_intake"      # OPEN-09: stop intake and store nothing about age
    state_content_approach: str = "verified_link_confirm"  # OPEN-06: verified link plus "Please confirm"
    outside_us_handling: str = "out_of_scope_message"     # OPEN-10
    # UC-CASE-12 config. The price is also written verbatim in the spec copy, pending legal review (card 55).
    subscription_price_display: str = "$14.99 a month"
    # UC-CASE-23. The AI reminder repeats after this much continuing interaction. The rest offer comes after this
    # much active use. A gap longer than active_gap_minutes between turns doesn't count as active use.
    ai_reminder_every_hours: int = 3
    rest_offer_after_minutes: int = 45
    active_gap_minutes: int = 5
    # Browser origins allowed to call the API (CORS). Empty allows none, which is right for native
    # clients and for a web client served from the API's own origin.
    cors_origins: tuple[str, ...] = ()
    # Development only. When set, POST /v1/dev/token exchanges a test login from the dev-only
    # cairn_dev database (database/tools/seed_test_db.py) for a token signed with this secret, so
    # registration can be tried without an Auth0 tenant. Never set it anywhere real users are.
    # The Cloudflare Worker never forwards it (cloudflare/src/index.ts).
    dev_auth_secret: str | None = None

    def __post_init__(self):
        if self.estate_plan_mode != "add_on":
            raise RuntimeError("CAIRN_ESTATE_PLAN_MODE: only add_on is built (OPEN-DECISION-01).")
        if self.pre_need_path != "not_built":
            raise RuntimeError("CAIRN_PRE_NEED_PATH: the pre-need path is not built (OPEN-DECISION-05).")
        if self.overwhelm_skip_threshold < 1:
            raise RuntimeError("CAIRN_OVERWHELM_SKIP_THRESHOLD must be at least 1.")
        if self.sms_enabled:
            # Text messages are not in the MVP (OPEN-05, decided 2026-10-05). The Twilio sender and Verify client
            # exist (twilio_client.py), but number collection, the consent line, and the sms channel don't.
            raise RuntimeError("CAIRN_SMS_ENABLED: text messages are not in the MVP (OPEN-05).")
        for name, value, built in (("CAIRN_NOTIFICATION_SCOPE", self.notification_scope, "per_journey"),
                                   ("CAIRN_DRAFT_CHECK_IN", self.draft_check_in, "next_open"),
                                   ("CAIRN_UNDER_18_HANDLING", self.under_18_handling, "stop_intake"),
                                   ("CAIRN_STATE_CONTENT_APPROACH", self.state_content_approach,
                                    "verified_link_confirm"),
                                   ("CAIRN_OUTSIDE_US_HANDLING", self.outside_us_handling, "out_of_scope_message")):
            if value != built:
                raise RuntimeError(f"{name}: only {built} is built.")
        if not 1 <= self.ai_reminder_every_hours <= 3:
            raise RuntimeError("CAIRN_AI_REMINDER_EVERY_HOURS must be 1 to 3 (UC-CASE-23, legal gate).")
        if self.dev_auth_secret is not None and len(self.dev_auth_secret) < 32:
            raise RuntimeError("CAIRN_DEV_AUTH_SECRET must be at least 32 characters.")
        if "*" in self.cors_origins:
            raise RuntimeError("CAIRN_CORS_ORIGINS must list origins. A wildcard is refused.")

    @property
    def auth0_issuer(self) -> str:
        return f"https://{self.auth0_domain}/"

    @property
    def auth0_jwks_url(self) -> str:
        return f"https://{self.auth0_domain}/.well-known/jwks.json"


def cors_origins_from_env() -> tuple[str, ...]:
    """CAIRN_CORS_ORIGINS, a comma-separated list such as https://app.cairn.example."""
    raw = os.environ.get("CAIRN_CORS_ORIGINS", "")
    return tuple(o.strip().rstrip("/") for o in raw.split(",") if o.strip())


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
        unsure_counts_as_skip=os.environ.get("CAIRN_UNSURE_COUNTS_AS_SKIP", "false").lower() == "true",
        sms_enabled=os.environ.get("CAIRN_SMS_ENABLED", "false").lower() == "true",
        notification_scope=os.environ.get("CAIRN_NOTIFICATION_SCOPE", "per_journey"),
        draft_check_in=os.environ.get("CAIRN_DRAFT_CHECK_IN", "next_open"),
        under_18_handling=os.environ.get("CAIRN_UNDER_18_HANDLING", "stop_intake"),
        state_content_approach=os.environ.get("CAIRN_STATE_CONTENT_APPROACH", "verified_link_confirm"),
        outside_us_handling=os.environ.get("CAIRN_OUTSIDE_US_HANDLING", "out_of_scope_message"),
        subscription_price_display=os.environ.get("CAIRN_SUBSCRIPTION_PRICE_DISPLAY", "$14.99 a month"),
        ai_reminder_every_hours=int(os.environ.get("CAIRN_AI_REMINDER_EVERY_HOURS", "3")),
        rest_offer_after_minutes=int(os.environ.get("CAIRN_REST_OFFER_AFTER_MINUTES", "45")),
        cors_origins=cors_origins_from_env(),
        dev_auth_secret=secret_from_env("CAIRN_DEV_AUTH_SECRET"),
    )
