"""The signed-in account: Settings (name, voice, time zone) and deletion (UC-ACCT-01).

Everything here stays available when the account is read-only (D-05).
"""
from fastapi import APIRouter, Depends, Request
from psycopg import sql

from .. import account as acct
from ..auth import Identity, get_identity
from ..copy_store import Copy
from ..schemas import (
    AccountDeletionInfo,
    AccountDeletionRequest,
    AccountDeletionResponse,
    AccountPatch,
    AccountResponse,
    NextStep,
    Note,
    Option,
)

router = APIRouter(prefix="/v1/me", tags=["Account"])


def _account_response(s, request: Request) -> AccountResponse:
    copy: Copy = request.app.state.copy
    account = acct.load_account(s)
    return AccountResponse(account=acct.account_out(account, copy), notes=acct.account_notes(s, account, copy))


@router.get("", response_model=AccountResponse, summary="The signed-in account",
            description="Includes the effective status (read_only after the trial), the trial end date in the "
                        "account's time zone, the AI guide label, and any banner or reminder to show.")
def me(request: Request, identity: Identity = Depends(get_identity)) -> AccountResponse:
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        return _account_response(s, request)


@router.patch("", response_model=AccountResponse, summary="Change Settings",
              description="Preferred name, pronunciation, voice, and time zone. The voice changes tone only. "
                          "It never changes the crisis protocol, AI disclosure, referrals, or citations.")
def update_me(req: AccountPatch, request: Request, identity: Identity = Depends(get_identity)) -> AccountResponse:
    with request.app.state.db.session(identity.subject) as s:
        uid = s.require_user()
        fields = {f: getattr(req, f) for f in req.model_fields_set}
        if "voice" in fields:
            fields["voice"] = fields["voice"].value
        assignments = sql.SQL(", ").join(
            sql.SQL("{} = {}").format(sql.Identifier(f), sql.Placeholder(f)) for f in sorted(fields))
        s.conn.execute(sql.SQL("UPDATE cairn.users SET {} WHERE id = {}").format(assignments, sql.Placeholder("id")),
                       {**fields, "id": uid})
        s.audit("account_settings_updated", object_type="user", object_id=uid)
        return _account_response(s, request)


@router.get("/deletion", response_model=AccountDeletionInfo, summary="What deleting the account removes",
            description="UC-ACCT-01 steps 1 and 2. Explain plainly, then ask for confirmation.")
def deletion_info(request: Request, identity: Identity = Depends(get_identity)) -> AccountDeletionInfo:
    copy: Copy = request.app.state.copy
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
    return AccountDeletionInfo(
        explanation=copy["deletion_explanation"],
        next_step=NextStep(action="confirm_account_deletion", prompt=copy["deletion_question"], options=[
            Option(value="confirm", label=copy["deletion_confirm_button"]),
            Option(value="cancel", label=copy["deletion_cancel_button"]),
        ]),
    )


@router.post(
    "/deletion",
    response_model=AccountDeletionResponse,
    summary="Delete the account and personal data",
    description=(
        "UC-ACCT-01. Deletes the account, its consents and reminders, and every case it created, now, in one "
        "transaction. This is deletion, not deactivation. The Auth0 user is deleted, and Apple tokens are "
        "revoked through the Sign in with Apple REST API, by a job that works through the queue this adds to. "
        "Works in every status, including read-only."
    ),
)
def delete_me(req: AccountDeletionRequest, request: Request,
              identity: Identity = Depends(get_identity)) -> AccountDeletionResponse:
    copy: Copy = request.app.state.copy
    with request.app.state.db.session(identity.subject) as s:
        s.require_user()
        s.one("SELECT cairn.delete_my_account() AS done")
    return AccountDeletionResponse(notes=[Note(kind="acknowledgment", text=copy["deletion_done"])])
