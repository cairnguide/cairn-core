"""The Cairn MongoDB schema: collections, validators, indexes, roles, and settings.

This file is the single description of the database. database/db/apply.py makes
a database match it, and is safe to run again. It replaces the numbered SQL
migrations that described the PostgreSQL schema (removed 2026-10-01).

What the database enforces, and what the application enforces
--------------------------------------------------------------
MongoDB has no row-level security, no column grants, and no triggers. The
protections that PostgreSQL gave us are split like this:

* Collection validators ($jsonSchema plus $expr), below. They apply to every
  writer, the owner included, unless it holds bypassDocumentValidation, which
  none of the Cairn roles do. They take the place of the CHECK constraints and
  domains: enums, lengths, the shape of every intake answer, the SSN digits,
  the 28-day trial, the date of death after the date of birth, a draft with no
  journey, and a valid notification choice. additionalProperties is false
  everywhere, so a field that isn't listed here (free text about how someone
  died, for example) cannot be stored.
* Roles (ROLES, below). They take the place of the grants. The app can append
  audit rows and never read them. It can queue identity cleanup and
  confirmations and never read them. It cannot write templates or settings.
* The data-access layer (api/cairn_api/store.py). It takes the place of the
  row-level security policies, the SECURITY DEFINER functions, and the
  triggers: the case boundary, read-only accounts, onboarding order, the trial
  starting once, consents and audit rows being append-only, and the activity
  clock. Every read and write the API makes goes through it.
  api/tests/test_data_security.py checks it.

Storage conventions
-------------------
* Ids are UUIDs, stored as BSON binary subtype 4 (uuidRepresentation=standard).
* Timestamps are BSON dates (UTC, millisecond precision).
* Calendar dates (a due date, a date of birth) are "YYYY-MM-DD" strings. They
  sort and compare correctly as strings, and have no time zone to get wrong.
"""
from __future__ import annotations

import hashlib
import json

SCHEMA_VERSION = 2

# ------------------------------------------------------------------ building blocks

UUID = {"bsonType": "binData"}
NULL_UUID = {"bsonType": ["binData", "null"]}
TIMESTAMP = {"bsonType": "date"}
NULL_TIMESTAMP = {"bsonType": ["date", "null"]}
BOOL = {"bsonType": "bool"}
INT = {"bsonType": ["int", "long"]}
DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
CALENDAR_DATE = {"bsonType": "string", "pattern": DATE_PATTERN}
NULL_CALENDAR_DATE = {"bsonType": ["string", "null"], "pattern": DATE_PATTERN}
STATE_CODE = {"bsonType": ["string", "null"], "pattern": r"^[A-Z]{2}$"}
KEY_PATTERN = r"^[a-z][a-z0-9_]{2,63}$"
NO_CONTROL_CHARS = r"^[^\x00-\x1f\x7f]*$"


def text(lo: int, hi: int, *, nullable: bool = False, pattern: str | None = None) -> dict:
    out = {"bsonType": ["string", "null"] if nullable else "string", "minLength": lo, "maxLength": hi}
    if pattern:
        out["pattern"] = pattern
    return out


def enum(*values, nullable: bool = False) -> dict:
    return {"enum": [*values, None] if nullable else list(values)}


def document(properties: dict, *, optional: tuple[str, ...] = (), **extra) -> dict:
    """A closed object: every listed field is required unless named in optional, and nothing else is allowed."""
    return {"bsonType": "object", "required": [k for k in properties if k not in optional],
            "additionalProperties": False, "properties": properties, **extra}


def closed(properties: dict, *, optional: tuple[str, ...] = ()) -> dict:
    """A collection's top-level $jsonSchema. _id is part of every document."""
    return document({"_id": properties.pop("_id", UUID), **properties}, optional=optional)


def is_null(field: str) -> dict:
    return {"$eq": [{"$ifNull": [field, None]}, None]}


def not_null(field: str) -> dict:
    return {"$ne": [{"$ifNull": [field, None]}, None]}


# ------------------------------------------------------------------ enums shared with the API

VOICES = ("steady_direct", "warm_patient", "brisk_businesslike", "plain_practical")  # voices/manifest.yaml
SIGN_IN_METHODS = ("google", "apple", "email")
ONBOARDING_STEPS = ("account_created", "privacy_terms_accepted", "trial_terms_accepted", "ai_notice_accepted",
                    "preferred_name_saved", "complete")
