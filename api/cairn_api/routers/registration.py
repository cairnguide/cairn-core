"""UC-1 to UC-4: account creation for each persona."""
from fastapi import APIRouter, Depends, Request, Response

from .. import messages
from ..auth import Identity, get_identity
from ..errors import ApiError
from ..schemas import (ConsentOut, PolicyVersions, Relationship, RegistrationRequest, RegistrationResponse,
                       UserOut)

router = APIRouter(prefix="/v1", tags=["Registration"])


@router.get("/policies", response_model=PolicyVersions, summary="Current terms and privacy policy versions")
def current_policies(request: Request) -> PolicyVersions:
    settings = request.app.state.settings
    return PolicyVersions(terms_version=settings.terms_version, privacy_version=settings.privacy_version)


@router.post(
    "/registrations",
    response_model=RegistrationResponse,
    status_code=201,
    summary="Create the Cairn account for a signed-in identity",
    description=(
        "Call after the identity provider has signed the user in (password or passkey) and confirmed "
        "their email. Email comes from the verified token, never from the body. Records acceptance of the "
        "current terms and privacy policy. Safe to repeat: a returning user gets 200 and their existing account."
    ),
    responses={200: {"description": "Account already existed. Returned unchanged apart from any name edits."},
               403: {"description": "Email not yet confirmed with the identity provider."},
               422: {"description": "Terms or privacy version is not the current one."}},
)
def register(req: RegistrationRequest, request: Request, response: Response,
             identity: Identity = Depends(get_identity)) -> RegistrationResponse:
    settings = request.app.state.settings
    if not identity.email or not identity.email_verified:
        raise ApiError(403, "email_not_verified",
                       "Please confirm your email address using the link we sent, then come back here.")
    if (req.accepted_terms_version != settings.terms_version
            or req.accepted_privacy_version != settings.privacy_version):
        raise ApiError(422, "consent_required",
                       "Please review and accept the current terms of use and privacy policy.",
                       terms_version=settings.terms_version, privacy_version=settings.privacy_version)

    with request.app.state.db.session() as s:
        existing = s.one("SELECT cairn.resolve_user(%s) AS id", (identity.subject,))["id"]
        user_id = s.one("SELECT cairn.register_user(%s, %s, %s, %s, %s) AS id",
                        (identity.subject, identity.email, req.first_name, req.last_name, req.phone))["id"]
        s.conn.execute("SELECT set_config('app.user_id', %s, true)", (str(user_id),))
        s.user_id = user_id

        if existing:
            s.conn.execute("UPDATE cairn.users SET first_name = %s, last_name = %s, phone = %s WHERE id = %s",
                           (req.first_name, req.last_name, req.phone, user_id))
        for purpose, version in (("terms", settings.terms_version), ("privacy", settings.privacy_version)):
            already = s.one("SELECT 1 FROM cairn.consents WHERE user_id = %s AND purpose = %s "
                            "AND policy_version = %s AND withdrawn_at IS NULL", (user_id, purpose, version))
            if not already:
                s.conn.execute("INSERT INTO cairn.consents (user_id, purpose, policy_version) VALUES (%s, %s, %s)",
                               (user_id, purpose, version))
                s.audit("consent_granted", object_type="user", object_id=user_id)
        if not existing:
            s.audit("user_registered", object_type="user", object_id=user_id)

        user = s.one("SELECT id, email, first_name, last_name, phone FROM cairn.users WHERE id = %s", (user_id,))
        consents = s.all("SELECT purpose, policy_version, granted_at FROM cairn.consents "
                         "WHERE user_id = %s AND withdrawn_at IS NULL ORDER BY granted_at", (user_id,))

    if existing:
        response.status_code = 200

    rel = req.relationship_to_deceased
    professional = rel == Relationship.fiduciary
    notes = [messages.WELCOME_PROFESSIONAL if professional else messages.WELCOME_FAMILY]
    if rel == Relationship.power_of_attorney:
        notes.append(messages.POA_ENDS_AT_DEATH)
    return RegistrationResponse(
        user=UserOut.model_validate(user),
        consents=[ConsentOut.model_validate(c) for c in consents],
        language_profile="professional" if professional else "family",
        notes=notes,
        next_step=messages.registration_next_step(rel),
    )


@router.get("/me", response_model=UserOut, summary="The signed-in user's account")
def me(request: Request, identity: Identity = Depends(get_identity)) -> UserOut:
    with request.app.state.db.session(identity.subject) as s:
        uid = s.require_user()
        return UserOut.model_validate(
            s.one("SELECT id, email, first_name, last_name, phone FROM cairn.users WHERE id = %s", (uid,)))
