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
    # Replacement Take a break and subscription copy files. None uses the bundled ones.
    break_copy_path: str | None = None
    subscription_copy_path: str | None = None
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
    notification_scope: str = "account"         # OPEN-04, resolved by account D-13: one choice for the account
    draft_check_in: str = "account_channels"    # OPEN-08, resolved by account D-13: the account's channels
    # Take a break open decisions, with their defaults (cairn-take-a-break-use-cases-v32.json).
    break_notice_during_care_rest: bool = False  # OPEN-BRK-01: no break-ending notice outside Cairn in a care rest
    break_notice_without_reminders: bool = True  # OPEN-BRK-02: the notice still goes when frequency is none
    under_18_handling: str = "stop_intake"      # OPEN-09: stop intake and store nothing about age
    state_content_approach: str = "verified_link_confirm"  # OPEN-06: verified link plus "Please confirm"
    outside_us_handling: str = "out_of_scope_message"     # OPEN-10
    # D-04 and UC-SUB-02. One value for the price, used to show it and to charge it, so they can't differ. Every
    # price in the copy files must match it, or the API refuses to start.
    subscription_price_cents: int = 1499
    # Stripe (cairn-subscription-use-cases-v33.json). Without a secret key the subscription routes answer that
    # payments aren't available, and the webhook refuses everything. Secrets come from the secret manager.
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_product_id: str | None = None       # the Cairn subscription product. The price is set from the value above
    app_url: str = "https://app.cairnguide.app"  # where Stripe sends the user back (success, cancel, portal return)
    # Browser notifications (UC-REG-15). The VAPID public key the browser subscribes with. The private key lives only
    # in the jobs container. Unset hides the browser choice, since nothing could be delivered.
    vapid_public_key: str | None = None
    # UC-SUB-18. A scheduled price change, shown in Cairn at care level 1 between 30 days before and the date. The
    # jobs container emails it (price_change_notices). Unset when no change is scheduled.
    price_change_effective_date: str | None = None
    price_change_new_price: str | None = None
    # D-20 and UC-REG-19. Sign out after this long with no activity, warn this long before, and sign in again at
    # least every overall_session_days. last_active_at is written at most every activity_write_seconds.
    inactivity_timeout_seconds: int = 300
    warning_before_timeout_seconds: int = 20
    overall_session_days: int = 30
    activity_write_seconds: int = 15
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
        for name, value, built in (("CAIRN_NOTIFICATION_SCOPE", self.notification_scope, "account"),
                                   ("CAIRN_DRAFT_CHECK_IN", self.draft_check_in, "account_channels"),
                                   ("CAIRN_UNDER_18_HANDLING", self.under_18_handling, "stop_intake"),
                                   ("CAIRN_STATE_CONTENT_APPROACH", self.state_content_approach,
                                    "verified_link_confirm"),
                                   ("CAIRN_OUTSIDE_US_HANDLING", self.outside_us_handling, "out_of_scope_message")):
            if value != built:
                raise RuntimeError(f"{name}: only {built} is built.")
        if not 1 <= self.ai_reminder_every_hours <= 3:
            raise RuntimeError("CAIRN_AI_REMINDER_EVERY_HOURS must be 1 to 3 (UC-CASE-23, legal gate).")
        if self.break_notice_during_care_rest:
            raise RuntimeError("CAIRN_BREAK_NOTICE_DURING_CARE_REST: OPEN-BRK-01 isn't decided. Only false is built.")
        if not 60 <= self.inactivity_timeout_seconds <= 900:
            raise RuntimeError("CAIRN_INACTIVITY_TIMEOUT_SECONDS must be 60 to 900 (D-20, at most 15 minutes).")
        if not 20 <= self.warning_before_timeout_seconds < self.inactivity_timeout_seconds:
            raise RuntimeError("CAIRN_TIMEOUT_WARNING_SECONDS must be at least 20 (WCAG 2.2.1) and under the timeout.")
        if not 1 <= self.overall_session_days <= 30:
            raise RuntimeError("CAIRN_OVERALL_SESSION_DAYS must be 1 to 30 (D-20, NIST AAL1).")
        if self.subscription_price_cents < 50:
            raise RuntimeError("CAIRN_SUBSCRIPTION_PRICE_CENTS must be at least 50.")
        if not self.app_url.startswith("https://") and not self.app_url.startswith("http://localhost"):
            raise RuntimeError("CAIRN_APP_URL must be an https URL.")
        if bool(self.price_change_effective_date) != bool(self.price_change_new_price):
            raise RuntimeError("Set CAIRN_PRICE_CHANGE_EFFECTIVE_DATE and CAIRN_PRICE_CHANGE_NEW_PRICE together.")
        if self.dev_auth_secret is not None and len(self.dev_auth_secret) < 32:
            raise RuntimeError("CAIRN_DEV_AUTH_SECRET must be at least 32 characters.")
        if "*" in self.cors_origins:
            raise RuntimeError("CAIRN_CORS_ORIGINS must list origins. A wildcard is refused.")

    @property
    def price_text(self) -> str:
        """The price as the copy writes it, for example $14.99."""
        return f"${self.subscription_price_cents // 100}.{self.subscription_price_cents % 100:02d}"

    @property
    def subscription_price_display(self) -> str:
        return f"{self.price_text} a month"

    @property
    def stripe_enabled(self) -> bool:
        return bool(self.stripe_secret_key and self.stripe_product_id)

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
        break_copy_path=os.environ.get("CAIRN_BREAK_COPY") or None,
        subscription_copy_path=os.environ.get("CAIRN_SUBSCRIPTION_COPY") or None,
        estate_plan_mode=os.environ.get("CAIRN_ESTATE_PLAN_MODE", "add_on"),
        pre_need_path=os.environ.get("CAIRN_PRE_NEED_PATH", "not_built"),
        overwhelm_skip_threshold=int(os.environ.get("CAIRN_OVERWHELM_SKIP_THRESHOLD", "3")),
        unsure_counts_as_skip=os.environ.get("CAIRN_UNSURE_COUNTS_AS_SKIP", "false").lower() == "true",
        sms_enabled=os.environ.get("CAIRN_SMS_ENABLED", "false").lower() == "true",
        notification_scope=os.environ.get("CAIRN_NOTIFICATION_SCOPE", "account"),
        draft_check_in=os.environ.get("CAIRN_DRAFT_CHECK_IN", "account_channels"),
        break_notice_during_care_rest=os.environ.get("CAIRN_BREAK_NOTICE_DURING_CARE_REST", "false").lower() == "true",
        break_notice_without_reminders=os.environ.get("CAIRN_BREAK_NOTICE_WITHOUT_REMINDERS",
                                                      "true").lower() == "true",
        under_18_handling=os.environ.get("CAIRN_UNDER_18_HANDLING", "stop_intake"),
        state_content_approach=os.environ.get("CAIRN_STATE_CONTENT_APPROACH", "verified_link_confirm"),
        outside_us_handling=os.environ.get("CAIRN_OUTSIDE_US_HANDLING", "out_of_scope_message"),
        subscription_price_cents=int(os.environ.get("CAIRN_SUBSCRIPTION_PRICE_CENTS", "1499")),
        stripe_secret_key=secret_from_env("CAIRN_STRIPE_SECRET_KEY"),
        stripe_webhook_secret=secret_from_env("CAIRN_STRIPE_WEBHOOK_SECRET"),
        stripe_product_id=os.environ.get("CAIRN_STRIPE_PRODUCT_ID") or None,
        app_url=os.environ.get("CAIRN_APP_URL", "https://app.cairnguide.app").rstrip("/"),
        vapid_public_key=os.environ.get("CAIRN_VAPID_PUBLIC_KEY") or None,
        price_change_effective_date=os.environ.get("CAIRN_PRICE_CHANGE_EFFECTIVE_DATE") or None,
        price_change_new_price=os.environ.get("CAIRN_PRICE_CHANGE_NEW_PRICE") or None,
        inactivity_timeout_seconds=int(os.environ.get("CAIRN_INACTIVITY_TIMEOUT_SECONDS", "300")),
        warning_before_timeout_seconds=int(os.environ.get("CAIRN_TIMEOUT_WARNING_SECONDS", "20")),
        overall_session_days=int(os.environ.get("CAIRN_OVERALL_SESSION_DAYS", "30")),
        ai_reminder_every_hours=int(os.environ.get("CAIRN_AI_REMINDER_EVERY_HOURS", "3")),
        rest_offer_after_minutes=int(os.environ.get("CAIRN_REST_OFFER_AFTER_MINUTES", "45")),
        cors_origins=cors_origins_from_env(),
        dev_auth_secret=secret_from_env("CAIRN_DEV_AUTH_SECRET"),
    )
