"""How and when Cairn keeps in touch (UC-CASE-19, UC-CASE-20).

Spec: database/docs/cairn-case-creation-use-cases-2026-09-25.json. The schema is
migration 0011 (cairn.notification_preferences, one row per journey).

Rules this module keeps:
- No choice, or skipping, means in_app_only. Nothing is sent outside the app (D-2026-09-25-N2).
- The user decides the channel, the reasons, the timing, and the pace. Nothing else is sent except
  confirmations of what the user just did (UC-CASE-21).
- Changing preferences is always free, including on a read-only account (D-2026-09-25-F1).
- Preferences are never used for marketing or advertising (UC-REG-07).
- SMS is not offered: it is an open question for the MVP and needs legal review (card 50).
"""
from __future__ import annotations

from uuid import UUID

from psycopg import sql

from . import account as acct
from .copy_store import Copy
from .db import Session
from .errors import case_access_denied
from .schemas import (
    NotificationChannel,
    NotificationChoice,
    NotificationFrequency,
    NotificationPreferencesOut,
    NotificationQuestion,
    NotificationReason,
    Option,
)

# The shortcut in UC-CASE-19. Draft values, open question on card 50.
KEEP_IT_SIMPLE = NotificationChoice(channels=[NotificationChannel.email],
                                    reasons=[NotificationReason.due_date_upcoming],
                                    due_date_lead_days=3, frequency=NotificationFrequency.daily_max)
IN_APP_ONLY = NotificationChoice(channels=[NotificationChannel.in_app_only])

CHANNEL_ORDER = [NotificationChannel.email, NotificationChannel.push, NotificationChannel.in_app_only]
REASON_ORDER = [NotificationReason.due_date_upcoming, NotificationReason.inactivity]

_COLUMNS = ("case_id, channels, reasons, due_date_lead_days, inactivity_days, frequency, push_permission_granted, "
            "updated_at")


# ------------------------------------------------------------------ reads

def load(s: Session, case_id: UUID) -> dict | None:
    return s.one(f"SELECT {_COLUMNS} FROM cairn.notification_preferences WHERE case_id = %s", (case_id,))


def choice_of(row: dict | None) -> NotificationChoice:
    if row is None:
        return IN_APP_ONLY
    return NotificationChoice(channels=row["channels"], reasons=row["reasons"],
                              due_date_lead_days=row["due_date_lead_days"], inactivity_days=row["inactivity_days"],
                              frequency=row["frequency"])


def any_email_choice(s: Session) -> bool:
    """Whether the user chose email for any of their journeys. The trial reminder follows this (0011)."""
    return s.one("SELECT EXISTS (SELECT 1 FROM cairn.notification_preferences p JOIN cairn.cases c ON c.id = p.case_id "
                 "WHERE c.created_by = cairn.current_user_id() AND 'email' = ANY (p.channels)) AS x")["x"]


# ------------------------------------------------------------------ plain language

def _join(parts: list[str], copy: Copy) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} {copy['readback_and']} {parts[-1]}"


def readback(copy: Copy, choice: NotificationChoice) -> str:
    """One sentence that says the choice back (UC-CASE-19 step 6, UC-CASE-20)."""
    if NotificationChannel.in_app_only in choice.channels:
        return copy["readback_in_app_only"]
    channels = [copy[f"readback_channel_{c.value}"] for c in CHANNEL_ORDER if c in choice.channels]
    reasons = []
    if NotificationReason.due_date_upcoming in choice.reasons:
        days = choice.due_date_lead_days
        reasons.append(copy["readback_due_date_upcoming_1"] if days == 1
                       else copy["readback_due_date_upcoming"].format(days=days))
    if NotificationReason.inactivity in choice.reasons:
        reasons.append(copy["readback_inactivity"].format(days=choice.inactivity_days))
    return copy["readback_choice"].format(channels=_join(channels, copy), reasons=_join(reasons, copy),
                                          frequency=copy[f"readback_frequency_{choice.frequency.value}"])


def out(case_id: UUID, row: dict | None, copy: Copy,
        choice: NotificationChoice | None = None) -> NotificationPreferencesOut:
    """The stored row, or a proposed choice that isn't stored yet."""
    ch = choice or choice_of(row)
    return NotificationPreferencesOut(
        case_id=case_id, stored=row is not None and choice is None, channels=ch.channels, reasons=ch.reasons,
        due_date_lead_days=ch.due_date_lead_days, inactivity_days=ch.inactivity_days, frequency=ch.frequency,
        push_permission_granted=bool(row and row["push_permission_granted"]
                                     and NotificationChannel.push in ch.channels),
        readback=readback(copy, ch), updated_at=row["updated_at"] if row and choice is None else None)


