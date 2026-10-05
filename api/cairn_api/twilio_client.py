"""Twilio: the one provider for every email and text message Cairn sends.

Three Twilio products, each over its REST API with httpx (no SDK):

* Email goes through Twilio SendGrid's v3 Mail Send API. SendGrid authenticates with its own API key
  (TWILIO_SENDGRID_API_KEY), not the Twilio Account SID and Auth Token: Twilio issues the two separately.
* Text messages go through Twilio Programmable Messaging, with the Account SID and Auth Token.
* Phone number verification by code goes through Twilio Verify, with the Account SID and Auth Token.

Where they are used today
-------------------------
Email is live: every message in outbound.py (deletion confirmations, trial reminders, notifications, and the
opted-in check-in) goes through SendGridMailer once TWILIO_SENDGRID_API_KEY and CAIRN_EMAIL_FROM are set.
Auth0 sends the sign-in magic link itself (UC-REG-04). Point Auth0's email provider at SendGrid so that goes
through Twilio too (auth0/README.md).

Text messages and Verify are built and tested here, and nothing user-facing calls them. Text messages are not
in the MVP (OPEN-05, decided 2026-10-05). If they are added later, after legal review of the consent line,
UC-CASE-19's text_message flow collects the number just in time, verifies it with
TwilioClient.start_verification and check_verification, and outbound.py sends with TwilioClient.send_sms.

Privacy
-------
Errors carry an HTTP status and Twilio's numeric error code only. Never the response body, the address, the
phone number, or the message, since providers echo those back. Open and click tracking are switched off on
every email, so SendGrid never rewrites links or adds a tracking pixel (D-07, no marketing or analytics).
"""
from __future__ import annotations

from dataclasses import dataclass
from email.utils import parseaddr

import httpx

TWILIO_API = "https://api.twilio.com/2010-04-01"
VERIFY_API = "https://verify.twilio.com/v2"
SENDGRID_API = "https://api.sendgrid.com/v3"
TIMEOUT = 15.0


class DeliveryError(Exception):
    """A provider refused or could not be reached. str() is a short code that is safe to log."""


def _error_code(r: httpx.Response) -> str:
    """http_<status>, plus Twilio's numeric error code when the body has one (for example 21608, an
    unverified number on a trial account). Nothing else from the body is kept."""
    code = None
    try:
        body = r.json()
        if isinstance(body, dict) and isinstance(body.get("code"), int):
            code = body["code"]
    except ValueError:
        pass
    return f"http_{r.status_code}" + (f"_twilio_{code}" if code else "")


def _request(http: httpx.Client | None, method: str, url: str, **kw) -> httpx.Response:
    try:
        if http is None:
            with httpx.Client(timeout=TIMEOUT) as client:
                r = client.request(method, url, **kw)
        else:
            r = http.request(method, url, **kw)
    except httpx.HTTPError as exc:
        raise DeliveryError(type(exc).__name__) from None
    if r.status_code >= 400:
        raise DeliveryError(_error_code(r))
    return r


# ------------------------------------------------------------------ email (Twilio SendGrid)

@dataclass(frozen=True)
class SendGridMailer:
    """Implements outbound.Mailer. Plain text only, one recipient per message, no tracking."""
    api_key: str            # TWILIO_SENDGRID_API_KEY, from the secret manager, never from the repo
    sender: str             # CAIRN_EMAIL_FROM, for example "Cairn <no-reply@mail.example>"
    http: httpx.Client | None = None

    def payload(self, to: str, subject: str, body: str) -> dict:
        name, address = parseaddr(self.sender)
        if "@" not in address:
            raise ValueError("CAIRN_EMAIL_FROM must be an address, for example Cairn <no-reply@mail.example>.")
        sender = {"email": address, **({"name": name} if name else {})}
        return {
            "personalizations": [{"to": [{"email": to}]}],
            "from": sender,
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}],
            "tracking_settings": {"click_tracking": {"enable": False, "enable_text": False},
                                  "open_tracking": {"enable": False},
                                  "subscription_tracking": {"enable": False},
                                  "ganalytics": {"enable": False}},
        }

    def send(self, to: str, subject: str, body: str) -> None:
        _request(self.http, "POST", f"{SENDGRID_API}/mail/send", json=self.payload(to, subject, body),
                 headers={"Authorization": f"Bearer {self.api_key}"})


# ------------------------------------------------------------------ text messages and Verify

@dataclass(frozen=True)
class AccountInfo:
    status: str     # active, suspended, or closed
    type: str       # Trial or Full


@dataclass(frozen=True)
class TwilioClient:
    account_sid: str                         # TWILIO_ACCOUNT_SID
    auth_token: str                          # TWILIO_AUTH_TOKEN, from the secret manager
    from_number: str | None = None           # TWILIO_FROM_NUMBER, E.164, for example +15005550006
    messaging_service_sid: str | None = None # TWILIO_MESSAGING_SERVICE_SID, used instead of from_number when set
    verify_service_sid: str | None = None    # TWILIO_VERIFY_SERVICE_SID
    http: httpx.Client | None = None

    @property
    def _auth(self) -> tuple[str, str]:
        return (self.account_sid, self.auth_token)

    def account(self) -> AccountInfo:
        """Checks the credentials. Free: sends nothing and uses no trial credit."""
        r = _request(self.http, "GET", f"{TWILIO_API}/Accounts/{self.account_sid}.json", auth=self._auth)
        body = r.json()
        return AccountInfo(status=body.get("status", ""), type=body.get("type", ""))

    def send_sms(self, to: str, body: str) -> str:
        """Sends one text message. Returns Twilio's message SID. On a trial account, `to` must be a verified
        caller ID, and Twilio adds "Sent from your Twilio trial account" to the text."""
        form = {"To": to, "Body": body}
        if self.messaging_service_sid:
            form["MessagingServiceSid"] = self.messaging_service_sid
        elif self.from_number:
            form["From"] = self.from_number
        else:
            raise ValueError("Set TWILIO_FROM_NUMBER or TWILIO_MESSAGING_SERVICE_SID to send text messages.")
        r = _request(self.http, "POST", f"{TWILIO_API}/Accounts/{self.account_sid}/Messages.json",
                     data=form, auth=self._auth)
        return r.json()["sid"]

    def _verify_url(self, path: str) -> str:
        if not self.verify_service_sid:
            raise ValueError("Set TWILIO_VERIFY_SERVICE_SID to verify phone numbers.")
        return f"{VERIFY_API}/Services/{self.verify_service_sid}/{path}"

    def start_verification(self, to: str, channel: str = "sms") -> str:
        """Twilio Verify texts a one-time code to `to`. Returns the verification status (pending)."""
        r = _request(self.http, "POST", self._verify_url("Verifications"), data={"To": to, "Channel": channel},
                     auth=self._auth)
        return r.json()["status"]

    def check_verification(self, to: str, code: str) -> bool:
        """True only when Twilio says the code is approved. A wrong or expired code is False, not an error."""
        try:
            r = _request(self.http, "POST", self._verify_url("VerificationCheck"), data={"To": to, "Code": code},
                         auth=self._auth)
        except DeliveryError as exc:
            if str(exc).startswith("http_404"):   # Verify answers 404 once a verification has expired or been used
                return False
            raise
        return r.json().get("status") == "approved"
