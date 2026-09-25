"""Redaction of sensitive numbers typed into the chat (UC-CASE-15).

Runs server-side on every free-text field before anything else sees it: the
database, the log, analytics, or a model request. Request models use the
RedactedText type, so a handler only ever receives the redacted string. The
raw value is not kept anywhere, and replies never repeat it or any part of it.

What is caught (never_collect_at_case_creation):
- Social Security numbers, with or without dashes or spaces.
- Card numbers of 13 to 19 digits that pass the Luhn check.
- Digit runs labeled as an account number (account, routing, policy, and so on).
- Unlabeled digit runs of 12 or more that are not a card number, as account numbers.
- A date written right after a date-of-birth label.

Legal names and medical history can't be recognized by pattern. They are kept
out by never storing free text at all: only the extracted data_fields values
are saved, and those have fixed shapes.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

REMOVED = "[removed]"

_CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
_ACCOUNT_LABEL = re.compile(
    r"\b(?:account|acct|routing|member|policy|iban|a/c|checking|savings|card)\b"
    r"(?:\s*(?:number|no\.?|num\.?|#))?\s*[:#]?\s*((?:\d[ -]?){3,}\d)",
    re.IGNORECASE,
)
_SSN = re.compile(r"(?<![\d-])\d{3}([- ]?)\d{2}\1\d{4}(?![\d-])")
_LONG_RUN = re.compile(r"(?<!\d)\d{12,}(?!\d)")
_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE = (r"(?:\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}-\d{2}-\d{2}|"
         rf"{_MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}})")
_DOB = re.compile(rf"\b(?:born(?:\s+on)?|date\s+of\s+birth|birth\s*day|d\.?o\.?b\.?)\s*[:,-]?\s*({_DATE})",
                  re.IGNORECASE)


def luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


@dataclass(frozen=True)
class Redaction:
    text: str
    kinds: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.kinds)


def redact(text: str) -> Redaction:
    kinds: set[str] = set()

    def card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and luhn_valid(digits):
            kinds.add("card_number")
            return REMOVED
        return m.group(0)

    def labeled(m: re.Match) -> str:
        kinds.add("account_number")
        start, end = m.span(1)
        return m.group(0)[: start - m.start()] + REMOVED + m.group(0)[end - m.start():]

    def replace_as(kind: str):
        def inner(m: re.Match) -> str:
            kinds.add(kind)
            return REMOVED
        return inner

    def dob(m: re.Match) -> str:
        kinds.add("date_of_birth")
        start, end = m.span(1)
        return m.group(0)[: start - m.start()] + REMOVED + m.group(0)[end - m.start():]

    out = _CARD.sub(card, text)
    out = _ACCOUNT_LABEL.sub(labeled, out)
    out = _SSN.sub(replace_as("ssn"), out)
    out = _LONG_RUN.sub(replace_as("account_number"), out)
    out = _DOB.sub(dob, out)
    return Redaction(text=out, kinds=tuple(sorted(kinds)))


class RedactedStr(str):
    """A string that has been through redact(). kinds says what was removed, never what it was."""
    redactions: tuple[str, ...] = ()


def redacted_str(value: str) -> RedactedStr:
    result = redact(value)
    out = RedactedStr(result.text)
    out.redactions = result.kinds
    return out


class RedactingFilter(logging.Filter):
    """Defense in depth for log lines. Code should never log free text in the first place."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg).text
        if isinstance(record.args, dict):
            record.args = {k: redact(v).text if isinstance(v, str) else v for k, v in record.args.items()}
        elif record.args:
            record.args = tuple(redact(a).text if isinstance(a, str) else a for a in record.args)
        return True
