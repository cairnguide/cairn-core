"""UC-REG-06 to UC-REG-16: the onboarding steps after the account exists (account spec 3.2.0).

Each step is its own call and its own screen. Steps must be done in order.
Repeating a step that is already done returns the current screen unchanged,
so clients can retry safely. Setup progress lives on the account, so it
continues on any device (UC-REG-04).
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, Request

from .. import account as acct
from .. import messages, onboarding
from .. import notifications as nt
from ..auth import Identity, get_identity
from ..copy_store import Copy
from ..db import Session
from ..errors import ApiError
from ..schemas import (
    AcknowledgmentIn,
    AdultAnswerIn,
    CaseHandoffRequest,
    CaseHandoffResponse,
    ConsentType,
    Link,
    NextStep,
    Note,
    NotificationChannelsIn,
    NotificationFrequencyIn,
    OnboardingResponse,
    Option,
    PreferredNameIn,
    Relationship,
    Screen,
    ScreenId,
    SetupCheckInIn,
    VoiceChoiceIn,
)
from ..voices import VoiceCatalog

router = APIRouter(prefix="/v1/onboarding", tags=["Onboarding"])

_ORDER = {409: {"description": "An earlier step isn't finished yet. next_step says which one."}}

CONSENT_STEP = acct.CONSENT_STEP

# The hand-off relationship as a case creation user_role (UC-CASE-02), so it isn't asked twice.
HANDOFF_USER_ROLE = {
    Relationship.spouse: "spouse_partner",
    Relationship.child: "child",
    Relationship.sibling: "other_family",
    Relationship.other_family: "other_family",
    Relationship.power_of_attorney: "power_of_attorney",
    Relationship.fiduciary: "professional_fiduciary",
}


def _at_screen(s: Session, request: Request, target: ScreenId) -> OnboardingResponse | None:
    """None if target is the current screen. The unchanged current screen if target was already passed."""
    account = acct.load_account(s)
    current, _ = onboarding.current_screen(s, request, account)
    if current == target:
        return None
    if current == ScreenId.under_18:
        # UC-REG-06. Onboarding stopped at a no. Nothing more is collected.
        raise ApiError(409, "onboarding_stopped", request.app.state.copy["age_under_18"],
                       next_step=onboarding.response(s, request).next_step.model_dump())
    if onboarding.screen_index(target) < onboarding.screen_index(current):
        return onboarding.response(s, request)
    raise ApiError(409, "out_of_order", request.app.state.copy["onboarding_incomplete"],
                   next_step=onboarding.response(s, request).next_step.model_dump())


def _links(request: Request, copy: Copy) -> list[Link]:
    st = request.app.state.settings
    return [Link(label=copy["journey_map_link"], url=st.journey_map_url),
            Link(label=copy["support_link"], url=st.support_url)]


@router.get("", response_model=OnboardingResponse, summary="Where the user is in onboarding",
            description="UC-REG-13. Returns the first incomplete step, or a question to answer again (a changed "
                        "acknowledgment, or the adult question for an account made before it). After a level 3 or "
                        "4 moment during setup, send offer_check_in=true once the user is ready to go on, and the "
                        "follow-up is offered once before setup continues (DEC-26-04).")
def get_onboarding(request: Request, identity: Identity = Depends(get_identity),
                   offer_check_in: bool = False) -> OnboardingResponse:
    copy: Copy = request.app.state.copy
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        if offer_check_in:
            return onboarding.response(s, request, screen=onboarding.paused_screen(copy),
                                       next_step=onboarding.paused_step(copy, offer_check_in=True,
                                                                        ask_email=nt.load(s) is None))
        return onboarding.response(s, request)


@router.post(
    "/adult",
    response_model=OnboardingResponse,
    summary="Are you 18 or older?",
    description=(
        "UC-REG-06. Yes records adult_attested with the time and goes on to the Privacy Policy and Terms. No stops "
        "onboarding: the under-18 message and the 988 line, links to the journey map and Support resources, and "
        "nothing more is collected. The account is deleted after the pending-account period (UC-REG-10). No "
        "birthdate or age is asked for or stored. [LEGAL REVIEW REQUIRED] the age gate approach (OPEN-09)."
    ),
    responses=_ORDER,
)
def answer_adult(req: AdultAnswerIn, request: Request, identity: Identity = Depends(get_identity)
                 ) -> OnboardingResponse:
    with request.app.state.db.session(identity.subject) as s:
        uid = s.require_user()
        account = acct.load_account(s)
        if account.get("adult_attested") is not None:
            return onboarding.response(s, request)
        s.record_adult_answer(req.answer == "yes")
        s.audit("adult_attested" if req.answer == "yes" else "adult_not_attested", object_type="user", object_id=uid)
        return onboarding.response(s, request)


@router.post(
    "/acknowledgments/{consent_type}",
    response_model=OnboardingResponse,
    summary="Agree to, or say \"I'm not sure\" about, an acknowledgment",
    description=(
        "UC-REG-07 (privacy_terms), UC-REG-08 (trial_terms), UC-REG-09 (ai_notice), and UC-REG-10. Agreeing "
        "requires the document_version shown with the checkbox and appends a consent record with the version, "
        "time, sign-in method, and client. agreed=false records nothing and offers ways forward. Also used to "
        "re-acknowledge a changed version (UC-REG-13). The trial clock does not start here. Agreeing to the AI "
        "notice counts as that day's session-start AI reminder (UC-CASE-23)."
    ),
    responses={**_ORDER, 422: {"description": "document_version isn't the current one. Show the screen again."}},
)
def acknowledge(consent_type: ConsentType, req: AcknowledgmentIn, request: Request,
                identity: Identity = Depends(get_identity)) -> OnboardingResponse:
    copy: Copy = request.app.state.copy
    with request.app.state.db.session(identity.subject) as s:
        uid = s.require_user()
        if (done := _at_screen(s, request, onboarding.ACK_SCREEN[consent_type])) is not None:
            return done
        if not req.agreed:
            return onboarding.response(s, request, screen=Screen(id=ScreenId.declined, links=_links(request, copy)),
                                       next_step=_declined_step(copy, consent_type))
        current = acct.current_versions(request)[consent_type.value]
        if req.document_version != current:
            raise ApiError(422, "acknowledgment_outdated", copy["acknowledgment_version_changed"],
                           document_version=current)
        s.add_consent(consent_type.value, current, identity.sign_in_method.value, req.client)
        s.audit("consent_granted", object_type="user", object_id=uid)
        if consent_type == ConsentType.ai_notice:
            s.mark_ai_reminder_shown()
        if not acct.step_reached(acct.load_account(s), CONSENT_STEP[consent_type]):
            s.advance_onboarding(CONSENT_STEP[consent_type].value)
        return onboarding.response(s, request)


def _declined_step(copy: Copy, consent_type: ConsentType) -> NextStep:
    """UC-REG-10. Never a dead end. The account stays pending_onboarding."""
    return NextStep(action="acknowledgment_declined", prompt=copy["decline_acknowledgment"], options=[
        Option(value=f"read_again:{consent_type.value}", label=copy["read_again_link"]),
        Option(value="journey_map", label=copy["journey_map_link"]),
        Option(value="contact_support", label=copy["support_link"]),
    ])


@router.put(
    "/preferred-name",
    response_model=OnboardingResponse,
    summary="Save what Cairn should call the user",
    description=(
        "UC-REG-11. The field is never pre-filled from a provider. At most 50 characters and no 5 or more digits, "
        "so a number can't be stored as a name: anything else gets preferred_name_invalid and is neither saved nor "
        "logged. Legal names are not collected. If the text shows signs of distress, nothing is saved and setup "
        "pauses (UC-REG-14, crisis plan context account_setup)."
    ),
    responses={**_ORDER, 422: {"description": "preferred_name_invalid: the copy to show. The text isn't kept."}},
)
def save_preferred_name(req: PreferredNameIn, request: Request,
                        identity: Identity = Depends(get_identity)) -> OnboardingResponse:
    copy: Copy = request.app.state.copy
    with request.app.state.db.session(identity.subject) as s:
        uid = s.require_user()
        if (done := _at_screen(s, request, ScreenId.preferred_name)) is not None:
            return done
        level = onboarding.distress_level(req.preferred_name, req.name_pronunciation)
        if level >= 3:
            if level == 4:
                s.count_level_4_referral()  # SB 243: an anonymous monthly count
            # Level 4: 988 first, nothing to answer. The follow-up is offered on the next turn (offer_check_in).
            return onboarding.response(s, request, screen=onboarding.paused_screen(copy),
                                       next_step=onboarding.paused_step(copy, offer_check_in=level == 3,
                                                                        ask_email=nt.load(s) is None))
        if (problem := onboarding.preferred_name_problem(req.preferred_name, copy)) is not None:
            raise ApiError(422, "preferred_name_invalid", problem)
        s.update_account(preferred_name=req.preferred_name, name_pronunciation=req.name_pronunciation)
        s.advance_onboarding("preferred_name_saved")
        s.audit("preferred_name_saved", object_type="user", object_id=uid)
        return onboarding.response(s, request)


@router.post(
    "/check-in",
    response_model=OnboardingResponse,
    summary="May Cairn check in tomorrow? (during setup)",
    description=(
        "DEC-26-04 and AC-26-12. Only on a yes: account.check_in_at, with no case id. It shows in Cairn the next "
        "time the user signs in, and goes through the channels chosen at UC-REG-15. Before that step there are no "
        "channels, so yes_email also sends it to the sign-in email and yes_in_cairn keeps it in Cairn only. A no "
        "stores nothing. Then setup resumes where it was."
    ),
)
def setup_check_in(req: SetupCheckInIn, request: Request, identity: Identity = Depends(get_identity)
                   ) -> OnboardingResponse:
    copy: Copy = request.app.state.copy
    with request.app.state.db.session(identity.subject) as s:
        uid = s.require_user()
        if req.answer != "no":
            s.set_check_in(s.now + timedelta(days=1), None, by_email=req.answer == "yes_email")
            s.audit("check_in_scheduled", object_type="user", object_id=uid)
        ack = copy["check_in_yes_saved" if req.answer != "no" else "check_in_no_saved"]
        return onboarding.response(s, request, notes=[Note(kind="acknowledgment", text=ack)])


@router.put(
    "/personality",
    response_model=OnboardingResponse,
    summary="Choose how Cairn talks with the user",
    description=(
        "UC-REG-12. Saves the chosen voice on the account. choose_for_me selects Steady and Direct. The voice "
        "changes tone only, never the crisis protocol, AI disclosure, attorney referrals, or citations. Then "
        "notification channels (UC-REG-15), with copy.voice_confirm first."
    ),
    responses=_ORDER,
)
def choose_voice(req: VoiceChoiceIn, request: Request,
                 identity: Identity = Depends(get_identity)) -> OnboardingResponse:
    copy: Copy = request.app.state.copy
    voices: VoiceCatalog = request.app.state.voices
    voice = voices.default if req.choice == "choose_for_me" else voices[req.choice].id
    with request.app.state.db.session(identity.subject) as s:
        uid = s.require_user()
        if (done := _at_screen(s, request, ScreenId.personality)) is not None:
            return done
        s.update_account(voice=voice.value)
        s.advance_onboarding("voice_saved")
        s.audit("voice_saved", object_type="user", object_id=uid)
        account = acct.load_account(s)
        confirm = copy["voice_confirm"].format(preferred_name=account["preferred_name"])
        return onboarding.response(s, request, notes=[Note(kind="acknowledgment", text=confirm)])


@router.put(
    "/notification-channels",
    response_model=OnboardingResponse,
    summary="How should Cairn let the user know?",
    description=(
        "UC-REG-15, first screen. Email and in-app are pre-selected, and in-app is always on. Ask the browser for "
        "permission only after the user chose browser, in direct response to their select, then send what it said. "
        "Denied or unsupported takes browser off and shows notify_browser_denied. No phone number is asked for "
        "and text messages aren't offered (D-12)."
    ),
    responses=_ORDER,
)
def notification_channels(req: NotificationChannelsIn, request: Request,
                          identity: Identity = Depends(get_identity)) -> OnboardingResponse:
    copy: Copy = request.app.state.copy
    with request.app.state.db.session(identity.subject) as s:
        uid = s.require_user()
        if (done := _at_screen(s, request, ScreenId.notification_channels)) is not None:
            return done
        channels, endpoint = nt.channels_from(req)
        s.save_notification_preferences({"channels": channels, "browser_push_endpoint": endpoint})
        s.audit("notification_preferences_saved", object_type="user", object_id=uid)
        notes = []
        if req.browser and req.browser_permission == "denied":
            notes.append(Note(kind="info", text=copy["notify_browser_denied"]))
        return onboarding.response(s, request, notes=notes)


@router.put(
    "/notification-frequency",
    response_model=OnboardingResponse,
    summary="How often would the user like to hear from Cairn?",
    description=(
        "UC-REG-15, second screen, then UC-REG-16. Due only is the default. No reminders saves none, and service "
        "notices still go to email (D-14). Quiet hours use the browser's time zone and aren't asked. Finishes setup "
        "(status setup_complete) and queues one welcome confirmation to the sign-in email, with no personal "
        "details beyond the preferred name (UC-CASE-21). The free days don't start here."
    ),
    responses=_ORDER,
)
def notification_frequency(req: NotificationFrequencyIn, request: Request,
                           identity: Identity = Depends(get_identity)) -> OnboardingResponse:
    copy: Copy = request.app.state.copy
    with request.app.state.db.session(identity.subject) as s:
        uid = s.require_user()
        if (done := _at_screen(s, request, ScreenId.notification_frequency)) is not None:
            return done
        s.save_notification_preferences({"frequency": req.frequency.value})
        s.advance_onboarding("complete")
        s.queue_confirmation("setup_complete")
        s.audit("onboarding_completed", object_type="user", object_id=uid)
        # UC-REG-19: the shared device tip, once, on the setup complete screen.
        return onboarding.response(s, request, notes=[Note(kind="info", text=copy["signout_shared_device_tip"])])


@router.post(
    "/case-handoff",
    response_model=CaseHandoffResponse,
    summary="Start toward case creation",
    description=(
        "After UC-REG-16, Start a case. The relationship shapes the wording of case creation. A power of attorney "
        "is told that authority generally ends at death. Not stored here. Send user_role to POST /v1/cases to save "
        "it."
    ),
    responses={409: {"description": "Setup isn't finished, or an acknowledgment changed."}},
)
def case_handoff(req: CaseHandoffRequest, request: Request,
                 identity: Identity = Depends(get_identity)) -> CaseHandoffResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        ready = acct.require_ready(s, request, write=False)
    rel = req.relationship
    notes: list[Note] = list(ready.notes)
    if rel == Relationship.power_of_attorney:
        notes.append(messages.POA_ENDS_AT_DEATH)
    return CaseHandoffResponse(language_profile="professional" if rel == Relationship.fiduciary else "family",
                               user_role=HANDOFF_USER_ROLE[rel], notes=notes,
                               next_step=messages.registration_next_step(rel))