ACCOUNT_STATUSES = ("pending_onboarding", "active_no_case", "trial_active", "read_only", "subscribed",
                    "pending_deletion")
CONSENT_PURPOSES = ("privacy_terms", "trial_terms", "ai_notice",
                    "terms", "privacy", "ai_processing")  # the last three only for records moved from PostgreSQL
# draft (case creation spec). The rest come from the card 50 data model. completed, closed_open_steps, and
# pending_deletion belong to ending a journey (UC-END), which sets them. A 7-day hold is still recorded as
# deletion_requested_at, and CaseOut reports it as pending_deletion. Read-only is an account state, never a case status.
CASE_STATUSES = ("draft", "active", "paused", "closed", "completed", "closed_open_steps", "pending_deletion")
# The MVP allows one owner per case (decision 8). Widen this when invitations ship.
MEMBER_ROLES = ("owner",)
RELATIONSHIPS = ("spouse", "child", "sibling", "other_family", "power_of_attorney", "fiduciary")
ATTORNEY_TRIGGERS = ("contested_will", "family_disagreement", "unsure_of_authority", "multi_state_property",
                     "early_property_disposal")  # selling, moving, or giving away property early (card 59)
# Only these keys can be stored, so nothing about distress or cause of death can end up in shown_notices.
SHOWN_NOTICES = ("poa_authority_note",)
NAME_FALLBACKS = ("your_loved_one", "the_person_who_died")
TRI_STATE = ("yes", "no", "unknown")
PLACE_TYPES = ("hospital", "hospice", "home", "facility", "other")
# Card 50 statuses map onto these: open is not_started, in_progress, check_on_this, or not_today. not_needed is
# skipped or not_applicable. done and handled_elsewhere are the same. needs_check is a flag since v2, so
# check_on_this is kept only for tasks written before it.
TASK_STATUSES = ("not_started", "check_on_this", "in_progress", "done", "not_today", "skipped", "not_applicable",
                 "handled_elsewhere")
FIELD_KEYS = ("user_role", "display_name", "date_of_death", "place_of_death", "residence_jurisdiction", "circumstance",
              "veteran_status", "estate_plan_status", "completed_items")
ANSWER_STATES = ("answered", "skipped", "unsure")
USER_ROLES = ("spouse_partner", "child", "other_family", "named_executor", "power_of_attorney",
              "professional_fiduciary", "friend", "other", "prefer_not_to_say")
CIRCUMSTANCES = ("expected_illness_or_hospice", "sudden_natural", "accident_or_unexpected", "under_investigation",
                 "prefer_not_to_say")
ESTATE_PLAN_STATUSES = ("yes_location_known", "yes_location_unknown", "no", "unknown")
COMPLETED_ITEMS = ("death_pronounced", "home_pets_vehicles_secured", "funeral_provider_chosen", "funeral_home_has_ssn",
                   "certificates_ordered", "ssa_notified", "bank_insurer_or_employer_notified", "other")
# All 50 states, DC, and the five territories (DEC-COV). The intake picker offers exactly these.
JURISDICTIONS = ("AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY",
                 "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND",
                 "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
                 "PR", "GU", "VI", "AS", "MP")
CHANNELS = ("email", "push", "in_app_only")  # no sms: card 50 and legal review first
REASONS = ("due_date_upcoming", "inactivity")
FREQUENCIES = ("as_it_happens", "daily_max", "weekly_max")
REMINDER_KINDS = ("trial_day_21", "trial_day_27", "trial_ends_soon")
CONFIRMATION_TYPES = ("case_deleted_now", "case_deleted_after_hold", "account_deleted")
CONTEXT_KEYS = ("CERT_ORDER", "FUNERAL", "BANK_NOTICES", "CONVO_SUMMARY")
SETTING_RANGES = {"draft_retention_days": (1, 365), "trial_reminder_days_before": (1, 27),
                  "case_deletion_hold_days": (1, 30)}

# ------------------------------------------------------------------ intake answer shapes

# One branch per data_fields key. The API validates first. This is the backstop
# that keeps anything outside the spec's fields and shapes out of the database.
def _answer(field: str, value: dict) -> dict:
    return {"properties": {"field_key": enum(field), "value": value}}


