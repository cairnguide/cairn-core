"""UC-REG-07 to UC-REG-14: the onboarding steps after the account exists.

There is no age step. UC-REG-06 is not built, by product decision.

Each step is its own call and its own screen. Steps must be done in order.
Repeating a step that is already done returns the current screen unchanged,
so clients can retry safely.
"""
from fastapi import APIRouter, Depends, Request

from .. import account as acct
from .. import messages, onboarding
from ..auth import Identity, get_identity
from ..copy_store import Copy
from ..db import Session
from ..errors import ApiError
from ..schemas import (
    AcknowledgmentIn,
    CaseHandoffRequest,
    CaseHandoffResponse,
    ConsentType,
    Link,
    NextStep,
    Note,
    OnboardingResponse,
    Option,
    PauseResponse,
    PreferredNameIn,
    Relationship,
    Screen,
    ScreenId,
    VoiceChoiceIn,
)
from ..voices import VoiceCatalog

router = APIRouter(prefix="/v1/onboarding", tags=["Onboarding"])

_ORDER = {409: {"description": "An earlier step isn't finished yet. next_step says which one."}}

CONSENT_STEP = acct.CONSENT_STEP


def _at_screen(s: Session, request: Request, target: ScreenId) -> OnboardingResponse | None:
    """None if target is the current screen. The unchanged current screen if target was already passed."""
    account = acct.load_account(s)
    current, _ = onboarding.current_screen(s, request, account)
    if current == target:
        return None
    if onboarding.screen_index(target) < onboarding.screen_index(current):
        return onboarding.response(s, request)
    raise ApiError(409, "out_of_order", request.app.state.copy["onboarding_incomplete"],
                   next_step=onboarding.response(s, request).next_step.model_dump())


def _links(request: Request, copy: Copy) -> list[Link]:
    st = request.app.state.settings
    return [Link(label=copy["journey_map_link"], url=st.journey_map_url),
            Link(label=copy["support_link"], url=st.support_url)]


@router.get("", response_model=OnboardingResponse, summary="Where the user is in onboarding",
            description="UC-REG-13. Returns the first incomplete step, or a changed acknowledgment to re-read.")
def get_onboarding(request: Request, identity: Identity = Depends(get_identity)) -> OnboardingResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        return onboarding.response(s, request)


@router.post(
    "/acknowledgments/{consent_type}",
    response_model=OnboardingResponse,
    summary="Agree to, or say \"I'm not sure\" about, an acknowledgment",
    description=(
        "UC-REG-07 (privacy_terms), UC-REG-08 (trial_terms), UC-REG-09 (ai_notice), and UC-REG-10. Agreeing "
        "requires the document_version shown with the checkbox and appends a consent record with the version, "
        "time, sign-in method, and client. agreed=false records nothing and offers ways forward. Also used to "
        "re-acknowledge a changed version (UC-REG-13). The trial clock does not start here."
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
        s.conn.execute(
            "INSERT INTO cairn.consents (user_id, purpose, policy_version, auth_provider, client) "
            "VALUES (%s, %s, %s, %s, %s)",
            (uid, consent_type.value, current, identity.sign_in_method.value, req.client))
        s.audit("consent_granted", object_type="user", object_id=uid)
        if not acct.step_reached(acct.load_account(s), CONSENT_STEP[consent_type]):
            s.one("SELECT cairn.advance_onboarding(%s)", (CONSENT_STEP[consent_type].value,))
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
        "UC-REG-11. A name pre-filled from Google or Apple is only saved when the user sends it here. "
        "Legal names are not collected. If the text shows signs of distress, nothing is saved and sign-up "
        "pauses (UC-REG-14)."
    ),
    responses=_ORDER,
)
def save_preferred_name(req: PreferredNameIn, request: Request,
                        identity: Identity = Depends(get_identity)) -> OnboardingResponse:
    copy: Copy = request.app.state.copy
    with request.app.state.db.session(identity.subject) as s:
        uid = s.require_user()
        if (done := _at_screen(s, request, ScreenId.preferred_name)) is not None:
            return done
        if onboarding.shows_distress(req.preferred_name, req.name_pronunciation):
            return onboarding.response(s, request, screen=onboarding.paused_screen(copy, distress=True),
                                       next_step=onboarding.paused_step(copy))
        s.conn.execute("UPDATE cairn.users SET preferred_name = %s, name_pronunciation = %s WHERE id = %s",
                       (req.preferred_name, req.name_pronunciation, uid))
        s.one("SELECT cairn.advance_onboarding('preferred_name_saved')")
        s.audit("preferred_name_saved", object_type="user", object_id=uid)
        return onboarding.response(s, request)


@router.put(
    "/personality",
    response_model=OnboardingResponse,
    summary="Choose how Cairn talks with the user",
    description=(
        "UC-REG-12. Saves the chosen voice on the account. choose_for_me selects the default voice. The voice "
        "changes tone only, never the crisis protocol, AI disclosure, attorney referrals, or citations. "
        "Finishes onboarding (status active_no_case) and hands off to case creation with a confirmation in "
        "the chosen voice."
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
        s.conn.execute("UPDATE cairn.users SET voice = %s WHERE id = %s", (voice.value, uid))
        s.one("SELECT cairn.advance_onboarding('complete')")
        s.audit("onboarding_completed", object_type="user", object_id=uid)
        account = acct.load_account(s)
        confirm = copy[f"voice_{voice.value}_confirm"].format(preferred_name=account["preferred_name"])
        screen, step = onboarding.screen_for(ScreenId.case_handoff, request, account)
        screen.acknowledgment = confirm
        return onboarding.response(s, request, screen=screen, next_step=step)


@router.get(
    "/need-a-moment",
    response_model=PauseResponse,
    summary="I need a moment",
    description=(
        "UC-REG-14. Stops the task flow. Returns an acknowledgment and the 988 resource, and nothing else. "
        "Nothing is stored, no timers or reminders are set, and progress already saved stays saved. "
        "No sign-in needed, so it works from the welcome screen too. The same for every voice."
    ),
)
def need_a_moment(request: Request) -> PauseResponse:
    copy: Copy = request.app.state.copy
    return PauseResponse(screen=onboarding.paused_screen(copy, distress=False), support=onboarding.support(copy),
                         next_step=onboarding.paused_step(copy))


@router.post(
    "/case-handoff",
    response_model=CaseHandoffResponse,
    summary="Start toward case creation",
    description=(
        "After UC-REG-12. The relationship shapes the wording of case creation. A power of attorney is told "
        "that authority generally ends at death. Not stored here. It's saved on the case when the case is created."
    ),
    responses={409: {"description": "Onboarding isn't finished, or an acknowledgment changed."}},
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
                               notes=notes, next_step=messages.registration_next_step(rel))

