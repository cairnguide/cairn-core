"""Live checks against Twilio. Run by hand only, from GitHub Actions > Twilio integration > Run workflow
(.github/workflows/twilio-integration.yml), or locally with the same variables.

This folder is outside pytest's testpaths (api/pyproject.toml), so the push and pull request workflows never
collect it, and nothing here runs on a schedule. The trial account has a limited number of messages, so:

* The credential check always runs. It only reads the account, so it is free.
* Each message is opt-in. One run sends at most two text messages (one by Programmable Messaging, one by
  Verify) and one email. Leave the inputs off and nothing is sent.
* A message that was asked for but has a missing setting fails the run instead of skipping, so a green run
  always means the message really went out.

  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN      always
  CAIRN_TWILIO_LIVE_SMS=1                    send one text with TWILIO_FROM_NUMBER (or
                                             TWILIO_MESSAGING_SERVICE_SID) to TWILIO_TEST_TO_NUMBER
  CAIRN_TWILIO_LIVE_VERIFY=1                 start one Verify code to TWILIO_TEST_TO_NUMBER with
                                             TWILIO_VERIFY_SERVICE_SID, then check a wrong code is refused
  CAIRN_TWILIO_LIVE_EMAIL=1                  send one email with TWILIO_SENDGRID_API_KEY from CAIRN_EMAIL_FROM to
                                             TWILIO_TEST_TO_EMAIL. Use an Apple relay address to check UC-REG-03.

On a trial account TWILIO_TEST_TO_NUMBER must be a verified caller ID in the Twilio console. Nothing here prints
a phone number, an address, or a credential.
"""
from __future__ import annotations

import os

import pytest

from cairn_api.twilio_client import SendGridMailer, TwilioClient

SMS_BODY = "Cairn integration test. No action needed."
EMAIL_SUBJECT = "Cairn: integration test"
EMAIL_BODY = "This is a test of Cairn's email delivery through Twilio SendGrid. No action needed."


def _need(*names: str) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        pytest.fail(f"Set {', '.join(missing)} (GitHub repository secrets) for this check.")
    return {n: os.environ[n] for n in names}


def _wanted(flag: str) -> None:
    if os.environ.get(flag) != "1":
        pytest.skip(f"{flag} is not 1, so nothing is sent.")


def _client(**extra) -> TwilioClient:
    env = _need("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN")
    return TwilioClient(env["TWILIO_ACCOUNT_SID"], env["TWILIO_AUTH_TOKEN"], **extra)


def test_the_credentials_work_and_the_account_is_active():
    info = _client().account()
    print(f"Twilio account type: {info.type}")
    assert info.status == "active"


def test_one_text_message_is_accepted():
    _wanted("CAIRN_TWILIO_LIVE_SMS")
    if not (os.environ.get("TWILIO_FROM_NUMBER") or os.environ.get("TWILIO_MESSAGING_SERVICE_SID")):
        pytest.fail("Set TWILIO_FROM_NUMBER or TWILIO_MESSAGING_SERVICE_SID for this check.")
    to = _need("TWILIO_TEST_TO_NUMBER")["TWILIO_TEST_TO_NUMBER"]
    client = _client(from_number=os.environ.get("TWILIO_FROM_NUMBER") or None,
                     messaging_service_sid=os.environ.get("TWILIO_MESSAGING_SERVICE_SID") or None)
    assert client.send_sms(to, SMS_BODY).startswith(("SM", "MM"))


def test_one_verify_code_is_sent_and_a_wrong_code_is_refused():
    _wanted("CAIRN_TWILIO_LIVE_VERIFY")
    env = _need("TWILIO_VERIFY_SERVICE_SID", "TWILIO_TEST_TO_NUMBER")
    client = _client(verify_service_sid=env["TWILIO_VERIFY_SERVICE_SID"])
    to = env["TWILIO_TEST_TO_NUMBER"]
    assert client.start_verification(to) == "pending"
    assert client.check_verification(to, "000000") is False


def test_one_email_is_accepted_by_twilio_sendgrid():
    _wanted("CAIRN_TWILIO_LIVE_EMAIL")
    env = _need("TWILIO_SENDGRID_API_KEY", "CAIRN_EMAIL_FROM", "TWILIO_TEST_TO_EMAIL")
    SendGridMailer(api_key=env["TWILIO_SENDGRID_API_KEY"], sender=env["CAIRN_EMAIL_FROM"]).send(
        env["TWILIO_TEST_TO_EMAIL"], EMAIL_SUBJECT, EMAIL_BODY)
