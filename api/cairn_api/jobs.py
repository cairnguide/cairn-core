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
  CAIRN_REGISTRATION_COPY, CAIRN_CASE_COPY, CAIRN_BREAK_COPY, CAIRN_SUBSCRIPTION_COPY
                                    optional, the same replacement copy files the API uses
  CAIRN_VAPID_PRIVATE_KEY           or CAIRN_VAPID_PRIVATE_KEY_FILE, the browser notification key (PEM, P-256).
                                    Without it, browser notifications are skipped and email still goes
  CAIRN_VAPID_SUBJECT               a mailto: or https: contact for the push services
  CAIRN_STRIPE_SECRET_KEY           or CAIRN_STRIPE_SECRET_KEY_FILE, for the Stripe retry jobs
  CAIRN_PRICE_CHANGE_EFFECTIVE_DATE, CAIRN_PRICE_CHANGE_NEW_PRICE
                                    UC-SUB-18, only while a price change is scheduled, for example 2027-03-01
                                    and $16.99. Unset, the price_change_notices job is skipped

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
from .copy_store import load_break_copy, load_case_copy, load_copy, load_subscription_copy
from .db import client_for
from .identity_cleanup import CleanupSettings, IdentityCleanup
from .identity_cleanup import run_once as run_identity_cleanup
from .outbound import Mailer, PushSender, SmtpMailer
from .outbound import run_once as run_outbound
from .stripe_client import Stripe
from .twilio_client import SendGridMailer
from .webpush import WebPushSender, public_key_from_pem

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


def push_from_env() -> PushSender | None:
    """Browser notifications, when a VAPID key is set. Optional: without it, email still goes."""
    key = secret_from_env("CAIRN_VAPID_PRIVATE_KEY")
    if not key:
        return None
    return WebPushSender(private_key_pem=key, public_key=public_key_from_pem(key),
                         subject=_env("CAIRN_VAPID_SUBJECT"))


def stripe_from_env() -> Stripe:
    return Stripe(_secret("CAIRN_STRIPE_SECRET_KEY"))


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
    """UC-CASE-19, UC-CASE-21, D-14, DEC-26-04, UC-BRK-09, UC-SUB-17. Every 5 minutes."""
    outcome = run_outbound(jobs_database(), mailer_from_env(),
                           load_copy(os.environ.get("CAIRN_REGISTRATION_COPY") or None),
                           load_case_copy(os.environ.get("CAIRN_CASE_COPY") or None),
                           break_copy=load_break_copy(os.environ.get("CAIRN_BREAK_COPY") or None),
                           sub_copy=load_subscription_copy(os.environ.get("CAIRN_SUBSCRIPTION_COPY") or None),
                           push=push_from_env())
    return {"sent": outcome.sent, "failed": outcome.failed}


def process_stripe_events() -> dict:
    """UC-SUB-22. Events the webhook couldn't finish, fetched again from Stripe. Every 15 minutes."""
    done, failed = maintenance.process_stripe_events(jobs_database(), stripe_from_env())
    return {"affected": done, "failed": failed}


def retry_cancellations() -> dict:
    """UC-SUB-13. Cancel requests recorded while Stripe couldn't be reached. Every 15 minutes."""
    done, failed = maintenance.retry_cancellations(jobs_database(), stripe_from_env())
    return {"affected": done, "failed": failed}


def price_change_notices() -> dict:
    """UC-SUB-18. Daily, only while a price change is configured."""
    from datetime import datetime, timezone
    effective = datetime.fromisoformat(_env("CAIRN_PRICE_CHANGE_EFFECTIVE_DATE")).replace(tzinfo=timezone.utc)
    from .outbound import send_price_change_notices
    outcome = send_price_change_notices(jobs_database(), mailer_from_env(),
                                        load_subscription_copy(os.environ.get("CAIRN_SUBSCRIPTION_COPY") or None),
                                        effective, _env("CAIRN_PRICE_CHANGE_NEW_PRICE"))
    return {"sent": outcome.sent, "failed": outcome.failed}


def sync_early_subscriptions() -> dict:
    """UC-SUB-06. An early subscriber's first charge follows the free days' end. Hourly."""
    return {"affected": maintenance.sync_early_subscriptions(jobs_database(), stripe_from_env()), "failed": 0}


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
    "expire_trials": _database_job("expire_trials"),                  # keeps users.access current, daily
    "settle_trial_clocks": _database_job("settle_trial_clocks"),      # care rests that ended, at least hourly
    "process_stripe_events": process_stripe_events,                    # UC-SUB-22, every 15 minutes
    "retry_cancellations": retry_cancellations,                        # UC-SUB-13, every 15 minutes
    "sync_early_subscriptions": sync_early_subscriptions,              # UC-SUB-06, hourly
    "price_change_notices": price_change_notices,                      # UC-SUB-18, daily, when configured
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
