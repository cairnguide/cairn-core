"""Take a break (database/docs/cairn-take-a-break-use-cases-v32.json).

One control, labeled Take a break, on every view (BRK-D-01). What it opens
depends on where the user is:

  S-01   before sign-in. Nothing saved, nothing sent (UC-BRK-02).
  S-02   during account setup. Progress is already saved (UC-BRK-03).
  S-02b  after setup with no case yet.
  S-03   in a draft case. Saves where they left off (UC-BRK-04).
  S-04   on an active journey: the four rest choices (UC-BRK-05, UC-BRK-07).
  S-05   the break has started.
  S-06   the resting screen, shown first whenever Cairn is opened during a break (UC-BRK-08).
  S-07   back from a break (UC-BRK-10).

A break covers the person, not one case (BRK-D-06): it is stored on the account,
and every active journey's tasks_paused_until follows it. No break type, reason,
or care level is stored. A care rest (levels 2 to 4) also stops the free days
while they are running (DEC-26-01). No break of any kind changes a subscription,
and nothing here calls Stripe (BRK-D-08, AC-BRK-09).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import account as acct
from . import notifications as nt
from .copy_store import Copy
from .db import Session
from .schemas import BreakResponse, BreakState, NextStep, Option

CHOICES = ("today", "three_days", "week", "until_back")
CHOICE_COPY = {"today": "rest_choice_today", "three_days": "rest_choice_3_days", "week": "rest_choice_7_days",
               "until_back": "rest_choice_open"}
NOTICE_BEFORE = timedelta(hours=24)


def break_until(choice: str, started: datetime, now: datetime, tz: str | None) -> datetime | None:
    """BRK-D-07. The rest of today ends at 11:59 PM in the user's time zone. A few days and a week count from the
    break's start (so a change in UC-BRK-11 keeps the original start). Until I come back has no end."""
    if choice == "until_back":
        return None
    if choice == "today":
        local = now.astimezone(ZoneInfo(tz or "UTC"))
        return local.replace(hour=23, minute=59, second=0, microsecond=0).astimezone(timezone.utc)
    return started + timedelta(days=3 if choice == "three_days" else 7)


def choice_of(state: dict) -> str | None:
    """Which of the four choices the stored break matches, for marking it on S-04 (UC-BRK-11)."""
    if state.get("break_started_at") is None:
        return None
    until = state.get("break_until")
    if until is None:
        return "until_back"
    days = (until - state["break_started_at"]).total_seconds() / 86400
    return "week" if days >= 6.5 else "three_days" if days >= 2.5 else "today"


def notice_at(started: datetime, until: datetime | None, *, care: bool, tz: str | None, prefs: dict | None,
              send_without_reminders: bool) -> datetime | None:
    """UC-BRK-09. One notice 24 hours before a normal break that is longer than 24 hours, moved to the end of quiet
    hours if it falls inside them. None for the rest of today, Until I come back, and a care rest (BRK-D-03). With
    frequency none it is still sent unless OPEN-BRK-02 decides otherwise."""
    if care or until is None or until - started <= NOTICE_BEFORE:
        return None
    if prefs is not None and prefs["frequency"] == "none" and not send_without_reminders:
        return None
    return nt.after_quiet_hours(until - NOTICE_BEFORE, tz, prefs)


def notice_channels(prefs: dict | None) -> list[str]:
    """Every channel the user selected, and no other (UC-BRK-09). In-app always shows it."""
    channels = (prefs or {"channels": ["in_app"]})["channels"]
    return [c for c in ("email", "browser", "in_app") if c in channels]


# ------------------------------------------------------------------ screens

def _end_text(copy: Copy, account: dict, until: datetime | None) -> str | None:
    if until is None:
        return None
    local = until.astimezone(ZoneInfo(account.get("time_zone") or "UTC"))
    if local.date() == datetime.now(timezone.utc).astimezone(local.tzinfo).date():
        return copy["break_end_today"]
    return acct.format_date(local.date())


def state_out(copy: Copy, account: dict, prefs: dict | None) -> BreakState:
    return BreakState(on_break=account["on_break"], started_at=account["break_started_at"],
                      until=account["break_until"], end_date=_end_text(copy, account, account["break_until"]),
                      notice_at=account["break_notice_at"],
                      notice_channels=notice_channels(prefs) if account["break_notice_at"] else [])


def _base(copy: Copy, screen: str, text: str, step: NextStep, *, signed_in: bool = True, **extra) -> BreakResponse:
    """BRK-D-04. After sign-in, break screens show the quiet 988 line and the Support resources link."""
    return BreakResponse(screen=screen, text=text, next_step=step, support_resources_label=copy[
        "support_resources_link"], quiet_988_line=copy["quiet_988_line"] if signed_in else None, **extra)


def pre_sign_in(copy: Copy) -> BreakResponse:
    """S-01. Nothing saved, nothing sent. A magic link already sent stays valid (UC-BRK-02)."""
    return _base(copy, "S-01", copy["presignin_break"], NextStep(action="go_back", prompt=copy["presignin_break"],
                 options=[Option(value="go_back", label=copy["presignin_break_back"])]), signed_in=False)


