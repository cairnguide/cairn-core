"""UC-REG-01 to UC-REG-05: welcome, sign-in choices, and account creation. Also the pages that work signed out:
Support resources (crisis plan support_resources), can't get into the sign-in email (UC-REG-20), and Take a break
before sign-in (UC-BRK-02)."""
from fastapi import APIRouter, Depends, Request, Response

from .. import account as acct
from .. import breaks, onboarding
from ..auth import Identity, get_identity
from ..copy_store import Copy
from ..errors import ApiError
from ..schemas import (
    BreakResponse,
    Link,
    NextStep,
    Note,
    OnboardingResponse,
    Option,
    PolicyVersions,
    RegistrationRequest,
    SignInHelpResponse,
    SignInMethod,
    SignInMethodsResponse,
    SupportResource,
    SupportResourcesPage,
    WelcomeResponse,
)

router = APIRouter(prefix="/v1", tags=["Registration"])


@router.get(
    "/welcome",
    response_model=WelcomeResponse,
    summary="The welcome screen",
    description=(
        "UC-REG-01. Acknowledgment first, then three equally weighted ways to continue, a sign-in link, a link to "
        "the public journey map, and the Support resources link, which works signed out (AC-26-10). Also the copy "
        "around the email link and its landing page (UC-REG-04), and the session policy (D-20). Asks for nothing. "
        "After a cancelled Google or Apple sign-in, call with oauth_cancelled=true to add the reassurance note "
        "(UC-REG-02, UC-REG-03)."
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
        email_sign_in=onboarding.email_sign_in(copy),
        magic_link=onboarding.magic_link(copy),
        cant_get_into_email=copy["cant_get_into_email"],
        notes=notes,
        support=onboarding.support(copy),
        session=acct.session_policy(request),
    )


@router.get(
    "/support-resources",
    response_model=SupportResourcesPage,
    summary="Support resources",
    description=(
        "The crisis plan's Support resources page: 988 (call, text, chat), the Veterans Crisis Line, the Crisis Text "
        "Line, and 911 for immediate danger. No sign-in needed, so it works from the welcome screen and every setup "
        "screen (AC-26-10). Opening it never changes the care level, never starts a rest, and is never logged with a "
        "user or case id. AFSP loss survivor support is added on a case that has it (GET /v1/cases/{id})."
    ),
)
def support_resources(request: Request) -> SupportResourcesPage:
    copy: Copy = request.app.state.case_copy
    plan = request.app.state.copy
    return SupportResourcesPage(intro=plan["support_resources_intro"], resources=[
        SupportResource(id="lifeline_988", text=copy["support_988"], url="https://988lifeline.org"),
        SupportResource(id="veterans_crisis_line", text=copy["support_veterans_crisis_line"],
                        url="https://www.veteranscrisisline.net"),
        SupportResource(id="crisis_text_line", text=copy["support_crisis_text_line"],
                        url="https://www.crisistextline.org"),
        SupportResource(id="emergency_911", text=copy["emergency_911"], url="tel:911"),
    ])


# UC-REG-20. Where Google and Apple accounts are recovered. Cairn can't change those sign-ins itself.
PROVIDER_RECOVERY = {SignInMethod.google: ("sign_in_help_google", "sign_in_help_google_link",
                                           "https://accounts.google.com/signin/recovery"),
                     SignInMethod.apple: ("sign_in_help_apple", "sign_in_help_apple_link", "https://iforgot.apple.com")}


@router.get(
    "/sign-in-help",
    response_model=SignInHelpResponse,
    summary="I can't get into my email",
    description=(
        "UC-REG-20 (proposed). For email accounts, opens a support request: support checks it's really the person "
        "with a documented process before changing the sign-in email, and tells the old address first if it still "
        "works. For Google or Apple, access follows that account, so this points to the provider's recovery page. "
        "No sign-in needed. Nothing is stored. [LEGAL REVIEW REQUIRED] security review of the identity check."
    ),
)
def sign_in_help(request: Request, method: SignInMethod | None = None) -> SignInHelpResponse:
    copy: Copy = request.app.state.copy
    contact = NextStep(action="contact_support", prompt=copy["recovery_intro"],
                       options=[Option(value="contact_support", label=copy["sign_in_help_contact"])])
    if method in PROVIDER_RECOVERY:
        text_key, link_key, url = PROVIDER_RECOVERY[method]
        return SignInHelpResponse(intro=copy[text_key], provider_recovery=Link(label=copy[link_key], url=url),
                                  next_step=contact)
    return SignInHelpResponse(intro=copy["recovery_intro"], next_step=contact)


@router.get(
    "/break",
    response_model=BreakResponse,
    summary="Take a break before signing in",
    description="UC-BRK-02 (screen S-01). Nothing is saved and nothing is sent. A magic link already sent stays "
                "valid for its 15 minutes. Signed-in screens use /v1/me/break.",
)
def pre_sign_in_break(request: Request) -> BreakResponse:
    return breaks.pre_sign_in(request.app.state.break_copy)


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
        "pending_onboarding and the response is the adult question (UC-REG-06). A returning user gets 200 and the "
        "first incomplete step, with a welcome-back note. Setup progress follows the account, not the browser, so "
        "it continues on any device (UC-REG-04)."
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
                raise account_exists(copy, taken_by, identity.sign_in_method.value)
        user_id = s.create_account(identity.subject, identity.email, identity.sign_in_method.value,
                                   time_zone=req.time_zone)
        s.user_id = user_id
        if existing and req.time_zone:
            s.update_account(time_zone=req.time_zone)
        if not existing:
            s.audit("user_registered", object_type="user", object_id=user_id)
        result = onboarding.response(s, request, resumed=bool(existing))

    if existing:
        response.status_code = 200
    return result


def account_exists(copy: Copy, method: str, attempted: str) -> ApiError:
    """UC-REG-05. The method used before is the primary button. The second option adds the method just tried, which
    the client does by signing in with the original method and then calling POST /v1/me/sign-in-methods."""
    name = onboarding.provider_name(method, copy)
    primary = Option(value=method, label=copy["sign_in_with_provider"].format(provider=name))
    link = Option(value="link_after_sign_in", label=copy["link_after_sign_in"].format(
        provider=name, new_provider=onboarding.provider_name(attempted, copy)))
    return ApiError(409, "account_exists", copy["account_exists"].format(provider=name), sign_in_method=method,
                    next_step=NextStep(action="sign_in_with_existing_method", prompt=primary.label,
                                       options=[primary, link]).model_dump())
