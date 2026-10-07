"""Application factory. Run with: uvicorn cairn_api.main:app"""
from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from . import dev_auth
from .auth import TokenVerifier
from .config import Settings, cors_origins_from_env, load_settings
from .copy_store import Copy, load_break_copy, load_case_copy, load_copy, load_subscription_copy
from .db import Database
from .errors import ApiError, api_error_handler, unhandled_error_handler, validation_error_handler
from .redaction import RedactingFilter
from .routers import (
    account,
    breaks,
    case_intake,
    cases,
    home,
    journey,
    notifications,
    onboarding,
    registration,
    subscription,
    tasks,
)
from .stripe_client import Stripe
from .voices import load_voices

API_DESCRIPTION = """
Cairn walks a family through the logistics of a death, one step at a time.

Every response that moves a conversation forward carries a `next_step` with a
single prompt, so clients can ask one question at a time. Errors use RFC 9457
problem details (`application/problem+json`) and never echo submitted values.

Authentication is an Auth0 access token. Accounts can be created with Google,
Apple, or an email magic link (see GET /v1/welcome). Account setup follows the
account spec 3.2.0 (UC-REG-01 to UC-REG-20), one question per screen, from
the adult question to the notification choices. Case creation follows the case
creation spec 3.2.0 (UC-CASE-01 to UC-CASE-25): a case starts as a draft, and
the free period of 28 days starts only when the user starts their first
journey. After it ends the account is read-only until subscribed, through
Stripe (UC-SUB-01 to UC-SUB-23). No payment information is asked for anywhere
in case creation, and Cairn never sees a card number. Free text is redacted as
it is received and never stored. The case is the security boundary. It is
enforced in one data-access layer that every request goes through, with
MongoDB validators and least-privilege roles as the backstop.

Take a break is on every screen and covers the person, not one case
(UC-BRK-01 to UC-BRK-12). The Support resources page works signed out. Users
are signed out after 5 minutes with no activity (D-20).

The user decides how and how often Cairn keeps in touch, once for the whole
account (account D-13). Apart from that, Cairn sends only service notices:
sign-in links, confirmations of things the user did (UC-CASE-21), the
trial-ending note (D-14), an opted-in check-in, and the break-ending notice.
Deleting a case or the account, downloading all data, and changing
notifications are always free, including on a read-only account.
"""

OPENAPI_TAGS = [
    {"name": "Registration", "description": "UC-REG-01 to UC-REG-05 and UC-REG-20. Welcome, sign-in, account "
                                             "creation, sign-in help, and the Support resources page."},
    {"name": "Onboarding", "description": "UC-REG-06 to UC-REG-16. Adult question, acknowledgments, name, voice, "
                                           "notification choices, setup complete."},
    {"name": "Account", "description": "Settings (UC-REG-17), signing out (UC-REG-19), deleting the account "
                                        "(UC-ACCT-01), downloading all data (UC-REG-16), and account requests in "
                                        "chat."},
    {"name": "Home", "description": "UC-CASE-25 and UC-REG-18. The home screen and where a sign-in goes first."},
    {"name": "Take a break", "description": "UC-BRK-01 to UC-BRK-12. Breaks on the account, the rest choices, and "
                                             "coming back."},
    {"name": "Subscription", "description": "UC-SUB-01 to UC-SUB-23. Terms, Stripe Checkout, the customer portal, "
                                             "cancelling, and Stripe events."},
    {"name": "Case creation", "description": "UC-CASE-01 to UC-CASE-11 and UC-CASE-14 to UC-CASE-18. "
                                              "Drafts, intake answers, own words, review, and safety."},
    {"name": "Journey", "description": "UC-CASE-12 and UC-CASE-13 (start the journey that fits), UC-12 and UC-13 "
                                        "(pausing and status)."},
    {"name": "Tasks", "description": "UC-10, UC-11. Working through individual tasks."},
    {"name": "Keeping in touch", "description": "UC-REG-17 and UC-CASE-19. How and when Cairn reaches the user "
                                                 "outside the app, for the whole account."},
]


def check_prices(price: str, *copies: Copy) -> None:
    """UC-SUB-02. Every price in the copy equals the configured one, so what is shown and what is charged can't
    differ. Refuses to start otherwise."""
    for copy in copies:
        for text in copy.all_strings:
            for found in re.findall(r"\$\d+(?:\.\d{2})?", text):
                if found != price:
                    raise RuntimeError(f"Copy {copy.version} shows {found} but CAIRN_SUBSCRIPTION_PRICE_CENTS is "
                                       f"{price}. Change the copy and the price together.")


def create_app(settings: Settings | None = None, database: Database | None = None,
               verifier: TokenVerifier | None = None, stripe=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = settings or load_settings()
        app.state.settings = cfg
        app.state.copy = load_copy(cfg.registration_copy_path)
        app.state.case_copy = load_case_copy(cfg.case_copy_path)
        app.state.break_copy = load_break_copy(cfg.break_copy_path)
        app.state.subscription_copy = load_subscription_copy(cfg.subscription_copy_path)
        check_prices(cfg.price_text, app.state.copy, app.state.case_copy, app.state.break_copy,
                     app.state.subscription_copy)
        app.state.stripe = stripe if stripe is not None else (
            Stripe(cfg.stripe_secret_key) if cfg.stripe_enabled else None)
        app.state.voices = load_voices(cfg.voices_dir)  # fails fast if a stored voice can't be loaded
        app.state.db = database or Database(cfg.mongodb_uri, cfg.mongodb_db, cfg.pool_min_size, cfg.pool_max_size)
        app.state.token_verifier = verifier or TokenVerifier(cfg.auth0_issuer, cfg.auth0_audience,
                                                             cfg.auth0_jwks_url, cfg.claim_namespace)
        if cfg.dev_auth_secret:
            logging.getLogger("cairn_api").warning(
                "Development sign-in is ON (CAIRN_DEV_AUTH_SECRET). Never run this way with real users.")
            app.state.token_verifier = dev_auth.DevTokenVerifier(
                app.state.token_verifier, cfg.dev_auth_secret, cfg.auth0_audience, cfg.claim_namespace)
        app.state.db.open()
        try:
            yield
        finally:
            app.state.db.close()

    app = FastAPI(title="Cairn API", version="0.1.0", description=API_DESCRIPTION,
                  openapi_tags=OPENAPI_TAGS, lifespan=lifespan)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
    # Middleware has to be added before startup, so CORS reads its origins here, not in lifespan.
    cors_origins = settings.cors_origins if settings else cors_origins_from_env()
    if cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(cors_origins), allow_credentials=False,
                           allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                           allow_headers=["Authorization", "Content-Type"], max_age=600)
    for module in (registration, onboarding, account, home, breaks, subscription, cases, case_intake, journey,
                   notifications, tasks, dev_auth):
        app.include_router(module.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"status": "ok"}

    return app


logging.basicConfig(level=logging.INFO)
# UC-CASE-15. Defense in depth: redact sensitive numbers from any log line.
for handler in logging.getLogger().handlers:
    handler.addFilter(RedactingFilter())
app = create_app()
