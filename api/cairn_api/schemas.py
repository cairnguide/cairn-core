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
    """not_started is the spec's todo."""
    not_started = "not_started"
    check_on_this = "check_on_this"
    in_progress = "in_progress"
    done = "done"
    not_today = "not_today"
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
    source_urls: list[str] = Field(default_factory=list, description="Citations. Render next to the text.")
    attorney_line: str | None = Field(default=None, description="Shown with anything flagged for an attorney.")


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


class Voice(str, Enum):
    """How Cairn talks with the user (UC-REG-12). Tone only. Defined in voices/manifest.yaml."""
    steady_direct = "steady_direct"
    warm_patient = "warm_patient"
    brisk_businesslike = "brisk_businesslike"
    plain_practical = "plain_practical"


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
    tagline: str | None = None
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
    voice: Voice = Field(description="The voice chosen in onboarding or Settings. Tone only.")
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


class VoiceChoiceIn(RequestModel):
    choice: Voice | Literal["choose_for_me"] = Field(
        description="A voice from the personality screen. choose_for_me selects the default voice, steady_direct.")


class AccountPatch(RequestModel):
    """Settings. The voice can be changed at any time. Omitted fields are unchanged."""
    preferred_name: Name | None = None
    name_pronunciation: Pronunciation | None = None
    voice: Voice | None = None
    time_zone: TimeZone | None = None

    @model_validator(mode="after")
    def _check(self):
        if not self.model_fields_set:
            raise ValueError("send at least one field to change")
        for required in ("preferred_name", "voice"):
            if required in self.model_fields_set and getattr(self, required) is None:
                raise ValueError(f"{required} can't be cleared")
        return self


class CaseHandoffRequest(RequestModel):
    relationship: Relationship


class CaseHandoffResponse(ResponseModel):
    language_profile: Literal["family", "professional"]
    user_role: str = Field(description="The same answer as a case creation user_role. Send it to POST /v1/cases "
                                       "so the question is not asked again.")
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


# ------------------------------------------------------------------ case creation (UC-CASE-01 to UC-CASE-18)
#
# Only the spec's data_fields are collected at case creation. Legal names, dates
# of birth, SSNs, account numbers, and medical details never are
# (never_collect_at_case_creation). Free text is redacted as it is parsed
# (RedactedText) and is never stored. Only confirmed field values are.

def _redact(value: str) -> str:
    from .redaction import redacted_str
    return redacted_str(value)


# Free text from the user. Redacted during request parsing, so handlers, the
# database, logs, and any model call only ever see the redacted string.
RedactedText = Annotated[str, Field(min_length=1, max_length=2000), AfterValidator(_no_control_chars),
                         AfterValidator(_redact)]
OwnWords = Annotated[str, Field(min_length=1, max_length=120), AfterValidator(_no_control_chars),
                     AfterValidator(_redact)]
DisplayName = Annotated[str, Field(min_length=1, max_length=60), AfterValidator(_no_control_chars),
                        AfterValidator(_redact)]


class FieldKey(str, Enum):
    """The spec's data_fields, in the order Cairn suggests them. There is no required order."""
    user_role = "user_role"
    display_name = "display_name"
    date_of_death = "date_of_death"
    place_of_death = "place_of_death"
    residence_state = "residence_state"
    circumstance = "circumstance"
    veteran_status = "veteran_status"
    estate_plan_status = "estate_plan_status"
    completed_items = "completed_items"


class AnswerState(str, Enum):
    answered = "answered"
    skipped = "skipped"
    unsure = "unsure"


class UserRole(str, Enum):
    spouse_partner = "spouse_partner"
    child = "child"
    other_family = "other_family"
    named_executor = "named_executor"
    power_of_attorney = "power_of_attorney"
    professional_fiduciary = "professional_fiduciary"
    friend = "friend"
    other = "other"
    prefer_not_to_say = "prefer_not_to_say"


