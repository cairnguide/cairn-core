"""Application factory. Run with: uvicorn cairn_api.main:app"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from .auth import TokenVerifier
from .config import Settings, load_settings
from .copy_store import load_copy
from .db import Database
from .errors import ApiError, api_error_handler, unhandled_error_handler, validation_error_handler
from .routers import account, cases, journey, onboarding, registration, tasks

API_DESCRIPTION = """
Cairn walks a family through the logistics of a death, one step at a time.

Every response that moves a conversation forward carries a `next_step` with a
single prompt, so clients can ask one question at a time. Errors use RFC 9457
problem details (`application/problem+json`) and never echo submitted values.

Authentication is an Auth0 access token. Accounts can be created with Google,
Apple, or an email magic link (see GET /v1/welcome). Onboarding follows
UC-REG-01 to UC-REG-14, one question per screen. The free trial is 28 days from
the first case, after which the account is read-only until subscribed. The
case is the security boundary and is enforced in the database with
row-level security.
"""

OPENAPI_TAGS = [
    {"name": "Registration", "description": "UC-REG-01 to UC-REG-05. Welcome, sign-in, and account creation."},
    {"name": "Onboarding", "description": "UC-REG-07 to UC-REG-14. Acknowledgments, name, personality."},
    {"name": "Account", "description": "Settings and account deletion (UC-ACCT-01)."},
    {"name": "Cases", "description": "UC-5 to UC-8. The case, the deceased, and the death."},
    {"name": "Journey", "description": "UC-9, UC-12, UC-13. The four-week journey, pausing, and status."},
    {"name": "Tasks", "description": "UC-10, UC-11. Working through individual tasks."},
]


def create_app(settings: Settings | None = None, database: Database | None = None,
               verifier: TokenVerifier | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = settings or load_settings()
        app.state.settings = cfg
        app.state.copy = load_copy(cfg.registration_copy_path)
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
    for module in (registration, onboarding, account, cases, journey, tasks):
        app.include_router(module.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"status": "ok"}

    return app


logging.basicConfig(level=logging.INFO)
app = create_app()
