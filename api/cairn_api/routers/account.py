"""The signed-in account: Settings (UC-REG-17: name, pronunciation, voice, time zone), other ways to sign in
(UC-REG-05), signing out (UC-REG-19), deleting the account (UC-ACCT-01, which UC-REG-15 described before),
downloading all of its data (UC-REG-16), and account requests typed to Cairn.

Everything here stays available when the account is read-only, and deleting and
downloading are always free (D-05, D-08, D-2026-09-25-F1). Neither is gated on
onboarding or on a changed acknowledgment. At care level 4 no account action is
carried out in the same turn (AC-26-11).
"""
import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from .. import account as acct
from .. import account_chat, export, intake, onboarding
from ..account import CareLevel
from ..auth import Identity, get_identity
from ..copy_store import Copy
from ..errors import ApiError
from ..schemas import (
    AccountChatSession,
    AccountDeletionInfo,
    AccountDeletionRequest,
    AccountDeletionResponse,
    AccountMessageIn,
    AccountMessageResponse,
    AccountPatch,
    AccountResponse,
    DataExport,
    DataExportInfo,
    IntakeSession,
    LinkSignInMethodRequest,
    NextStep,
    Note,
    NotificationSettingsPatch,
    Option,
    ReadAloud,
    SafetyMode,
    SignInMethod,
    SignInMethodsChange,
    SignOutResponse,
    SupportResource,
)
from ..stripe_client import StripeError
from .notifications import apply_settings, settings_saved

router = APIRouter(prefix="/v1/me", tags=["Account"])
log = logging.getLogger("cairn_api.account")


def _account_response(s, request: Request, notes: list[Note] | None = None, *, care_level: int = 1) -> AccountResponse:
    copy: Copy = request.app.state.copy
    account = acct.load_account(s)
    return AccountResponse(account=acct.account_out(account, copy),
                           notes=[*(notes or []), *acct.account_notes(s, account, request, care_level=care_level)],
                           session=acct.session_policy(request))


@router.get("", response_model=AccountResponse, summary="The signed-in account",
            description="Includes the effective status (read_only after the trial), the trial end date in the "
                        "account's time zone, the AI guide label, and any banner or reminder to show.")
def me(request: Request, identity: Identity = Depends(get_identity), care_level: CareLevel = 1) -> AccountResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        return _account_response(s, request, care_level=care_level)


@router.patch("", response_model=AccountResponse, summary="Change Settings",
              description="UC-REG-17. Preferred name, pronunciation, voice, and time zone, each on its own. Each "
                          "change is read back in one line, saved, and confirmed to the sign-in email "
                          "(copy.settings_saved says where). Same validation and data boundary as setup: a name of "
                          "at most 50 characters with no 5 or more digits, and distress language is never saved as "
                          "a name. The voice changes tone only. Works on a read-only account. At care level 4 "
                          "nothing changes in that turn (AC-26-11).",
              responses={422: {"description": "preferred_name_invalid. The text isn't kept."}})
def update_me(req: AccountPatch, request: Request, identity: Identity = Depends(get_identity)) -> AccountResponse:
    copy: Copy = request.app.state.copy
    with request.app.state.db.session(identity.subject) as s:
        uid = s.require_user()
        if req.care_level == 4:
            return _account_response(s, request, [Note(kind="acknowledgment", text=copy["settings_not_now"])])
        fields = {f: getattr(req, f) for f in req.model_fields_set - {"care_level"}}
        if "preferred_name" in fields:
            if onboarding.shows_distress(fields["preferred_name"]):
                return _account_response(s, request, [Note(kind="crisis", text=copy["crisis_resource"])])
            if (problem := onboarding.preferred_name_problem(fields["preferred_name"], copy)) is not None:
                raise ApiError(422, "preferred_name_invalid", problem)
        if "voice" in fields:
            fields["voice"] = fields["voice"].value
        s.update_account(**fields)
        s.audit("account_settings_updated", object_type="user", object_id=uid)
        account = acct.load_account(s)
        lines = [_readback(copy, f, account) for f in ("preferred_name", "name_pronunciation", "voice", "time_zone")
                 if f in fields]
        saved = settings_saved(s, request, account)
        return _account_response(s, request, [Note(kind="acknowledgment", text=" ".join([*lines, saved]))])


def _readback(copy: Copy, field: str, account: dict) -> str:
    if field == "preferred_name":
        return copy["readback_preferred_name"].format(preferred_name=account["preferred_name"])
    if field == "voice":
        return copy["readback_voice"].format(voice=copy[f"voice_{account['voice']}_label"])
    return copy["readback_pronunciation" if field == "name_pronunciation" else "readback_time_zone"]


