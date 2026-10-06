"""Onboarding screens, one question per screen (account spec 3.2.0, UC-REG-01 to UC-REG-16).

Every screen acknowledges first where the spec says so, asks one thing
(next_step.prompt), offers rather than demands, and always leaves a next
action. Take a break, the Support resources link, Read this to me, and the 988
resource ride along on every response and never depend on the voice.

The order is the spec's onboarding_sequence: adult confirmation (UC-REG-06),
the three acknowledgments (UC-REG-07 to 09), preferred name (UC-REG-11), voice
(UC-REG-12), notification channels then frequency (UC-REG-15), and setup
complete (UC-REG-16).
"""
from __future__ import annotations

from fastapi import Request

from . import account as acct
from . import safety
from .copy_store import Copy
from .db import Session
from .schemas import (
    Checkbox,
    Choice,
    ConsentType,
    EmailSignInCopy,
    Link,
    MagicLinkCopy,
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
    OnboardingStep.account_created: ScreenId.adult,
    OnboardingStep.adult_confirmed: ScreenId.privacy_terms,
    OnboardingStep.privacy_terms_accepted: ScreenId.trial_terms,
    OnboardingStep.trial_terms_accepted: ScreenId.ai_notice,
    OnboardingStep.ai_notice_accepted: ScreenId.preferred_name,
    OnboardingStep.preferred_name_saved: ScreenId.personality,
    OnboardingStep.voice_saved: ScreenId.notification_channels,
}
SCREEN_ORDER = [ScreenId.adult, ScreenId.privacy_terms, ScreenId.trial_terms, ScreenId.ai_notice,
                ScreenId.preferred_name, ScreenId.personality, ScreenId.notification_channels,
                ScreenId.notification_frequency, ScreenId.setup_complete]
ACK_SCREEN = {ConsentType.privacy_terms: ScreenId.privacy_terms, ConsentType.trial_terms: ScreenId.trial_terms,
              ConsentType.ai_notice: ScreenId.ai_notice}

PROVIDER_NAMES = {"google": "Google", "apple": "Apple"}
AUTH0_CONNECTIONS = {SignInMethod.google: "google-oauth2", SignInMethod.apple: "apple"}
FREQUENCIES = ("due_only", "daily", "weekly", "none")


def support(copy: Copy) -> Support:
    return Support(take_a_break_label=copy["take_a_break_control"],
                   support_resources_label=copy["support_resources_link"], read_this_to_me=copy["read_this_to_me"],
                   crisis_resource=copy["crisis_resource"])


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


def magic_link(copy: Copy) -> MagicLinkCopy:
    """UC-REG-04. The landing page the email link opens. Only Continue uses the token."""
    return MagicLinkCopy(landing=copy["magic_link_landing"], landing_button=copy["magic_link_landing_button"],
                         other_device=copy["magic_link_other_device"], send_new_link_here=copy["send_new_link_here"])


def provider_name(method: str | None, copy: Copy) -> str:
    if method == "email":
        return copy["provider_email"]
    return PROVIDER_NAMES.get(method or "", copy["provider_unknown"])


# ------------------------------------------------------------------ preferred name (UC-REG-11)

def preferred_name_problem(name: str, copy: Copy) -> str | None:
    """data_boundary.enforcement: at most 50 characters, and no 5 or more digits, so a number such as an SSN or a
    card can't be stored as a name. Returns the copy to show, or None. The rejected text is never stored or logged."""
    if sum(ch.isdigit() for ch in name) >= 5:
        return copy["preferred_name_invalid"]
    if len(name) > 50:
        return copy["preferred_name_invalid_too_long"]
    return None


# ------------------------------------------------------------------ distress (UC-REG-14)

# Free text typed during sign-up is read by the same detector as case creation: the reviewed phrase list in
# safety.py, which follows the Support and Crisis Plan (card 26). Acute distress or risk of harm (level 3 or 4)
# stops the flow. Ending language not clearly about the paperwork counts (DEC-26-05). Someone naming a person who
# died by suicide is a loss, not a risk to the user, so it doesn't by itself. A false positive only pauses
# sign-up. The text that matched is never stored, logged, or recorded as an inference.
def distress_level(*texts: str | None) -> int:
    """The care level the words call for: 1, or 3 or 4 when one signal is enough."""
    levels = [safety.SEVERITY.index(m) + 1 for t in texts if t and (m := safety.classify(t).mode) is not None]
    return max(levels, default=1)


def shows_distress(*texts: str | None) -> bool:
    return distress_level(*texts) > 1


def paused_screen(copy: Copy) -> Screen:
    """Stops the setup flow (UC-REG-14). No timers, nudges, or reminders. Identical for every voice."""
    return Screen(id=ScreenId.paused, acknowledgment=copy["distress_acknowledgment"], body=[copy["crisis_resource"]])


