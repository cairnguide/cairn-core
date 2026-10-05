"""Messages Cairn sends outside the app. Run on a schedule as the cairnJobs user.

Three queues, in this order:
1. Confirmations for something the user just did (UC-CASE-21, UC-REG-15): case deleted now, case deleted
   after the 7-day hold, account deleted. Exactly one each, by email, the only message sent outside
   notification_preferences. On success the address is purged and the send is logged without content.
   On failure the claim is released and the next run tries again.
2. Trial reminders, only to users who chose email for a journey (0011, UC-CASE-12 change). They always
   show in the app regardless.
3. Notifications the user chose (UC-CASE-19): a step coming up, or checking in after a quiet stretch,
   by email, at the pace the user chose. Browser notifications are not delivered yet (no web push provider).
4. The one check-in a user said yes to after a hard moment (DEC-26-04), by email when they chose email.

While a journey is resting, nothing goes out except confirmations and an opted-in check-in (crisis plan).
Before anything is sent, private_enough checks that it names no person who died and no circumstance
(UC-CASE-19). A message that fails is not sent.

Every message is short and private. It never names the person who died, the circumstance, task details,
or anything that was deleted. Nothing personal is logged: counts and error codes only.

Email goes through Twilio SendGrid (twilio_client.SendGridMailer, chosen in jobs.mailer_from_env). SmtpMailer
stays for local development with an SMTP catcher. The sending domain must be authenticated in SendGrid (SPF and
DKIM) and registered with Apple's Private Email Relay Service so relay addresses receive mail (UC-REG-03).
"""
from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Protocol

from . import account as acct
from . import maintenance
from .copy_store import Copy
from .twilio_client import DeliveryError

log = logging.getLogger("cairn_api.outbound")

CONFIRMATION_KEYS = {"case_deleted_now": "email_case_deleted_now",
                     "case_deleted_after_hold": "email_case_deleted_after_hold",
                     "account_deleted": "email_account_deleted"}


class Mailer(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


@dataclass(frozen=True)
class SmtpMailer:
    host: str
    port: int
    username: str
    password: str       # from the secret manager, never from the repo
    sender: str         # for example "Cairn <no-reply@mail.example>"
    timeout: float = 15

    def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            smtp.starttls()
            smtp.login(self.username, self.password)
            smtp.send_message(msg)


@dataclass
class Outcome:
    sent: dict[str, int] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)

    def count(self, queue: str, ok: bool) -> None:
        table = self.sent if ok else self.failed
        table[queue] = table.get(queue, 0) + 1


@dataclass(frozen=True)
class Message:
    subject: str
    body: str


def confirmation(action_type: str, copy: Copy) -> Message:
    return Message(copy["email_subject_confirmation"], copy[CONFIRMATION_KEYS[action_type]])


def notification(reason: str, copy: Copy, case_copy: Copy) -> Message:
    body = case_copy["notification_example"] if reason == "due_date_upcoming" else copy["email_notification_inactivity"]
    return Message(copy["email_subject_notification"], body)


def trial_reminder(row: dict, copy: Copy) -> Message:
    return Message(copy["email_subject_trial"], acct.reminder_text(row["kind"], row, copy))


def check_in(case_copy: Copy) -> Message:
    return Message(case_copy["check_in_email_subject"], case_copy["check_in_outside_cairn"])


# Every circumstance choice's words, which no outbound message may contain (UC-CASE-19).
def circumstance_terms(case_copy: Copy) -> list[str]:
    return [case_copy[k] for k in case_copy.spec | case_copy.flow | case_copy.draft
            if k.startswith("label_circumstance_") and not k.endswith("prefer_not_to_say")]


def private_enough(msg: Message, forbidden: list[str]) -> bool:
    """[PRIVACY] UC-CASE-19. The message never names the person who died or the circumstance."""
    text = f"{msg.subject}\n{msg.body}".casefold()
    return not any(term and term.casefold() in text for term in forbidden)


def _try(mailer: Mailer, to: str, msg: Message) -> str | None:
    """Send one message. Returns an error code, never the address or the provider's message."""
    try:
        mailer.send(to, msg.subject, msg.body)
    except DeliveryError as exc:
        return str(exc)[:100]
    except (smtplib.SMTPException, OSError) as exc:
        return type(exc).__name__[:100]
    return None


def run_once(db, mailer: Mailer, copy: Copy, case_copy: Copy, limit: int = 100) -> Outcome:
    """Works through all three queues once. db is the Cairn database, connected as the cairnJobs user."""
    out = Outcome()

    for row in maintenance.claim_action_confirmations(db, limit):
        error = _try(mailer, row["email"], confirmation(row["action_type"], copy))
        if error is None:
            maintenance.complete_action_confirmation(db, row["confirmation_id"])
        else:
            maintenance.release_action_confirmation(db, row["confirmation_id"], error)
            log.warning("confirmation not sent id=%s code=%s", row["confirmation_id"], error)
        out.count("confirmations", error is None)

    # Claimed rows are marked sent first. A failed send is not repeated with a date that no longer fits.
    for row in maintenance.claim_due_trial_reminders(db, limit):
        error = _try(mailer, row["email"], trial_reminder(row, copy))
        if error:
            log.warning("trial reminder not sent id=%s code=%s", row["reminder_id"], error)
        out.count("trial_reminders", error is None)

    # Logged when claimed. A missed nudge is better than a repeated one.
    circumstances = circumstance_terms(case_copy)
    for row in maintenance.claim_due_notifications(db, limit):
        msg = notification(row["reason"], copy, case_copy)
        if not private_enough(msg, maintenance.private_terms(db, row["user_id"]) + circumstances):
            log.warning("notification not sent id=%s code=private_check", row["notification_id"])
            out.count("notifications", False)
            continue
        error = _try(mailer, row["email"], msg)
        if error:
            log.warning("notification not sent id=%s code=%s", row["notification_id"], error)
        out.count("notifications", error is None)

    for row in maintenance.claim_due_check_ins(db, limit):
        msg = check_in(case_copy)
        if not private_enough(msg, maintenance.private_terms(db, row["user_id"]) + circumstances):
            log.warning("check-in not sent case=%s code=private_check", row["case_id"])
            out.count("check_ins", False)
            continue
        error = _try(mailer, row["email"], msg)
        if error:
            log.warning("check-in not sent case=%s code=%s", row["case_id"], error)
        out.count("check_ins", error is None)
    return out
