"""Onboarding screens, one question per screen (UC-REG-01 to UC-REG-14).

Every screen acknowledges first where the spec says so, asks one thing
(next_step.prompt), offers rather than demands, and always leaves a next
action. The "I need a moment" control and the 988 resource ride along on
every response and never depend on the voice.
"""
from __future__ import annotations

from fastapi import Request

from . import account as acct
from . import safety
from .copy_store import Copy
from .db import Session
from .messages import registration_next_step
from .schemas import (
    Checkbox,
    Choice,
    ConsentType,
    EmailSignInCopy,
    Link,
    NextStep,
    Note,
    OnboardingResponse,
    OnboardingStep,
    Option,
    Screen,
    ScreenId,
    SignInMethod,
    SignInOption,
    Support,
    TextInput,
)
from .voices import VoiceCatalog

# The screen shown after each completed step, in the spec's onboarding_sequence.
NEXT_SCREEN = {
    OnboardingStep.account_created: ScreenId.privacy_terms,
    OnboardingStep.privacy_terms_accepted: ScreenId.trial_terms,
    OnboardingStep.trial_terms_accepted: ScreenId.ai_notice,
    OnboardingStep.ai_notice_accepted: ScreenId.preferred_name,
    OnboardingStep.preferred_name_saved: ScreenId.personality,
}
SCREEN_ORDER = [ScreenId.privacy_terms, ScreenId.trial_terms, ScreenId.ai_notice,
                ScreenId.preferred_name, ScreenId.personality, ScreenId.case_handoff]
ACK_SCREEN = {ConsentType.privacy_terms: ScreenId.privacy_terms, ConsentType.trial_terms: ScreenId.trial_terms,
              ConsentType.ai_notice: ScreenId.ai_notice}

PROVIDER_NAMES = {"google": "Google", "apple": "Apple"}
AUTH0_CONNECTIONS = {SignInMethod.google: "google-oauth2", SignInMethod.apple: "apple"}


def support(copy: Copy) -> Support:
    return Support(need_a_moment_label=copy["need_a_moment_control"], crisis_resource=copy["crisis_resource"])


def sign_in_options(copy: Copy, email_connection: str) -> list[SignInOption]:
    labels = {SignInMethod.google: copy["continue_with_google"], SignInMethod.apple: copy["continue_with_apple"],
              SignInMethod.email: copy["continue_with_email"]}
    return [SignInOption(method=m, label=labels[m], auth0_connection=AUTH0_CONNECTIONS.get(m, email_connection))
            for m in SignInMethod]


# UC-REG-04. Auth0's passwordless email connection must use the same lifetime (auth0/README.md).
EMAIL_LINK_LIFETIME_MINUTES = 15
EMAIL_RESEND_AFTER_SECONDS = 60


def email_sign_in(copy: Copy) -> EmailSignInCopy:
    return EmailSignInCopy(check_inbox=copy["email_check_inbox"], link_lifetime_minutes=EMAIL_LINK_LIFETIME_MINUTES,
                           link_expired=copy["email_link_expired"], send_new_link=copy["send_new_link"],
                           resend_after_seconds=EMAIL_RESEND_AFTER_SECONDS, check_spelling=copy["email_check_spelling"],
                           resend=copy["resend_link"])


def provider_name(method: str | None, copy: Copy) -> str:
    if method == "email":
        return copy["provider_email"]
    return PROVIDER_NAMES.get(method or "", copy["provider_unknown"])


# ------------------------------------------------------------------ distress (UC-REG-14)

# Free text typed during sign-up is read by the same detector as case creation: the reviewed phrase list in
# safety.py, which follows the Support and Crisis Plan (card 26). Acute distress or risk of harm (level 3 or 4)
# stops the flow. Ending language not clearly about the paperwork counts (DEC-26-05). Someone naming a person who
# died by suicide is a loss, not a risk to the user, so it doesn't by itself. A false positive only pauses
# sign-up. The text that matched is never stored, logged, or recorded as an inference.
def shows_distress(*texts: str | None) -> bool:
    return any(t and safety.classify(t).mode is not None for t in texts)


def paused_screen(copy: Copy, distress: bool) -> Screen:
    """Stops the task flow. No timers, nudges, or reminders. Identical for every voice."""
    return Screen(
        id=ScreenId.paused,
        acknowledgment=copy["distress_acknowledgment" if distress else "need_a_moment_acknowledgment"],
        body=[copy["crisis_resource"]],
    )


def paused_step(copy: Copy) -> NextStep:
    return NextStep(action="paused", prompt=copy["crisis_resource"],
                    options=[Option(value="continue", label=copy["ready_to_continue"])])


# ------------------------------------------------------------------ screens

def _ack_links(request: Request, copy: Copy) -> list[Link]:
    st = request.app.state.settings
    return [Link(label=copy["privacy_policy_link"], url=st.privacy_policy_url),
            Link(label=copy["terms_link"], url=st.terms_url)]


