"""How Cairn keeps in touch, for the whole account (account D-13, UC-REG-15, UC-REG-17, case UC-CASE-19).

Stored in the notification_preferences collection, one document per account
(database/db/schema.py). Rules this module keeps:
- Channels and frequency are chosen at setup. Email and in-app are pre-selected (OPEN-11), and in-app is always
  on. Browser notifications only after the user chose them and the browser said yes.
- Due date lead time and inactivity notices are confirmed with the first journey (UC-CASE-19), with the OPEN-03
  defaults (3 days before, no inactivity notices) when skipped.
- Nothing outside the user's choices except service notices (D-14): sign-in links, confirmations of things the
  user did, the trial-ending note, an opted-in check-in, and the break-ending notice.
- Quiet hours (21:00 to 08:00 in the browser's time zone by default) hold everything but service notices.
- Changing preferences is always free, including on a read-only account (D-2026-09-25-F1).
- Never used for marketing or advertising (D-07). No phone number and no text messages (D-12, OPEN-05).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .copy_store import Copy
from .db import Session
from .schemas import NotificationChannelsIn, NotificationPreferencesOut
from .store import DEFAULT_NOTIFICATIONS

# The days each choice stands for.
LEAD_DAYS = {"day_before": 1, "three_days": 3, "one_week": 7}
INACTIVITY_DAYS = {"off": None, "three_days": 3, "one_week": 7, "two_weeks": 14}


def load(s: Session) -> dict | None:
    return s.notification_preferences()


def channels_from(req: NotificationChannelsIn) -> tuple[list[str], str | None]:
    """The channels the user chose, and the push endpoint. Browser stays only when the browser said yes. Denied
    or unsupported takes it off (UC-REG-15 alternate flows)."""
    browser = req.browser and req.browser_permission not in ("denied", "unsupported")
    channels = [c for c, on in (("email", req.email), ("in_app", True), ("browser", browser)) if on]
    return channels, req.browser_push_endpoint if browser else None


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"


def readback(copy: Copy, prefs: dict) -> str:
    """The choices in one line (UC-REG-17), from the account copy."""
    if prefs["frequency"] == "none":
        return copy["notify_readback_none"]
    labels = {"email": copy["notify_channel_email_label"], "browser": copy["notify_channel_browser_label"],
              "in_app": copy["notify_channel_inapp_label"]}
    by = _join([f"by {labels[c]}" if c != "in_app" else labels[c] for c in ("email", "browser", "in_app")
                if c in prefs["channels"]])
    return copy["notify_readback"].format(channels=by, frequency=copy[f"notify_frequency_{prefs['frequency']}_label"],
                                          quiet_start=_clock(prefs["quiet_hours_start"]),
                                          quiet_end=_clock(prefs["quiet_hours_end"]))


def _clock(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    suffix = "AM" if h < 12 else "PM"
    return f"{(h % 12) or 12}:{m:02d} {suffix}" if m else f"{(h % 12) or 12} {suffix}"


def out(row: dict | None, copy: Copy) -> NotificationPreferencesOut:
    prefs = row or {**DEFAULT_NOTIFICATIONS, "channels": ["in_app"], "frequency": "none"}
    return NotificationPreferencesOut(
        stored=row is not None, channels=prefs["channels"], frequency=prefs["frequency"],
        quiet_hours_start=prefs["quiet_hours_start"], quiet_hours_end=prefs["quiet_hours_end"],
        browser_notifications_on="browser" in prefs["channels"] and bool(prefs["browser_push_endpoint"]),
        due_date_lead=prefs["due_date_lead"], inactivity_after=prefs["inactivity_after"],
        journey_confirmed=bool(prefs.get("journey_confirmed_at")), readback=readback(copy, prefs),
        updated_at=row["updated_at"] if row else None)


# ------------------------------------------------------------------ quiet hours

def _local(now: datetime, tz: str | None) -> datetime:
    return now.astimezone(ZoneInfo(tz or "UTC"))


def in_quiet_hours(now: datetime, tz: str | None, prefs: dict | None) -> bool:
    """Quiet hours can wrap past midnight (21:00 to 08:00)."""
    p = prefs or DEFAULT_NOTIFICATIONS
    start, end = (time.fromisoformat(p[k]) for k in ("quiet_hours_start", "quiet_hours_end"))
    t = _local(now, tz).time()
    if start == end:
        return False
    return start <= t or t < end if start > end else start <= t < end


def after_quiet_hours(when: datetime, tz: str | None, prefs: dict | None) -> datetime:
    """The same time, or the end of quiet hours if it falls inside them (UC-BRK-09)."""
    if not in_quiet_hours(when, tz, prefs):
        return when
    p = prefs or DEFAULT_NOTIFICATIONS
    end = time.fromisoformat(p["quiet_hours_end"])
    local = _local(when, tz)
    candidate = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(when.tzinfo)