INTAKE_VALUE_SHAPES = [
    {"properties": {"value": {"bsonType": "null"}}},
    _answer("user_role", enum(*USER_ROLES)),
    _answer("display_name", text(1, 60, pattern=NO_CONTROL_CHARS)),
    _answer("date_of_death", document({"precision": enum("exact", "today"), "date": CALENDAR_DATE})),
    _answer("date_of_death", document({"precision": enum("this_week", "unknown"), "date": {"bsonType": "null"}})),
    _answer("place_of_death", document({"outside_us": {"enum": [True]}, "jurisdiction": {"bsonType": "null"},
                                        "county_or_city": text(1, 100, nullable=True)})),
    _answer("place_of_death", document({"outside_us": {"enum": [False]},
                                        "jurisdiction": enum(*JURISDICTIONS, nullable=True),
                                        "county_or_city": text(1, 100, nullable=True)})),
    _answer("residence_jurisdiction", document({"choice": enum("same_as_place_of_death", "different", "unknown"),
                                                "jurisdiction": {"bsonType": "null"}})),
    _answer("residence_jurisdiction", document({"choice": enum("different"),
                                                "jurisdiction": enum(*JURISDICTIONS)})),
    _answer("circumstance", enum(*CIRCUMSTANCES)),
    _answer("veteran_status", enum(*TRI_STATE)),
    _answer("estate_plan_status", enum(*ESTATE_PLAN_STATUSES)),
    # An item is done (its name), or someone else is handling it (UC-CASE-08), with an optional name.
    _answer("completed_items", {"bsonType": "array", "minItems": 1, "uniqueItems": True, "items": {"anyOf": [
        enum(*COMPLETED_ITEMS),
        document({"item": enum(*COMPLETED_ITEMS), "handled_by": text(1, 60, nullable=True, pattern=NO_CONTROL_CHARS)}),
    ]}}),
    _answer("completed_items", {"bsonType": "array", "minItems": 1, "maxItems": 1,
                                "items": enum("none_or_unsure")}),
]

# ------------------------------------------------------------------ collections

COLLECTIONS: dict[str, dict] = {}


def collection(name: str, schema: dict, *expr: dict) -> None:
    validator: dict = {"$jsonSchema": schema}
    if expr:
        validator = {"$and": [validator, {"$expr": {"$and": list(expr)} if len(expr) > 1 else expr[0]}]}
    COLLECTIONS[name] = validator


collection("users", closed({
    "idp_subject": text(1, 255),
    "email": text(3, 320),
    "email_lower": text(3, 320),
    "sign_in_method": enum(*SIGN_IN_METHODS, nullable=True),
    "preferred_name": text(1, 100, nullable=True),
    "name_pronunciation": text(1, 200, nullable=True),
    # A name shared by Google or Apple. Pre-fill for UC-REG-11 only, cleared once a preferred name is saved.
    "name_prefill": text(1, 100, nullable=True),
    "voice": enum(*VOICES),
    "time_zone": text(1, 64, nullable=True),
    "onboarding_step": enum(*ONBOARDING_STEPS),
    "status": enum(*ACCOUNT_STATUSES),
    # Set once, by store.start_journey on the account's first journey. Never reset.
    "trial_started_at": NULL_TIMESTAMP,
    # 28 days after the start, plus every care rest (DEC-26-01). Only ever moves later.
    "trial_ends_at": NULL_TIMESTAMP,
    # A care rest stopped the free days at this time (crisis plan). The reason is never stored.
    "trial_clock_paused_at": NULL_TIMESTAMP,
    # The AI reminder (UC-CASE-23): when it was last shown, and on which local day for the once-a-day rule.
    "ai_reminder_shown_at": NULL_TIMESTAMP,
    "ai_reminder_shown_on": NULL_CALENDAR_DATE,
    "created_at": TIMESTAMP,
}),
    {"$eq": ["$email_lower", {"$toLower": "$email"}]},
    # At least 28 days (D-02), counted in hours so daylight saving time never shifts it. Later only by care rests.
    {"$or": [{"$and": [is_null("$trial_started_at"), is_null("$trial_ends_at"), is_null("$trial_clock_paused_at")]},
             {"$and": [not_null("$trial_started_at"),
                       {"$gte": ["$trial_ends_at", {"$dateAdd": {"startDate": "$trial_started_at", "unit": "hour",
                                                                  "amount": 672}}]}]}]},
)