def screen_for(screen_id: ScreenId, request: Request, account: dict) -> tuple[Screen, NextStep]:
    copy: Copy = request.app.state.copy
    settings = request.app.state.settings
    versions = acct.current_versions(request)
    agree = [Option(value="agree", label=copy["continue"]), Option(value="not_sure", label=copy["not_sure"])]

    if screen_id == ScreenId.privacy_terms:
        return (Screen(id=screen_id, body=[copy["privacy_terms_summary"]], links=_ack_links(request, copy),
                       checkbox=Checkbox(label=copy["privacy_terms_checkbox"],
                                         document_version=versions["privacy_terms"]),
                       ai_provider=settings.ai_provider_name, legal_review_required=True),
                NextStep(action="acknowledge_privacy_terms", prompt=copy["privacy_terms_checkbox"], options=agree))

    if screen_id == ScreenId.trial_terms:
        return (Screen(id=screen_id, body=[copy["trial_summary"]],
                       checkbox=Checkbox(label=copy["trial_checkbox"], document_version=versions["trial_terms"])),
                NextStep(action="acknowledge_trial_terms", prompt=copy["trial_checkbox"], options=agree))

    if screen_id == ScreenId.ai_notice:
        return (Screen(id=screen_id, body=[copy["ai_notice"]], legal_notice=[copy["ai_notice_legal"]],
                       checkbox=Checkbox(label=copy["ai_checkbox"], document_version=versions["ai_notice"]),
                       legal_review_required=True),
                NextStep(action="acknowledge_ai_notice", prompt=copy["ai_checkbox"], options=agree))

    if screen_id == ScreenId.preferred_name:
        return (Screen(id=screen_id, input=TextInput(prefill=account.get("name_prefill"),
                                                     optional_link_label=copy["pronunciation_link"],
                                                     optional_prompt=copy["pronunciation_question"])),
                NextStep(action="provide_preferred_name", prompt=copy["preferred_name_question"]))

    if screen_id == ScreenId.personality:
        # Every voice in the manifest, each with its reply to the same situation.
        voices: VoiceCatalog = request.app.state.voices
        choices = [Choice(value=v.id.value, label=v.label, tagline=v.tagline, sample=v.sample)
                   for v in voices.voices.values()]
        return (Screen(id=screen_id, sample_situation=voices.sample_situation, choices=choices,
                       legal_review_required=True),
                NextStep(action="choose_personality", prompt=copy["personality_question"],
                         options=[Option(value=c.value, label=c.label) for c in choices]
                         + [Option(value="choose_for_me", label=copy["personality_default_button"])]))

    if screen_id == ScreenId.case_handoff:
        return (Screen(id=screen_id), case_handoff_step(copy))

    return (Screen(id=ScreenId.ready), NextStep(action="open_cases", prompt=copy["continue"]))


def case_handoff_step(copy: Copy) -> NextStep:
    """Hand-off to case creation. The first question is the relationship, which shapes the wording."""
    first = registration_next_step(None)
    return first.model_copy(update={"prompt": f'{copy["case_handoff_prompt"]} {first.prompt}'})


def current_screen(s: Session, request: Request, account: dict) -> tuple[ScreenId, bool]:
    """The first incomplete step (UC-REG-13). The flag is True for a re-acknowledgment of a changed version."""
    step = OnboardingStep(account["onboarding_step"])
    stale = acct.stale_acknowledgment(s, request, account)
    if stale is not None:
        return ACK_SCREEN[stale], True
    if step != OnboardingStep.complete:
        return NEXT_SCREEN[step], False
    return (ScreenId.ready if acct.has_case(s) else ScreenId.case_handoff), False


def response(s: Session, request: Request, *, screen_id: ScreenId | None = None, notes: list[Note] | None = None,
             screen: Screen | None = None, next_step: NextStep | None = None,
             resumed: bool = False) -> OnboardingResponse:
    copy: Copy = request.app.state.copy
    account = acct.load_account(s)
    all_notes = list(notes or [])
    reack = False
    if screen is None:
        if screen_id is None:
            screen_id, reack = current_screen(s, request, account)
        screen, default_step = screen_for(screen_id, request, account)
        next_step = next_step or default_step
    if resumed and (account["onboarding_step"] != OnboardingStep.complete.value
                    or screen.id == ScreenId.case_handoff or reack):
        all_notes.insert(0, Note(kind="acknowledgment", text=copy["resume_onboarding"]))
    if reack:
        all_notes.append(Note(kind="info", text=copy["acknowledgment_version_changed"]))
    all_notes += acct.account_notes(s, account, copy)
    return OnboardingResponse(account=acct.account_out(account, copy), screen=screen, notes=all_notes,
                              support=support(copy), next_step=next_step)


def screen_index(screen_id: ScreenId) -> int:
    return SCREEN_ORDER.index(screen_id) if screen_id in SCREEN_ORDER else len(SCREEN_ORDER)