def paused_step(copy: Copy, *, offer_check_in: bool, ask_email: bool) -> NextStep:
    """After level 3 or 4, the follow-up is offered once (DEC-26-04). Before UC-REG-15 there are no channels yet,
    so the same question asks whether email is all right (crisis plan context account_setup)."""
    options = [Option(value="continue", label=copy["ready_to_continue"])]
    if offer_check_in:
        prompt = copy["checkin_email_ask_setup"] if ask_email else copy["crisis_resource"]
        return NextStep(action="check_in_offer", prompt=prompt, options=[
            Option(value="yes_email" if ask_email else "yes", label=copy["check_in_yes"]),
            *([Option(value="yes_in_cairn", label=copy["check_in_only_here"])] if ask_email else []),
            Option(value="no", label=copy["check_in_no"]), *options])
    return NextStep(action="paused", prompt=copy["crisis_resource"], options=options)


# ------------------------------------------------------------------ screens

def _ack_links(request: Request, copy: Copy) -> list[Link]:
    st = request.app.state.settings
    return [Link(label=copy["privacy_policy_link"], url=st.privacy_policy_url),
            Link(label=copy["terms_link"], url=st.terms_url)]


def _voice_label(copy: Copy, voice: str) -> str:
    return copy[f"voice_{voice}_label"]


def _channels_text(copy: Copy, channels: list[str]) -> str:
    labels = [copy[f"notify_channel_{c if c != 'in_app' else 'inapp'}_label"] for c in ("email", "browser", "in_app")
              if c in channels]
    return labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])} and {labels[-1]}"


def setup_summary(copy: Copy, account: dict, prefs: dict | None) -> str:
    """UC-REG-16. What the user chose, including where confirmations go."""
    prefs = prefs or {"channels": ["in_app"], "frequency": "none"}
    return copy["setup_complete_summary"].format(
        voice=_voice_label(copy, account["voice"]),
        frequency=copy[f"notify_frequency_{prefs['frequency']}_label"],
        channels=_channels_text(copy, prefs["channels"]), email=account["email"])


