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
    """The stored status. The card 50 status is TaskSummary.journey_status: open is not_started, in_progress,
    check_on_this, or not_today. not_needed is skipped or not_applicable."""
    not_started = "not_started"
    check_on_this = "check_on_this"
    in_progress = "in_progress"
    done = "done"
    not_today = "not_today"
    skipped = "skipped"
    not_applicable = "not_applicable"
    handled_elsewhere = "handled_elsewhere"


def journey_status(status: str) -> str:
    """The card 50 task status for a stored one."""
    if status in ("done", "handled_elsewhere"):
        return status
    if status in ("skipped", "not_applicable"):
        return "not_needed"
    return "open"


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
    """UC-REG-15. What will be deleted, where the confirmation goes, and one button. No reason is asked."""
    explanation: str = Field(description="Account, every case, every task, all conversation text, and "
                                         "notification settings.")
    subscription_note: Note | None = Field(description="Only when there is a subscription. Information, not a "
                                                       "question: store subscriptions are cancelled in the store.")
    masked_email: str = Field(description="The account email, masked. The one confirmation goes here.")
    confirmation_destination: str
    next_step: NextStep = Field(description="One option: Delete my account and everything in it.")


class AccountDeletionRequest(RequestModel):
    confirm: Literal[True] = Field(description="Must be true. Sent only after the user taps the one button.")


class AccountDeletionResponse(ResponseModel):
    notes: list[Note]
    signed_out: Literal[True] = Field(description="The account is gone. Clear the session and return the app "
                                                  "to its signed-out state.")
    next_step: NextStep


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
    residence_jurisdiction = "residence_jurisdiction"
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
    home_pets_vehicles_secured = "home_pets_vehicles_secured"
    funeral_provider_chosen = "funeral_provider_chosen"
    funeral_home_has_ssn = "funeral_home_has_ssn"
    certificates_ordered = "certificates_ordered"
    ssa_notified = "ssa_notified"
    bank_insurer_or_employer_notified = "bank_insurer_or_employer_notified"
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
    jurisdiction: StateCode | None = Field(
        default=None, description="The state or territory where the death happened: any of the 50 states, DC, PR, "
                                  "GU, VI, AS, or MP. This decides the death certificate office, never "
                                  "residence_jurisdiction (DEC-05).")
    county_or_city: Annotated[str, Field(min_length=1, max_length=100), AfterValidator(_no_control_chars),
                              AfterValidator(_redact)] | None = None
    outside_us: bool = False

    @model_validator(mode="after")
    def _check(self):
        if self.outside_us and self.jurisdiction is not None:
            raise ValueError("a death outside the United States has no state or territory")
        return self


class ResidenceJurisdiction(RequestModel):
    choice: ResidenceChoice
    jurisdiction: StateCode | None = Field(default=None, description="Only with choice different. The DMV step "
                                                                     "and later estate steps follow it (DEC-05).")

    @model_validator(mode="after")
    def _check(self):
        if self.jurisdiction is not None and self.choice != ResidenceChoice.different:
            raise ValueError("send a state or territory only when they lived somewhere else")
        return self


HandledBy = Annotated[str, Field(min_length=1, max_length=60), AfterValidator(_no_control_chars),
                      AfterValidator(_redact)]


class HandledElsewhere(RequestModel):
    """UC-CASE-08. Someone else is handling this item. The name is optional, never required to continue."""
    item: CompletedItem
    handled_by: HandledBy | None = Field(default=None, description="Optional. Third-party personal data.")


def _item(entry) -> CompletedItem:
    return entry.item if isinstance(entry, HandledElsewhere) else entry


def _completed(items: list) -> list:
    keys = [_item(e) for e in items]
    if len(set(keys)) != len(keys):
        raise ValueError("each item can be sent once")
    if CompletedItem.none_or_unsure in keys and len(keys) > 1:
        raise ValueError("none_or_unsure can't be combined with other items")
    if any(isinstance(e, HandledElsewhere) and e.item == CompletedItem.none_or_unsure for e in items):
        raise ValueError("none_or_unsure can't be handled by someone else")
    return items