class DatePrecision(str, Enum):
    exact = "exact"
    today = "today"
    this_week = "this_week"
    unknown = "unknown"


class ResidenceChoice(str, Enum):
    same_as_place_of_death = "same_as_place_of_death"
    different = "different"
    unknown = "unknown"


class Circumstance(str, Enum):
    expected_illness_or_hospice = "expected_illness_or_hospice"
    sudden_natural = "sudden_natural"
    accident_or_unexpected = "accident_or_unexpected"
    under_investigation = "under_investigation"
    prefer_not_to_say = "prefer_not_to_say"


class EstatePlanStatus(str, Enum):
    yes_location_known = "yes_location_known"
    yes_location_unknown = "yes_location_unknown"
    no = "no"
    unknown = "unknown"


class CompletedItem(str, Enum):
    death_pronounced = "death_pronounced"
    funeral_provider_chosen = "funeral_provider_chosen"
    funeral_home_has_ssn = "funeral_home_has_ssn"
    certificates_ordered = "certificates_ordered"
    ssa_notified = "ssa_notified"
    bank_notified = "bank_notified"
    other = "other"
    none_or_unsure = "none_or_unsure"


# DateOfDeath has a field named date, which would shadow the type inside the class.
_Date = date


class DateOfDeath(RequestModel):
    precision: DatePrecision
    date: _Date | None = Field(default=None, description="Required for exact. For today the server fills in "
                                                        "today's date in the user's time zone. Otherwise null.")

    @model_validator(mode="after")
    def _check(self):
        if self.precision == DatePrecision.exact:
            if self.date is None:
                raise ValueError("send the date for an exact date of death")
            if self.date > _Date.today():
                raise ValueError("the date of death can't be in the future")
        elif self.precision != DatePrecision.today and self.date is not None:
            raise ValueError("send a date only for exact or today")
        return self


class PlaceOfDeath(RequestModel):
    state: StateCode | None = Field(default=None, description="Where the death happened. This decides the "
                                                             "death certificate office, never residence_state.")
    county_or_city: Annotated[str, Field(min_length=1, max_length=100), AfterValidator(_no_control_chars),
                              AfterValidator(_redact)] | None = None
    outside_us: bool = False

    @model_validator(mode="after")
    def _check(self):
        if self.outside_us and self.state is not None:
            raise ValueError("a death outside the United States has no state")
        return self


class ResidenceState(RequestModel):
    choice: ResidenceChoice
    state: StateCode | None = Field(default=None, description="Only with choice different.")

    @model_validator(mode="after")
    def _check(self):
        if self.state is not None and self.choice != ResidenceChoice.different:
            raise ValueError("send a state only when they lived in a different state")
        return self


def _completed(items: list[CompletedItem]) -> list[CompletedItem]:
    if len(set(items)) != len(items):
        raise ValueError("each item can be sent once")
    if CompletedItem.none_or_unsure in items and len(items) > 1:
        raise ValueError("none_or_unsure can't be combined with other items")
    return items


CompletedItems = Annotated[list[CompletedItem], Field(min_length=1, max_length=8), AfterValidator(_completed)]

# The value type for each field. The API checks the value against this, and the
# database checks it again (cairn.intake_value_valid).
FIELD_VALUE_TYPES: dict[FieldKey, object] = {
    FieldKey.user_role: UserRole,
    FieldKey.display_name: DisplayName,
    FieldKey.date_of_death: DateOfDeath,
    FieldKey.place_of_death: PlaceOfDeath,
    FieldKey.residence_state: ResidenceState,
    FieldKey.circumstance: Circumstance,
    FieldKey.veteran_status: TriState,
    FieldKey.estate_plan_status: EstatePlanStatus,
    FieldKey.completed_items: CompletedItems,
}


class SafetyMode(str, Enum):
    normal = "normal"
    overwhelm = "overwhelm"
    acute_distress = "acute_distress"
    risk_of_harm = "risk_of_harm"


