"""Scheduled jobs as a small HTTP service, for hosts that trigger jobs over HTTP.

On Cloudflare, Cron Triggers call the Worker's scheduled handler, which posts to
this service in its own container (see cloudflare/src/index.ts). That container
holds the jobs connection string (the cairnJobs MongoDB user) and the provider
secrets. The API container never does, and the Worker never routes public
traffic here.

Run with: uvicorn cairn_api.jobs:app  (or CAIRN_PROCESS=jobs python -m cairn_api.serve)

  CAIRN_JOBS_MONGODB_URI            the cairnJobs user's connection string (required)
  CAIRN_MONGODB_DB                  the Cairn database name, cairn by default
  CAIRN_EMAIL_FROM                  the sender, for example "Cairn <no-reply@mail.example>"
  CAIRN_EMAIL_PROVIDER              twilio (the default) or smtp (local development only)
  TWILIO_SENDGRID_API_KEY           or TWILIO_SENDGRID_API_KEY_FILE, for twilio
  CAIRN_SMTP_HOST, CAIRN_SMTP_PORT, CAIRN_SMTP_USERNAME, and CAIRN_SMTP_PASSWORD(_FILE), for smtp
  CAIRN_PENDING_ACCOUNT_RETENTION_DAYS   UC-REG-10. 90 by default (D-2026-10-05-R1)
  CAIRN_NO_CASE_ACCOUNT_RETENTION_DAYS   UC-REG-13. Optional, unset until the retention schedule sets it
  CAIRN_AUTH0_DOMAIN, CAIRN_AUTH0_MGMT_CLIENT_ID
  CAIRN_AUTH0_MGMT_CLIENT_SECRET    or CAIRN_AUTH0_MGMT_CLIENT_SECRET_FILE
  CAIRN_APPLE_CLIENT_ID, CAIRN_APPLE_TEAM_ID, CAIRN_APPLE_KEY_ID
  CAIRN_APPLE_PRIVATE_KEY           or CAIRN_APPLE_PRIVATE_KEY_FILE (the ES256 .p8 key)
  CAIRN_REGISTRATION_COPY, CAIRN_CASE_COPY   optional, the same replacement copy files the API uses

A job whose provider isn't configured yet answers "skipped" instead of failing,
so the schedule can run before a provider's secrets or a retention period are set. Responses and logs
hold counts only, never anything personal.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Callable

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import maintenance
from .config import secret_from_env
from .copy_store import load_case_copy, load_copy
from .db import client_for
from .identity_cleanup import CleanupSettings, IdentityCleanup
from .identity_cleanup import run_once as run_identity_cleanup
from .outbound import Mailer, SmtpMailer
from .outbound import run_once as run_outbound
from .twilio_client import SendGridMailer

log = logging.getLogger("cairn_api.jobs")


class NotConfigured(Exception):
    """A provider the job needs has no settings yet."""


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise NotConfigured(name)
    return value


def _secret(name: str) -> str:
    value = secret_from_env(name)
    if not value:
        raise NotConfigured(name)
    return value


_client = None


def jobs_database():
    """The Cairn database as the cairnJobs user. One client per process, created on first use."""
    global _client
    if _client is None:
        _client = client_for(_env("CAIRN_JOBS_MONGODB_URI"), app_name="cairn-jobs", max_size=4)
    return _client[os.environ.get("CAIRN_MONGODB_DB", "cairn")]


def mailer_from_env() -> Mailer:
    """Twilio SendGrid by default. SMTP only for local development with a mail catcher."""
    provider = os.environ.get("CAIRN_EMAIL_PROVIDER", "twilio")
    if provider == "twilio":
        return SendGridMailer(api_key=_secret("TWILIO_SENDGRID_API_KEY"), sender=_env("CAIRN_EMAIL_FROM"))
    if provider == "smtp":
        return SmtpMailer(host=_env("CAIRN_SMTP_HOST"), port=int(os.environ.get("CAIRN_SMTP_PORT", "587")),
                          username=_env("CAIRN_SMTP_USERNAME"), password=_secret("CAIRN_SMTP_PASSWORD"),
                          sender=_env("CAIRN_EMAIL_FROM"))
    raise RuntimeError("CAIRN_EMAIL_PROVIDER must be twilio or smtp.")


def cleanup_settings_from_env() -> CleanupSettings:
    return CleanupSettings(
        auth0_domain=_env("CAIRN_AUTH0_DOMAIN"),
        auth0_client_id=_env("CAIRN_AUTH0_MGMT_CLIENT_ID"),
        auth0_client_secret=_secret("CAIRN_AUTH0_MGMT_CLIENT_SECRET"),
        apple_client_id=_env("CAIRN_APPLE_CLIENT_ID"),
        apple_team_id=_env("CAIRN_APPLE_TEAM_ID"),
        apple_key_id=_env("CAIRN_APPLE_KEY_ID"),
        apple_private_key=_secret("CAIRN_APPLE_PRIVATE_KEY"),
    )


def outbound() -> dict:
    """UC-CASE-19, UC-CASE-21, UC-REG-15. Every 5 minutes."""
    outcome = run_outbound(jobs_database(), mailer_from_env(),
                           load_copy(os.environ.get("CAIRN_REGISTRATION_COPY") or None),
                           load_case_copy(os.environ.get("CAIRN_CASE_COPY") or None))
    return {"sent": outcome.sent, "failed": outcome.failed}


def identity_cleanup() -> dict:
    """UC-ACCT-01. Every 15 minutes."""
    cleanup_settings = cleanup_settings_from_env()
    with httpx.Client(timeout=15) as http:
        done, failed = run_identity_cleanup(jobs_database(), IdentityCleanup(cleanup_settings, http))
    return {"sent": done, "failed": failed}


# D-2026-10-05-R1. An account that hasn't finished sign-up is deleted 90 days after it was created (UC-REG-10).
PENDING_ACCOUNT_RETENTION_DAYS = 90


def _days(name: str, default: int | None = None) -> timedelta:
    raw = os.environ.get(name)
    days = int(raw) if raw else default
    if days is None:
        raise NotConfigured(name)
    if days < 1:
        raise RuntimeError(f"{name} must be at least 1.")
    return timedelta(days=days)


def purge_stale_accounts() -> dict:
    """UC-REG-10 and UC-REG-13. Daily. Deletes accounts that haven't finished sign-up 90 days after they were
    created (D-2026-10-05-R1). Accounts that finished sign-up but never created a case are deleted only when
    CAIRN_NO_CASE_ACCOUNT_RETENTION_DAYS is set, because that period isn't decided. [LEGAL REVIEW REQUIRED]"""
    pending = _days("CAIRN_PENDING_ACCOUNT_RETENTION_DAYS", PENDING_ACCOUNT_RETENTION_DAYS)
    no_case = _days("CAIRN_NO_CASE_ACCOUNT_RETENTION_DAYS") if os.environ.get(
        "CAIRN_NO_CASE_ACCOUNT_RETENTION_DAYS") else None
    return {"affected": maintenance.purge_stale_accounts(jobs_database(), pending, no_case), "failed": 0}