# Append-only. Rows go only when the account is deleted.
collection("consents", closed({
    "user_id": UUID,
    "purpose": enum(*CONSENT_PURPOSES),
    "policy_version": text(1, 200),
    "granted_at": TIMESTAMP,
    "auth_provider": enum(*SIGN_IN_METHODS, nullable=True),
    "client": text(1, 100, nullable=True),
}))

# The case is the security boundary. members decides who can see and change it.
collection("cases", closed({
    "status": enum(*CASE_STATUSES),
    "created_by": UUID,
    "members": {"bsonType": "array", "minItems": 1, "items": document({
        "user_id": UUID,
        "role": enum(*MEMBER_ROLES),
        # The relationship is an intake answer (user_role) since 0010. Kept for records moved from PostgreSQL.
        "relationship": enum(*RELATIONSHIPS, nullable=True),
        "status": enum("active", "revoked"),
        "created_at": TIMESTAMP,
    })},
    "created_at": TIMESTAMP,
    "purge_after": NULL_TIMESTAMP,
    "journey_template_key": {"bsonType": ["string", "null"], "pattern": KEY_PATTERN},
    "journey_template_version": {"bsonType": ["int", "long", "null"], "minimum": 1},
    "journey_started_at": NULL_TIMESTAMP,
    # The date tasks count from, in the user's time zone.
    "journey_started_on": NULL_CALENDAR_DATE,
    "last_intake_step": {"bsonType": ["string", "null"], "pattern": KEY_PATTERN},
    # Any answer, edit, or open of a draft. A draft idle for draft_retention_days is deleted (DEC-07).
    "last_activity_at": TIMESTAMP,
    "death_not_yet_occurred": BOOL,
    "skip_explainers": BOOL,
    "name_fallback": enum(*NAME_FALLBACKS),
    "attorney_triggers": {"bsonType": "array", "uniqueItems": True, "items": enum(*ATTORNEY_TRIGGERS)},
    "shown_notices": {"bsonType": "array", "uniqueItems": True, "items": enum(*SHOWN_NOTICES)},
    # Lets the product step back from task mode, for every rest. Do not store distress inferences (decision 7).
    "tasks_paused_until": NULL_TIMESTAMP,
    # The user said yes to one check-in tomorrow after a hard moment (DEC-26-04). Cleared once shown or sent.
    "check_in_at": NULL_TIMESTAMP,
    # Only when the user themselves said the death was by suicide (UC-CASE-05). Adds loss survivor support.
    # Never shown back as a label.
    "loss_survivor_resources": BOOL,
    # Pets or dependents were mentioned, so the secure-now step is recommended first (UC-CASE-13).
    "secure_now_first": BOOL,
    # The user chose delete with a hold. Deleted case_deletion_hold_days later by the purge_held_cases job.
    "deletion_requested_at": NULL_TIMESTAMP,
}),
    # draft_has_no_journey
    {"$or": [{"$ne": ["$status", "draft"]},
             {"$and": [is_null("$journey_started_at"), is_null("$journey_template_key"),
                       is_null("$journey_template_version"), is_null("$journey_started_on")]}]},
    # started_case_has_start
    {"$or": [{"$eq": ["$status", "draft"]},
             {"$and": [not_null("$journey_started_at"), not_null("$journey_started_on")]}]},
)

# One per case, created inside the task that needs it, never on a draft. Sensitive. Never log.
collection("deceased", closed({
    "case_id": UUID,
    "legal_first_name": text(1, 100, nullable=True),
    "legal_middle_name": text(0, 100, nullable=True),
    "legal_last_name": text(1, 100, nullable=True),
    "date_of_birth": NULL_CALENDAR_DATE,
    # Last four digits only. The full SSN is deliberately not collected at MVP.
    "ssn_last4": {"bsonType": ["string", "null"], "pattern": r"^[0-9]{4}$"},
    "domicile_state": STATE_CODE,
    "veteran_status": enum(*TRI_STATE),
    "has_will": enum(*TRI_STATE),
    "date_of_death": NULL_CALENDAR_DATE,
    "place_type": enum(*PLACE_TYPES, nullable=True),
    "facility_name": text(0, 200, nullable=True),
    "city": text(0, 100, nullable=True),
    "county": text(0, 100, nullable=True),
    # Determines the issuing vital records office.
    "death_state": STATE_CODE,
}),
    {"$or": [is_null("$date_of_birth"),
             {"$lte": ["$date_of_birth", {"$dateToString": {"format": "%Y-%m-%d", "date": "$$NOW"}}]}]},
    {"$or": [is_null("$date_of_death"),
             {"$lte": ["$date_of_death", {"$dateToString": {"format": "%Y-%m-%d", "date": "$$NOW"}}]}]},
    # death_not_before_birth
    {"$or": [is_null("$date_of_birth"), is_null("$date_of_death"), {"$gte": ["$date_of_death", "$date_of_birth"]}]},
)