CompletedItems = Annotated[list[CompletedItem | HandledElsewhere], Field(min_length=1, max_length=9),
                           AfterValidator(_completed)]

# The value type for each field. The API checks the value against this, and the
# database checks it again (cairn.intake_value_valid).
FIELD_VALUE_TYPES: dict[FieldKey, object] = {
    FieldKey.user_role: UserRole,
    FieldKey.display_name: DisplayName,
    FieldKey.date_of_death: DateOfDeath,
    FieldKey.place_of_death: PlaceOfDeath,
    FieldKey.residence_jurisdiction: ResidenceJurisdiction,
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
    overwhelm_signals: Annotated[int, Field(ge=0, le=100)] = Field(
        default=0, description="Overwhelm signals in this conversation. The second one starts level 2 (DEC-26-03).")
    offered: list[Literal["loss_survivor_resources"]] = Field(default_factory=list, max_length=4)
    ask_residence: bool = Field(default=False, description="The user said the death happened away from home, "
                                                           "so residence_jurisdiction is asked (UC-CASE-04).")
    started_at: datetime | None = Field(default=None, description="When this session began. Set by the server.")
    last_turn_at: datetime | None = Field(default=None, description="The last turn. Set by the server.")
    active_seconds: Annotated[int, Field(ge=0, le=86400)] = Field(
        default=0, description="Active use in this session, for the 45-minute rest offer (UC-CASE-23).")
    rest_offered: list[Literal["time", "heavy"]] = Field(
        default_factory=list, max_length=2, description="Rest offers already made. Once per session per trigger.")
    check_in_asked: bool = Field(default=False, description="The check-in question is asked once (DEC-26-04).")
    intake_stopped: bool = Field(default=False, description="The user said they are under 18. No more intake "
                                                            "questions this session (UC-CASE-24). Never stored.")


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
                                                            "from home, so residence_jurisdiction is asked next.")
    session: IntakeSession | None = None

    @model_validator(mode="after")
    def _check(self):
        if (self.state == AnswerState.answered) != (self.value is not None):
            raise ValueError("send a value only with state answered")
        if self.state != AnswerState.answered and self.own_words is not None:
            raise ValueError("own_words go with an answer")
        return self


class IntakeMessageIn(RequestModel):
    """Free text from the user (UC-CASE-01 own words, and any chat turn), typed or a confirmed speech transcript
    (UC-CASE-22). Never stored."""
    text: RedactedText
    input_mode: Literal["typed", "speech"] = "typed"
    session: IntakeSession | None = None


class TranscriptIn(RequestModel):
    """UC-CASE-22. What the speech to text step heard. The audio never reaches Cairn's servers, and is never
    stored anywhere. The transcript is redacted, spoken digits included, before anything else sees it."""
    transcript: RedactedText | None = None
    unclear: bool = Field(default=False, description="The speech to text step wasn't sure what it heard.")
    session: IntakeSession | None = None


class CheckInIn(RequestModel):
    """DEC-26-04. The answer to "Would it be okay if I checked in with you tomorrow?", asked once."""
    answer: Literal["yes", "no"]
    session: IntakeSession | None = None


class Level2ChoiceIn(RequestModel):
    """UC-CASE-14 level 2: rest now, one small thing, or just talk."""
    choice: Literal["rest", "small_thing", "talk"]
    session: IntakeSession | None = None


RestChoice = Literal["today", "three_days", "week", "until_back"]


class TakeABreakIn(RequestModel):
    """Take a break (global rule). In a draft it saves and pauses. On a journey, send a rest_choice."""
    rest_choice: RestChoice | None = None
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
    early_property_disposal = "early_property_disposal"


class AttorneyReferralIn(RequestModel):
    """UC-CASE-16. Something Cairn should not guide alone. The journey is never blocked by it."""
    trigger: AttorneyTrigger
    session: IntakeSession | None = None