def _database_job(name: str) -> Callable[[], dict]:
    def run() -> dict:
        return {"affected": maintenance.JOBS[name](jobs_database()), "failed": 0}
    run.__name__ = name
    return run


# Name to job. The names are the Worker's contract (cloudflare/src/index.ts).
JOBS: dict[str, Callable[[], dict]] = {
    "outbound": outbound,
    "identity_cleanup": identity_cleanup,
    "purge_stale_accounts": purge_stale_accounts,                      # UC-REG-10, UC-REG-13, daily
    "purge_held_cases": _database_job("purge_held_cases"),            # UC-END-13, at least hourly
    "purge_inactive_drafts": _database_job("purge_inactive_drafts"),  # DEC-07, at least daily
    "expire_trials": _database_job("expire_trials"),                  # reporting only
    "settle_trial_clocks": _database_job("settle_trial_clocks"),      # care rests that ended, at least hourly
}


def create_jobs_app(jobs: dict[str, Callable[[], dict]] | None = None) -> FastAPI:
    registry = JOBS if jobs is None else jobs
    app = FastAPI(title="Cairn jobs", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/jobs/{name}")
    def run_job(name: str) -> JSONResponse:
        job = registry.get(name)
        if job is None:
            return JSONResponse({"job": name, "status": "unknown"}, status_code=404)
        try:
            result = job()
        except NotConfigured as missing:
            log.warning("job %s skipped: %s is not set", name, missing)
            return JSONResponse({"job": name, "status": "skipped", "missing": str(missing)})
        except Exception as exc:  # reported by class name only, never the message
            log.error("job %s failed: %s", name, type(exc).__name__)
            return JSONResponse({"job": name, "status": "error", "error": type(exc).__name__}, status_code=500)
        status = "partial" if result.get("failed") else "ok"
        log.info("job %s %s %s", name, status, result)
        return JSONResponse({"job": name, "status": status, **result}, status_code=500 if result.get("failed") else 200)

    return app


app = create_jobs_app()
