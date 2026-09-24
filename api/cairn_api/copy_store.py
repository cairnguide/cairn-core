"""Versioned user-facing copy for registration and onboarding (UC-REG-01 to UC-REG-14).

The spec's copy is stored verbatim in content/registration-copy.json so it can
be replaced after legal review without a code change. Point
CAIRN_REGISTRATION_COPY at another file to swap it.

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

# Which strings make up each acknowledgment. Changing any of them changes the version.
_CONSENT_TEXT = {
    "privacy_terms": ("privacy_terms_summary", "privacy_terms_checkbox"),
    "trial_terms": ("trial_summary", "trial_checkbox"),
    "ai_notice": ("ai_notice", "ai_notice_legal", "ai_checkbox"),
}


@dataclass(frozen=True)
class Copy:
    version: str
    status: str
    spec: dict[str, str]
    flow: dict[str, str]
    draft: dict[str, str]

    def __getitem__(self, key: str) -> str:
        for table in (self.spec, self.flow, self.draft):
            if key in table:
                return table[key]
        raise KeyError(key)

    def text_hash(self, consent_type: str) -> str:
        joined = "\n".join(self.spec[k] for k in _CONSENT_TEXT[consent_type])
        return hashlib.sha256(joined.encode()).hexdigest()[:12]

    @cached_property
    def all_strings(self) -> list[str]:
        return [*self.spec.values(), *self.flow.values(), *self.draft.values()]


def load_copy(path: str | pathlib.Path | None = None) -> Copy:
    data = json.loads(pathlib.Path(path or DEFAULT_PATH).read_text(encoding="utf-8"))
    return Copy(version=data["version"], status=data["status"], spec=data["spec_copy"],
                flow=data["flow_copy"], draft=data["draft_copy"])


def consent_versions(copy: Copy, privacy_version: str, terms_version: str) -> dict[str, str]:
    """The document_version recorded for each acknowledgment type."""
    return {
        "privacy_terms": f"privacy={privacy_version},terms={terms_version},copy={copy.text_hash('privacy_terms')}",
        "trial_terms": f"copy={copy.text_hash('trial_terms')}",
        "ai_notice": f"copy={copy.text_hash('ai_notice')}",
    }
