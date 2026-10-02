"""Send what Cairn sends outside the app, once (UC-CASE-19, UC-CASE-21, UC-REG-15). Schedule it every 5 minutes.

Connects as the cairnJobs MongoDB user, never the app user. Every value comes from the
environment and the secret manager. Nothing here is committed. On Cloudflare the
same job runs through cairn_api.jobs instead (see the repository README).

  CAIRN_JOBS_MONGODB_URI     the cairnJobs user's connection string
  CAIRN_MONGODB_DB           the Cairn database name, cairn by default
  CAIRN_SMTP_HOST            the email provider's SMTP host
  CAIRN_SMTP_PORT            587 by default (STARTTLS)
  CAIRN_SMTP_USERNAME
  CAIRN_SMTP_PASSWORD_FILE   path to the SMTP password (or CAIRN_SMTP_PASSWORD, the value itself)
  CAIRN_EMAIL_FROM           for example "Cairn <no-reply@mail.example>". Register this domain with
                             Apple's Private Email Relay Service so relay addresses receive it.
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