@router.post(
    "/sign-out",
    response_model=SignOutResponse,
    summary="Sign out",
    description=(
        "UC-REG-19. The token used here stops working, everything is saved, and the response is copy.signed_out. "
        "A break is never ended or changed by signing out (UC-BRK-12). At care levels 3 and 4 the response also "
        "carries the crisis resource and the Support resources link. The client also clears its Auth0 session and "
        "anything cached, so the back button shows no case data."
    ),
)
def sign_out(request: Request, identity: Identity = Depends(get_identity),
             care_level: int = Query(default=1, ge=1, le=4)) -> SignOutResponse:
    copy: Copy = request.app.state.copy
    st = request.app.state.settings
    request.app.state.db.end_session(identity.subject, datetime.now(timezone.utc),
                                     timedelta(seconds=st.inactivity_timeout_seconds))
    support = [copy["crisis_resource"], copy["support_resources_link"]] if care_level >= 3 else []
    return SignOutResponse(message=copy["signed_out"], support=support)


@router.post(
    "/sign-in-methods",
    response_model=SignInMethodsChange,
    summary="Add another way to sign in",
    description=(
        "UC-REG-05. The request is signed in with a sign-in that already belongs to this account. The body carries "
        "an access token from signing in with the method to add, which is verified the same way. Accounts are never "
        "linked automatically. One account per sign-in, and one sign-in per method."),
    responses={403: {"description": "The token to add is invalid or its email isn't confirmed."},
               409: {"description": "same_method: the account already signs in that way. sign_in_method_in_use: "
                                    "that sign-in or its email belongs to another account (support can merge, "
                                    "database/docs/support-account-merge.md)."}},
)
def link_sign_in_method(req: LinkSignInMethodRequest, request: Request,
                        identity: Identity = Depends(get_identity)) -> SignInMethodsChange:
    copy: Copy = request.app.state.copy
    second = request.app.state.token_verifier.verify(req.access_token)
    if not second.email or not second.email_verified:
        raise ApiError(403, "email_not_verified", copy["email_check_inbox"])
    provider = onboarding.provider_name(second.sign_in_method.value, copy)
    with request.app.state.db.session(identity.subject) as s:
        uid = s.require_user()
        result = s.link_identity(second.subject, second.sign_in_method.value, second.email)
        if result == "in_use":
            raise ApiError(409, "sign_in_method_in_use", copy["sign_in_method_in_use"].format(provider=provider))
        if result == "same_method":
            raise ApiError(409, "same_method", copy["sign_in_method_already_linked"].format(provider=provider))
        if result == "linked":
            s.audit("sign_in_method_linked", object_type="user", object_id=uid)
        key = "sign_in_method_linked" if result == "linked" else "sign_in_method_already_linked"
        return SignInMethodsChange(result=result, message=copy[key].format(provider=provider),
                                   account=acct.account_out(acct.load_account(s), copy))


@router.delete(
    "/sign-in-methods/{method}",
    response_model=SignInMethodsChange,
    summary="Remove a way to sign in that was added later",
    description="The sign-in the account was created with stays, so the account always has one. The removed "
                "sign-in's identity provider record is deleted by the identity cleanup job.",
    responses={409: {"description": "That's the sign-in the account was created with, or it isn't linked."}},
)
def unlink_sign_in_method(method: SignInMethod, request: Request,
                          identity: Identity = Depends(get_identity)) -> SignInMethodsChange:
    copy: Copy = request.app.state.copy
    provider = onboarding.provider_name(method.value, copy)
    with request.app.state.db.session(identity.subject) as s:
        uid = s.require_user()
        if not s.unlink_identity(method.value):
            raise ApiError(409, "sign_in_method_cannot_remove", copy["sign_in_method_cannot_remove"])
        s.audit("sign_in_method_unlinked", object_type="user", object_id=uid)
        return SignInMethodsChange(result="unlinked", message=copy["sign_in_method_unlinked"].format(provider=provider),
                                   account=acct.account_out(acct.load_account(s), copy))


def deletion_info_for(account: dict, copy: Copy, sub: Copy) -> AccountDeletionInfo:
    """UC-ACCT-01 steps 1 to 3. No reason asked, no retention offer, one button. With a subscription, says first that
    deleting cancels it right away with no refund (UC-SUB-16, SUB-D-05)."""
    note = None
    if acct.has_subscription(account):
        note = Note(kind="info", text=sub["delete_account_with_subscription"], legal_review_required=True)
    masked = acct.mask_email(account["email"])
    destination = copy["deletion_confirmation_destination"].format(masked_email=masked)
    return AccountDeletionInfo(
        explanation=copy["deletion_explanation"], subscription_note=note, masked_email=masked,
        confirmation_destination=destination,
        next_step=NextStep(action="confirm_account_deletion", prompt=destination,
                           options=[Option(value="confirm", label=copy["delete_account_confirm_button"])]))


