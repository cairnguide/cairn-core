"""Signs of distress during case creation (UC-CASE-14) and a volunteered cause of death (UC-CASE-05).

Rule-based and deliberately broad. A false positive only slows things down or
shows 988. Signal definitions, thresholds, and the human escalation path belong
to the crisis plan on Trello card 26, which isn't written yet. [REVIEW] Align
these lists with card 26 and the voice eval set on card 48.

Nothing here is stored. The matched text, the mode, and the fact that a cause
was mentioned stay in the client-held session (IntakeSession) and are never
written to the database or a log (database/CLAUDE.md decision 7).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .schemas import IntakeSession, SafetyMode

# Someone who died by suicide. Checked first and removed before the risk check,
# so "my brother died by suicide" is a loss, not a risk to the user.
_SUICIDE_LOSS = re.compile(
    r"\b(died by suicide|death by suicide|(committed|completed) suicide|suicide loss|"
    r"took (his|her|their) (own )?life|(killed|shot|hanged) (himself|herself|themself|themselves)|"
    r"(his|her|their|my \w+'?s?) suicide|lost (him|her|them|my \w+) to suicide)\b",
    re.IGNORECASE,
)
# Wanting to die, or to hurt oneself or someone else. Any other mention of
# suicide also lands here, to err on the side of safety.
_RISK = re.compile(
    r"\b(suicid\w*|kill(ing)? (my ?self|me|someone|somebody|him|her|them)|end(ing)? (it all|my (own )?life)|"
    r"take my (own )?life|want(ed)? to die|wish (i|i'?d) (was|were|had) (dead|died)|"
    r"don'?t want to (live|be here|wake up)|no reason to live|better off (dead|without me)|"
    r"hurt(ing)? (my ?self|someone|somebody|others)|self[- ]harm)\b",
    re.IGNORECASE,
)
_ACUTE = re.compile(
    r"\b(i can'?t do this|i can'?t go on|can'?t (breathe|stop crying|cope|function)|panic(king)?|"
    r"falling apart|(not|haven'?t been) (sleeping|eating)|haven'?t (slept|eaten)|losing my mind)\b",
    re.IGNORECASE,
)
_OVERWHELM = re.compile(
    r"\b(too much|overwhelm\w*|so much to do|don'?t know where to (start|begin)|can'?t think( straight)?|"
    r"my head is spinning)\b",
    re.IGNORECASE,
)
# A medical cause or mechanism of death. Never stored, never asked about.
_CAUSE = re.compile(
    r"\b(cancer|tumou?r|heart attack|cardiac arrest|stroke|overdose|overdosed|covid|pneumonia|dementia|"
    r"alzheimer'?s|aneurysm|sepsis|kidney failure|liver failure|seizure|copd|leukemia|lymphoma|gunshot|"
    r"drowned)\b",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    return text.replace("’", "'")


@dataclass(frozen=True)
class Signals:
    mode: SafetyMode | None      # the mode this message calls for, if any
    volunteered_cause: bool
    suicide_loss: bool


def classify(text: str) -> Signals:
    t = _clean(text)
    suicide_loss = bool(_SUICIDE_LOSS.search(t))
    remainder = _SUICIDE_LOSS.sub(" ", t)
    if _RISK.search(remainder):
        mode = SafetyMode.risk_of_harm
    elif _ACUTE.search(t):
        mode = SafetyMode.acute_distress
    elif _OVERWHELM.search(t):
        mode = SafetyMode.overwhelm
    else:
        mode = None
    return Signals(mode=mode, volunteered_cause=suicide_loss or bool(_CAUSE.search(t)), suicide_loss=suicide_loss)


SEVERITY = [SafetyMode.normal, SafetyMode.overwhelm, SafetyMode.acute_distress, SafetyMode.risk_of_harm]


def escalate(session: IntakeSession, mode: SafetyMode | None) -> IntakeSession:
    """A session only moves toward more care on its own. It steps back down when the user asks to continue."""
    if mode is None or SEVERITY.index(mode) <= SEVERITY.index(session.safety_mode):
        return session
    return session.model_copy(update={"safety_mode": mode})


def blocks_questions(session: IntakeSession) -> bool:
    """acute_distress and risk_of_harm: no intake question until the user asks to continue.
    overwhelm: stop asking and offer a pause or one small thing."""
    return session.safety_mode != SafetyMode.normal


def uses_steady_care(session: IntakeSession) -> bool:
    """Every voice converges on steady_care in acute_distress and risk_of_harm."""
    return session.safety_mode in (SafetyMode.acute_distress, SafetyMode.risk_of_harm)


def after_skip(session: IntakeSession, skipped: bool, threshold: int) -> IntakeSession:
    """Repeated skips are an overwhelm signal. Raised sensitivity lowers the bar by one."""
    skips = session.consecutive_skips + 1 if skipped else 0
    session = session.model_copy(update={"consecutive_skips": skips})
    bar = max(1, threshold - (1 if session.sensitivity == "raised" else 0))
    return escalate(session, SafetyMode.overwhelm) if skips >= bar else session