# One row per data_fields key. Only the spec's fields and shapes can be stored. Never log values.
collection("case_intake_answers", {**closed({
    "_id": {"bsonType": "objectId"},
    "case_id": UUID,
    "field_key": enum(*FIELD_KEYS),
    "answer_state": enum(*ANSWER_STATES),
    "value": {},
    # The user's own words for this one field, for the review screen (UC-CASE-11). Never for circumstance.
    "own_words": text(1, 120, nullable=True, pattern=NO_CONTROL_CHARS),
    "updated_at": TIMESTAMP,
}), "anyOf": INTAKE_VALUE_SHAPES},
    # value_only_when_answered
    {"$eq": [{"$eq": ["$answer_state", "answered"]}, not_null("$value")]},
    # own_words_allowed
    {"$or": [is_null("$own_words"),
             {"$and": [{"$ne": ["$field_key", "circumstance"]}, {"$eq": ["$answer_state", "answered"]}]}]},
)

_CITATION = document({
    "authority_name": text(1, 200),
    "url": {"bsonType": "string", "pattern": r"^https://"},
    "jurisdiction": {"bsonType": "string", "pattern": r"^(US|[A-Z]{2})$"},
    "last_verified_on": NULL_CALENDAR_DATE,
})

# Content. Immutable per version: a correction is a new version. Only active ever changes.
collection("task_templates", closed({
    "task_key": {"bsonType": "string", "pattern": KEY_PATTERN},
    "version": {**INT, "minimum": 1},
    "title": text(1, 120),
    "plain_summary": text(1, 600),
    "journey_week": {**INT, "minimum": 1, "maximum": 4},
    "sort_order": INT,
    "due_offset_days": {**INT, "minimum": 0, "maximum": 90},
    "jurisdiction": {"bsonType": "string", "pattern": r"^(US|[A-Z]{2})$"},
    "applies_when": {"bsonType": "object"},
    "attorney_referral": BOOL,
    "attorney_referral_note": text(1, 1000, nullable=True),
    "why_now": text(1, 200, nullable=True),
    "counsel_reviewed_at": NULL_TIMESTAMP,
    "citations": {"bsonType": "array", "items": _CITATION},
    "content_hash": {"bsonType": "string", "pattern": r"^[0-9a-f]{64}$"},
    "git_release": text(1, 200),
    "active": BOOL,
    "loaded_at": TIMESTAMP,
}),
    # attorney_referral_needs_note
    {"$or": [{"$eq": ["$attorney_referral", False]}, not_null("$attorney_referral_note")]},
)

# Journey selection rules (content/journeys). Immutable per version, like task_templates.
collection("journey_templates", closed({
    "version": {**INT, "minimum": 1},
    "definition": {"bsonType": "object"},
    "counsel_reviewed_at": NULL_TIMESTAMP,
    "content_hash": {"bsonType": "string", "pattern": r"^[0-9a-f]{64}$"},
    "git_release": text(1, 200),
    "active": BOOL,
    "loaded_at": TIMESTAMP,
}))

collection("case_tasks", closed({
    "case_id": UUID,
    # Pinned to one immutable template version, which records what the family was shown.
    "template_id": UUID,
    "task_key": {"bsonType": "string", "pattern": KEY_PATTERN},
    "status": enum(*TASK_STATUSES),
    "due_on": NULL_CALENDAR_DATE,
    "snoozed_until": NULL_TIMESTAMP,
    "completed_at": NULL_TIMESTAMP,
    # Whether the journey rules currently include this task (UC-CASE-09). Separate from status.
    "selected": BOOL,
    # Shown as "Check on this": the user wasn't sure whether it was done (UC-CASE-08).
    "needs_check": BOOL,
    # Shown last as "Probably doesn't apply". Never hidden (UC-CASE-06).
    "probably_not_applicable": BOOL,
    # Optional name of whoever is handling it. Third-party personal data. Never in a notification.
    "handled_by": text(1, 60, nullable=True, pattern=NO_CONTROL_CHARS),
    "created_at": TIMESTAMP,
    "updated_at": TIMESTAMP,
}),
    # done_needs_completed_at
    {"$or": [{"$ne": ["$status", "done"]}, not_null("$completed_at")]},
    # handled_by_only_when_handled_elsewhere
    {"$or": [is_null("$handled_by"), {"$eq": ["$status", "handled_elsewhere"]}]},
)