class StartJourneyIn(RequestModel):
    pre_button_notice_version: Annotated[str, Field(min_length=1, max_length=64)] = Field(
        description="From JourneyPreviewResponse.pre_button_notice.version. Proves the notice was shown "
                    "before Start journey was enabled.")
    session: IntakeSession | None = Field(default=None, description="At care levels 3 and 4 the journey "
                                                                    "doesn't start: setup waits for level 1.")


class FirstTaskIn(RequestModel):
    """UC-CASE-13. A task id, small_task, or not_today. Cairn never starts a task the user didn't choose."""
    choice: UUID | Literal["small_task", "not_today"]


class ReadAloud(ResponseModel):
    """Every case creation screen offers Read this to me."""
    label: str
    text: str


class ScreenControls(ResponseModel):
    """The controls on every case creation screen, with their screen reader labels (WCAG 2.2 AA)."""
    take_a_break: str = Field(description="One select, no confirmation.")
    read_this_to_me: str
    speak: str = Field(description="The speak button. Speech is never required (UC-CASE-22).")
    speak_first_use: str = Field(description="Shown the first time the speak button is used.")
    speak_permission_denied: str
    free_text: str


class Announcement(ResponseModel):
    """Something announced to screen readers (aria-live polite)."""
    kind: Literal["ai_reminder", "rest_offer", "check_in"]
    text: str
    options: list[Option] = Field(default_factory=list)


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
    input: Literal["choice", "multi_choice", "text", "date_of_death", "place_of_death", "residence_jurisdiction"]
    options: list[AnswerOption] = Field(description="Answer choices. Large tap targets.")
    skip: AnswerOption = Field(description="Skip for now. On every question.")
    not_sure: AnswerOption = Field(description="I'm not sure. On every question.")
    free_text_allowed: bool = True
    free_text_label: str
    speech_allowed: bool = Field(default=True, description="Every question accepts speech (UC-CASE-22).")
    picker_label: str | None = Field(default=None, description="'State or territory' for place questions.")
    jurisdictions: list[AnswerOption] = Field(
        default_factory=list, description="The 50 states, DC, and the 5 territories, by full name.")
    handled_elsewhere_label: str | None = Field(
        default=None, description="completed_items only: 'Someone else is handling this', for any item.")


class ProposedAnswer(ResponseModel):
    field: FieldKey
    value: object
    label: str = Field(description="How the value reads back to the user.")
    own_words: str | None = None


class CaseOut(ResponseModel):
    id: UUID
    status: Literal["draft", "active", "paused", "closed", "completed", "closed_open_steps",
                    "pending_deletion"] = Field(
        description="Card 50 statuses. pending_deletion while a 7-day hold is running. Read-only is an account "
                    "state, never a case status (see account_access).")
    account_access: Literal["full", "read_only"] = Field(description="The account's access. Read-only after the "
                                                                     "free days end without a subscription.")
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
    deletion_scheduled_for: datetime | None = Field(
        default=None, description="Set when the user chose to delete this case with a 7-day hold (UC-END-13).")
    delete_after: datetime | None = Field(default=None, description="Card 50 name for deletion_scheduled_for.")
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
    care_level: Literal[1, 2, 3, 4] = Field(description="Crisis plan levels: 1 steady, 2 heavy, 3 hurting, 4 unsafe.")
    announcements: list[Announcement] = Field(default_factory=list)
    controls: ScreenControls
    read_aloud: ReadAloud


