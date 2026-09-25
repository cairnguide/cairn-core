"""Send what Cairn sends outside the app, once (UC-CASE-19, UC-CASE-21, UC-REG-15). Schedule it every 5 minutes.

Connects as the OWNER role, never the app role. Every value comes from the
environment and the secret manager. Nothing here is committed.

  CAIRN_OWNER_DATABASE_URL   owner connection string
  CAIRN_SMTP_HOST            the email provider's SMTP host
  CAIRN_SMTP_PORT            587 by default (STARTTLS)
  CAIRN_SMTP_USERNAME
  CAIRN_SMTP_PASSWORD_FILE   path to the SMTP password
  CAIRN_EMAIL_FROM           for example "Cairn <no-reply@mail.example>". Register this domain with
                             Apple's Private Email Relay Service so relay addresses receive it.
  CAIRN_REGISTRATION_COPY    optional, the same replacement copy file the API uses
  CAIRN_CASE_COPY            optional, the same replacement copy file the API uses
"""
import logging
import os
import pathlib
import sys

from cairn_api.copy_store import load_case_copy, load_copy
from cairn_api.outbound import SmtpMailer, run_once

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    env = os.environ
    mailer = SmtpMailer(host=env["CAIRN_SMTP_HOST"], port=int(env.get("CAIRN_SMTP_PORT", "587")),
                        username=env["CAIRN_SMTP_USERNAME"],
                        password=pathlib.Path(env["CAIRN_SMTP_PASSWORD_FILE"]).read_text().strip(),
                        sender=env["CAIRN_EMAIL_FROM"])
    outcome = run_once(env["CAIRN_OWNER_DATABASE_URL"], mailer,
                       load_copy(env.get("CAIRN_REGISTRATION_COPY") or None),
                       load_case_copy(env.get("CAIRN_CASE_COPY") or None))
    print(f"outbound: sent {outcome.sent}, failed {outcome.failed}")
    sys.exit(1 if outcome.failed else 0)