def screen_for(screen_id: ScreenId, request: Request, account: dict, s: Session | None = None) -> tuple[Screen,
                                                                                                         NextStep]:
    copy: Copy = request.app.state.copy
    settings = request.app.state.settings
    versions = acct.current_versions(request)
    agree = [Option(value="agree", label=copy["continue"]), Option(value="not_sure", label=copy["not_sure"])]

    if screen_id == ScreenId.adult:
        return (Screen(id=screen_id, legal_review_required=True),
                NextStep(action="confirm_adult", prompt=copy["age_question"],
                         options=[Option(value="yes", label=copy["adult_yes"]),
                                  Option(value="no", label=copy["adult_no"])]))

    if screen_id == ScreenId.under_18:
        # UC-REG-06. Onboarding stops here. Nothing more is collected.
        return (Screen(id=screen_id, body=[copy["age_under_18"], copy["age_under_18_support"]],
                       links=[Link(label=copy["under_18_journey_link"], url=settings.journey_map_url)],
                       legal_review_required=True),
                NextStep(action="stopped_under_18", prompt=copy["age_under_18_support"],
                         options=[Option(value="support_resources", label=copy["support_resources_link"])]))

    if screen_id == ScreenId.privacy_terms:
        return (Screen(id=screen_id, body=[copy["privacy_terms_summary"]], links=_ack_links(request, copy),
                       checkbox=Checkbox(label=copy["privacy_terms_checkbox"],
                                         document_version=versions["privacy_terms"]),
                       ai_provider=settings.ai_provider_name, legal_review_required=True),
                NextStep(action="acknowledge_privacy_terms", prompt=copy["privacy_terms_checkbox"], options=agree))

    if screen_id == ScreenId.trial_terms:
        return (Screen(id=screen_id, body=[copy["trial_summary"]],
                       checkbox=Checkbox(label=copy["trial_checkbox"], document_version=versions["trial_terms"]),
                       legal_review_required=True),
                NextStep(action="acknowledge_trial_terms", prompt=copy["trial_checkbox"], options=agree))

    if screen_id == ScreenId.ai_notice:
        return (Screen(id=screen_id, body=[copy["ai_notice"]], legal_notice=[copy["ai_notice_legal"]],
                       checkbox=Checkbox(label=copy["ai_checkbox"], document_version=versions["ai_notice"]),
                       legal_review_required=True),
                NextStep(action="acknowledge_ai_notice", prompt=copy["ai_checkbox"], options=agree))

    if screen_id == ScreenId.preferred_name:
        return (Screen(id=screen_id, body=[copy["preferred_name_helper"]],
                       input=TextInput(prefill=None, optional_link_label=copy["pronunciation_link"],
                                       optional_prompt=copy["pronunciation_question"])),
                NextStep(action="provide_preferred_name", prompt=copy["preferred_name_question"]))

    if screen_id == ScreenId.personality:
        # Every voice as a card with its label, description, and sample reply to the same situation (UC-REG-12).
        voices: VoiceCatalog = request.app.state.voices
        choices = [Choice(value=v.id.value, label=copy[f"voice_{v.id.value}_label"],
                          tagline=copy[f"voice_{v.id.value}_description"], sample=copy[f"voice_{v.id.value}_sample"])
                   for v in voices.voices.values()]
        return (Screen(id=screen_id, body=[copy["voice_sample_intro"]], choices=choices, legal_review_required=True),
                NextStep(action="choose_personality", prompt=copy["voice_question"],
                         options=[Option(value=c.value, label=c.label) for c in choices]
                         + [Option(value="choose_for_me", label=copy["voice_default_button"])]))

    if screen_id == ScreenId.notification_channels:
        # UC-REG-15. Email and in-app are pre-selected (OPEN-11), and in-app can't be unchecked. Browser is offered
        # only when Cairn can deliver it (a VAPID key is set). The client also hides it when the browser can't.
        key = settings.vapid_public_key
        email = Choice(value="email", label=copy["notify_channel_email"].format(email=account["email"]),
                       tagline="selected")
        choices = [email, Choice(value="in_app", label=copy["notify_channel_inapp"], tagline="always_on")]
        if key:
            choices.append(Choice(value="browser", label=copy["notify_channel_browser"]))
        return (Screen(id=screen_id, acknowledgment=copy["notify_intro"], choices=choices, push_public_key=key,
                       body=[copy["notify_browser_permission_pre"]] if key else []),
                NextStep(action="choose_notification_channels", prompt=copy["notify_channel_question"],
                         options=[Option(value=c.value, label=c.label) for c in choices if c.value != "in_app"]))

    if screen_id == ScreenId.notification_frequency:
        return (Screen(id=screen_id, body=[copy["notify_quiet_hours_note"],
                                           copy["notify_service_notice"].format(email=account["email"])]),
                NextStep(action="choose_notification_frequency", prompt=copy["notify_frequency_question"],
                         options=[Option(value=f, label=copy[f"notify_frequency_{f}"]) for f in FREQUENCIES]))

    if screen_id == ScreenId.setup_complete:
        prefs = s.notification_preferences() if s is not None else None
        return (Screen(id=screen_id, acknowledgment=copy["setup_complete"].format(
                    preferred_name=account["preferred_name"]),
                       body=[setup_summary(copy, account, prefs), copy["setup_complete_next"]]),
                NextStep(action="setup_complete", prompt=copy["setup_complete_next"],
                         options=[Option(value="start_case", label=copy["setup_complete_primary_button"]),
                                  Option(value="home", label=copy["setup_complete_secondary_button"])]))

    return (Screen(id=ScreenId.ready), NextStep(action="open_home", prompt=copy["continue"]))


def current_screen(s: Session, request: Request, account: dict) -> tuple[ScreenId, bool]:
    """The first incomplete step (UC-REG-13). The flag is True for a question asked again: a changed
    acknowledgment, or the adult question for an account made before it existed."""
    step = OnboardingStep(account["onboarding_step"])
    if account.get("adult_attested") is False:
        return ScreenId.under_18, False
    if acct.adult_needed(account):
        return ScreenId.adult, step != OnboardingStep.account_created
    stale = acct.stale_acknowledgment(s, request, account)
    if stale is not None:
        return ACK_SCREEN[stale], True
    if step == OnboardingStep.voice_saved and s.notification_preferences() is not None:
        return ScreenId.notification_frequency, False
    if step != OnboardingStep.complete:
        return NEXT_SCREEN[step], False
    return (ScreenId.ready if acct.has_case(s) else ScreenId.setup_complete), False


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
        screen, default_step = screen_for(screen_id, request, account, s)
        next_step = next_step or default_step
    if resumed and (account["onboarding_step"] != OnboardingStep.complete.value or reack):
        all_notes.insert(0, Note(kind="acknowledgment", text=copy["resume_onboarding"]))
    if reack:
        all_notes.append(Note(kind="info", text=copy["acknowledgment_version_changed"]))
    all_notes += acct.account_notes(s, account, request)
    return OnboardingResponse(account=acct.account_out(account, copy), screen=screen, notes=all_notes,
                              support=support(copy), next_step=next_step)


def screen_index(screen_id: ScreenId) -> int:
    return SCREEN_ORDER.index(screen_id) if screen_id in SCREEN_ORDER else len(SCREEN_ORDER)
