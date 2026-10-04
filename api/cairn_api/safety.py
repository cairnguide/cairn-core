"""Signs of distress during case creation (UC-CASE-14), a volunteered cause of death (UC-CASE-05), and a user
who says they are under 18 (UC-CASE-24).

Follows the Support and Crisis Plan (database/docs/cairn-support-crisis-plan.json, card 26), which wins
wherever it and the case creation spec differ:

  level 1 normal          default
  level 2 overwhelm       two signals in one conversation, or three skipped questions in a row (DEC-26-03)
  level 3 acute_distress  any one signal
  level 4 risk_of_harm    any one signal, including ending language not clearly about the paperwork (DEC-26-05).
                          When unsure between levels, the higher one.

Rule-based and deliberately broad. A false positive only slows things down or shows 988. The crisis plan has two
detection layers, the reviewed phrase list and a model reading. This is the phrase list. The model layer arrives
with the model. [REVIEW] The phrase list needs licensed clinical sign-off (DEC-26-06) before any real user.

Nothing here is stored. The level, the matched words, and the fact that a cause was mentioned stay in the
client-held session (IntakeSession) and are never written to the database or a log (database/CLAUDE.md
decision 7). The only things a crisis stores are trial_clock_paused_at, check_in_at, and an anonymous count.
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
# Wanting to die, not wanting to be here, or hurting oneself or someone else. Any other mention of
# suicide also lands here, to err on the side of safety.
_RISK = re.compile(
    r"\b(suicid\w*|kill(ing)? (my ?self|me|someone|somebody|him|her|them)|end(ing)? (it all|my (own )?life)|"
    r"take my (own )?life|want(ed)? to die|wish (i|i'?d) (was|were|had) (dead|died)|"
    r"don'?t want to (live|be here|wake up)|no reason to live|better off (dead|without me)|"
    r"hurt(ing)? (my ?self|someone|somebody|others)|self[- ]harm)\b",
    re.IGNORECASE,
)
# DEC-26-05. Ending language that isn't clearly about the paperwork counts as risk of harm, and the first reply
# asks directly and kindly whether the user is thinking about ending their life.
_ENDING = re.compile(r"\b(i'?m done|i am done|i can'?t do this any ?more|i can'?t go on|i give up on everything|"
                     r"what'?s the point( of anything)?|i'?m finished)\b", re.IGNORECASE)
# "I'm done" that is plainly about the questions or the paperwork.
_ABOUT_THE_PAPERWORK = re.compile(
    r"\b(done|finished) (with|for) (now|today|tonight|this (part|step|question|list|form|page)|"
    r"the (questions?|forms?|paperwork|list|steps?)|these (questions|forms|steps))\b|"
    r"\b(done|finished) (answering|for the day)\b", re.IGNORECASE)
_ACUTE = re.compile(
    r"\b(i can'?t do this|can'?t (breathe|stop crying|cope|function)|panic(king)?|"
    r"falling apart|(not|haven'?t been) (sleeping|eating)|haven'?t (slept|eaten)|losing my mind|"
    r"(the )?grief is (too much|unbearable|crushing))\b",
    re.IGNORECASE,
)
# Level 2 signals: too much, can't keep up, and negative self-talk.
_OVERWHELM = re.compile(
    r"\b(too much|overwhelm\w*|so much to do|don'?t know where to (start|begin)|can'?t think( straight)?|"
    r"my head is spinning|can'?t keep up|i keep (getting|messing) (this|it|things) (wrong|up)|"
    r"i'?m (so )?(useless|stupid|hopeless at this))\b",
    re.IGNORECASE,
)
# A medical cause or mechanism of death. Never stored, never asked about.
_CAUSE = re.compile(
    r"\b(cancer|tumou?r|heart attack|cardiac arrest|stroke|overdose|overdosed|covid|pneumonia|dementia|"
    r"alzheimer'?s|aneurysm|sepsis|kidney failure|liver failure|seizure|copd|leukemia|lymphoma|gunshot|"
    r"drowned)\b",
    re.IGNORECASE,
)
# UC-CASE-24. The user, in the first person, says they are under 18. Never stored, never asked to prove it.
_MINOR = re.compile(
    r"\b(i'?m|i am|im) (only |just )?([1-9]|1[0-7]) ?(years? old|yrs? old|y/?o)\b|"
    r"\b(i'?m|i am|im) (only |just )?(1[0-7])(?=\s*([.!?,]|$|and\b|but\b))|"
    r"\b(i'?m|i am|im) (a minor|under ?18|underage|not 18( yet)?|in (middle|high) school|"
    r"a (high school|middle school) student)\b",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    return text.replace("’", "'")


@dataclass(frozen=True)
class Signals:
    mode: SafetyMode | None      # level 3 or 4 when one signal is enough, else None
    overwhelm_signal: bool       # one level 2 signal. Level 2 starts on the second (DEC-26-03)
    ask_directly: bool           # level 4 from unclear ending language: ask about suicide directly
    volunteered_cause: bool
    suicide_loss: bool
    minor: bool


def classify(text: str) -> Signals:
    t = _clean(text)
    suicide_loss = bool(_SUICIDE_LOSS.search(t))
    remainder = _SUICIDE_LOSS.sub(" ", t)
    explicit_risk = bool(_RISK.search(remainder))
    ending = bool(_ENDING.search(remainder)) and not _ABOUT_THE_PAPERWORK.search(remainder)
    if explicit_risk or ending:
        mode = SafetyMode.risk_of_harm
    elif _ACUTE.search(t):
        mode = SafetyMode.acute_distress
    else:
        mode = None
    return Signals(mode=mode, overwhelm_signal=bool(_OVERWHELM.search(t)), ask_directly=ending and not explicit_risk,
                   volunteered_cause=suicide_loss or bool(_CAUSE.search(t)), suicide_loss=suicide_loss,
                   minor=bool(_MINOR.search(t)))


SEVERITY = [SafetyMode.normal, SafetyMode.overwhelm, SafetyMode.acute_distress, SafetyMode.risk_of_harm]
OVERWHELM_SIGNALS_FOR_LEVEL_2 = 2


def care_level(session: IntakeSession) -> int:
    return SEVERITY.index(session.safety_mode) + 1


def escalate(session: IntakeSession, mode: SafetyMode | None) -> IntakeSession:
    """A session only moves toward more care on its own. It steps back down when the user asks to continue."""
    if mode is None or SEVERITY.index(mode) <= SEVERITY.index(session.safety_mode):
        return session
    return session.model_copy(update={"safety_mode": mode})


def after_message(session: IntakeSession, signals: Signals) -> IntakeSession:
    """Counts an overwhelm signal, and applies the level the message calls for."""
    if signals.overwhelm_signal:
        session = session.model_copy(update={"overwhelm_signals": session.overwhelm_signals + 1})
        if session.overwhelm_signals >= OVERWHELM_SIGNALS_FOR_LEVEL_2:
            session = escalate(session, SafetyMode.overwhelm)
    return escalate(session, signals.mode)


def blocks_questions(session: IntakeSession) -> bool:
    """Level 2: stop asking and say so. Levels 3 and 4: no intake question until the user asks to continue.
    Under 18: no more intake questions this session."""
    return session.safety_mode != SafetyMode.normal or session.intake_stopped


def uses_steady_care(session: IntakeSession) -> bool:
    """Every voice converges on steady_care at levels 3 and 4."""
    return session.safety_mode in (SafetyMode.acute_distress, SafetyMode.risk_of_harm)


def no_billing(session: IntakeSession) -> bool:
    """No subscription, price, or trial wording at levels 3 and 4 (global rule billing_during_crisis)."""
    return uses_steady_care(session)


def after_answer(session: IntakeSession, state: str, threshold: int, unsure_counts: bool) -> IntakeSession:
    """UC-CASE-09 and DEC-26-03. The third Skip for now in a row starts level 2. Two do not. An answer resets the
    count, and so does I'm not sure unless OPEN-07 decides it counts as a skip."""
    skipped = state == "skipped" or (unsure_counts and state == "unsure")
    skips = session.consecutive_skips + 1 if skipped else 0
    session = session.model_copy(update={"consecutive_skips": skips})
    return escalate(session, SafetyMode.overwhelm) if skips >= threshold else session
