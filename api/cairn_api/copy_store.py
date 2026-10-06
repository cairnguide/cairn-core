"""Versioned user-facing copy for account setup (UC-REG), case creation (UC-CASE), Take a break (UC-BRK), and the
subscription (UC-SUB).

Each spec's copy is stored verbatim in content/registration-copy.json,
content/case-creation-copy.json, content/take-a-break-copy.json, and
content/subscription-copy.json so it can be replaced after legal review without
a code change. Point CAIRN_REGISTRATION_COPY, CAIRN_CASE_COPY,
CAIRN_BREAK_COPY, or CAIRN_SUBSCRIPTION_COPY at another file to swap one.

Acknowledgment versions are derived from the exact text shown, so any wording
change produces a new version and the user is asked to acknowledge it again
on next sign-in (UC-REG-13). Nobody has to remember to bump a number.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass
from functools import cached_property

DEFAULT_PATH = pathlib.Path(__file__).parent / "content" / "registration-copy.json"
CASE_COPY_PATH = pathlib.Path(__file__).parent / "content" / "case-creation-copy.json"
BREAK_COPY_PATH = pathlib.Path(__file__).parent / "content" / "take-a-break-copy.json"
SUBSCRIPTION_COPY_PATH = pathlib.Path(__file__).parent / "content" / "subscription-copy.json"

# Which strings make up each acknowledgment. Changing any of them changes the version.
_CONSENT_TEXT = {
    "privacy_terms": ("privacy_terms_summary", "privacy_terms_checkbox"),
    "trial_terms": ("trial_summary", "trial_checkbox"),
    "ai_notice": ("ai_notice", "ai_notice_legal", "ai_checkbox"),
}
# UC-SUB-02. The subscription terms screen, recorded with each subscription_terms consent.
SUBSCRIPTION_TERMS = ("terms_title", "terms_price", "terms_tax", "terms_renewal", "terms_renewal_early",
                      "terms_cancel", "terms_includes", "terms_breaks", "terms_secure_note", "terms_checkbox")


@dataclass(frozen=True)
class Copy:
    version: str
    status: str
    spec: dict[str, str]
    flow: dict[str, str]
    draft: dict[str, str]
    # Keys whose wording needs counsel approval before launch (price, subscription, legal authority).
    legal_review: frozenset[str] = frozenset()

    def __getitem__(self, key: str) -> str:
        for table in (self.spec, self.flow, self.draft):
            if key in table:
                return table[key]
        raise KeyError(key)

    def text_hash(self, consent_type: str) -> str:
        return self.version_of(*_CONSENT_TEXT[consent_type])

    def version_of(self, *keys: str) -> str:
        """A short hash of the exact wording, so a client can prove which text it showed."""
        return self.version_of_text("\n".join(self[k] for k in keys))

    @staticmethod
    def version_of_text(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:12]

    def has(self, key: str) -> bool:
        return any(key in table for table in (self.spec, self.flow, self.draft))

    @cached_property
    def all_strings(self) -> list[str]:
        return [*self.spec.values(), *self.flow.values(), *self.draft.values()]


def load_copy(path: str | pathlib.Path | None = None, default: pathlib.Path = DEFAULT_PATH) -> Copy:
    data = json.loads(pathlib.Path(path or default).read_text(encoding="utf-8"))
    return Copy(version=data["version"], status=data["status"], spec=data["spec_copy"],
                flow=data["flow_copy"], draft=data["draft_copy"],
                legal_review=frozenset(data.get("legal_review_keys", ())))


def load_case_copy(path: str | pathlib.Path | None = None) -> Copy:
    return load_copy(path, CASE_COPY_PATH)


def load_break_copy(path: str | pathlib.Path | None = None) -> Copy:
    return load_copy(path, BREAK_COPY_PATH)


def load_subscription_copy(path: str | pathlib.Path | None = None) -> Copy:
    return load_copy(path, SUBSCRIPTION_COPY_PATH)


def subscription_terms_version(copy: Copy) -> str:
    """The document_version for subscription_terms: the exact text of the terms screen."""
    return f"copy={copy.version_of(*SUBSCRIPTION_TERMS)}"


def consent_versions(copy: Copy, privacy_version: str, terms_version: str) -> dict[str, str]:
    """The document_version recorded for each acknowledgment type."""
    return {
        "privacy_terms": f"privacy={privacy_version},terms={terms_version},copy={copy.text_hash('privacy_terms')}",
        "trial_terms": f"copy={copy.text_hash('trial_terms')}",
        "ai_notice": f"copy={copy.text_hash('ai_notice')}",
    }
