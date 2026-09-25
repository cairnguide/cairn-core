"""Application factory. Run with: uvicorn cairn_api.main:app"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from .auth import TokenVerifier
from .config import Settings, load_settings
from .copy_store import load_case_copy, load_copy
from .db import Database
from .errors import ApiError, api_error_handler, unhandled_error_handler, validation_error_handler
from .redaction import RedactingFilter
from .routers import account, case_intake, cases, journey, onboarding, registration, tasks
from .voices import load_voices

API_DESCRIPTION = """
Cairn walks a family through the logistics of a death, one step at a time.

Every response that moves a conversation forward carries a `next_step` with a
single prompt, so clients can ask one question at a time. Errors use RFC 9457
problem details (`application/problem+json`) and never echo submitted values.

Authentication is an Auth0 access token. Accounts can be created with Google,
Apple, or an email magic link (see GET /v1/welcome). Onboarding follows
UC-REG-01 to UC-REG-14, one question per screen. Case creation follows
UC-CASE-01 to UC-CASE-18: a case starts as a draft, and the free period of 28
days starts only when the user taps Start journey on their first case. After
it ends the account is read-only until subscribed. No payment information is
asked for anywhere in case creation. Free text is redacted as it is received
and never stored. The case is the security boundary and is enforced in the
database with row-level security.
"""

OPENAPI_TAGS = [
    {"name": "Registration", "description": "UC-REG-01 to UC-REG-05. Welcome, sign-in, and account creation."},
    {"name": "Onboarding", "description": "UC-REG-07 to UC-REG-14. Acknowledgments, name, voice."},
    {"name": "Account", "description": "Settings and account deletion (UC-ACCT-01)."},
    {"name": "Case creation", "description": "UC-CASE-01 to UC-CASE-11 and UC-CASE-14 to UC-CASE-18. "
                                              "Drafts, intake answers, own words, review, and safety."},
    {"name": "Journey", "description": "UC-CASE-12 and UC-CASE-13 (start the journey that fits), UC-12 and UC-13 "
                                        "(pausing and status)."},
    {"name": "Tasks", "description": "UC-10, UC-11. Working through individual tasks."},
]


def create_app(settings: Settings | None = None, database: Database | None = None,
               verifier: TokenVerifier | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = settings or load_settings()
        app.state.settings = cfg
        app.state.copy = load_copy(cfg.registration_copy_path)
        app.state.case_copy = load_case_copy(cfg.case_copy_path)
        app.state.voices = load_voices(cfg.voices_dir)  # fails fast if a stored voice can't be loaded
        app.state.db = database or Database(cfg.database_url, cfg.db_session_role,
                                            cfg.pool_min_size, cfg.pool_max_size)
        app.state.token_verifier = verifier or TokenVerifier(cfg.auth0_issuer, cfg.auth0_audience,
                                                             cfg.auth0_jwks_url, cfg.claim_namespace)
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
    for module in (registration, onboarding, account, cases, case_intake, journey, tasks):
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