class CaseListItem(ResponseModel):
    id: UUID
    status: str
    display_name: str
    last_activity_at: datetime
    draft_expires_at: datetime | None
    draft_notice: str | None = Field(default=None, description="Drafts only. The 28-day notice, on the draft's "
                                                               "card, for anyone who closed the tab (UC-CASE-10).")
    deletion_scheduled_for: datetime | None = None


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
    controls: ScreenControls
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
    announcements: list[Announcement] = Field(default_factory=list)
    next_step: NextStep
    controls: ScreenControls
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
    journey_status: Literal["open", "done", "not_needed", "handled_elsewhere"] = Field(
        default="open", description="The card 50 status.")
    needs_check: bool = Field(default=False, description="Shown as 'Check on this'.")
    probably_not_applicable: bool = Field(default=False, description="Shown last, as 'Probably doesn't apply. "
                                                                     "Select it if it does.'")
    recommended: bool = False
    handled_by: str | None = None
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
    status: TaskStatus = Field(description="done or handled_elsewhere from completed_items, else not_started.")
    journey_status: Literal["open", "done", "not_needed", "handled_elsewhere"]
    needs_check: bool
    probably_not_applicable: bool
    recommended: bool
    status_label: str = Field(description="Done, Someone else is handling this, Check on this, When you're ready, "
                                          "or Probably doesn't apply. Text, never color alone.")
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
    care_level: Literal[1, 2, 3, 4] = 1
    controls: ScreenControls
    notifications_chosen: bool = Field(
        description="UC-CASE-19. False until the user makes, or skips, a notification choice for this journey. "
                    "While false on a draft, next_step is choose_notifications (UC-CASE-12 step 3).")
    next_step: NextStep = Field(description="Choose notifications, then Start journey and Not yet, or why it "
                                            "can't start.")
    read_aloud: ReadAloud


class FirstTaskChoice(ResponseModel):
    task: TaskSummary
    why_now: str | None


class StartJourneyResponse(ResponseModel):
    case: CaseOut
    confirmation: str = Field(description="With the trial end date in the user's local time zone. No trial "
                                          "wording for a subscribed account.")
    legal_review_required: bool = Field(description="[LEGAL] The price and subscription wording needs counsel "
                                                    "approval before launch.")
    trial_started_now: bool
    trial_end_date: date | None
    recommended: list[FirstTaskChoice] = Field(description="The one or two most time-sensitive tasks.")
    small_task: str
    next_step: NextStep = Field(description="What feels doable right now? Recommended, small task, Not today.")
    controls: ScreenControls
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


# ------------------------------------------------------------------ keeping in touch (UC-CASE-19, UC-CASE-20)
#
# Per journey (one journey per case). No choice, or skipping, means in_app_only:
# nothing is sent outside the app (D-2026-09-25-N2). SMS is not offered: it is an
# open question for the MVP and needs legal review (card 50).

class NotificationChannel(str, Enum):
    email = "email"
    push = "push"
    in_app_only = "in_app_only"


class NotificationReason(str, Enum):
    due_date_upcoming = "due_date_upcoming"
    inactivity = "inactivity"


class NotificationFrequency(str, Enum):
    as_it_happens = "as_it_happens"
    daily_max = "daily_max"
    weekly_max = "weekly_max"


class NotificationChoice(RequestModel):
    """in_app_only stands alone. Any other channel needs at least one reason, and each reason its timing."""
    channels: list[NotificationChannel] = Field(min_length=1, max_length=3)
    reasons: list[NotificationReason] = Field(default_factory=list, max_length=2)
    due_date_lead_days: Literal[1, 3, 7] | None = None
    inactivity_days: Literal[3, 7, 14] | None = None
    frequency: NotificationFrequency = NotificationFrequency.daily_max

    @model_validator(mode="after")
    def _shape(self):
        if len(set(self.channels)) != len(self.channels) or len(set(self.reasons)) != len(self.reasons):
            raise ValueError("choose each channel and reason once")
        if NotificationChannel.in_app_only in self.channels:
            if len(self.channels) > 1:
                raise ValueError("in_app_only can't be combined with another channel")
            if self.reasons or self.due_date_lead_days or self.inactivity_days:
                raise ValueError("in_app_only has no reasons or timing")
            return self
        if not self.reasons:
            raise ValueError("choose at least one reason for Cairn to reach out")
        for reason, days, name in ((NotificationReason.due_date_upcoming, self.due_date_lead_days,
                                    "due_date_lead_days"),
                                   (NotificationReason.inactivity, self.inactivity_days, "inactivity_days")):
            if (reason in self.reasons) != (days is not None):
                raise ValueError(f"{name} goes with the {reason.value} reason, and only with it")
        return self


