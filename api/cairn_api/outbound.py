"""Messages Cairn sends outside the app. Run on a schedule as the cairnJobs user.

The queues, in this order:
1. Confirmations for something the user just did (UC-CASE-21): setup complete, a Settings change, subscribed,
   cancelled, case deleted now, case deleted after the 7-day hold, account deleted. Exactly one each, by email,
   even during a break (AC-BRK-07). On success the address is purged and the send is logged without content.
   On failure the claim is released and the next run tries again.
2. The trial-ending note (account D-14), a week before, by email whatever the reminder choices, and in Cairn.
   During a break it shows only in Cairn. An early subscriber gets the first payment note instead (UC-SUB-06).
3. Reminders the user chose (account D-13): a step coming up, or checking in after a quiet stretch, by email
   and browser notification, at the pace they chose, outside quiet hours, and never during a break.
4. The one check-in a user said yes to after a hard moment (DEC-26-04), through their channels.
5. The break-ending notice, 24 hours before a break ends (UC-BRK-09), through their channels.
6. The yearly reminder that the subscription renews (UC-SUB-17), by email.

Before anything is sent, private_enough checks that it names no person who died and no circumstance
(UC-CASE-19). A message that fails is not sent. Browser notifications carry no text at all (webpush.py).
Stripe sends its own failed-payment emails. Cairn sends none (SUB-D-08).

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
from .webpush import GONE, PushError

log = logging.getLogger("cairn_api.outbound")

CONFIRMATION_KEYS = {"case_deleted_now": "email_case_deleted_now",
                     "case_deleted_after_hold": "email_case_deleted_after_hold",
                     "account_deleted": "email_account_deleted",
                     "setup_complete": "email_setup_complete",
                     "settings_changed": "email_settings_changed",
                     "subscription_started": "email_subscription_started",
                     "subscription_canceled": "email_subscription_canceled"}
SUBSCRIPTION_CONFIRMATIONS = {"subscription_started", "subscription_canceled"}


class Mailer(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class PushSender(Protocol):
    def send(self, endpoint: str) -> None: ...


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


def confirmation(row: dict, copy: Copy, sub_copy: Copy | None = None) -> Message:
    action_type = row["action_type"]
    if action_type in SUBSCRIPTION_CONFIRMATIONS and sub_copy is not None:
        return Message(sub_copy["email_subject_subscription"], sub_copy[CONFIRMATION_KEYS[action_type]])
    if action_type == "setup_complete":
        # UC-REG-16. No personal details beyond the preferred name.
        return Message(copy["email_subject_welcome"],
                       copy["email_setup_complete"].format(preferred_name=row.get("preferred_name") or ""))
    return Message(copy["email_subject_confirmation"], copy[CONFIRMATION_KEYS[action_type]])


def notification(reason: str, copy: Copy, case_copy: Copy) -> Message:
    body = case_copy["notification_example"] if reason == "due_date_upcoming" else copy["email_notification_inactivity"]
    return Message(copy["email_subject_notification"], body)


def trial_reminder(row: dict, copy: Copy) -> Message:
    account = {"trial_ends_at": row["trial_ends_at"], "time_zone": row.get("time_zone"),
               "subscription_status": row.get("subscription_status", "none")}
    return Message(copy["email_subject_trial"], acct.reminder_text(account, copy))


def check_in(case_copy: Copy) -> Message:
    return Message(case_copy["check_in_email_subject"], case_copy["check_in_outside_cairn"])


def break_notice(break_copy: Copy) -> Message:
    return Message(break_copy["break_ending_notice_subject"], break_copy["break_ending_notice_body"])


def annual_reminder(sub_copy: Copy) -> Message:
    return Message(sub_copy["annual_reminder_subject"], sub_copy["annual_reminder_body"])


def price_change(sub_copy: Copy, effective_date: str, new_price: str) -> Message:
    return Message(sub_copy["price_change_subject"],
                   sub_copy["price_change_body"].format(effective_date=effective_date, new_price=new_price))


def send_price_change_notices(db, mailer: Mailer, sub_copy: Copy, effective, new_price: str) -> Outcome:
    """UC-SUB-18. Run by the price_change_notices job only while a price change is configured."""
    out = Outcome()
    rows, missed = maintenance.claim_price_change_notices(db, effective)
    when = acct.format_date(effective.date())
    for row in rows:
        error = _try(mailer, row["email"], price_change(sub_copy, when, new_price))
        if error:
            log.warning("price change notice not sent user=%s code=%s", row["user_id"], error)
        out.count("price_change_notices", error is None)
    for user_id in missed:
        # Never the address. The owner moves this account's effective date in Stripe.
        log.error("owner alert: price change notice window closed during a break user=%s", user_id)
        out.count("price_change_notices_missed", False)
    return out


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


def _deliver(db, mailer: Mailer, push: PushSender | None, row: dict, msg: Message, out: Outcome,
             queue: str) -> None:
    """Email and an empty browser push, by the channels the row names. A push subscription that's gone takes browser
    off the user's channels."""
    for channel in row.get("channels", ["email"]):
        if channel == "email":
            error = _try(mailer, row["email"], msg)
        elif push is None or not row.get("push_endpoint"):
            out.count(f"{queue}_browser_skipped", True)
            continue
        else:
            error = None
            try:
                push.send(row["push_endpoint"])
            except PushError as exc:
                error = str(exc)
                if exc.status in GONE:
                    maintenance.drop_browser_endpoint(db, row["user_id"])
        if error:
            log.warning("%s not sent channel=%s code=%s", queue, channel, error)
        out.count(queue, error is None)


def run_once(db, mailer: Mailer, copy: Copy, case_copy: Copy, limit: int = 100, *, break_copy: Copy | None = None,
             sub_copy: Copy | None = None, push: PushSender | None = None) -> Outcome:
    """Works through every queue once. db is the Cairn database, connected as the cairnJobs user."""
    out = Outcome()

    for row in maintenance.claim_action_confirmations(db, limit):
        error = _try(mailer, row["email"], confirmation(row, copy, sub_copy))
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
        _deliver(db, mailer, push, row, msg, out, "notifications")

    for row in maintenance.claim_due_check_ins(db, limit):
        msg = check_in(case_copy)
        if not private_enough(msg, maintenance.private_terms(db, row["user_id"]) + circumstances):
            log.warning("check-in not sent user=%s code=private_check", row["user_id"])
            out.count("check_ins", False)
            continue
        _deliver(db, mailer, push, row, msg, out, "check_ins")

    if break_copy is not None:
        for row in maintenance.claim_break_notices(db, limit):
            _deliver(db, mailer, push, row, break_notice(break_copy), out, "break_notices")

    if sub_copy is not None:
        for row in maintenance.claim_annual_reminders(db, limit):
            error = _try(mailer, row["email"], annual_reminder(sub_copy))
            if error:
                log.warning("annual reminder not sent user=%s code=%s", row["user_id"], error)
            out.count("annual_reminders", error is None)
    return out