# How and when Cairn keeps in touch, per journey (UC-CASE-19). _id is the case id. No row, or in_app_only,
# means nothing is sent outside the app (D-2026-09-25-N2). Never used for marketing or advertising.
collection("notification_preferences", closed({
    "channels": {"bsonType": "array", "minItems": 1, "uniqueItems": True, "items": enum(*CHANNELS)},
    "reasons": {"bsonType": "array", "uniqueItems": True, "items": enum(*REASONS)},
    "due_date_lead_days": enum(1, 3, 7, nullable=True),
    "inactivity_days": enum(3, 7, 14, nullable=True),
    "frequency": enum(*FREQUENCIES),
    # What the OS said when the user chose push. Push is never required for the app to work.
    "push_permission_granted": BOOL,
    "updated_at": TIMESTAMP,
}),
    # notification_choice_valid: in_app_only stands alone, any other channel needs a reason,
    # and each reason needs its timing.
    {"$cond": [{"$in": ["in_app_only", "$channels"]},
               {"$and": [{"$eq": [{"$size": "$channels"}, 1]}, {"$eq": [{"$size": "$reasons"}, 0]}]},
               {"$gte": [{"$size": "$reasons"}, 1]}]},
    {"$eq": [not_null("$due_date_lead_days"), {"$in": ["due_date_upcoming", "$reasons"]}]},
    {"$eq": [not_null("$inactivity_days"), {"$in": ["inactivity", "$reasons"]}]},
)

# What was sent outside the app, and why. No text, no address.
collection("notification_log", closed({
    "case_id": UUID,
    "case_task_id": NULL_UUID,
    "reason": enum(*REASONS),
    "channel": enum("email"),
    "sent_at": TIMESTAMP,
}))

collection("trial_reminders", closed({
    "user_id": UUID,
    "kind": enum(*REMINDER_KINDS),
    "due_at": TIMESTAMP,
    "email_sent_at": NULL_TIMESTAMP,
}))

# Auth0 user deletion and Apple token revocation, queued by account deletion and run by a job.
collection("identity_deletion_requests", closed({
    "idp_subject": text(1, 255),
    "provider": enum(*SIGN_IN_METHODS, nullable=True),
    "requested_at": TIMESTAMP,
    "attempts": {**INT, "minimum": 0},
    "last_error": text(0, 200, nullable=True),
}))

# The one confirmation for something the user just did. The address is kept only until it is sent.
collection("action_confirmation_outbox", closed({
    "action_type": enum(*CONFIRMATION_TYPES),
    "email": text(3, 320),
    "user_id": NULL_UUID,
    "queued_at": TIMESTAMP,
    "claimed_at": NULL_TIMESTAMP,
    "attempts": {**INT, "minimum": 0},
    "last_error": text(0, 200, nullable=True),
}))

# content_stored is false by construction: there is no field that could hold content or say who it went to.
collection("action_confirmation_log", closed({
    "action_type": enum(*CONFIRMATION_TYPES),
    "channel": enum("email"),
    "sent_at": TIMESTAMP,
}))

# Opaque ids only, no PII. actor_id and case_id outlive the account and the case on purpose.
# actor_id is null for system actions. Retention period is a policy decision. [LEGAL REVIEW REQUIRED]
collection("audit_events", closed({
    "actor_id": NULL_UUID,
    "case_id": NULL_UUID,
    "action": text(1, 100),
    "object_type": text(1, 100, nullable=True),
    "object_id": NULL_UUID,
    "occurred_at": TIMESTAMP,
}))

# AI-facing case context (CONTEXT_ITEM). No direct identifiers in the payload.
collection("context_items", closed({
    "_id": {"bsonType": "objectId"},
    "case_id": UUID,
    "item_key": enum(*CONTEXT_KEYS),
    "payload": {"bsonType": "object"},
    "updated_at": TIMESTAMP,
    "expires_at": NULL_TIMESTAMP,
}),
    {"$lte": [{"$bsonSize": "$payload"}, 32768]},
)

