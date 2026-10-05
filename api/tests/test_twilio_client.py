"""The Twilio email, text message, and Verify clients, and how the jobs service picks the email provider.

No network: every request goes to httpx.MockTransport, so these run on every push and pull request and never
use Twilio trial credit. The live check is api/integration/test_twilio_live.py, run by hand from GitHub Actions
(.github/workflows/twilio-integration.yml).
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from cairn_api import jobs, outbound
from cairn_api.outbound import SmtpMailer
from cairn_api.twilio_client import DeliveryError, SendGridMailer, TwilioClient

SID = "AC" + "0" * 32          # a made-up Account SID, never a real one
TOKEN = "test-auth-token"


def recorder(status: int = 200, body: dict | None = None):
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body if body is not None else {})
    return seen, httpx.Client(transport=httpx.MockTransport(handle))


def form(request: httpx.Request) -> dict:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


# ------------------------------------------------------------------ email (Twilio SendGrid)

def test_email_goes_to_sendgrid_as_plain_text_with_every_kind_of_tracking_off():
    seen, http = recorder(202)
    SendGridMailer(api_key="SG.test", sender="Cairn <no-reply@mail.example>", http=http).send(
        "pat@privaterelay.appleid.com", "Cairn: confirmation", "Your Cairn account has been deleted.")
    (r,) = seen
    assert r.method == "POST" and str(r.url) == "https://api.sendgrid.com/v3/mail/send"
    assert r.headers["Authorization"] == "Bearer SG.test"
    body = json.loads(r.content)
    assert body["from"] == {"email": "no-reply@mail.example", "name": "Cairn"}
    assert body["personalizations"] == [{"to": [{"email": "pat@privaterelay.appleid.com"}]}]
    assert body["content"] == [{"type": "text/plain", "value": "Your Cairn account has been deleted."}]
    # [PRIVACY] D-07. No pixel, no rewritten links, no unsubscribe footer that implies a mailing list.
    tracking = body["tracking_settings"]
    assert tracking["open_tracking"]["enable"] is False
    assert tracking["click_tracking"]["enable"] is False
    assert tracking["subscription_tracking"]["enable"] is False
    assert tracking["ganalytics"]["enable"] is False


def test_a_sender_with_no_address_is_refused():
    with pytest.raises(ValueError):
        SendGridMailer(api_key="SG.test", sender="Cairn").payload("a@example.test", "s", "b")


def test_a_refused_email_raises_a_code_and_never_the_address_or_body():
    seen, http = recorder(400, {"errors": [{"message": "bad address pat@example.test", "field": "to"}]})
    with pytest.raises(DeliveryError) as exc:
        SendGridMailer(api_key="SG.test", sender="no-reply@mail.example", http=http).send(
            "pat@example.test", "s", "secret body")
    assert str(exc.value) == "http_400"
    assert "pat" not in str(exc.value) and "secret" not in str(exc.value)


def test_a_network_failure_is_a_delivery_error_by_class_name():
    def boom(request):
        raise httpx.ConnectError("could not reach api.sendgrid.com for pat@example.test")
    http = httpx.Client(transport=httpx.MockTransport(boom))
    with pytest.raises(DeliveryError) as exc:
        SendGridMailer(api_key="SG.test", sender="no-reply@mail.example", http=http).send("pat@example.test", "s", "b")
    assert str(exc.value) == "ConnectError"


def test_outbound_logs_the_twilio_code_only():
    class Refusing:
        def send(self, to, subject, body):
            raise DeliveryError("http_403")
    assert outbound._try(Refusing(), "pat@example.test", outbound.Message("s", "b")) == "http_403"


# ------------------------------------------------------------------ text messages

def test_a_text_message_uses_basic_auth_and_the_from_number():
    seen, http = recorder(201, {"sid": "SM123", "status": "queued"})
    client = TwilioClient(SID, TOKEN, from_number="+15005550006", http=http)
    assert client.send_sms("+15005550010", "A note from Cairn") == "SM123"
    (r,) = seen
    assert str(r.url) == f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json"
    assert r.headers["Authorization"] == httpx.BasicAuth(SID, TOKEN)._auth_header
    assert form(r) == {"To": "+15005550010", "From": "+15005550006", "Body": "A note from Cairn"}


def test_a_messaging_service_is_preferred_over_the_from_number():
    seen, http = recorder(201, {"sid": "SM1"})
    TwilioClient(SID, TOKEN, from_number="+15005550006", messaging_service_sid="MG1", http=http).send_sms("+1555", "x")
    assert form(seen[0]) == {"To": "+1555", "Body": "x", "MessagingServiceSid": "MG1"}


def test_a_text_message_needs_a_sender():
    with pytest.raises(ValueError):
        TwilioClient(SID, TOKEN).send_sms("+15005550010", "x")


def test_a_trial_account_refusing_an_unverified_number_keeps_twilios_code_only():
    _, http = recorder(400, {"code": 21608, "message": "The number +15005550010 is unverified.", "status": 400})
    with pytest.raises(DeliveryError) as exc:
        TwilioClient(SID, TOKEN, from_number="+15005550006", http=http).send_sms("+15005550010", "x")
    assert str(exc.value) == "http_400_twilio_21608"
    assert "5550010" not in str(exc.value)


def test_checking_the_account_sends_nothing():
    seen, http = recorder(200, {"status": "active", "type": "Trial", "friendly_name": "My first account"})
    info = TwilioClient(SID, TOKEN, http=http).account()
    assert (info.status, info.type) == ("active", "Trial")
    assert seen[0].method == "GET" and str(seen[0].url).endswith(f"/Accounts/{SID}.json")


# ------------------------------------------------------------------ Verify

def test_verify_starts_a_code_by_text_and_checks_it():
    seen, http = recorder(201, {"status": "pending"})
    client = TwilioClient(SID, TOKEN, verify_service_sid="VA1", http=http)
    assert client.start_verification("+15005550010") == "pending"
    assert str(seen[0].url) == "https://verify.twilio.com/v2/Services/VA1/Verifications"
    assert form(seen[0]) == {"To": "+15005550010", "Channel": "sms"}

    _, http = recorder(200, {"status": "approved"})
    assert TwilioClient(SID, TOKEN, verify_service_sid="VA1", http=http).check_verification("+15005550010", "123456")
    _, http = recorder(200, {"status": "pending"})
    assert not TwilioClient(SID, TOKEN, verify_service_sid="VA1", http=http).check_verification("+1555", "000000")


def test_an_expired_or_used_code_is_simply_not_approved():
    _, http = recorder(404, {"code": 20404, "message": "not found"})
    assert TwilioClient(SID, TOKEN, verify_service_sid="VA1", http=http).check_verification("+1555", "123456") is False


def test_verify_needs_a_service():
    with pytest.raises(ValueError):
        TwilioClient(SID, TOKEN).start_verification("+15005550010")


# ------------------------------------------------------------------ choosing the email provider

EMAIL_ENV = ("CAIRN_EMAIL_PROVIDER", "TWILIO_SENDGRID_API_KEY", "TWILIO_SENDGRID_API_KEY_FILE", "CAIRN_EMAIL_FROM",
             "CAIRN_SMTP_HOST", "CAIRN_SMTP_USERNAME", "CAIRN_SMTP_PASSWORD", "CAIRN_SMTP_PASSWORD_FILE")


@pytest.fixture
def clean_env(monkeypatch):
    for name in EMAIL_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_twilio_sendgrid_is_the_default_email_provider(clean_env):
    clean_env.setenv("TWILIO_SENDGRID_API_KEY", "SG.test")
    clean_env.setenv("CAIRN_EMAIL_FROM", "Cairn <no-reply@mail.example>")
    mailer = jobs.mailer_from_env()
    assert isinstance(mailer, SendGridMailer) and mailer.api_key == "SG.test"


def test_without_the_sendgrid_key_the_outbound_job_is_skipped(clean_env):
    clean_env.setenv("CAIRN_EMAIL_FROM", "Cairn <no-reply@mail.example>")
    with pytest.raises(jobs.NotConfigured, match="TWILIO_SENDGRID_API_KEY"):
        jobs.mailer_from_env()


def test_smtp_stays_available_for_local_development(clean_env):
    clean_env.setenv("CAIRN_EMAIL_PROVIDER", "smtp")
    for name, value in (("CAIRN_SMTP_HOST", "localhost"), ("CAIRN_SMTP_USERNAME", "u"),
                        ("CAIRN_SMTP_PASSWORD", "p"), ("CAIRN_EMAIL_FROM", "no-reply@mail.example")):
        clean_env.setenv(name, value)
    assert isinstance(jobs.mailer_from_env(), SmtpMailer)


def test_an_unknown_email_provider_is_refused(clean_env):
    clean_env.setenv("CAIRN_EMAIL_PROVIDER", "pigeon")
    with pytest.raises(RuntimeError):
        jobs.mailer_from_env()
