"""User-facing copy, kept in one place for product, design, and counsel review.

House style from database/CLAUDE.md: plain prose, no em dashes, no semicolons
within a sentence. Acknowledge first, then ask one thing.
Anything touching legal authority is marked legal_review_required.
"""
from .schemas import NextStep, Note, Option, Relationship

# ------------------------------------------------------------------ registration

WELCOME_FAMILY = Note(
    kind="acknowledgment",
    text="We're so sorry for your loss. There's no rush here. We'll take this one small step at a time, "
         "and you can stop whenever you need to.",
)

WELCOME_PROFESSIONAL = Note(
    kind="acknowledgment",
    text="Welcome to Cairn. We'll help you keep this estate's first steps organized and in one place.",
)

POA_ENDS_AT_DEATH = Note(
    kind="legal",
    text="A power of attorney generally ends when the person dies. Someone else, often a named executor "
         "or next of kin, usually handles things from here. Cairn can't give legal advice. An attorney "
         "can tell you what applies in your state.",
    legal_review_required=True,  # [LEGAL REVIEW REQUIRED] open question 3 in database/CLAUDE.md
)

START_CASE_FOR = {
    Relationship.spouse: "When you're ready, we'll start with a few details about your spouse. "
                         "First, what was their legal first name?",
    Relationship.child: "When you're ready, we'll start with a few details about your parent. "
                        "First, what was their legal first name?",
}
START_CASE_DEFAULT = ("When you're ready, we'll start with a few details about the person who died. "
                      "First, what was their legal first name?")

INVITATIONS_UNAVAILABLE = "Joining a case someone else started needs an invitation. That's coming soon."


def registration_next_step(relationship: Relationship | None) -> NextStep:
    if relationship is None:
        return NextStep(
            action="choose_relationship",
            prompt="So we can use the right words, how are you connected to the person who died?",
            options=[
                Option(value="spouse", label="They were my spouse or partner"),
                Option(value="child", label="They were my parent"),
                Option(value="sibling", label="They were my brother or sister"),
                Option(value="other_family", label="Another family relationship"),
                Option(value="power_of_attorney", label="I held their power of attorney"),
                Option(value="fiduciary", label="I'm a professional fiduciary"),
            ],
        )
    if relationship == Relationship.child:
        return NextStep(
            action="choose_case_start",
            prompt="Are you starting something new, or picking up a case that's already in progress?",
            options=[
                Option(value="start_new_case", label="Start a new case"),
                Option(value="join_existing_case", label="Pick up an existing case",
                       available=False, unavailable_reason=INVITATIONS_UNAVAILABLE),
            ],
        )
    if relationship == Relationship.power_of_attorney:
        # These values are client routing hints, not stored roles. The relationship
        # saved on the case is still one of the Relationship values.
        return NextStep(
            action="confirm_current_role",
            prompt="Before we go on, what is your role now?",
            options=[
                Option(value="named_executor", label="I'm named as executor in the will"),
                Option(value="next_of_kin", label="I'm their next of kin"),
                Option(value="not_sure", label="I'm not sure yet"),
            ],
        )
    if relationship == Relationship.fiduciary:
        return NextStep(
            action="confirm_fiduciary",
            prompt="Please confirm you're acting as a professional fiduciary for this estate, "
                   "so we use professional language rather than assuming a family relationship.",
            options=[
                Option(value="confirm", label="Yes, I'm the fiduciary"),
                Option(value="change", label="No, I'm family"),
            ],
        )
    return NextStep(action="start_case", prompt=START_CASE_FOR.get(relationship, START_CASE_DEFAULT))


# ------------------------------------------------------------------ case intake

def after_identity(professional: bool) -> NextStep:
    return NextStep(
        action="record_death_event",
        prompt="Next, the date of death." if professional
        else "Thank you. Next, we'll note a few details about the death. What was the date?",
    )


ASK_ESTATE_QUESTIONS = NextStep(
    action="answer_estate_questions",
    prompt="Two quick questions, and you can skip either one. Did they serve in the military?",
    options=[Option(value="yes", label="Yes"), Option(value="no", label="No"),
             Option(value="unknown", label="I don't know"), Option(value="skip", label="Skip for now")],
)

ASK_HAS_WILL = NextStep(
    action="answer_has_will",
    prompt="Did they have a will or a trust?",
    options=[Option(value="yes", label="Yes"), Option(value="no", label="No"),
             Option(value="unknown", label="I don't know"), Option(value="skip", label="Skip for now")],
)

READY_FOR_JOURNEY = NextStep(
    action="start_journey",
    prompt="That's everything we need to begin. We'll put together the first few weeks, one step at a time.",
)

VIEW_JOURNEY = NextStep(action="view_journey", prompt="Here's what's next.")

UNKNOWN_IS_FINE = Note(
    kind="info", text="\"I don't know\" is a fine answer for now. You can change it any time.")

WILL_LOCATION_LATER = Note(
    kind="info", text="You'll be able to add where the will is kept later. You don't need it right now.")

FILL_IN_LATER = Note(
    kind="info", text="Anything you don't have yet can be filled in later. It won't hold up the journey.")

DEATH_STATE_HINT = Note(
    kind="info",
    text="The state where the death happened decides which office issues death certificates. "
         "It can be different from the state where they lived.",
)


def missing_for_journey(missing: list[str]) -> NextStep:
    if "date_of_death" in missing or "death_state" in missing:
        return NextStep(action="record_death_event",
                        prompt="Before we can build the journey, we need the date and the state of death.")
    return NextStep(action="complete_identity", prompt="We still need the person's legal name.")


# ------------------------------------------------------------------ journey

JOURNEY_EMPTY = Note(
    kind="info",
    text="We couldn't find guidance for this state yet. We're adding more states, "
         "and we'll let you know when steps are ready.",
)

CHECK_IN_MESSAGE = ("We've set the tasks aside. Nothing is lost, and everything will be right here "
                    "when you come back. There's nothing you need to do today.")

CHECK_IN_OPTIONS = [
    Option(value="resume", label="I'm ready to pick things back up"),
    Option(value="stay_paused", label="Not yet"),
]

RESUMED = Note(kind="acknowledgment", text="Welcome back. We'll start with just one thing.")

ALL_DONE = NextStep(action="journey_complete",
                    prompt="You've worked through the first weeks. That's a lot, and it matters.")

TASK_NEXT = NextStep(action="open_task", prompt="Here's the one thing to look at next.")

UNREVIEWED_CONTENT = Note(
    kind="legal",
    text="This guidance is a draft that hasn't been reviewed by an attorney yet.",
    legal_review_required=True,
)

CERT_ORDER_SAVED = Note(kind="acknowledgment", text="Got it. We've noted your certificate order.")

INSTITUTION_SAVED = Note(kind="acknowledgment", text="Done. We've noted that they've been told.")

CATEGORY_LABELS = {
    "certificates": "Death certificates",
    "funeral": "Funeral and service",
    "agencies": "Government agencies",
    "financial_institutions": "Banks and financial institutions",
    "home_and_personal": "Home and personal",
    "other": "Other steps",
}