# The anonymous monthly count of crisis referrals for SB 243 reporting (UC-CASE-14). _id is "YYYY-MM".
# No user id, no case id, nothing else: there is no field that could hold one.
collection("safety_referral_counts", closed({
    "_id": {"bsonType": "string", "pattern": r"^[0-9]{4}-[0-9]{2}$"},
    "level_4_referrals": {**INT, "minimum": 0},
    "updated_at": TIMESTAMP,
}))

# Values that change without a code change. Jobs and the owner write. The app reads.
collection("app_settings", closed({
    "_id": enum(*SETTING_RANGES),
    "value": {"bsonType": ["int", "long", "double", "decimal"]},
    "description": text(1, 500),
    "updated_at": TIMESTAMP,
}),
    {"$or": [{"$and": [{"$eq": ["$_id", key]}, {"$gte": ["$value", lo]}, {"$lte": ["$value", hi]}]}
             for key, (lo, hi) in SETTING_RANGES.items()]},
)

# One sender at a time for a job that must not overlap with itself.
collection("job_locks", closed({
    "_id": text(1, 100),
    "locked_until": TIMESTAMP,
}))

collection("schema_migrations", closed({
    "_id": text(1, 200),
    "checksum": {"bsonType": "string", "pattern": r"^[0-9a-f]{64}$"},
    "applied_at": TIMESTAMP,
}))

# ------------------------------------------------------------------ indexes

# (collection, keys, options). Names are stable so apply.py can tell what already exists.
INDEXES: list[tuple[str, list[tuple[str, int]], dict]] = [
    ("users", [("idp_subject", 1)], {"unique": True, "name": "users_idp_subject_uq"}),
    ("users", [("email_lower", 1)], {"unique": True, "name": "users_email_lower_uq"}),
    ("consents", [("user_id", 1), ("purpose", 1)], {"name": "consents_user_idx"}),
    ("cases", [("created_by", 1), ("created_at", 1)], {"name": "cases_created_by_idx"}),
    ("cases", [("members.user_id", 1)], {"name": "cases_members_idx"}),
    ("cases", [("status", 1), ("last_activity_at", 1)], {"name": "cases_status_activity_idx"}),
    ("cases", [("deletion_requested_at", 1)],
     {"name": "cases_deletion_requested_idx", "partialFilterExpression": {"deletion_requested_at": {"$type": "date"}}}),
    ("cases", [("check_in_at", 1)],
     {"name": "cases_check_in_idx", "partialFilterExpression": {"check_in_at": {"$type": "date"}}}),
    ("cases", [("purge_after", 1)],
     {"name": "cases_purge_after_idx", "partialFilterExpression": {"purge_after": {"$type": "date"}}}),
    ("deceased", [("case_id", 1)], {"unique": True, "name": "deceased_case_uq"}),
    ("case_intake_answers", [("case_id", 1), ("field_key", 1)], {"unique": True, "name": "answers_case_field_uq"}),
    ("task_templates", [("task_key", 1), ("version", -1)], {"unique": True, "name": "task_templates_key_version_uq"}),
    ("task_templates", [("active", 1), ("task_key", 1), ("version", -1)], {"name": "task_templates_active_idx"}),
    ("journey_templates", [("version", -1)], {"unique": True, "name": "journey_templates_version_uq"}),
    ("case_tasks", [("case_id", 1), ("template_id", 1)], {"unique": True, "name": "case_tasks_case_template_uq"}),
    ("case_tasks", [("case_id", 1), ("status", 1)], {"name": "case_tasks_case_status_idx"}),
    # Supports "which families are still on an old template version" queries.
    ("case_tasks", [("template_id", 1)], {"name": "case_tasks_template_idx"}),
    ("notification_log", [("case_id", 1), ("sent_at", 1)], {"name": "notification_log_case_idx"}),
    ("notification_log", [("case_task_id", 1)], {"name": "notification_log_task_idx"}),
    ("trial_reminders", [("user_id", 1), ("kind", 1)], {"unique": True, "name": "trial_reminders_user_kind_uq"}),
    ("trial_reminders", [("email_sent_at", 1), ("due_at", 1)], {"name": "trial_reminders_due_idx"}),
    ("identity_deletion_requests", [("requested_at", 1)], {"name": "identity_deletion_requested_idx"}),
    ("action_confirmation_outbox", [("queued_at", 1)], {"name": "outbox_queued_idx"}),
    ("action_confirmation_outbox", [("user_id", 1)], {"name": "outbox_user_idx"}),
    ("audit_events", [("case_id", 1), ("occurred_at", 1)], {"name": "audit_events_case_idx"}),
    ("audit_events", [("actor_id", 1), ("occurred_at", 1)], {"name": "audit_events_actor_idx"}),
    ("context_items", [("case_id", 1), ("item_key", 1)], {"unique": True, "name": "context_items_case_key_uq"}),
]

