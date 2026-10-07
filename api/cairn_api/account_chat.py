"""Account requests typed to Cairn: delete my account (UC-ACCT-01), download my data (UC-REG-16), and
change how Cairn keeps in touch (UC-REG-17, which took in case UC-CASE-20: "Stop texting me", "Email me instead").

Rule-based and deliberately narrow, like extraction.py, until a model does this.
A message nobody recognizes gets a short list of what Cairn can do here.

Safety first (UC-REG-15 alternate flow): a risk-of-harm signal is answered with
988, the Veterans Crisis Line when a case says the person was a veteran, and
911, and no account action is taken in that turn. If the user asks again, the
request goes ahead with no extra questions, and the crisis lines stay on
screen. That the safety turn happened lives only in the client-held
AccountChatSession. Nothing about it is stored (decision 7).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import safety
from .schemas import NotificationChannel, NotificationChannelsIn, SafetyMode

_DELETE = re.compile(
    r"\b(delete|remove|erase|close|get rid of|wipe)\b.{0,40}\b(account|profile|everything|all (of )?my "
    r"(data|information|info|stuff))\b", re.IGNORECASE)
_DOWNLOAD = re.compile(
    r"\b(download|export|(a )?copy of|send me|get|see)\b.{0,40}\b(my data|my information|my info|"
    r"everything (you have|you hold|you'?ve got|about me)|all (of )?my (data|information|info))\b", re.IGNORECASE)
_EMAIL_INSTEAD = re.compile(
    r"\b(e-?mail me|by e-?mail|e-?mails? instead|switch to e-?mail|use (my )?e-?mail|prefer e-?mail|"
    r"send (it|them|me) (an )?e-?mails?)\b", re.IGNORECASE)
_PUSH_INSTEAD = re.compile(
    r"\b(push notifications?|notify me on my phone|(send|use|switch to|prefer)\b.{0,20}\b(phone|app) "
    r"notifications?)\b", re.IGNORECASE)
_STOP = re.compile(
    r"\b(stop|quit|no more|don'?t|do not|never)\b.{0,25}\b(text\w*|e-?mail\w*|messag\w*|notif\w*|remind\w*|"
    r"contact\w*|send\w*|reach\w*)\b|\bunsubscribe\b|\bleave me alone\b|\bturn off (all )?notifications\b",
    re.IGNORECASE)
_SMS = re.compile(r"\b(text me|texts? instead|by text|sms|text messages?)\b", re.IGNORECASE)
# "Stop texting me, email me instead" is a switch. "Don't email me anymore" is a stop.
_SWITCH = re.compile(r"\b(instead|switch|rather|prefer)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Reading:
    intent: str
    risk_of_harm: bool
    channel: NotificationChannel | None = None


def read(text: str) -> Reading:
    """Which account request this is, if any, and whether it carries a risk-of-harm signal."""
    risk = safety.classify(text).mode == SafetyMode.risk_of_harm
    stop = _STOP.search(text)
    switch_to = (NotificationChannel.email if _EMAIL_INSTEAD.search(text)
                 else NotificationChannel.browser if _PUSH_INSTEAD.search(text) else None)
    if _DELETE.search(text):
        intent = "delete_account"
    elif _DOWNLOAD.search(text):
        intent = "download_data"
    elif switch_to is not None and (not stop or _SWITCH.search(text)):
        return Reading("change_notifications", risk, switch_to)
    elif stop:
        intent = "stop_notifications"
    elif _SMS.search(text):
        intent = "sms_not_available"
    else:
        intent = "help"
    return Reading(intent, risk)


def switch_channel(channel: NotificationChannel) -> NotificationChannelsIn:
    """Use one channel instead of the others. In-app always stays on. The pace and timing the user chose stay as
    they are. Browser notifications still need the browser's permission, asked by the client."""
    return NotificationChannelsIn(email=channel == NotificationChannel.email,
                                  browser=channel == NotificationChannel.browser)