class NotificationPreset(str, Enum):
    keep_it_simple = "keep_it_simple"
    skip = "skip"
    same_as = "same_as"


class NotificationSetIn(RequestModel):
    """Send a choice, or a shortcut. same_as copies another journey's choice (UC-CASE-18)."""
    choice: NotificationChoice | None = None
    preset: NotificationPreset | None = None
    same_as_case_id: UUID | None = None

    @model_validator(mode="after")
    def _one(self):
        if (self.choice is None) == (self.preset is None):
            raise ValueError("send a choice or a preset, not both")
        if (self.preset == NotificationPreset.same_as) != (self.same_as_case_id is not None):
            raise ValueError("same_as_case_id goes with the same_as preset, and only with it")
        return self


class NotificationPreferencesOut(ResponseModel):
    case_id: UUID
    stored: bool = Field(description="False when nothing was chosen yet. The effective choice is then in_app_only.")
    channels: list[NotificationChannel]
    reasons: list[NotificationReason]
    due_date_lead_days: int | None
    inactivity_days: int | None
    frequency: NotificationFrequency
    push_permission_granted: bool
    readback: str = Field(description="The choice in plain language, one sentence.")
    updated_at: datetime | None


class NotificationQuestion(ResponseModel):
    id: Literal["channels", "reasons", "due_date_lead_days", "inactivity_days", "frequency"]
    prompt: str
    multi_select: bool
    options: list[Option]
    asked_when: str = Field(description="When the client shows this question.")


class NotificationSetupResponse(ResponseModel):
    """UC-CASE-19. The explanation, the shortcuts (same as another journey first), then one question at a time."""
    case_id: UUID
    display_name: str
    explanation: str
    preferences: NotificationPreferencesOut
    masked_email: str = Field(description="Where email goes, shown masked. Works with Apple private email relay.")
    shortcuts: list[Option] = Field(description="Use the same as another journey (when there is one), Keep it "
                                                "simple for me, Choose for myself, Only in the app.")
    questions: list[NotificationQuestion]
    next_step: NextStep
    read_aloud: ReadAloud


class NotificationReadbackResponse(ResponseModel):
    """Read back before saving. Nothing is stored."""
    preferences: NotificationPreferencesOut
    next_step: NextStep


class NotificationSavedResponse(ResponseModel):
    preferences: NotificationPreferencesOut
    acknowledgment: str
    next_step: NextStep = Field(description="request_push_permission when push was chosen and the OS hasn't "
                                            "been asked, so the prompt only ever follows the user's choice.")


class PushPermissionIn(RequestModel):
    granted: bool = Field(description="What the OS permission prompt returned.")


class JourneyNotifications(ResponseModel):
    case_id: UUID
    display_name: str
    status: str
    preferences: NotificationPreferencesOut


class AccountNotificationsResponse(ResponseModel):
    journeys: list[JourneyNotifications]
    next_step: NextStep


class NotificationChangeIn(RequestModel):
    """UC-CASE-20. stop_everything applies in one step. A choice is read back first and applied on confirm."""
    scope: Literal["all"] | list[UUID] | None = Field(
        default=None, description="Which journeys. Omit when there is only one. stop_everything with no scope "
                                  "means all of them.")
    stop_everything: bool = False
    choice: NotificationChoice | None = None
    confirm: bool = Field(default=False, description="true once the user said yes to the readback.")

    @model_validator(mode="after")
    def _what(self):
        if self.stop_everything == (self.choice is not None):
            raise ValueError("send stop_everything or a choice")
        if isinstance(self.scope, list) and not self.scope:
            raise ValueError("scope needs at least one journey")
        return self