# ------------------------------------------------------------------ roles

# Custom roles, scoped to the Cairn database. Login users get exactly one of them.
#   cairnApp     the API. Never connects as anything else.
#   cairnJobs    the scheduled jobs container (purges, outbound email, identity cleanup).
#   cairnLoader  the template loader at deploy time. Can only add template versions.
# None of them has bypassDocumentValidation, so the validators above bind every Cairn login.
# Atlas manages custom roles outside the database: see database/README.md.
RW = ["find", "insert", "update", "remove"]

ROLES: dict[str, dict[str, list[str]]] = {
    "cairnApp": {
        "users": RW,
        "consents": ["find", "insert", "remove"],              # append-only in store.py, removed with the account
        "cases": RW,
        "deceased": RW,
        "case_intake_answers": RW,
        "case_tasks": RW,
        "notification_preferences": RW,
        "context_items": RW,
        "notification_log": ["find", "remove"],                # read for the download, removed with the case
        "trial_reminders": ["find", "insert", "remove"],
        "task_templates": ["find"],
        "journey_templates": ["find"],
        "app_settings": ["find"],
        "audit_events": ["insert"],                            # write-only: the app can't read audit rows
        "identity_deletion_requests": ["insert"],              # queued by account deletion, never read
        "action_confirmation_outbox": ["insert", "remove"],    # queued by deletions, never read
        "safety_referral_counts": ["insert", "update"],        # count up only, never read
    },
    "cairnJobs": {
        "users": ["find", "update", "remove"],
        "consents": ["find", "remove"],
        "cases": ["find", "update", "remove"],
        "deceased": ["find", "remove"],
        "case_intake_answers": ["find", "remove"],
        "case_tasks": ["find", "remove"],
        "notification_preferences": ["find", "remove"],
        "context_items": ["find", "remove"],
        "notification_log": ["find", "insert", "remove"],
        "trial_reminders": ["find", "update", "remove"],
        "app_settings": ["find"],
        "audit_events": ["insert"],
        "identity_deletion_requests": ["find", "insert", "update", "remove"],
        "action_confirmation_outbox": ["find", "insert", "update", "remove"],
        "action_confirmation_log": ["insert"],                 # append-only
        "job_locks": ["find", "insert", "update"],
        "safety_referral_counts": ["find"],                    # the monthly report
    },
    "cairnLoader": {
        "task_templates": ["find", "insert", "update"],        # update: the active flag only, see load_templates.py
        "journey_templates": ["find", "insert", "update"],
    },
}


def role_document(name: str, db: str) -> dict:
    """createRole / updateRole arguments for one role, scoped to db."""
    return {"privileges": [{"resource": {"db": db, "collection": coll}, "actions": actions}
                           for coll, actions in ROLES[name].items()],
            "roles": []}


# ------------------------------------------------------------------ settings

SETTINGS = {
    "draft_retention_days": (28, "DEC-07. A draft case with no activity for this many days is deleted. "
                                 "The pause copy says 28 days, so change the copy too."),
    "trial_reminder_days_before": (3, "OPEN-DECISION-02. Days before trial_ends_at to schedule the "
                                      "trial_ends_soon reminder."),
    "case_deletion_hold_days": (7, "UC-END-13. A case the user chose to delete with a hold is deleted this many "
                                   "days later. The copy says 7 days, so change the copy too."),
}


def checksum() -> str:
    """A digest of everything above, recorded with each apply."""
    body = {"version": SCHEMA_VERSION, "collections": COLLECTIONS, "indexes": INDEXES, "roles": ROLES,
            "settings": {k: v[0] for k, v in SETTINGS.items()}}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