@router.get("/deletion", response_model=AccountDeletionInfo, summary="What deleting the account removes",
            description="UC-ACCT-01. Says in plain words what will be deleted, says a subscription is cancelled "
                        "right away with no refund (UC-SUB-16), shows the masked address the one confirmation goes "
                        "to, and offers one button. No reason is asked and nothing is offered to keep the user. "
                        "Available in every status.")
def deletion_info(request: Request, identity: Identity = Depends(get_identity)) -> AccountDeletionInfo:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        return deletion_info_for(acct.load_account(s), request.app.state.copy, request.app.state.subscription_copy)


@router.post(
    "/deletion",
    response_model=AccountDeletionResponse,
    summary="Delete the account and everything in it",
    description=(
        "UC-ACCT-01. A Stripe subscription is cancelled first, right away, so no further charge can happen. If "
        "that fails, nothing is deleted and the user is told plainly (UC-SUB-16). Then everything goes now, in one "
        "transaction: the account, every case in any status (including cases in a 7-day hold), tasks, answers, "
        "conversation text, notification choices and the browser push endpoint, a scheduled check-in, scheduled "
        "reminders and notices, consents, and the account. This is deletion, not deactivation. One confirmation "
        "email is queued, then its address is purged once it is sent. The Auth0 user is deleted and Apple tokens "
        "are revoked (TN3194) by the identity cleanup job. The response says the user is signed out: clear the "
        "session. Works in every status, always free. At care level 4 nothing is deleted in that turn, and it "
        "works when the user asks again (AC-26-11)."
    ),
    responses={409: {"description": "not_now: care level 4."},
               503: {"description": "The subscription couldn't be cancelled. Nothing was deleted."}},
)
def delete_me(req: AccountDeletionRequest, request: Request,
              identity: Identity = Depends(get_identity)) -> AccountDeletionResponse:
    copy: Copy = request.app.state.copy
    sub: Copy = request.app.state.subscription_copy
    if req.care_level == 4:
        raise ApiError(409, "not_now", copy["settings_not_now"])
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        account = acct.load_account(s)
    if account.get("stripe_subscription_id") and account["subscription_status"] == "active":
        stripe = request.app.state.stripe
        try:
            if stripe is None:
                raise StripeError("not_configured")
            stripe.cancel_now(account["stripe_subscription_id"])
        except StripeError as exc:
            log.error("owner alert: subscription not cancelled before deletion code=%s", exc)
            raise ApiError(503, "subscription_not_cancelled", sub["delete_cancel_failed"]) from None
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        s.delete_my_account()
    done = copy["deletion_done"]
    return AccountDeletionResponse(notes=[Note(kind="acknowledgment", text=done)], signed_out=True,
                                   next_step=NextStep(action="signed_out", prompt=done))


# ------------------------------------------------------------------ download my data (UC-REG-16)

def export_info_for(copy: Copy) -> DataExportInfo:
    return DataExportInfo(explanation=copy["export_explanation"], format="json",
                          next_step=NextStep(action="download_data", prompt=copy["export_explanation"],
                                             options=[Option(value="download", label=copy["export_button"])]))


@router.get("/data-export", response_model=DataExportInfo, summary="What the download includes",
            description="UC-REG-16 step 1. One sentence on what is included. Available in every status.")
def data_export_info(request: Request, identity: Identity = Depends(get_identity)) -> DataExportInfo:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
    return export_info_for(request.app.state.copy)


