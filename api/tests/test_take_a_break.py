"""Take a break (database/docs/cairn-take-a-break-use-cases-v32.json, UC-BRK-01 to UC-BRK-12) and the home screen
(case UC-CASE-25), against a real MongoDB. Rules that need no database come first.

[SAFETY] items are release blockers. Fake data only.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from cairn_api import breaks, maintenance
from cairn_api.copy_store import load_break_copy, load_case_copy, load_copy
from cairn_api.main import create_app
from cairn_api.store import DEFAULT_NOTIFICATIONS

from .conftest import REPO, SETTINGS, active_case, answer, as_user, expire_trial, new_draft, register
from .test_account_lifecycle import ALWAYS, FakeMailer, send_outbound

SPEC = json.loads((REPO / "database" / "docs" / "cairn-take-a-break-use-cases-v32.json").read_text())
CASE_SPEC = json.loads((REPO / "database" / "docs" / "cairn-case-creation-use-cases-v32.json").read_text())
COPY = load_break_copy()
CASE_COPY = load_case_copy()
REG = load_copy()
UTC = timezone.utc


def user(api, subject):
    return api.db.users.find_one({"idp_subject": subject})


def take(api, subject, **body):
    r = api.post("/v1/me/break", json=body, headers=as_user(subject))
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ rules (no database)

def test_break_end_times():
    """BRK-D-07: the rest of today ends at 11:59 PM local time, a few days after 3, a week after 7, and Until I come
    back has no end."""
    start = datetime(2026, 10, 7, 15, 0, tzinfo=UTC)  # 11 AM in New York
    tonight = breaks.break_until("today", start, start, "America/New_York")
    assert tonight.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M") == "23:59"
    assert breaks.break_until("three_days", start, start, None) == start + timedelta(days=3)
    assert breaks.break_until("week", start, start, None) == start + timedelta(days=7)
    assert breaks.break_until("until_back", start, start, None) is None


def test_a_changed_break_counts_from_its_original_start():
    """UC-BRK-11."""
    start = datetime(2026, 10, 1, 12, tzinfo=UTC)
    later = start + timedelta(days=2)
    assert breaks.break_until("week", start, later, None) == start + timedelta(days=7)


def test_the_notice_is_24_hours_before_and_moves_out_of_quiet_hours():
    """UC-BRK-09 and BRK-D-03."""
    start = datetime(2026, 10, 1, 12, tzinfo=UTC)
    prefs = {**DEFAULT_NOTIFICATIONS}
    until = start + timedelta(days=3)  # noon UTC, 8 AM in New York
    assert breaks.notice_at(start, until, care=False, tz="UTC", prefs=prefs,
                            send_without_reminders=True) == until - timedelta(hours=24)
    late = datetime(2026, 10, 4, 3, 0, tzinfo=UTC)  # 11 PM in New York the night before
    notice = breaks.notice_at(start, late, care=False, tz="America/New_York", prefs=prefs,
                              send_without_reminders=True)
    assert notice.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M") == "08:00"  # end of quiet hours
    assert notice < late
    for until_ in (None, start + timedelta(hours=20)):
        assert breaks.notice_at(start, until_, care=False, tz="UTC", prefs=prefs, send_without_reminders=True) is None
    assert breaks.notice_at(start, until, care=True, tz="UTC", prefs=prefs, send_without_reminders=True) is None
    none = {**prefs, "frequency": "none"}
    assert breaks.notice_at(start, until, care=False, tz="UTC", prefs=none, send_without_reminders=True)  # OPEN-BRK-02
    assert breaks.notice_at(start, until, care=False, tz="UTC", prefs=none, send_without_reminders=False) is None


def test_break_copy_never_names_a_case_or_a_crisis():
    """UC-BRK-12 and UC-BRK-09 [PRIVACY]."""
    for key in ("break_ending_notice_subject", "break_ending_notice_body", "resting_screen", "break_started"):
        text = COPY[key].lower()
        for word in ("died", "death", "crisis", "suicide", "grief", "{display_name}"):
            assert word not in text, (key, word)


def test_every_signed_in_screen_offers_take_a_break_and_support_resources():
    """UC-BRK-01 over the API: every response a screen renders carries the control and the link. The view inventory
    test of the web client covers placement, focus, and target size."""
    spec = create_app(settings=SETTINGS).openapi()
    schemas = spec["components"]["schemas"]
    for name in ("OnboardingResponse", "WelcomeResponse", "HomeResponse"):
        assert "support" in schemas[name]["properties"], name
    assert "take_a_break_label" in schemas["Support"]["properties"]
    assert "support_resources_label" in schemas["Support"]["properties"]
    assert "take_a_break" in schemas["ScreenControls"]["properties"]
    assert "support_resources_label" in schemas["BreakResponse"]["properties"]


# ------------------------------------------------------------------ UC-BRK-03 and UC-BRK-04

def test_a_setup_break_saves_progress_shows_988_and_sends_nothing(api):
    """UC-BRK-03 and AC-BRK-03 [SAFETY]."""
    api.post("/v1/registrations", json={}, headers=as_user("bk01"))
    body = take(api, "bk01")
    assert body["screen"] == "S-02" and body["text"] == COPY["setup_break"]
    assert body["quiet_988_line"] == COPY["quiet_988_line"]
    assert body["support_resources_label"] == COPY["support_resources_link"]
    assert body["next_step"]["options"][0]["label"] == COPY["setup_break_resume"]
    u = user(api, "bk01")
    assert u["break_started_at"] is None and u["trial_started_at"] is None
    assert api.db.action_confirmation_outbox.count_documents({"user_id": u["_id"]}) == 0


def test_after_setup_with_no_case_and_in_a_draft(api):
    register(api, "bk02")
    assert take(api, "bk02")["screen"] == "S-02b"
    cid = new_draft(api, "bk02")["case"]["id"]
    body = take(api, "bk02", choice="week")  # a choice means nothing without a journey
    assert body["screen"] == "S-03" and body["text"] == COPY["draft_pause"]
    assert user(api, "bk02")["break_started_at"] is None  # drafts get no break fields and no notices (DEC-06)
    turn = api.post(f"/v1/cases/{cid}/take-a-break", json={}, headers=as_user("bk02")).json()
    assert turn["next_step"]["prompt"] == COPY["draft_pause"]


# ------------------------------------------------------------------ UC-BRK-05 to UC-BRK-07

def test_rest_choices_on_a_journey_say_the_free_days_keep_counting(api):
    register(api, "bk03")
    active_case(api, "bk03")
    body = take(api, "bk03")
    assert body["screen"] == "S-04" and body["text"] == COPY["rest_choices_question"]
    assert [c["label"] for c in body["choices"]] == [COPY["rest_choice_today"], COPY["rest_choice_3_days"],
                                                     COPY["rest_choice_7_days"], COPY["rest_choice_open"]]
    assert body["next_step"]["options"][-1]["label"] == COPY["rest_keep_going"]
    assert body["body"][0].startswith("Your free days keep counting while you rest. They end on ")
    assert COPY["rest_subscription_note"] not in body["body"]  # not subscribed


def test_a_normal_break_covers_every_journey_keeps_the_clock_and_schedules_one_notice(api):
    register(api, "bk04", time_zone="America/Chicago")
    one, _ = active_case(api, "bk04")
    two, _ = active_case(api, "bk04")
    draft = new_draft(api, "bk04")["case"]["id"]
    before = user(api, "bk04")["trial_ends_at"]
    body = take(api, "bk04", choice="three_days")
    u = user(api, "bk04")
    assert body["screen"] == "S-05" and body["state"]["on_break"] is True
    assert body["text"] == COPY["break_started"].format(break_end_date=body["state"]["end_date"])
    assert u["break_until"] - u["break_started_at"] == timedelta(days=3)
    assert u["trial_ends_at"] == before and u["trial_clock_paused_at"] is None  # AC-BRK-05
    assert u["break_notice_at"] is not None
    assert body["body"][0] == COPY["break_notice_line"].format(channels="email and a note here in Cairn")
    paused = {str(c["_id"]): c["tasks_paused_until"] for c in api.db.cases.find({"created_by": u["_id"]})}
    assert paused[one] == paused[two] == u["break_until"] and paused[draft] is None  # BRK-D-06


def test_the_rest_of_today_has_no_notice_and_ends_tonight(api):
    register(api, "bk05", time_zone="America/New_York")
    active_case(api, "bk05")
    body = take(api, "bk05", choice="today")
    u = user(api, "bk05")
    assert u["break_notice_at"] is None and body["body"] == []
    end = u["break_until"].astimezone(ZoneInfo("America/New_York"))
    assert end.strftime("%H:%M") == "23:59"


def test_until_i_come_back_is_open_ended(api):
    register(api, "bk06")
    active_case(api, "bk06")
    body = take(api, "bk06", choice="until_back")
    assert body["text"] == COPY["break_started_open_ended"] and body["state"]["until"] is None
    assert user(api, "bk06")["break_notice_at"] is None


def test_a_care_rest_pauses_the_free_days_says_so_once_and_schedules_no_notice(api):
    """UC-BRK-07: level 2 shows the care note once on a trial. Levels 3 and 4 show no trial or billing words."""
    register(api, "bk07")
    active_case(api, "bk07")
    level_2 = take(api, "bk07", care_level=2)
    assert level_2["body"] == [COPY["rest_care_clock_note"]]
    level_3 = take(api, "bk07", session={"safety_mode": "acute_distress"})
    assert level_3["body"] == [] and "free days" not in json.dumps(level_3).lower()
    take(api, "bk07", choice="week", care_level=2)
    u = user(api, "bk07")
    assert u["trial_clock_paused_at"] is not None and u["break_notice_at"] is None  # BRK-D-03, AC-BRK-08


def test_a_subscriber_hears_the_subscription_keeps_going_at_level_1_only(api):
    """BRK-D-08 on S-04 and S-05."""
    register(api, "bk08")
    active_case(api, "bk08")
    expire_trial(api, "bk08")
    api.db.users.update_one({"idp_subject": "bk08"}, {"$set": {"subscription_status": "active"}})
    assert COPY["rest_subscription_note"] in take(api, "bk08")["body"]
    assert take(api, "bk08", care_level=2)["body"] == []
    started = take(api, "bk08", choice="week")
    assert COPY["rest_subscription_note"] in started["body"]


# ------------------------------------------------------------------ UC-BRK-08 to UC-BRK-12

def test_opening_cairn_during_a_break_shows_the_resting_screen_first_and_asks_nothing(api):
    register(api, "bk09")
    cid, _ = active_case(api, "bk09")
    take(api, "bk09", choice="week")
    home = api.get("/v1/home", headers=as_user("bk09")).json()
    assert home["route"] == "resting" and home["cases"] == []
    resting = home["resting"]
    assert resting["screen"] == "S-06" and resting["text"].startswith("You're on a break until ")
    assert [o["value"] for o in resting["next_step"]["options"]] == ["keep_resting", "back", "change"]
    assert resting["current_choice"] == "week"
    # Break screens show nothing from a case except the end date (UC-BRK-12).
    assert "your loved one" not in json.dumps(resting).lower()
    # Looking at a case keeps the break (UC-BRK-08).
    assert api.get(f"/v1/cases/{cid}/journey", headers=as_user("bk09")).status_code == 200
    assert user(api, "bk09")["break_started_at"] is not None


def test_editing_a_task_during_a_break_ends_it_first(api):
    register(api, "bk10")
    cid, journey = active_case(api, "bk10")
    take(api, "bk10", choice="week")
    task = journey["next_action"]["id"]
    r = api.patch(f"/v1/cases/{cid}/tasks/{task}", json={"status": "in_progress"}, headers=as_user("bk10"))
    assert r.status_code == 200
    assert user(api, "bk10")["break_started_at"] is None
    assert api.db.cases.find_one({"_id": UUID(cid)})["tasks_paused_until"] is None


def test_changing_how_long_never_sends_two_notices(api):
    """UC-BRK-11."""
    register(api, "bk11")
    active_case(api, "bk11")
    take(api, "bk11", choice="week")
    api.db.users.update_one({"idp_subject": "bk11"}, {"$set": {"break_notice_at": datetime.now(UTC)
                                                               - timedelta(minutes=1)}})
    mine = lambda: [r for r in maintenance.claim_break_notices(api.jobs_db) if r["email"].startswith("bk11")]  # noqa
    assert len(mine()) == 1
    r = api.put("/v1/me/break", json={"choice": "three_days"}, headers=as_user("bk11")).json()
    assert r["screen"] == "S-05"
    u = user(api, "bk11")
    assert u["break_until"] - u["break_started_at"] == timedelta(days=3)  # from the original start
    assert mine() == []
    assert api.put("/v1/me/break", json={"choice": "week"}, headers=as_user("bk11")).status_code == 200
    api.delete("/v1/me/break", headers=as_user("bk11"))
    assert api.put("/v1/me/break", json={"choice": "week"}, headers=as_user("bk11")).status_code == 409


def test_coming_back_clears_everything_and_never_asks_why(api):
    """UC-BRK-10."""
    register(api, "bk12")
    cid, _ = active_case(api, "bk12")
    take(api, "bk12", choice="until_back", care_level=3)
    u = user(api, "bk12")
    api.db.users.update_one({"_id": u["_id"]}, {"$set": {
        "trial_clock_paused_at": u["trial_clock_paused_at"] - timedelta(days=3),
        "break_started_at": u["break_started_at"] - timedelta(days=3)}})
    back = api.delete("/v1/me/break", headers=as_user("bk12")).json()
    assert back["screen"] == "S-07" and back["text"].startswith("Welcome back, Pat. Here's where we left off: ")
    assert back["body"][0].startswith("Your free days were paused while you rested. They now end on ")
    assert "why" not in json.dumps(back).lower()
    after = user(api, "bk12")
    assert after["break_started_at"] is None and after["trial_clock_paused_at"] is None
    assert after["trial_ends_at"] - u["trial_ends_at"] >= timedelta(days=3)
    assert api.db.cases.find_one({"_id": UUID(cid)})["tasks_paused_until"] is None


def test_a_break_that_ended_while_away_resumes_quietly(api):
    """UC-BRK-10 alternate flow: reminders resume on their normal schedule, with no catch-up burst."""
    register(api, "bk13")
    active_case(api, "bk13")
    take(api, "bk13", choice="three_days")
    past = datetime.now(UTC) - timedelta(hours=1)
    api.db.users.update_one({"idp_subject": "bk13"}, {"$set": {"break_started_at": past - timedelta(days=3),
                                                               "break_until": past}})
    home = api.get("/v1/home", headers=as_user("bk13")).json()
    assert home["route"] == "home" and user(api, "bk13")["break_started_at"] is None


def test_the_break_ending_notice_goes_by_the_chosen_channels_once_and_privately(api):
    """UC-BRK-09 and AC-BRK-04."""
    register(api, "bk14")
    active_case(api, "bk14", {"display_name": "Zebulon", "place_of_death": {"jurisdiction": "NH"}})
    api.patch("/v1/me/notification-preferences", json=ALWAYS, headers=as_user("bk14"))
    take(api, "bk14", choice="week")
    api.db.users.update_one({"idp_subject": "bk14"}, {"$set": {"break_notice_at": datetime.now(UTC)
                                                               - timedelta(minutes=1)}})
    mailer = FakeMailer()
    send_outbound(api, mailer)
    send_outbound(api, mailer)
    sent = [m for m in mailer.to("bk14@example.test") if m[0] == COPY["break_ending_notice_subject"]
            and m[1] == COPY["break_ending_notice_body"]]
    assert len(sent) == 1 and "Zebulon" not in json.dumps(mailer.sent)
    resting = api.get("/v1/me/break", headers=as_user("bk14")).json()
    assert COPY["break_notice_in_app"] in resting["body"]  # shown in Cairn too


def test_signing_out_never_ends_or_changes_a_break(api):
    """UC-BRK-12."""
    register(api, "bk15")
    active_case(api, "bk15")
    take(api, "bk15", choice="week")
    before = user(api, "bk15")["break_until"]
    api.post("/v1/me/sign-out", headers=as_user("bk15"))
    assert user(api, "bk15")["break_until"] == before


# ------------------------------------------------------------------ the home screen (UC-CASE-25)

def test_home_with_no_cases_has_one_action(api):
    register(api, "hm01")
    home = api.get("/v1/home", headers=as_user("hm01")).json()
    assert home["greeting"] == REG["home_greeting"].format(preferred_name="Pat")
    assert home["next_step"]["prompt"] == CASE_COPY["empty_state"]
    assert [o["label"] for o in home["next_step"]["options"]] == [CASE_COPY["empty_state_button"]]
    assert home["always_visible"] == ["Take a break", REG["support_resources_link"], "Settings", "Sign out",
                                      "AI guide", REG["read_this_to_me"]]


def test_home_cards_for_a_draft_and_a_journey_never_put_the_name_in_the_title(api):
    """[PRIVACY] The page title and browser tab never show display_name."""
    register(api, "hm02")
    active_case(api, "hm02", {"display_name": "Zebulon", "place_of_death": {"jurisdiction": "NH"}})
    draft = new_draft(api, "hm02")["case"]["id"]
    answer(api, "hm02", draft, "display_name", "Quincy")
    home = api.get("/v1/home", headers=as_user("hm02")).json()
    assert home["page_title"] == REG["home_page_title"] and "Zebulon" not in home["page_title"]
    cards = {c["display_name"]: c for c in home["cases"]}
    assert cards["Quincy"]["draft_notice"] == COPY["draft_pause"] and cards["Quincy"]["draft_expires_at"]
    assert [a["value"] for a in cards["Quincy"]["actions"]] == ["keep_going", "delete"]
    assert cards["Zebulon"]["next_task"].startswith("Next: ")
    assert cards["Zebulon"]["trial_line"].startswith("Your free days end on ")
    at_3 = api.get("/v1/home", params={"care_level": 3}, headers=as_user("hm02")).json()
    assert all(c["trial_line"] is None for c in at_3["cases"])  # [SAFETY] no trial wording at levels 3 and 4


def test_a_returning_sign_in_to_only_a_draft_resumes_it(api):
    """UC-REG-18: a draft case goes to UC-CASE-10 (UC-BRK-04)."""
    register(api, "hm03")
    new_draft(api, "hm03")
    home = api.get("/v1/home", params={"session_start": True}, headers=as_user("hm03")).json()
    assert home["route"] == "resume_draft"
    assert home["greeting"] == REG["signin_welcome_back"].format(preferred_name="Pat")


def test_setup_not_finished_resumes_setup(api):
    api.post("/v1/registrations", json={}, headers=as_user("hm04"))
    home = api.get("/v1/home", headers=as_user("hm04")).json()
    assert home["route"] == "resume_setup" and home["next_step"]["action"] == "resume_onboarding"


def test_a_check_in_shows_once_at_home(api):
    register(api, "hm05")
    api.db.users.update_one({"idp_subject": "hm05"}, {"$set": {"check_in_at": datetime.now(UTC)
                                                               - timedelta(minutes=1)}})
    first = api.get("/v1/home", headers=as_user("hm05")).json()
    assert [n["text"] for n in first["notes"] if n["kind"] == "crisis"] == [CASE_COPY["check_in_in_cairn"]]
    again = api.get("/v1/home", headers=as_user("hm05")).json()
    assert not [n for n in again["notes"] if n["kind"] == "crisis"]


def test_the_session_start_ai_reminder_shows_on_a_new_day(api):
    """UC-CASE-23: every signed-in session. UC-REG-09 counted as the reminder on setup day."""
    register(api, "hm06")
    assert api.get("/v1/home", params={"session_start": True}, headers=as_user("hm06")).json()["ai_reminder"] is None
    yesterday = datetime.now(UTC) - timedelta(days=1)
    api.db.users.update_one({"idp_subject": "hm06"}, {"$set": {"ai_reminder_shown_on": yesterday.date().isoformat(),
                                                               "ai_reminder_shown_at": yesterday}})
    home = api.get("/v1/home", params={"session_start": True}, headers=as_user("hm06")).json()
    assert home["ai_reminder"] == CASE_COPY["ai_reminder"]


@pytest.mark.parametrize("path", ["/v1/home", "/v1/me/break"])
def test_new_signed_in_routes_need_a_signed_in_user(contract_client, path):
    assert contract_client.get(path).status_code == 401
