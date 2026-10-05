"""Send what Cairn sends outside the app, once (UC-CASE-19, UC-CASE-21, UC-REG-15). Schedule it every 5 minutes.

Connects as the cairnJobs MongoDB user, never the app user. Every value comes from the
environment and the secret manager. Nothing here is committed. On Cloudflare the
same job runs through cairn_api.jobs instead (see the repository README).

  CAIRN_JOBS_MONGODB_URI     the cairnJobs user's connection string
  CAIRN_MONGODB_DB           the Cairn database name, cairn by default
  TWILIO_SENDGRID_API_KEY    the Twilio SendGrid key with Mail Send access (or TWILIO_SENDGRID_API_KEY_FILE)
  CAIRN_EMAIL_FROM           for example "Cairn <no-reply@mail.example>". Authenticate this domain in SendGrid
                             and register it with Apple's Private Email Relay Service so relay addresses
                             receive it.
  CAIRN_EMAIL_PROVIDER       twilio by default. smtp, with CAIRN_SMTP_HOST, CAIRN_SMTP_PORT,
                             CAIRN_SMTP_USERNAME, and CAIRN_SMTP_PASSWORD(_FILE), for local development only
  CAIRN_REGISTRATION_COPY    optional, the same replacement copy file the API uses
  CAIRN_CASE_COPY            optional, the same replacement copy file the API uses
"""
import logging
import os
import sys

from cairn_api.copy_store import load_case_copy, load_copy
from cairn_api.jobs import jobs_database, mailer_from_env
from cairn_api.outbound import run_once

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    env = os.environ
    outcome = run_once(jobs_database(), mailer_from_env(),
                       load_copy(env.get("CAIRN_REGISTRATION_COPY") or None),
                       load_case_copy(env.get("CAIRN_CASE_COPY") or None))
    print(f"outbound: sent {outcome.sent}, failed {outcome.failed}")
    sys.exit(1 if outcome.failed else 0)
