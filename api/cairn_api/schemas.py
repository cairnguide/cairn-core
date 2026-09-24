"""Request and response contracts. These models are the OpenAPI (Swagger) schema.

Every request model forbids unknown fields and trims whitespace, so the API
accepts exactly what the contract describes and nothing else. Validation here
mirrors the database constraints so users get a plain-language answer before a
round trip. The database remains the enforced backstop.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

# ------------------------------------------------------------------ shared types

# USPS codes for the 50 states, DC, and the territories that run their own vital
# records offices. The database domain only checks the shape (two capital letters).
US_STATE_CODES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR GU VI AS MP".split()
)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _state_code(value: object) -> object:
    if isinstance(value, str):
        value = value.strip().upper()
        if value not in US_STATE_CODES:
            raise ValueError("must be a two-letter US state or territory code")
    return value


def _no_control_chars(value: str) -> str:
    if _CONTROL_CHARS.search(value):
        raise ValueError("contains characters that aren't allowed")
    return value


def _not_future(value: date) -> date:
    if value > date.today():
        raise ValueError("can't be in the future")
    return value


StateCode = Annotated[str, BeforeValidator(_state_code), Field(examples=["NH"], pattern=r"^[A-Z]{2}$")]
Name = Annotated[str, Field(min_length=1, max_length=100), AfterValidator(_no_control_chars)]
OptionalText100 = Annotated[str, Field(min_length=1, max_length=100), AfterValidator(_no_control_chars)]
OptionalText200 = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(_no_control_chars)]
PastDate = Annotated[date, AfterValidator(_not_future)]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Relationship(str, Enum):
    spouse = "spouse"
    child = "child"
    sibling = "sibling"
    other_family = "other_family"
    power_of_attorney = "power_of_attorney"
    fiduciary = "fiduciary"


class TriState(str, Enum):
    yes = "yes"
    no = "no"
    unknown = "unknown"


class TriStateAnswer(str, Enum):
    """What the client may send. 'skip' is stored as 'unknown'."""
    yes = "yes"
    no = "no"
    unknown = "unknown"
    skip = "skip"


class PlaceType(str, Enum):
    hospital = "hospital"
    hospice = "hospice"
    home = "home"
    facility = "facility"
    other = "other"


class TaskStatus(str, Enum):
    not_started = "not_started"
    in_progress = "in_progress"
    done = "done"
    skipped = "skipped"
    not_applicable = "not_applicable"


class TaskCategory(str, Enum):
    certificates = "certificates"
    funeral = "funeral"
    agencies = "agencies"
    financial_institutions = "financial_institutions"
    home_and_personal = "home_and_personal"
    other = "other"


class TaskKind(str, Enum):
    """Tasks with a structured completion record. Everything else is 'general'."""
    certificate_order = "certificate_order"
    institution_notice = "institution_notice"
    general = "general"


# ------------------------------------------------------------------ conversational envelope

class Option(ResponseModel):
    value: str
    label: str
    available: bool = True
    unavailable_reason: str | None = None


class NextStep(ResponseModel):
    """The single next thing to ask or do. Clients render one question at a time."""
    action: str = Field(description="Machine-readable step code the client routes on.")
    prompt: str = Field(description="Plain-language text to show the user.")
    options: list[Option] | None = None


class Note(ResponseModel):
    kind: Literal["acknowledgment", "info", "legal", "crisis", "reminder", "account"]
    text: str
    legal_review_required: bool = False


# ------------------------------------------------------------ registration and onboarding (UC-REG-01 to UC-REG-14)

def _time_zone(value: str) -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    if not re.fullmatch(r"[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}", value) or value == "localtime":
        raise ValueError("must be an IANA time zone name, for example America/New_York")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("must be an IANA time zone name, for example America/New_York") from None
    return value


TimeZone = Annotated[str, Field(min_length=1, max_length=64, examples=["America/New_York"]),
                     AfterValidator(_time_zone)]
Pronunciation = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(_no_control_chars)]
ClientId = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9 ._/()+-]+$",
                                examples=["ios/1.0.0"],
                                description="App platform and version, stored on the consent record.")]


class SignInMethod(str, Enum):
    google = "google"
    apple = "apple"
    email = "email"


class Personality(str, Enum):
    gentle = "gentle"
    steady = "steady"
    straightforward = "straightforward"


class AccountStatus(str, Enum):
    pending_onboarding = "pending_onboarding"
    active_no_case = "active_no_case"
    trial_active = "trial_active"
    read_only = "read_only"
    subscribed = "subscribed"
    pending_deletion = "pending_deletion"


class OnboardingStep(str, Enum):
    """The last completed step."""
    account_created = "account_created"
    privacy_terms_accepted = "privacy_terms_accepted"
    trial_terms_accepted = "trial_terms_accepted"
    ai_notice_accepted = "ai_notice_accepted"
    preferred_name_saved = "preferred_name_saved"
    complete = "complete"


class ConsentType(str, Enum):
    privacy_terms = "privacy_terms"
    trial_terms = "trial_terms"
    ai_notice = "ai_notice"


class ScreenId(str, Enum):
    welcome = "welcome"
    privacy_terms = "privacy_terms"
    trial_terms = "trial_terms"
    ai_notice = "ai_notice"
    declined = "declined"
    preferred_name = "preferred_name"
    personality = "personality"
    case_handoff = "case_handoff"
    ready = "ready"
    paused = "paused"


class Link(ResponseModel):
    label: str
    url: str


class Checkbox(ResponseModel):
    label: str
    checked: Literal[False] = Field(default=False, description="Never pre-checked.")
    document_version: str = Field(description="Send back when agreeing, to show which text was agreed to.")


class TextInput(ResponseModel):
    prefill: str | None = Field(default=None, description="Shared by Google or Apple. Show it for the user "
                                                          "to confirm or change. Never save it without that.")
    optional_link_label: str | None = None
    optional_prompt: str | None = None


class Choice(ResponseModel):
    value: str
    label: str
    sample: str | None = Field(default=None, description="How Cairn would reply in this voice.")


class Screen(ResponseModel):
    """What to render. One question per screen. The question itself is next_step.prompt."""
    id: ScreenId
    acknowledgment: str | None = None
    body: list[str] = []
    legal_notice: list[str] = []
    sample_situation: str | None = None
    checkbox: Checkbox | None = None
    input: TextInput | None = None
    choices: list[Choice] = []
    links: list[Link] = []
    ai_provider: str | None = Field(default=None, description="Third-party AI provider, named before any "
                                                             "data is sent to it.")
    legal_review_required: bool = False


class Support(ResponseModel):
    """Shown on every onboarding screen (UC-REG-14)."""
    need_a_moment_label: str
    crisis_resource: str


class SignInOption(ResponseModel):
    method: SignInMethod
    label: str
    auth0_connection: str = Field(description="Pass as the connection parameter to Auth0's /authorize "
                                              "to skip the chooser and go straight to this method.")


class SignInMethodsResponse(ResponseModel):
    methods: list[SignInOption]


class WelcomeResponse(ResponseModel):
    acknowledgment: str
    methods: list[SignInOption] = Field(description="Equally weighted. Show all three with the same emphasis.")
    sign_in_label: str
    not_ready: Link
    notes: list[Note]
    support: Support


class RegistrationRequest(RequestModel):
    name_from_provider: Name | None = Field(
        default=None,
        description="A name Google or Apple shared at sign-in. Kept only to pre-fill the preferred name "
                    "question. Apple sends it on the first sign-in only, so send it then.")
    time_zone: TimeZone | None = Field(default=None, description="Used to show trial dates in local time.")


class AccountOut(ResponseModel):
    id: UUID
    email: str
    sign_in_method: SignInMethod | None
    preferred_name: str | None
    name_pronunciation: str | None
    personality: Personality
    status: AccountStatus = Field(description="Effective status. read_only once the trial has ended.")
    onboarding_step: OnboardingStep
    trial_started_at: datetime | None
    trial_ends_at: datetime | None
    trial_end_date: date | None = Field(description="trial_ends_at as a date in the account's time zone.")
    time_zone: str | None
    ai_label: str | None = Field(description="Show in every chat view once set.")


class OnboardingResponse(ResponseModel):
    account: AccountOut
    screen: Screen
    notes: list[Note]
    support: Support
    next_step: NextStep


class PauseResponse(ResponseModel):
    screen: Screen
    support: Support
    next_step: NextStep


class AccountResponse(ResponseModel):
    account: AccountOut
    notes: list[Note] = Field(description="The read-only banner or a due trial reminder, when there is one.")


class AcknowledgmentIn(RequestModel):
    """agreed=false is the "I'm not sure" path (UC-REG-10). Nothing is recorded for it."""
    agreed: bool
    document_version: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    client: ClientId

    @model_validator(mode="after")
    def _version_when_agreeing(self):
        if self.agreed and not self.document_version:
            raise ValueError("send the document_version shown with the checkbox")
        return self


class PreferredNameIn(RequestModel):
    preferred_name: Name
    name_pronunciation: Pronunciation | None = None


class PersonalityIn(RequestModel):
    choice: Literal["gentle", "steady", "straightforward", "choose_for_me"] = Field(
        description="choose_for_me selects steady.")


class AccountPatch(RequestModel):
    """Settings. Personality can be changed at any time. Omitted fields are unchanged."""
    preferred_name: Name | None = None
    name_pronunciation: Pronunciation | None = None
    personality: Personality | None = None
    time_zone: TimeZone | None = None

    @model_validator(mode="after")
    def _check(self):
        if not self.model_fields_set:
            raise ValueError("send at least one field to change")
        for required in ("preferred_name", "personality"):
            if required in self.model_fields_set and getattr(self, required) is None:
                raise ValueError(f"{required} can't be cleared")
        return self


class CaseHandoffRequest(RequestModel):
    relationship: Relationship


class CaseHandoffResponse(ResponseModel):
    language_profile: Literal["family", "professional"]
    notes: list[Note]
    next_step: NextStep


class AccountDeletionInfo(ResponseModel):
    explanation: str
    next_step: NextStep


class AccountDeletionRequest(RequestModel):
    confirm: Literal[True] = Field(description="Must be true. Sent only after the user confirms.")


class AccountDeletionResponse(ResponseModel):
    notes: list[Note]


class PolicyVersions(ResponseModel):
    terms_version: str
    privacy_version: str
    acknowledgments: dict[ConsentType, str] = Field(description="Current document_version for each acknowledgment.")


# ------------------------------------------------------------------ case and deceased (UC-5 to UC-8)

class DeceasedIdentityIn(RequestModel):
    legal_first_name: Name
    legal_middle_name: OptionalText100 | None = None
    legal_last_name: Name
    date_of_birth: PastDate | None = None
    domicile_state: StateCode | None = Field(default=None, description="State of legal residence.")


class DeceasedIdentityPatch(RequestModel):
    """Edits after the first save. Omitted fields are left unchanged. Null clears an optional field."""
    legal_first_name: Name | None = None
    legal_middle_name: OptionalText100 | None = None
    legal_last_name: Name | None = None
    date_of_birth: PastDate | None = None
    domicile_state: StateCode | None = None

    @model_validator(mode="after")
    def _check(self):
        if not self.model_fields_set:
            raise ValueError("send at least one field to change")
        for required in ("legal_first_name", "legal_last_name"):
            if required in self.model_fields_set and getattr(self, required) is None:
                raise ValueError(f"{required} can't be cleared")
        return self


class DeathEventIn(RequestModel):
    date_of_death: PastDate
    death_state: StateCode = Field(description="State where the death occurred. This decides which vital "
                                               "records office issues the certificate. It may differ from "
                                               "the state of residence.")
    place_type: PlaceType | None = None
    county: OptionalText100 | None = None
    city: OptionalText100 | None = None
    facility_name: OptionalText200 | None = None


class EstateFlagsIn(RequestModel):
    """Answer one question at a time. Omitted fields are unchanged. 'skip' saves 'unknown'."""
    veteran_status: TriStateAnswer | None = None
    has_will: TriStateAnswer | None = None

    @model_validator(mode="after")
    def _at_least_one(self):
        if self.veteran_status is None and self.has_will is None:
            raise ValueError("send veteran_status, has_will, or both")
        return self


class CreateCaseRequest(RequestModel):
    """Creates the case, the first owner membership, and the deceased record in one transaction.

    death_event and estate_flags are optional so a fiduciary with complete
    records can send everything in one session (UC-8). Each section is still
    validated and audited on its own.
    """
    relationship: Relationship
    deceased: DeceasedIdentityIn
    death_event: DeathEventIn | None = None
    estate_flags: EstateFlagsIn | None = None
    start_journey: bool = Field(
        default=False,
        description="Generate the journey in the same request if the minimum fields are present.",
    )

    @model_validator(mode="after")
    def _dates_in_order(self):
        dob = self.deceased.date_of_birth
        if dob and self.death_event and self.death_event.date_of_death < dob:
            raise ValueError("the date of death is earlier than the date of birth")
        return self


class CaseOut(ResponseModel):
    id: UUID
    status: Literal["active", "paused", "closed"]
    relationship: Relationship | None
    journey_started_on: date
    tasks_paused_until: datetime | None
    created_at: datetime


class DeceasedOut(ResponseModel):
    id: UUID
    legal_first_name: str
    legal_middle_name: str | None
    legal_last_name: str
    date_of_birth: date | None
    domicile_state: str | None
    veteran_status: TriState
    has_will: TriState
    date_of_death: date | None
    place_type: PlaceType | None
    death_state: str | None
    county: str | None
    city: str | None
    facility_name: str | None


class IntakeStatus(ResponseModel):
    ready_for_journey: bool = Field(description="True when the minimum fields for the journey are present.")
    missing_required: list[str]
    unanswered_optional: list[str]


class CaseResponse(ResponseModel):
    case: CaseOut
    deceased: DeceasedOut
    intake: IntakeStatus
    journey_task_count: int
    notes: list[Note]
    next_step: NextStep


# ------------------------------------------------------------------ journey and tasks (UC-9 to UC-13)

class CitationOut(ResponseModel):
    authority_name: str
    url: str
    jurisdiction: str
    last_verified_on: date | None


class TaskSummary(ResponseModel):
    id: UUID
    task_key: str
    template_version: int
    title: str
    plain_summary: str
    journey_week: int
    sort_order: int
    category: TaskCategory
    kind: TaskKind
    status: TaskStatus
    due_on: date | None
    snoozed_until: datetime | None
    completed_at: datetime | None
    attorney_referral: bool


class CertificateOrderRecord(ResponseModel):
    copies_requested: int
    ordered_on: date
    expected_by: date | None = None
    issuing_office: str | None = None


class InstitutionNotice(ResponseModel):
    id: UUID
    institution_name: str
    institution_type: str
    notified_on: date
    method: str | None = None
    status: Literal["notified"]


class TaskDetail(TaskSummary):
    attorney_referral_note: str | None
    content_reviewed_by_counsel: bool = Field(
        description="False while the guidance is an unreviewed draft. Clients should label it as such.")
    jurisdiction: str
    death_state: str | None
    citations: list[CitationOut]
    certificate_order: CertificateOrderRecord | None = None
    institution_notices: list[InstitutionNotice] | None = None


class WeekOut(ResponseModel):
    week: int
    total: int
    done: int
    tasks: list[TaskSummary]


class CheckIn(ResponseModel):
    message: str
    options: list[Option]


class JourneyResponse(ResponseModel):
    case_id: UUID
    mode: Literal["tasks", "paused", "not_started"]
    journey_started_on: date
    current_week: int
    paused_until: datetime | None = None
    check_in: CheckIn | None = None
    next_action: TaskSummary | None = None
    weeks: list[WeekOut]
    notes: list[Note]
    next_step: NextStep


class TaskResponse(ResponseModel):
    task: TaskDetail
    next_action: TaskSummary | None
    notes: list[Note]


class TaskUpdateRequest(RequestModel):
    status: TaskStatus | None = None
    snoozed_until: datetime | None = None

    @model_validator(mode="after")
    def _at_least_one(self):
        if not self.model_fields_set:
            raise ValueError("send status, snoozed_until, or both")
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status can't be null")
        return self


class CertificateOrderRequest(RequestModel):
    copies_requested: Annotated[int, Field(ge=1, le=50)]
    ordered_on: PastDate = Field(default_factory=date.today)
    expected_by: date | None = None
    issuing_office: OptionalText200 | None = None

    @model_validator(mode="after")
    def _order(self):
        if self.expected_by and self.expected_by < self.ordered_on:
            raise ValueError("expected_by can't be before ordered_on")
        return self


class InstitutionType(str, Enum):
    bank = "bank"
    credit_union = "credit_union"
    brokerage = "brokerage"
    insurer = "insurer"
    credit_bureau = "credit_bureau"
    other = "other"


class InstitutionNoticeRequest(RequestModel):
    """Record that an institution was told. Do not send account numbers. There is no field for them."""
    institution_name: OptionalText200
    institution_type: InstitutionType = InstitutionType.bank
    notified_on: PastDate = Field(default_factory=date.today)
    method: Literal["phone", "in_person", "mail", "online"] | None = None
    complete_task: bool = Field(default=True, description="Mark the task done after recording this notice.")


class PauseRequest(RequestModel):
    """No reason field, on purpose. Cairn does not store why someone stepped back."""
    pause_days: Annotated[int, Field(ge=1, le=90)] = 30


class CategoryStatus(ResponseModel):
    category: TaskCategory
    label: str
    done: list[TaskSummary]
    in_progress: list[TaskSummary]
    up_next: list[TaskSummary]
    set_aside: list[TaskSummary] = Field(description="Skipped or not applicable.")


class StatusCounts(ResponseModel):
    total: int
    done: int
    in_progress: int
    not_started: int
    set_aside: int
    overdue: int


class CaseStatusResponse(ResponseModel):
    case_id: UUID
    as_of: datetime
    deceased_name: str
    journey_paused: bool
    paused_until: datetime | None
    counts: StatusCounts
    next_action: TaskSummary | None
    categories: list[CategoryStatus]
    certificate_order: CertificateOrderRecord | None
    institution_notices: list[InstitutionNotice]