class IntakeSession(RequestModel):
    """Per-session state the client keeps and sends back with each turn (UC-CASE-14).

    The server never stores it. Decision 7 in database/CLAUDE.md: no inference
    about the user's emotional state is persisted. Send back the session from
    the last response. Omit it at the start of a new session.
    """
    safety_mode: SafetyMode = SafetyMode.normal
    sensitivity: Literal["normal", "raised"] = "normal"
    consecutive_skips: Annotated[int, Field(ge=0, le=100)] = 0
    offered: list[Literal["loss_survivor_resources"]] = Field(default_factory=list, max_length=4)
    ask_residence: bool = Field(default=False, description="The user said the death happened away from home, "
                                                           "so residence_state is asked (UC-CASE-04).")


class StartCaseRequest(RequestModel):
    user_role: UserRole | None = Field(
        default=None,
        description="The answer from the onboarding hand-off (CaseHandoffResponse.user_role), so the "
                    "question is not asked twice. Omit to ask it during intake.")


class AnswerIn(RequestModel):
    """Answer one question by button or form. Every question accepts skipped and unsure."""
    state: AnswerState
    value: object | None = Field(default=None, description="Required when state is answered. Its shape "
                                                           "depends on the field. See FIELD_VALUE_TYPES.")
    own_words: OwnWords | None = Field(default=None, description="The user's words for this answer, shown on "
                                                                "the review screen. Never for circumstance.")
    away_from_home: bool = Field(default=False, description="place_of_death only. The death happened away "
                                                            "from home, so residence_state is asked next.")
    session: IntakeSession | None = None

    @model_validator(mode="after")
    def _check(self):
        if (self.state == AnswerState.answered) != (self.value is not None):
            raise ValueError("send a value only with state answered")
        if self.state != AnswerState.answered and self.own_words is not None:
            raise ValueError("own_words go with an answer")
        return self


class IntakeMessageIn(RequestModel):
    """Free text from the user (UC-CASE-01 own words, and any chat turn). Never stored."""
    text: RedactedText
    session: IntakeSession | None = None


class ProposedAnswerIn(RequestModel):
    field: FieldKey
    value: object
    own_words: OwnWords | None = None


class ConfirmationIn(RequestModel):
    """The proposals the user confirmed ("Did I get that right?"). Only data_fields keys are accepted."""
    answers: Annotated[list[ProposedAnswerIn], Field(min_length=1, max_length=9)]
    session: IntakeSession | None = None


class SessionIn(RequestModel):
    session: IntakeSession | None = None


class IntakePreferencesIn(RequestModel):
    skip_explainers: bool | None = Field(default=None, description="UC-CASE-02. Shorter pace, no explainers.")
    name_fallback: Literal["your_loved_one", "the_person_who_died"] | None = Field(
        default=None, description="UC-CASE-03. What to say when no display name was given.")
    session: IntakeSession | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.skip_explainers is None and self.name_fallback is None:
            raise ValueError("send skip_explainers, name_fallback, or both")
        return self


class DeathNotYetIn(RequestModel):
    """UC-CASE-17. not_yet records that the person hasn't died. has_happened clears it."""
    choice: Literal["not_yet", "save_draft", "come_back_later", "has_happened"]
    session: IntakeSession | None = None


class AttorneyTrigger(str, Enum):
    contested_will = "contested_will"
    family_disagreement = "family_disagreement"
    unsure_of_authority = "unsure_of_authority"
    multi_state_property = "multi_state_property"


class AttorneyReferralIn(RequestModel):
    """UC-CASE-16. Something Cairn should not guide alone. The journey is never blocked by it."""
    trigger: AttorneyTrigger
    session: IntakeSession | None = None