def setup_pause(copy: Copy, *, has_setup_left: bool) -> BreakResponse:
    """S-02 during setup, S-02b after setup with no case. No timers, nudges, or reminders (UC-BRK-03)."""
    if has_setup_left:
        return _base(copy, "S-02", copy["setup_break"], NextStep(action="resume_setup", prompt=copy["setup_break"],
                     options=[Option(value="resume", label=copy["setup_break_resume"])]))
    return _base(copy, "S-02b", copy["signed_in_nothing_to_pause"], NextStep(
        action="resume", prompt=copy["signed_in_nothing_to_pause"],
        options=[Option(value="resume", label=copy["setup_break_resume"])]))


def draft_pause(copy: Copy) -> BreakResponse:
    """S-03. The draft is saved. The 28-day draft notice is said plainly (UC-BRK-04, DEC-06)."""
    return _base(copy, "S-03", copy["draft_pause"], NextStep(action="paused", prompt=copy["draft_resume_question"],
                 options=[Option(value="keep_going", label=copy["setup_break_resume"])]))


def rest_choices(copy: Copy, account: dict, *, care_level: int, current: str | None = None) -> BreakResponse:
    """S-04. Exactly four choices. On the free days, whether they keep counting. A subscriber hears that the
    subscription keeps going as normal, at level 1 only (BRK-D-08). Nothing about the trial or billing at levels 3
    and 4 (AC-26-03). At level 2 the care note shows only while the free days run (rest_care_clock_note_condition)."""
    body: list[str] = []
    if care_level == 1:
        if acct.on_free_days(account):
            body.append(copy["rest_normal_clock_note"].format(
                trial_end_date=acct.format_date(acct.local_trial_end(account))))
        if acct.has_subscription(account):
            body.append(copy["rest_subscription_note"])
    elif care_level == 2 and acct.on_free_days(account):
        body.append(copy["rest_care_clock_note"])
    choices = [Option(value=c, label=copy[CHOICE_COPY[c]]) for c in CHOICES]
    return _base(copy, "S-04", copy["rest_choices_question"], NextStep(
        action="choose_rest", prompt=copy["rest_choices_question"],
        options=[*choices, Option(value="keep_going", label=copy["rest_keep_going"])]),
        body=body, choices=choices, current_choice=current)


def started(copy: Copy, account: dict, prefs: dict | None, *, care_level: int) -> BreakResponse:
    """S-05. The end date in the user's time zone, or open-ended, and the notice line only when one is set."""
    state = state_out(copy, account, prefs)
    text = (copy["break_started"].format(break_end_date=state.end_date) if account["break_until"]
            else copy["break_started_open_ended"])
    body = []
    if account["break_notice_at"]:
        labels = {"email": copy["break_notice_channel_email"], "browser": copy["break_notice_channel_browser"],
                  "in_app": copy["break_notice_channel_in_app"]}
        names = [labels[c] for c in state.notice_channels]
        body.append(copy["break_notice_line"].format(
            channels=names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"))
    if care_level == 1 and acct.has_subscription(account):
        body.append(copy["rest_subscription_note"])
    return _base(copy, "S-05", text, NextStep(action="resting", prompt=text, options=[
        Option(value="keep_resting", label=copy["resting_keep"]), Option(value="back", label=copy["resting_back"])]),
        body=body, state=state)


def resting(copy: Copy, account: dict, prefs: dict | None) -> BreakResponse:
    """S-06. Shown first whenever Cairn is opened during a break. Never asks the user to explain or to return."""
    state = state_out(copy, account, prefs)
    text = (copy["resting_screen"].format(break_end_date=state.end_date) if account["break_until"]
            else copy["resting_screen_open_ended"])
    body = []
    if account["break_notice_at"] and account["break_notice_at"] <= datetime.now(timezone.utc):
        body.append(copy["break_notice_in_app"])
    return _base(copy, "S-06", text, NextStep(action="resting", prompt=text, options=[
        Option(value="keep_resting", label=copy["resting_keep"]), Option(value="back", label=copy["resting_back"]),
        Option(value="change", label=copy["resting_change"])]), body=body, state=state,
        current_choice=choice_of(account))


def back(copy: Copy, account: dict, *, preferred_name: str | None, last_step: str, paused: timedelta | None,
         care_level: int = 1) -> BreakResponse:
    """S-07. Where the user left off in one line, the free days note once if they were paused, and one clear next
    action. Never asks about the gap (UC-BRK-10)."""
    text = copy["welcome_back"].format(preferred_name=preferred_name or "", last_step=last_step)
    body = []
    if paused and care_level == 1 and acct.local_trial_end(account):
        body.append(copy["trial_resumed_after_care_rest"].format(
            trial_end_date=acct.format_date(acct.local_trial_end(account))))
    return _base(copy, "S-07", text, NextStep(action="continue", prompt=text, options=[
        Option(value="continue", label=copy["back_next_action"])]), body=body)


def has_active_journey(s: Session) -> bool:
    return any(c["status"] != "draft" for c in s.owned_cases())