def questions(copy: Copy, masked_email: str) -> list[NotificationQuestion]:
    """UC-CASE-19 steps 2 to 5, in order. The client asks one at a time."""
    return [
        NotificationQuestion(
            id="channels", prompt=copy["notifications_question_channels"], multi_select=True,
            asked_when="always",
            options=[Option(value="email", label=copy["channel_email"].format(masked_email=masked_email)),
                     Option(value="push", label=copy["channel_push"]),
                     Option(value="in_app_only", label=copy["channel_in_app_only"])]),
        NotificationQuestion(
            id="reasons", prompt=copy["notifications_question_reasons"], multi_select=True,
            asked_when="a channel other than in_app_only was chosen",
            options=[Option(value=r.value, label=copy[f"reason_{r.value}"]) for r in REASON_ORDER]),
        NotificationQuestion(
            id="due_date_lead_days", prompt=copy["notifications_question_due_date_lead_days"], multi_select=False,
            asked_when="due_date_upcoming was chosen",
            options=[Option(value=str(d), label=copy[f"lead_days_{d}"]) for d in (1, 3, 7)]),
        NotificationQuestion(
            id="inactivity_days", prompt=copy["notifications_question_inactivity_days"], multi_select=False,
            asked_when="inactivity was chosen",
            options=[Option(value=str(d), label=copy[f"inactivity_days_{d}"]) for d in (3, 7, 14)]),
        NotificationQuestion(
            id="frequency", prompt=copy["notifications_question_frequency"], multi_select=False,
            asked_when="a channel other than in_app_only was chosen",
            options=[Option(value=f.value, label=copy[f"frequency_{f.value}"]) for f in NotificationFrequency]),
    ]


def display_names(s: Session, copy: Copy) -> dict[UUID, str]:
    """What to call the person for each of the user's cases. Conversation only, never a legal name."""
    rows = s.all("SELECT c.id, c.name_fallback, a.value #>> '{}' AS display_name FROM cairn.cases c "
                 "LEFT JOIN cairn.case_intake_answers a ON a.case_id = c.id AND a.field_key = 'display_name' "
                 "AND a.answer_state = 'answered' WHERE c.created_by = cairn.current_user_id() "
                 "ORDER BY c.created_at")
    return {r["id"]: r["display_name"] or copy[r["name_fallback"]] for r in rows}


def same_as_candidate(s: Session, case_id: UUID) -> UUID | None:
    """UC-CASE-18 change. The user's first other journey that has a choice, offered first."""
    row = s.one("SELECT c.id FROM cairn.cases c JOIN cairn.notification_preferences p ON p.case_id = c.id "
                "WHERE c.created_by = cairn.current_user_id() AND c.id <> %s ORDER BY c.created_at LIMIT 1",
                (case_id,))
    return row["id"] if row else None


def shortcuts(s: Session, copy: Copy, case_id: UUID, names: dict[UUID, str]) -> list[Option]:
    options = []
    other = same_as_candidate(s, case_id)
    if other is not None:
        options.append(Option(value=f"same_as:{other}",
                              label=copy["use_the_same_as"].format(display_name=names.get(other, ""))))
    options += [Option(value="keep_it_simple", label=copy["keep_it_simple"]),
                Option(value="set_up", label=copy["notifications_set_up"]),
                Option(value="skip", label=copy["notifications_skip"])]
    return options


# ------------------------------------------------------------------ writes

def save(s: Session, case_id: UUID, choice: NotificationChoice, *, push_granted: bool | None = None) -> dict:
    """Upsert one journey's choice. Push permission is kept only while push is chosen."""
    wants_push = NotificationChannel.push in choice.channels
    row = load(s, case_id)
    granted = push_granted if push_granted is not None else bool(row and row["push_permission_granted"])
    values = {"channels": [c.value for c in choice.channels], "reasons": [r.value for r in choice.reasons],
              "due_date_lead_days": choice.due_date_lead_days, "inactivity_days": choice.inactivity_days,
              "frequency": choice.frequency.value, "push_permission_granted": granted and wants_push}
    if row is None:
        cols = ["case_id", *values]
        saved = s.one(sql.SQL("INSERT INTO cairn.notification_preferences ({}) VALUES ({}) RETURNING case_id").format(
            sql.SQL(", ").join(map(sql.Identifier, cols)), sql.SQL(", ").join(map(sql.Placeholder, cols))),
            {"case_id": case_id, **values})
    else:
        query = sql.SQL("UPDATE cairn.notification_preferences SET {} WHERE case_id = {} RETURNING case_id")
        saved = s.one(query.format(
            sql.SQL(", ").join(sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder(k)) for k in values),
            sql.Placeholder("case_id")), {"case_id": case_id, **values})
    if saved is None:
        raise case_access_denied()
    # Opaque ids only. Never the choice or the address.
    s.audit("notification_preferences_saved", case_id, "case", case_id)
    return load(s, case_id)


def ensure_default(s: Session, case_id: UUID) -> None:
    """Every started journey has a row. Skipped or never asked means in_app_only (UC-CASE-19 postconditions)."""
    s.conn.execute("INSERT INTO cairn.notification_preferences (case_id) VALUES (%s) ON CONFLICT (case_id) DO NOTHING",
                   (case_id,))


def owned_cases(s: Session) -> list[dict]:
    return s.all("SELECT id, status, created_at FROM cairn.cases WHERE created_by = cairn.current_user_id() "
                 "ORDER BY created_at")


def masked_account_email(account: dict) -> str:
    return acct.mask_email(account["email"])