class StartJourneyIn(RequestModel):
    pre_button_notice_version: Annotated[str, Field(min_length=1, max_length=64)] = Field(
        description="From JourneyPreviewResponse.pre_button_notice.version. Proves the notice was shown "
                    "before Start journey was enabled.")


class FirstTaskIn(RequestModel):
    """UC-CASE-13. A task id, small_task, or not_today. Cairn never starts a task the user didn't choose."""
    choice: UUID | Literal["small_task", "not_today"]


class ReadAloud(ResponseModel):
    """Every case creation screen offers Read this to me."""
    label: str
    text: str


class SupportResource(ResponseModel):
    id: str
    text: str
    url: str


class AnswerOption(ResponseModel):
    value: str
    label: str


class Question(ResponseModel):
    field: FieldKey
    pre_question: str | None = Field(default=None, description="Why Cairn asks. Shown before the question.")
    prompt: str
    input: Literal["choice", "multi_choice", "text", "date_of_death", "place_of_death", "residence_state"]
    options: list[AnswerOption] = Field(description="Answer choices. Large tap targets.")
    skip: AnswerOption = Field(description="Skip for now. On every question.")
    not_sure: AnswerOption = Field(description="I'm not sure. On every question.")
    free_text_allowed: bool = True
    free_text_label: str


class ProposedAnswer(ResponseModel):
    field: FieldKey
    value: object
    label: str = Field(description="How the value reads back to the user.")
    own_words: str | None = None


class CaseOut(ResponseModel):
    id: UUID
    status: Literal["draft", "active", "read_only", "paused", "closed"] = Field(
        description="read_only when the case is active and the account's free period has ended.")
    display_name: str = Field(description="What to call the person who died. Not a legal name.")
    journey_template_key: str | None
    journey_template_version: int | None
    journey_started_at: datetime | None
    journey_started_on: date | None
    last_intake_step: FieldKey | None
    last_activity_at: datetime
    draft_expires_at: datetime | None = Field(description="Drafts only. Any activity moves it out again.")
    death_not_yet_occurred: bool
    skip_explainers: bool
    tasks_paused_until: datetime | None
    created_at: datetime


class IntakeTurnResponse(ResponseModel):
    """One conversational turn. Acknowledgment first, at most one question, exactly one next action."""
    case: CaseOut
    voice: Literal["steady_direct", "warm_patient", "brisk_businesslike", "plain_practical", "steady_care"]
    safety_mode: SafetyMode
    acknowledgment: str | None = None
    body: list[str] = Field(default_factory=list, description="Statements, never questions.")
    notes: list[Note] = Field(default_factory=list)
    question: Question | None = None
    proposals: list[ProposedAnswer] | None = Field(default=None, description="Free-text readback. Nothing is "
                                                                             "saved until confirmed.")
    support: list[SupportResource] = Field(default_factory=list)
    redactions: list[str] = Field(default_factory=list, description="Kinds of values removed, never the values.")
    masked_text: str | None = Field(default=None, description="The user's message with sensitive numbers "
                                                              "removed. Replace the local copy with this.")
    next_step: NextStep
    session: IntakeSession
    read_aloud: ReadAloud


class CaseListItem(ResponseModel):
    id: UUID
    status: str
    display_name: str
    last_activity_at: datetime
    draft_expires_at: datetime | None


class CaseListResponse(ResponseModel):
    cases: list[CaseListItem]


class ReviewLine(ResponseModel):
    field: FieldKey
    label: str
    answer: str = Field(description="The user's own words where free text was given, except circumstance.")
    answer_state: AnswerState | None
    edit: NextStep


class ReviewResponse(ResponseModel):
    case: CaseOut
    acknowledgment: str
    told_me: list[ReviewLine] = Field(description="What you told me")
    told_me_heading: str
    later: list[ReviewLine] = Field(description="What we can figure out later")
    later_heading: str
    next_step: NextStep
    read_aloud: ReadAloud


