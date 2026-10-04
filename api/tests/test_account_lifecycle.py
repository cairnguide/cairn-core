"""UC-REG-15, UC-REG-16, and UC-CASE-19 to UC-CASE-21 against a real MongoDB database, as the cairnApp user.

Specs: database/docs/cairn-account-use-cases-2026-09-25.json and
database/docs/cairn-case-creation-use-cases-2026-09-25.json. Case deletion with a
7-day hold is UC-END-13, which those specs rely on. Each test names what it covers.
Rules that need no database are in test_account_lifecycle_rules.py.

Skipped unless CAIRN_TEST_MONGODB_URI points at a scratch MongoDB replica set.
Fake data only.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import pytest

from cairn_api import maintenance, outbound
from cairn_api.copy_store import load_case_copy, load_copy

from .conftest import active_case, answer, as_user, expire_trial, new_draft, register, start_journey, user_id

COPY = load_copy()
CASE_COPY = load_case_copy()
KEEP_IT_SIMPLE = {"preset": "keep_it_simple"}
EMAIL_WEEKLY = {"choice": {"channels": ["email"], "reasons": ["inactivity"], "inactivity_days": 7,
                           "frequency": "weekly_max"}}


def prefs_row(api, case_id):
    row = api.db.notification_preferences.find_one({"_id": UUID(case_id)})
    if row is None:
        return None
    return (row["channels"], row["reasons"], row["due_date_lead_days"], row["inactivity_days"], row["frequency"],
            row["push_permission_granted"])


def set_prefs(api, subject, case_id, body):
    r = api.put(f"/v1/cases/{case_id}/notification-preferences", json=body, headers=as_user(subject))
    assert r.status_code == 200, r.text
    return r.json()


def outbox(api, email):
    return [(r["action_type"], r["user_id"])
            for r in api.db.action_confirmation_outbox.find({"email": email}, sort=[("queued_at", 1)])]


class FakeMailer:
    def __init__(self, fail_for: set[str] | None = None):
        self.sent: list[tuple[str, str, str]] = []
        self.fail_for = fail_for or set()

    def send(self, to, subject, body):
        if to in self.fail_for:
            raise OSError("connection refused")
        self.sent.append((to, subject, body))

    def to(self, email):
        return [(s, b) for t, s, b in self.sent if t == email]


def send_outbound(api, mailer):
    return outbound.run_once(api.jobs_db, mailer, COPY, CASE_COPY)


def due_tomorrow(api, case_id):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    api.db.case_tasks.update_many({"case_id": UUID(case_id)}, {"$set": {"due_on": tomorrow}})


def add_conversation(api, case_id, text):
    api.db.context_items.insert_one({"case_id": UUID(case_id), "item_key": "CONVO_SUMMARY", "payload": {"text": text},
                                     "updated_at": datetime.now(timezone.utc), "expires_at": None})


def named_case(api, subject, name="Dan", **answers):
    return active_case(api, subject, {"display_name": name, "place_of_death": {"jurisdiction": "NH"}, **answers})


# ------------------------------------------------------------------ UC-CASE-19 choose how Cairn keeps in touch

def test_uc19_setup_explains_then_offers_shortcuts_and_questions_in_order(api):
    register(api, "nt01")
    cid = new_draft(api, "nt01")["case"]["id"]
    setup = api.get(f"/v1/cases/{cid}/notification-preferences", headers=as_user("nt01")).json()
    assert setup["explanation"] == CASE_COPY["notifications_intro"]
    assert "change this any time" in setup["explanation"]
    assert [q["id"] for q in setup["questions"]] == ["channels", "reasons", "due_date_lead_days", "inactivity_days",
                                                     "frequency"]
    channels = setup["questions"][0]
    assert channels["multi_select"] and [o["value"] for o in channels["options"]] == ["email", "push", "in_app_only"]
    assert "sms" not in json.dumps(setup)  # open question card 50, not offered
    assert setup["masked_email"] == "n•••1@example.test" and setup["masked_email"] in channels["options"][0]["label"]
    assert [o["value"] for o in setup["shortcuts"]] == ["keep_it_simple", "set_up", "skip"]
    assert setup["shortcuts"][0]["label"] == "Keep it simple for me"
    # Nothing chosen yet: effectively in_app_only, and nothing is stored.
    assert setup["preferences"]["stored"] is False and setup["preferences"]["channels"] == ["in_app_only"]
    assert prefs_row(api, cid) is None


def test_uc19_readback_before_saving_stores_nothing(api):
    register(api, "nt02")
    cid = new_draft(api, "nt02")["case"]["id"]
    body = {"choice": {"channels": ["email"], "reasons": ["due_date_upcoming", "inactivity"],
                       "due_date_lead_days": 1, "inactivity_days": 14, "frequency": "weekly_max"}}
    r = api.post(f"/v1/cases/{cid}/notification-preferences/readback", json=body, headers=as_user("nt02")).json()
    assert r["preferences"]["readback"] == ("Cairn will reach you by email the day before a step is due and when you "
                                            "haven't been in Cairn for 14 days, no more than once a week.")
    assert r["next_step"]["prompt"].endswith("Does that sound right?")
    assert prefs_row(api, cid) is None
    saved = set_prefs(api, "nt02", cid, body)
    assert saved["preferences"]["stored"] is True
    assert prefs_row(api, cid) == (["email"], ["due_date_upcoming", "inactivity"], 1, 14, "weekly_max", False)
    assert saved["next_step"]["action"] == "preview_journey"  # a draft goes back to Start journey


def test_uc19_keep_it_simple_uses_the_draft_preset(api):
    register(api, "nt03")
    cid = new_draft(api, "nt03")["case"]["id"]
    set_prefs(api, "nt03", cid, KEEP_IT_SIMPLE)
    assert prefs_row(api, cid) == (["email"], ["due_date_upcoming"], 3, None, "daily_max", False)


def test_uc19_skipped_or_never_asked_is_in_app_only_and_nothing_is_sent(api):
    """D-2026-09-25-N2 and the skipped_or_in_app_only flow."""
    register(api, "nt04")
    cid = new_draft(api, "nt04")["case"]["id"]
    set_prefs(api, "nt04", cid, {"preset": "skip"})
    assert prefs_row(api, cid)[0] == ["in_app_only"]
    # Starting a journey with no choice at all stores in_app_only too.
    cid2, _ = named_case(api, "nt04")
    assert prefs_row(api, cid2) == (["in_app_only"], [], None, None, "daily_max", False)
    due_tomorrow(api, cid2)
    mailer = FakeMailer()
    send_outbound(api, mailer)
    assert mailer.to("nt04@example.test") == []


def test_uc19_invalid_choices_are_refused(api):
    register(api, "nt05")
    cid = new_draft(api, "nt05")["case"]["id"]
    for choice in ({"channels": ["in_app_only", "email"]},
                   {"channels": ["email"]},                                       # no reason
                   {"channels": ["sms"], "reasons": ["inactivity"], "inactivity_days": 7},
                   {"channels": ["email"], "reasons": ["due_date_upcoming"]},     # no lead time
                   {"channels": ["email"], "reasons": ["inactivity"], "inactivity_days": 7, "due_date_lead_days": 3},
                   {"channels": ["email"], "reasons": ["inactivity"], "inactivity_days": 5},
                   {"channels": ["email", "email"], "reasons": ["inactivity"], "inactivity_days": 7}):
        r = api.put(f"/v1/cases/{cid}/notification-preferences", json={"choice": choice}, headers=as_user("nt05"))
        assert r.status_code == 422, choice
    assert prefs_row(api, cid) is None


def test_uc19_push_prompt_only_after_push_is_chosen_and_never_required(api):
    register(api, "nt06")
    cid = new_draft(api, "nt06")["case"]["id"]
    r = set_prefs(api, "nt06", cid, KEEP_IT_SIMPLE)
    assert r["next_step"]["action"] != "request_push_permission"
    body = {"choice": {"channels": ["email", "push"], "reasons": ["inactivity"], "inactivity_days": 3}}
    r = set_prefs(api, "nt06", cid, body)
    assert r["next_step"]["action"] == "request_push_permission"
    # Declined: push comes off, email stays, and the readback no longer mentions the phone.
    r = api.post(f"/v1/cases/{cid}/notification-preferences/push-permission", json={"granted": False},
                 headers=as_user("nt06")).json()
    assert r["preferences"]["channels"] == ["email"] and "phone" not in r["preferences"]["readback"]
    assert r["acknowledgment"] == CASE_COPY["notifications_push_declined"]
    # Push only, declined: nothing outside the app.
    set_prefs(api, "nt06", cid, {"choice": {**body["choice"], "channels": ["push"]}})
    r = api.post(f"/v1/cases/{cid}/notification-preferences/push-permission", json={"granted": False},
                 headers=as_user("nt06")).json()
    assert r["preferences"]["channels"] == ["in_app_only"] and r["preferences"]["reasons"] == []
    # Granted is recorded.
    set_prefs(api, "nt06", cid, {"choice": {**body["choice"], "channels": ["push"]}})
    r = api.post(f"/v1/cases/{cid}/notification-preferences/push-permission", json={"granted": True},
                 headers=as_user("nt06")).json()
    assert r["preferences"]["push_permission_granted"] is True
    assert r["next_step"]["action"] == "preview_journey"


def test_uc19_email_is_shown_masked_and_works_with_apple_private_relay(api):
    subject = "apple|nt07"
    relay = "x7k2mq9zp4@privaterelay.appleid.com"
    r = api.post("/v1/registrations", json={}, headers=as_user(subject, relay, method="apple"))
    assert r.status_code in (200, 201), r.text
    from .conftest import onboard
    onboard(api, subject, method="apple")
    h = as_user(subject, relay, method="apple")
    cid = api.post("/v1/cases", headers=h).json()["case"]["id"]
    setup = api.get(f"/v1/cases/{cid}/notification-preferences", headers=h).json()
    assert setup["masked_email"] == "x•••4@privaterelay.appleid.com"
    assert relay not in json.dumps(setup)


def test_uc12_confirmation_says_the_reminder_goes_the_way_the_user_chose(api):
    """UC-CASE-12 change: the reminder always shows in Cairn, and outside it only by the chosen channel."""
    register(api, "nt08")
    cid = new_draft(api, "nt08")["case"]["id"]
    set_prefs(api, "nt08", cid, KEEP_IT_SIMPLE)
    started = start_journey(api, "nt08", cid)
    assert started["confirmation"].endswith(
        "a few days before your free time ends, and we'll also send it by email.")
    assert "They keep counting if you pause" in CASE_COPY["pre_button_notice"]


def test_uc12_confirmation_without_an_outside_channel_only_mentions_cairn(api):
    """UC-CASE-12: with in-Cairn only, the clause is empty and nothing promises an email."""
    register(api, "nt08b")
    cid = new_draft(api, "nt08b")["case"]["id"]
    started = start_journey(api, "nt08b", cid)
    assert started["confirmation"].endswith("a reminder here in Cairn a few days before your free time ends.")
    assert "email" not in started["confirmation"]


def test_uc19_trial_reminder_email_only_with_an_email_choice(api):
    register(api, "nt09")
    in_app, _ = named_case(api, "nt09")
    api.db.trial_reminders.update_one({"user_id": user_id(api, "nt09"), "kind": "trial_day_21"},
                                      {"$set": {"due_at": datetime.now(timezone.utc) - timedelta(minutes=1)}})
    mailer = FakeMailer()
    send_outbound(api, mailer)
    assert mailer.to("nt09@example.test") == []
    # Still shown in Cairn.
    me = api.get("/v1/me", headers=as_user("nt09")).json()
    assert me["notes"] and me["notes"][0]["kind"] == "reminder"
    set_prefs(api, "nt09", in_app, KEEP_IT_SIMPLE)
    send_outbound(api, mailer)
    [body] = [b for s, b in mailer.to("nt09@example.test") if s == COPY["email_subject_trial"]]
    assert "free time" in body


def test_uc19_notifications_are_short_private_and_at_the_chosen_pace(api):
    register(api, "nt10")
    cid, _ = named_case(api, "nt10", name="Zebulon", veteran_status="yes", circumstance="accident_or_unexpected")
    set_prefs(api, "nt10", cid, KEEP_IT_SIMPLE)
    due_tomorrow(api, cid)
    mailer = FakeMailer()
    send_outbound(api, mailer)
    [(subject, body)] = mailer.to("nt10@example.test")  # one message for the journey, not one per task
    assert body == "You have a step coming up in Cairn."
    for private in ("Zebulon", "accident", "veteran", "certificate", "Social Security"):
        assert private.lower() not in (subject + body).lower()
    send_outbound(api, mailer)  # daily_max
    assert len(mailer.to("nt10@example.test")) == 1
    sent = [(r["reason"], r["channel"]) for r in api.db.notification_log.find({"case_id": UUID(cid)})]
    assert sent == [("due_date_upcoming", "email")]


def test_uc18_offers_the_same_as_the_first_case_first_and_each_journey_keeps_its_own(api):
    register(api, "nt11")
    first, _ = named_case(api, "nt11", name="Dan")
    set_prefs(api, "nt11", first, EMAIL_WEEKLY)
    second = new_draft(api, "nt11")["case"]["id"]
    setup = api.get(f"/v1/cases/{second}/notification-preferences", headers=as_user("nt11")).json()
    assert setup["shortcuts"][0] == {"value": f"same_as:{first}", "label": "Use the same as Dan",
                                     "available": True, "unavailable_reason": None}
    set_prefs(api, "nt11", second, {"preset": "same_as", "same_as_case_id": first})
    assert prefs_row(api, second) == prefs_row(api, first)
    set_prefs(api, "nt11", second, {"preset": "skip"})
    assert prefs_row(api, first)[0] == ["email"]


def test_uc19_another_user_cannot_read_set_or_copy_preferences(api):
    register(api, "nt12a")
    register(api, "nt12b")
    theirs = new_draft(api, "nt12a")["case"]["id"]
    set_prefs(api, "nt12a", theirs, EMAIL_WEEKLY)
    mine = new_draft(api, "nt12b")["case"]["id"]
    h = as_user("nt12b")
    assert api.get(f"/v1/cases/{theirs}/notification-preferences", headers=h).status_code == 403
    assert api.put(f"/v1/cases/{theirs}/notification-preferences", json=KEEP_IT_SIMPLE, headers=h).status_code == 403
    r = api.put(f"/v1/cases/{mine}/notification-preferences", json={"preset": "same_as", "same_as_case_id": theirs},
                headers=h)
    assert r.status_code == 403
    assert prefs_row(api, theirs)[0] == ["email"]


# ------------------------------------------------------------------ UC-CASE-20 change how Cairn keeps in touch

def test_uc20_stop_everything_is_one_step_on_every_journey(api):
    register(api, "nt20")
    a, _ = named_case(api, "nt20", name="Dan")
    b, _ = named_case(api, "nt20", name="Rosa")
    set_prefs(api, "nt20", a, KEEP_IT_SIMPLE)
    set_prefs(api, "nt20", b, EMAIL_WEEKLY)
    r = api.post("/v1/me/notification-preferences/changes", json={"stop_everything": True},
                 headers=as_user("nt20")).json()
    assert r["applied"] is True and r["acknowledgment"] == CASE_COPY["notifications_stopped"]
    assert r["next_step"]["action"] == "done" and not r["next_step"].get("options")  # no follow-up question
    assert "?" not in r["acknowledgment"]
    assert prefs_row(api, a)[0] == prefs_row(api, b)[0] == ["in_app_only"]


def test_uc20_a_change_is_read_back_in_one_line_and_applied_on_yes(api):
    register(api, "nt21")
    cid, journey_before = named_case(api, "nt21")
    body = {"choice": {"channels": ["email"], "reasons": ["inactivity"], "inactivity_days": 3,
                       "frequency": "as_it_happens"}}
    r = api.post("/v1/me/notification-preferences/changes", json=body, headers=as_user("nt21")).json()
    assert r["applied"] is False and r["readback"].count(".") == 1
    assert r["next_step"]["prompt"] == f"{r['readback']} Should I make this change?"
    assert prefs_row(api, cid)[0] == ["in_app_only"]
    r = api.post("/v1/me/notification-preferences/changes", json={**body, "confirm": True},
                 headers=as_user("nt21")).json()
    assert r["applied"] is True and prefs_row(api, cid)[:2] == (["email"], ["inactivity"])
    # Turning notifications on or off never changes the journey itself.
    api.post("/v1/me/notification-preferences/changes", json={"stop_everything": True}, headers=as_user("nt21"))
    after = api.get(f"/v1/cases/{cid}/journey", headers=as_user("nt21")).json()
    assert after["weeks"] == journey_before["weeks"]


def test_uc20_with_several_journeys_asks_which_by_name_and_offers_all(api):
    register(api, "nt22")
    a, _ = named_case(api, "nt22", name="Dan")
    b, _ = named_case(api, "nt22", name="Rosa")
    r = api.post("/v1/me/notification-preferences/changes", json={"choice": EMAIL_WEEKLY["choice"]},
                 headers=as_user("nt22")).json()
    assert r["applied"] is False and r["next_step"]["action"] == "choose_journeys"
    assert [o["label"] for o in r["next_step"]["options"]] == ["Dan", "Rosa", "All of them"]
    r = api.post("/v1/me/notification-preferences/changes",
                 json={"choice": EMAIL_WEEKLY["choice"], "scope": [b], "confirm": True}, headers=as_user("nt22")).json()
    assert r["applied"] and [j["display_name"] for j in r["journeys"]] == ["Rosa"]
    assert prefs_row(api, a)[0] == ["in_app_only"] and prefs_row(api, b)[0] == ["email"]
    listing = api.get("/v1/me/notification-preferences", headers=as_user("nt22")).json()
    assert [j["display_name"] for j in listing["journeys"]] == ["Dan", "Rosa"]


def test_uc20_always_free_on_a_read_only_account(api):
    register(api, "nt23")
    cid, _ = named_case(api, "nt23")
    set_prefs(api, "nt23", cid, KEEP_IT_SIMPLE)
    expire_trial(api, "nt23")
    assert api.get("/v1/me", headers=as_user("nt23")).json()["account"]["status"] == "read_only"
    r = api.post("/v1/me/notification-preferences/changes", json={"stop_everything": True}, headers=as_user("nt23"))
    assert r.status_code == 200 and prefs_row(api, cid)[0] == ["in_app_only"]
    set_prefs(api, "nt23", cid, EMAIL_WEEKLY)
    assert prefs_row(api, cid)[0] == ["email"]


def test_uc20_by_chat_stop_texting_me_and_email_me_instead(api):
    register(api, "nt24")
    cid, _ = named_case(api, "nt24")
    set_prefs(api, "nt24", cid, EMAIL_WEEKLY)
    h = as_user("nt24")
    r = api.post("/v1/me/messages", json={"text": "Stop texting me"}, headers=h).json()
    assert r["intent"] == "stop_notifications" and prefs_row(api, cid)[0] == ["in_app_only"]
    r = api.post("/v1/me/messages", json={"text": "Email me instead"}, headers=h).json()
    assert r["intent"] == "change_notifications" and prefs_row(api, cid)[0] == ["in_app_only"]  # not yet
    assert r["next_step"]["action"] == "confirm_notification_change"
    assert r["proposal"]["channels"] == ["email"]
    r = api.post("/v1/me/notification-preferences/changes", json={"choice": r["proposal"], "confirm": True},
                 headers=h).json()
    assert r["applied"] and prefs_row(api, cid)[0] == ["email"]
    r = api.post("/v1/me/messages", json={"text": "Can you text me instead?"}, headers=h).json()
    assert r["intent"] == "sms_not_available"
    # With two journeys, the switch asks which one first.
    named_case(api, "nt24", name="Rosa")
    r = api.post("/v1/me/messages", json={"text": "Email me instead"}, headers=h).json()
    assert r["next_step"]["action"] == "choose_journeys" and r["next_step"]["options"][-1]["label"] == "All of them"


# ------------------------------------------------------------------ UC-END-13 and UC-CASE-21 deleting a case

def test_uc21_where_the_confirmation_goes_is_shown_before_confirming(api):
    register(api, "dc01")
    cid = new_draft(api, "dc01")["case"]["id"]
    info = api.get(f"/v1/cases/{cid}/deletion", headers=as_user("dc01")).json()
    assert info["masked_email"] == "d•••1@example.test"
    assert info["masked_email"] in info["confirmation_destination"]
    assert [o["value"] for o in info["next_step"]["options"]] == ["now", "hold"]
    assert outbox(api, "dc01@example.test") == []


def test_uc21_delete_now_sends_exactly_one_private_confirmation_then_purges_the_address(api):
    register(api, "dc02")
    cid, _ = named_case(api, "dc02", name="Zebulon")
    r = api.post(f"/v1/cases/{cid}/deletion", json={"mode": "now"}, headers=as_user("dc02")).json()
    assert r["deleted"] is True and r["acknowledgment"] == CASE_COPY["case_deleted_now"]
    assert api.get(f"/v1/cases/{cid}", headers=as_user("dc02")).status_code == 403
    assert api.db.case_intake_answers.count_documents({"case_id": UUID(cid)}) == 0
    assert [a for a, _ in outbox(api, "dc02@example.test")] == ["case_deleted_now"]
    before = api.db.action_confirmation_log.count_documents({"action_type": "case_deleted_now"})
    mailer = FakeMailer()
    send_outbound(api, mailer)
    send_outbound(api, mailer)
    [(subject, body)] = mailer.to("dc02@example.test")
    assert body == COPY["email_case_deleted_now"] and "Zebulon" not in subject + body
    assert outbox(api, "dc02@example.test") == []  # the address is purged once sent
    after = api.db.action_confirmation_log.count_documents({"action_type": "case_deleted_now"})
    assert after > before


def test_uc21_a_failed_send_is_retried_and_still_sent_only_once(api):
    register(api, "dc03")
    cid = new_draft(api, "dc03")["case"]["id"]
    api.post(f"/v1/cases/{cid}/deletion", json={"mode": "now"}, headers=as_user("dc03"))
    down = FakeMailer(fail_for={"dc03@example.test"})
    out = send_outbound(api, down)
    assert out.failed.get("confirmations", 0) >= 1 and down.to("dc03@example.test") == []
    assert [a for a, _ in outbox(api, "dc03@example.test")] == ["case_deleted_now"]
    up = FakeMailer()
    send_outbound(api, up)
    send_outbound(api, up)
    assert len(up.to("dc03@example.test")) == 1 and outbox(api, "dc03@example.test") == []


def test_hold_keeps_the_case_for_7_days_can_be_cancelled_and_confirms_when_deleted(api):
    """UC-END-13 as the specs rely on it: delete now, or a 7-day hold."""
    register(api, "dc04", time_zone="America/New_York")
    cid, _ = named_case(api, "dc04")
    h = as_user("dc04")
    r = api.post(f"/v1/cases/{cid}/deletion", json={"mode": "hold"}, headers=h).json()
    when = datetime.fromisoformat(r["deletion_scheduled_for"])
    assert r["deleted"] is False
    assert timedelta(days=6, hours=23) < when - datetime.now(timezone.utc) <= timedelta(days=7)
    assert r["next_step"]["options"][0]["value"] == "keep"
    case = api.get(f"/v1/cases/{cid}", headers=h).json()["case"]
    assert case["deletion_scheduled_for"] == r["deletion_scheduled_for"]
    assert api.get("/v1/cases", headers=h).json()["cases"][0]["deletion_scheduled_for"]
    assert outbox(api, "dc04@example.test") == []  # nothing is sent until it is actually deleted
    # UC-REG-16: a case in a hold is in the download until it is deleted.
    data = api.get("/v1/me/data-export/file", headers=h).json()
    assert [c["deletion_scheduled_for"] for c in data["cases"]] == [r["deletion_scheduled_for"]]
    # Keep it.
    r = api.delete(f"/v1/cases/{cid}/deletion", headers=h).json()
    assert r["acknowledgment"] == CASE_COPY["case_deletion_cancelled"]
    assert api.get(f"/v1/cases/{cid}", headers=h).json()["case"]["deletion_scheduled_for"] is None
    # Hold again, and let the hold run out.
    api.post(f"/v1/cases/{cid}/deletion", json={"mode": "hold"}, headers=h)
    api.db.cases.update_one({"_id": UUID(cid)},
                            {"$set": {"deletion_requested_at": datetime.now(timezone.utc) - timedelta(days=7)}})
    assert maintenance.purge_held_cases(api.jobs_db) >= 1
    assert api.get(f"/v1/cases/{cid}", headers=h).status_code == 403
    assert [a for a, _ in outbox(api, "dc04@example.test")] == ["case_deleted_after_hold"]


def test_case_deletion_is_free_on_drafts_and_read_only_accounts_and_owner_only(api):
    register(api, "dc05")
    register(api, "dc05x")
    cid, _ = named_case(api, "dc05")
    draft = new_draft(api, "dc05")["case"]["id"]
    assert api.post(f"/v1/cases/{cid}/deletion", json={"mode": "now"}, headers=as_user("dc05x")).status_code == 403
    assert api.get(f"/v1/cases/{cid}/deletion", headers=as_user("dc05x")).status_code == 403
    expire_trial(api, "dc05")
    assert api.post(f"/v1/cases/{draft}/deletion", json={"mode": "hold"}, headers=as_user("dc05")).status_code == 200
    assert api.post(f"/v1/cases/{cid}/deletion", json={"mode": "now"}, headers=as_user("dc05")).status_code == 200


# ------------------------------------------------------------------ UC-REG-15 delete my account

def test_reg15_explains_everything_shows_where_the_confirmation_goes_and_offers_one_button(api):
    register(api, "da01")
    info = api.get("/v1/me/deletion", headers=as_user("da01")).json()
    for thing in ("your account", "every case", "every task", "conversations", "notification settings"):
        assert thing in info["explanation"]
    assert info["subscription_note"] is None
    assert info["masked_email"] == "d•••1@example.test" and info["masked_email"] in info["confirmation_destination"]
    assert [o["label"] for o in info["next_step"]["options"]] == ["Delete my account and everything in it"]
    # No reason is asked and nothing is offered to keep the user.
    shown = [info["explanation"], info["confirmation_destination"], info["next_step"]["prompt"],
             *(o["label"] for o in info["next_step"]["options"])]
    text = " ".join(shown).lower()
    for persuasion in ("why", "reason", "discount", "offer", "sure?", "miss you", "keep my account"):
        assert persuasion not in text


def test_reg15_a_store_subscription_is_information_only(api):
    register(api, "da02")
    api.db.users.update_one({"idp_subject": "da02"}, {"$set": {"status": "subscribed"}})
    note = api.get("/v1/me/deletion", headers=as_user("da02")).json()["subscription_note"]
    assert note["text"] == COPY["deletion_store_subscription"] and "?" not in note["text"]
    assert note["source_urls"] == ["https://support.apple.com/en-us/HT202039",
                                   "https://support.google.com/googleplay/answer/7018481"]


def test_reg15_deletes_everything_now_including_held_cases_and_sends_one_confirmation(api):
    subject = "apple|da03"
    email = "da03@example.test"
    r = api.post("/v1/registrations", json={}, headers=as_user(subject, email, method="apple"))
    assert r.status_code in (200, 201)
    from .conftest import onboard
    onboard(api, subject, method="apple")
    h = as_user(subject, email, method="apple")
    draft = api.post("/v1/cases", headers=h).json()["case"]["id"]
    active = api.post("/v1/cases", headers=h).json()["case"]["id"]
    answer(api, subject, active, "display_name", "Dan")
    start_journey(api, subject, active)
    api.put(f"/v1/cases/{active}/notification-preferences", json=KEEP_IT_SIMPLE, headers=h)
    add_conversation(api, active, "fake conversation")
    held = api.post("/v1/cases", headers=h).json()["case"]["id"]
    api.post(f"/v1/cases/{held}/deletion", json={"mode": "hold"}, headers=h)
    api.post(f"/v1/cases/{draft}/deletion", json={"mode": "now"}, headers=h)  # a pending case confirmation
    uid = user_id(api, subject)

    r = api.post("/v1/me/deletion", json={"confirm": True}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["signed_out"] is True and body["next_step"]["action"] == "signed_out"
    assert api.get("/v1/me", headers=h).json()["code"] == "registration_required"
    ids = [UUID(c) for c in (active, held, draft)]
    for collection in ("cases", "notification_preferences", "case_intake_answers", "case_tasks", "notification_log",
                       "context_items", "deceased"):
        field = "_id" if collection in ("cases", "notification_preferences") else "case_id"
        assert api.db[collection].count_documents({field: {"$in": ids}}) == 0, collection
    for collection in ("users", "consents", "trial_reminders"):
        field = "_id" if collection == "users" else "user_id"
        assert api.db[collection].count_documents({field: uid}) == 0, collection
    # Exactly one confirmation, with nothing that links it to the account, then the address is purged.
    assert outbox(api, email) == [("account_deleted", None)]
    # Apple token revocation (TN3194) is queued.
    assert [r["provider"] for r in api.db.identity_deletion_requests.find({"idp_subject": subject})] == ["apple"]
    mailer = FakeMailer()
    send_outbound(api, mailer)
    assert mailer.to(email) == [(COPY["email_subject_confirmation"], COPY["email_account_deleted"])]
    assert outbox(api, email) == []
    # Logged without content: ids only.
    rows = [(r["action"], r["object_type"])
            for r in api.db.audit_events.find({"actor_id": uid, "action": "account_deleted"})]
    assert rows == [("account_deleted", "user")]


def test_reg15_a_read_only_account_can_delete_for_free(api):
    register(api, "da04")
    named_case(api, "da04")
    expire_trial(api, "da04")
    assert api.post("/v1/me/deletion", json={"confirm": True}, headers=as_user("da04")).status_code == 200
    assert outbox(api, "da04@example.test") == [("account_deleted", None)]


def test_reg15_asked_in_chat_with_a_risk_of_harm_signal_puts_safety_first_then_proceeds(api):
    """[SAFETY] 988 first, and the Veterans Crisis Line for a veteran. No account action in that turn."""
    register(api, "da05")
    named_case(api, "da05", veteran_status="yes")
    h = as_user("da05")
    counted = sum(r["level_4_referrals"] for r in api.db.safety_referral_counts.find())
    text = "Please delete my account. I don't want to live anymore."
    first = api.post("/v1/me/messages", json={"text": text}, headers=h).json()
    assert first["intent"] == "safety_first" and first["session"]["safety_first_shown"] is True
    # Crisis plan level 4: 988 first, the Veterans Crisis Line for a veteran, and the Crisis Text Line.
    assert first["support"][0]["id"] == "lifeline_988"
    assert {s["id"] for s in first["support"]} == {"lifeline_988", "veterans_crisis_line", "crisis_text_line"}
    # SB 243: an anonymous count, with no user or case.
    assert sum(r["level_4_referrals"] for r in api.db.safety_referral_counts.find()) == counted + 1
    assert "988" in first["read_aloud"]["text"]
    assert "confirm_account_deletion" not in json.dumps(first)
    assert api.get("/v1/me", headers=h).status_code == 200  # nothing happened to the account
    # Asked again: the main flow, with no extra questions. The crisis lines stay on screen.
    again = api.post("/v1/me/messages", json={"text": text, "session": first["session"]}, headers=h).json()
    assert again["intent"] == "delete_account" and again["next_step"]["action"] == "confirm_account_deletion"
    assert [o["label"] for o in again["next_step"]["options"]] == ["Delete my account and everything in it"]
    assert {s["id"] for s in again["support"]} >= {"lifeline_988"}
    assert again["session"]["safety_first_shown"] is False
    assert api.get("/v1/me", headers=h).status_code == 200  # deleting still needs the button
    # A signal with no account request in it stays on safety, never an account menu.
    still = api.post("/v1/me/messages", json={"text": "I don't want to be here anymore",
                                              "session": first["session"]}, headers=h).json()
    assert still["intent"] == "safety_first" and "choose_account_request" not in json.dumps(still)


def test_reg15_asked_in_chat_without_a_signal_goes_straight_to_the_steps(api):
    register(api, "da06")
    r = api.post("/v1/me/messages", json={"text": "I'd like to delete my account"}, headers=as_user("da06")).json()
    assert r["intent"] == "delete_account" and r["support"] == []
    assert r["body"][0] == COPY["deletion_explanation"]
    assert r["next_step"]["action"] == "confirm_account_deletion"


def test_reg15_chat_text_is_redacted_and_never_echoed(api):
    register(api, "da07")
    r = api.post("/v1/me/messages", json={"text": "delete my account, my SSN is 123-45-6789"},
                 headers=as_user("da07"))
    assert r.status_code == 200 and "123-45-6789" not in r.text and "6789" not in r.text
    assert r.json()["redactions"] and r.json()["masked_text"]


# ------------------------------------------------------------------ UC-REG-16 download all my data

def test_reg16_explains_in_one_sentence(api):
    register(api, "dx01")
    info = api.get("/v1/me/data-export", headers=as_user("dx01")).json()
    assert info["explanation"].count(".") == 1 and info["explanation"].endswith(".")
    for thing in ("profile", "case", "summary", "task", "notification settings", "conversation"):
        assert thing in info["explanation"]
    assert info["format"] == "json"


def test_reg16_the_download_has_everything_and_never_a_sensitive_number(api):
    register(api, "dx02")
    cid, _ = named_case(api, "dx02", name="Dan", veteran_status="yes")
    set_prefs(api, "dx02", cid, KEEP_IT_SIMPLE)
    api.patch(f"/v1/cases/{cid}/deceased", json={"legal_first_name": "Daniel", "legal_last_name": "Fakerton"},
              headers=as_user("dx02"))
    api.db.deceased.update_one({"case_id": UUID(cid)}, {"$set": {"ssn_last4": "4821"}})
    add_conversation(api, cid, "We talked about the bank. Card 4111 1111 1111 1111, SSN 123-45-6789.")
    draft = new_draft(api, "dx02")["case"]["id"]
    r = api.get("/v1/me/data-export/file", headers=as_user("dx02"))
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith('attachment; filename="cairn-my-data-')
    assert r.headers["cache-control"] == "no-store"
    data = r.json()
    assert data["format"] == "cairn-data-export" and data["profile"]["email"] == "dx02@example.test"
    assert {a["purpose"] for a in data["acknowledgments"]} == {"privacy_terms", "trial_terms", "ai_notice"}
    assert [c["id"] for c in data["cases"]] == [cid, draft]
    case = data["cases"][0]
    assert case["display_name"] == "Dan" and case["person_who_died"]["legal_last_name"] == "Fakerton"
    assert {a["field"] for a in case["answers"]} >= {"display_name", "veteran_status", "place_of_death"}
    assert case["tasks"] and {"title", "status", "due_on"} <= set(case["tasks"][0])
    assert case["summary"]["tasks_by_status"] and case["summary"]["next_step"]
    assert case["notification_preferences"]["channels"] == ["email"]
    assert case["conversation"][0]["payload"]["text"].startswith("We talked about the bank.")
    assert "4821" not in json.dumps(case["person_who_died"])
    assert "ssn_last4" not in r.text
    for secret in ("4111 1111 1111 1111", "4111", "123-45-6789", "6789"):
        assert secret not in json.dumps(case["conversation"]), secret


def test_reg16_is_free_on_a_read_only_account_and_never_includes_another_users_data(api):
    register(api, "dx03")
    register(api, "dx03x")
    named_case(api, "dx03", name="Mine")
    named_case(api, "dx03x", name="Theirs")
    expire_trial(api, "dx03")
    r = api.get("/v1/me/data-export/file", headers=as_user("dx03"))
    assert r.status_code == 200 and "Theirs" not in r.text and "dx03x" not in r.text
    assert [c["display_name"] for c in r.json()["cases"]] == ["Mine"]
    assert r.json()["profile"]["status"] == "read_only"


def test_reg16_asked_in_chat(api):
    register(api, "dx04")
    r = api.post("/v1/me/messages", json={"text": "Can I get a copy of everything you have about me?"},
                 headers=as_user("dx04")).json()
    assert r["intent"] == "download_data" and r["next_step"]["action"] == "download_data"
    assert r["next_step"]["prompt"] == COPY["export_explanation"]


# ------------------------------------------------------------------ pausing (D-2026-09-25-P1, UC-END-08 hook)

def test_pausing_says_the_free_days_keep_counting_and_offers_to_change_notifications(api):
    register(api, "pz01")
    cid, _ = named_case(api, "pz01")
    before = api.db.users.find_one({"idp_subject": "pz01"})["trial_ends_at"]
    r = api.post(f"/v1/cases/{cid}/journey/pause", json={"pause_days": 14}, headers=as_user("pz01")).json()
    texts = [n["text"] for n in r["notes"]]
    assert CASE_COPY["pause_trial_note"] in texts and CASE_COPY["pause_notifications_offer"] in texts
    assert api.db.users.find_one({"idp_subject": "pz01"})["trial_ends_at"] == before


@pytest.mark.parametrize("path", ["/v1/me/deletion", "/v1/me/data-export", "/v1/me/data-export/file",
                                  "/v1/me/notification-preferences"])
def test_always_available_even_when_an_acknowledgment_changed(api, path, monkeypatch):
    import dataclasses
    """Deleting, downloading, and changing notifications are never gated on re-acknowledging new wording."""
    register(api, "ak01")
    changed = dataclasses.replace(api.app.state.copy, spec={**api.app.state.copy.spec,
                                                           "trial_summary": COPY["trial_summary"] + " Changed."})
    monkeypatch.setattr(api.app.state, "copy", changed)
    assert api.get("/v1/cases", headers=as_user("ak01")).json()["code"] == "acknowledgment_required"
    assert api.get(path, headers=as_user("ak01")).status_code == 200


def test_date_fields_in_the_download_are_iso(api):
    register(api, "dx05")
    named_case(api, "dx05")
    data = api.get("/v1/me/data-export/file", headers=as_user("dx05")).json()
    date.fromisoformat(data["cases"][0]["tasks"][0]["due_on"])
    datetime.fromisoformat(data["generated_at"])