@router.get(
    "/data-export/file",
    response_model=DataExport,
    summary="Download all my data",
    description=(
        "UC-REG-16. The profile, acknowledgments, every case (drafts and cases in a 7-day hold included) with "
        "its answers, summary, tasks, notification settings, notifications sent, and conversation text, as a "
        "JSON file (GDPR Article 20 portability). Never includes Social Security number digits, account "
        "numbers, or card numbers. Always free, in every status. [OPEN QUESTION card 50, Q14] a printable page "
        "or PDF as well."
    ),
)
def data_export_file(request: Request, identity: Identity = Depends(get_identity)) -> JSONResponse:
    with request.app.state.db.session(identity.subject) as s:
        uid = s.require_user()
        c = intake.ctx(s, request, acct.load_account(s))
        data = export.build(c)
        s.audit("data_exported", object_type="user", object_id=uid)
    name = f"cairn-my-data-{date.today().isoformat()}.json"
    return JSONResponse(data.model_dump(mode="json"),
                        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


# ------------------------------------------------------------------ account requests in chat

@router.post(
    "/messages",
    response_model=AccountMessageResponse,
    summary="Ask Cairn about the account",
    description=(
        "UC-REG-15, UC-REG-16, UC-CASE-20 by chat. Delete my account and download my data lead to the same "
        "steps as Settings (deleting still needs its one button). Stop messages applies in one step, with no "
        "persuasion. Email me instead is read back and needs a yes. A risk-of-harm signal is answered with "
        "safety first and no account action in that turn. Asked again, the request goes ahead with no extra "
        "questions. Free text is redacted as it is received and never stored."
    ),
)
def message(req: AccountMessageIn, request: Request,
            identity: Identity = Depends(get_identity)) -> AccountMessageResponse:
    copy: Copy = request.app.state.copy
    session = req.session or AccountChatSession()
    reading = account_chat.read(req.text)
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        account = acct.load_account(s)
        c = intake.ctx(s, request, account)
        # UC-CASE-15. The text was redacted while the request was parsed. Say what kind, never the value.
        redactions = getattr(req.text, "redactions", ())
        base = dict(redactions=redactions, masked_text=str(req.text) if redactions else None)
        support: list[SupportResource] = []
        crisis_body: list[str] = []
        if reading.risk_of_harm:
            crisis_body, support = intake.crisis_support(c, _veteran_answers(s), level=4)
            if not session.safety_first_shown:
                s.count_level_4_referral()  # SB 243: an anonymous monthly count, once per crisis
            if not session.safety_first_shown or reading.intent == "help":
                # Safety first. No account action in this turn. With no account request in it, stay on safety.
                step = intake.safety_step(c, IntakeSession(safety_mode=SafetyMode.risk_of_harm))
                return _reply(c, "safety_first", c.copy["risk_ack"], crisis_body, support, step,
                              AccountChatSession(safety_first_shown=True), **base)
        session = AccountChatSession()

        if reading.intent == "delete_account":
            info = deletion_info_for(account, copy, request.app.state.subscription_copy)
            body = [info.explanation, *([info.subscription_note.text] if info.subscription_note else [])]
            return _reply(c, "delete_account", copy["chat_delete_start"], [*body, *crisis_body], support,
                          info.next_step, session, **base)
        if reading.intent == "download_data":
            info = export_info_for(copy)
            return _reply(c, "download_data", copy["chat_download_start"], crisis_body, support, info.next_step,
                          session, **base)
        if reading.intent == "stop_notifications":
            result = apply_settings(s, request, NotificationSettingsPatch(stop_all_reminders=True))
            return _reply(c, "stop_notifications", result.acknowledgment, crisis_body, support, result.next_step,
                          session, **base)
        if reading.intent == "change_notifications":
            # Read back first. Nothing is saved until the user confirms in Settings (UC-REG-17).
            proposal = account_chat.switch_channel(reading.channel)
            step = NextStep(action="confirm_notification_change", prompt=copy["chat_notifications_start"],
                            options=[Option(value="confirm", label=copy["continue"])])
            return _reply(c, "change_notifications", copy["chat_notifications_start"], crisis_body, support,
                          step, session, proposal=proposal, **base)
        if reading.intent == "sms_not_available":
            return _reply(c, "sms_not_available", c.copy["notifications_sms_not_available"], crisis_body, support,
                          NextStep(action="change_notifications", prompt=c.copy["notifications_intro"]),
                          session, **base)
        return _reply(c, "help", None, crisis_body, support, NextStep(
            action="choose_account_request", prompt=copy["chat_help"],
            options=[Option(value="delete_account", label=copy["delete_my_account"]),
                     Option(value="download_data", label=copy["download_my_data"]),
                     Option(value="change_notifications", label=c.copy["change_notifications"])]),
            session, **base)


def _veteran_answers(s) -> dict:
    """Any of the user's cases says the person was a veteran: show the Veterans Crisis Line."""
    return {"veteran_status": {"answer_state": "answered", "value": "yes"}} if s.any_case_says_veteran() else {}


def _reply(c: intake.Ctx, intent: str, ack: str | None, body: list[str], support: list[SupportResource],
           step: NextStep, session: AccountChatSession, *, proposal=None, redactions=(),
           masked_text=None) -> AccountMessageResponse:
    text = " ".join(x for x in [ack, *body, step.prompt] if x)
    return AccountMessageResponse(intent=intent, acknowledgment=ack, body=body, support=support, proposal=proposal,
                                  redactions=list(redactions), masked_text=masked_text, next_step=step,
                                  session=session, read_aloud=ReadAloud(label=c.copy["read_this_to_me"], text=text))