class NotificationChangeResponse(ResponseModel):
    applied: bool
    acknowledgment: str | None
    readback: str | None
    journeys: list[JourneyNotifications]
    next_step: NextStep


# ------------------------------------------------------------------ case deletion (UC-END-13, UC-CASE-21)

class CaseDeletionInfo(ResponseModel):
    explanation: str
    masked_email: str
    confirmation_destination: str = Field(description="Where the one confirmation goes, shown before the user "
                                                       "confirms (UC-CASE-21).")
    deletion_scheduled_for: datetime | None
    next_step: NextStep


class CaseDeletionIn(RequestModel):
    mode: Literal["now", "hold"] = Field(description="Delete now, or in 7 days with a chance to keep it.")


class CaseDeletionResponse(ResponseModel):
    deleted: bool
    deletion_scheduled_for: datetime | None
    acknowledgment: str
    next_step: NextStep


# ------------------------------------------------------------------ download my data (UC-REG-16)

class DataExportInfo(ResponseModel):
    explanation: str = Field(description="One sentence on what the download includes.")
    format: Literal["json"]
    next_step: NextStep


class ExportConsent(ResponseModel):
    purpose: str
    policy_version: str
    granted_at: datetime
    auth_provider: str | None
    client: str | None


class ExportReminder(ResponseModel):
    kind: str
    due_at: datetime
    email_sent_at: datetime | None


class ExportAnswer(ResponseModel):
    field: str
    answer_state: str
    value: object | None
    own_words: str | None


class ExportTask(ResponseModel):
    title: str
    plain_summary: str
    journey_week: int
    status: str
    due_on: date | None
    completed_at: datetime | None


class ExportNotificationSent(ResponseModel):
    reason: str
    channel: str
    sent_at: datetime


class ExportCase(ResponseModel):
    id: UUID
    status: str
    display_name: str
    created_at: datetime
    journey_template_key: str | None
    journey_started_at: datetime | None
    tasks_paused_until: datetime | None
    deletion_scheduled_for: datetime | None
    summary: dict = Field(description="Where things stand: counts by status and the next step.")
    answers: list[ExportAnswer]
    person_who_died: dict | None = Field(description="Legal identity, if it was given inside a task. Never the "
                                                     "Social Security number digits.")
    tasks: list[ExportTask]
    notification_preferences: NotificationPreferencesOut
    notifications_sent: list[ExportNotificationSent]
    conversation: list[dict] = Field(description="Conversation text and records kept for this case.")


class DataExport(ResponseModel):
    """UC-REG-16. Everything Cairn holds about the user, in a portable format (GDPR Article 20)."""
    format: Literal["cairn-data-export"]
    format_version: Literal[1]
    generated_at: datetime
    profile: dict
    acknowledgments: list[ExportConsent]
    trial_reminders: list[ExportReminder]
    cases: list[ExportCase]


# ------------------------------------------------------------------ account requests in chat

class AccountChatSession(RequestModel):
    """Kept by the client and sent back, like IntakeSession. The server never stores it (decision 7)."""
    safety_first_shown: bool = Field(default=False, description="The last turn answered a risk-of-harm signal "
                                                                "with safety first and took no account action.")


class AccountMessageIn(RequestModel):
    text: RedactedText
    session: AccountChatSession | None = None


class AccountMessageResponse(ResponseModel):
    intent: Literal["delete_account", "download_data", "stop_notifications", "change_notifications",
                    "sms_not_available", "safety_first", "help"]
    acknowledgment: str | None
    body: list[str] = Field(default_factory=list, description="Statements, never questions.")
    support: list[SupportResource] = Field(default_factory=list)
    proposal: NotificationChoice | None = Field(default=None, description="A notification change to read back. "
                                                                          "Nothing is saved until confirmed.")
    redactions: list[str] = Field(default_factory=list)
    masked_text: str | None = None
    next_step: NextStep
    session: AccountChatSession
    read_aloud: ReadAloud