class DeceasedIdentityPatch(RequestModel):
    """Just-in-time identity, inside a task that needs it, once the journey has started.

    Never part of case creation. Omitted fields are left unchanged. Null clears a field.
    """
    legal_first_name: Name | None = None
    legal_middle_name: OptionalText100 | None = None
    legal_last_name: Name | None = None
    date_of_birth: PastDate | None = None

    @model_validator(mode="after")
    def _check(self):
        if not self.model_fields_set:
            raise ValueError("send at least one field to change")
        return self


class DeceasedOut(ResponseModel):
    id: UUID
    legal_first_name: str | None
    legal_middle_name: str | None
    legal_last_name: str | None
    date_of_birth: date | None


class CaseResponse(ResponseModel):
    """Opening a case. For a draft this is the resume turn (UC-CASE-10) and counts as activity."""
    case: CaseOut
    deceased: DeceasedOut | None = Field(description="Only once a task has collected it.")
    acknowledgment: str | None = None
    body: list[str] = Field(default_factory=list)
    notes: list[Note]
    next_step: NextStep
    read_aloud: ReadAloud


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
    attorney_line: str | None = None
    why_now: str | None = None
    waypoint: str | None = None
    notes: list[Note] = Field(default_factory=list, description="Journey notes attached to this task.")


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
    journey_started_on: date | None
    current_week: int
    paused_until: datetime | None = None
    check_in: CheckIn | None = None
    next_action: TaskSummary | None = None
    weeks: list[WeekOut]
    notes: list[Note]
    support: list[SupportResource] = Field(default_factory=list)
    next_step: NextStep


class PreviewTask(ResponseModel):
    task_key: str
    title: str
    plain_summary: str
    journey_week: int
    waypoint: str | None
    status: TaskStatus = Field(description="done for completed_items, check_on_this when unsure, else not_started.")
    status_label: str = Field(description="Done, Check on this, or When you're ready. Text, never color alone.")
    attorney_referral: bool
    attorney_line: str | None
    citations: list[CitationOut]
    notes: list[Note]


class PreviewWeek(ResponseModel):
    week: int
    label: str
    tasks: list[PreviewTask]


class PreButtonNotice(ResponseModel):
    text: str
    version: str = Field(description="Send back with Start journey.")


class JourneyPreviewResponse(ResponseModel):
    """UC-CASE-12. The journey that fits, before it starts. Nothing here starts the free period."""
    case: CaseOut
    journey_template_key: str
    journey_template_version: int
    explanation: str = Field(description="One or two sentences on why this journey fits.")
    weeks: list[PreviewWeek]
    notes: list[Note]
    support: list[SupportResource]
    pre_button_notice: PreButtonNotice | None = Field(description="Shown above the buttons. None when the "
                                                                  "journey can't start (the death hasn't happened).")
    start_available: bool
    next_step: NextStep = Field(description="Start journey and Not yet, or why it can't start.")
    read_aloud: ReadAloud


class FirstTaskChoice(ResponseModel):
    task: TaskSummary
    why_now: str | None


class StartJourneyResponse(ResponseModel):
    case: CaseOut
    confirmation: str = Field(description="With the trial end date in the user's local time zone.")
    trial_started_now: bool
    trial_end_date: date
    recommended: list[FirstTaskChoice] = Field(description="The one or two most time-sensitive tasks.")
    small_task: str
    next_step: NextStep = Field(description="What feels doable right now? Recommended, small task, Not today.")
    read_aloud: ReadAloud


class FirstTaskResponse(ResponseModel):
    case: CaseOut
    acknowledgment: str | None
    task: TaskSummary | None
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
    deceased_name: str = Field(description="The display name from intake, or its fallback. Not a legal name.")
    journey_paused: bool
    paused_until: datetime | None
    counts: StatusCounts
    next_action: TaskSummary | None
    categories: list[CategoryStatus]
    certificate_order: CertificateOrderRecord | None
    institution_notices: list[InstitutionNotice]
