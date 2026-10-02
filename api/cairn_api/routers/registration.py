"""UC-REG-01 to UC-REG-05: welcome, sign-in choices, and account creation."""
from fastapi import APIRouter, Depends, Request, Response

from .. import account as acct
from .. import onboarding
from ..auth import Identity, get_identity
from ..copy_store import Copy
from ..errors import ApiError
from ..schemas import (
    Link,
    NextStep,
    Note,
    OnboardingResponse,
    Option,
    PolicyVersions,
    RegistrationRequest,
    SignInMethodsResponse,
    WelcomeResponse,
)

router = APIRouter(prefix="/v1", tags=["Registration"])


@router.get(
    "/welcome",
    response_model=WelcomeResponse,
    summary="The welcome screen",
    description=(
        "UC-REG-01. Acknowledgment first, then three equally weighted ways to continue, a sign-in link, and a "
        "link to the public journey map. Asks for nothing. After a cancelled Google or Apple sign-in, call "
        "with oauth_cancelled=true to add the reassurance note (UC-REG-02, UC-REG-03)."
    ),
)
def welcome(request: Request, oauth_cancelled: bool = False) -> WelcomeResponse:
    copy: Copy = request.app.state.copy
    settings = request.app.state.settings
    notes = [Note(kind="info", text=copy["oauth_cancelled"])] if oauth_cancelled else []
    return WelcomeResponse(
        acknowledgment=copy["welcome_acknowledgment"],
        methods=onboarding.sign_in_options(copy, settings.email_connection),
        sign_in_label=copy["sign_in_link"],
        not_ready=Link(label=copy["welcome_not_ready_link"], url=settings.journey_map_url),
        notes=notes,
        support=onboarding.support(copy),
    )


@router.get(
    "/sign-in-methods",
    response_model=SignInMethodsResponse,
    summary="Ways to create an account or sign in",
    description="Google, Apple, or email (a passwordless magic link by default). Each option names the Auth0 "
                "connection the client passes to Auth0's /authorize endpoint. No sign-in is needed to call this.",
)
def sign_in_methods(request: Request) -> SignInMethodsResponse:
    copy: Copy = request.app.state.copy
    return SignInMethodsResponse(methods=onboarding.sign_in_options(copy, request.app.state.settings.email_connection))


@router.get("/policies", response_model=PolicyVersions, summary="Current policy and acknowledgment versions")
def current_policies(request: Request) -> PolicyVersions:
    settings = request.app.state.settings
    return PolicyVersions(terms_version=settings.terms_version, privacy_version=settings.privacy_version,
                          acknowledgments=acct.current_versions(request))


@router.post(
    "/registrations",
    response_model=OnboardingResponse,
    status_code=201,
    summary="Create the account, or sign in and resume onboarding",
    description=(
        "UC-REG-02 to UC-REG-05 and UC-REG-13. Call after every Auth0 sign-in. Auth0 has already verified the "
        "Google or Apple identity token, or the email magic link, and this API verifies Auth0's signed token. "
        "Email and sign-in method come from the token, never from the body. A new account starts in "
        "pending_onboarding and the response is the Privacy Policy and Terms acknowledgment. There is no age "
        "question. A returning user gets 200 and the first incomplete step, with a welcome-back note."
    ),
    responses={200: {"description": "Account already existed. Resumes at the first incomplete step."},
               403: {"description": "Email not yet confirmed, or an unsupported sign-in method."},
               409: {"description": "This email already has an account created another way (UC-REG-05). "
                                    "sign_in_method names it and next_step offers it as the primary button."}},
)
def register(req: RegistrationRequest, request: Request, response: Response,
             identity: Identity = Depends(get_identity)) -> OnboardingResponse:
    copy: Copy = request.app.state.copy
    if not identity.email or not identity.email_verified:
        raise ApiError(403, "email_not_verified", copy["email_check_inbox"])

    with request.app.state.db.session() as s:
        existing = s.resolve_user(identity.subject)
        if not existing:
            # Called only with the verified email from the caller's own token.
            # Accounts are never linked automatically across providers.
            taken_by = s.sign_in_method_for_email(identity.email)
            if taken_by:
                raise account_exists(copy, taken_by)
        user_id = s.create_account(identity.subject, identity.email, identity.sign_in_method.value,
                                   req.name_from_provider, req.time_zone)
        s.user_id = user_id
        if existing and req.time_zone:
            s.update_account(time_zone=req.time_zone)
        if not existing:
            s.audit("user_registered", object_type="user", object_id=user_id)
        result = onboarding.response(s, request, resumed=bool(existing))

    if existing:
        response.status_code = 200
    return result


def account_exists(copy: Copy, method: str) -> ApiError:
    name = onboarding.provider_name(method, copy)
    primary = Option(value=method, label=copy["sign_in_with_provider"].format(provider=name))
    return ApiError(409, "account_exists", copy["account_exists"].format(provider=name), sign_in_method=method,
                    next_step=NextStep(action="sign_in_with_existing_method", prompt=primary.label,
                                       options=[primary]).model_dump())
