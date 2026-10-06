"""Account deletion (UC-ACCT-01), the download (UC-REG-16), keeping in touch (account D-13, UC-REG-17, case
UC-CASE-19), and confirmations (UC-CASE-21) against a real MongoDB database, as the cairnApp user.

Specs: database/docs/cairn-account-use-cases-v32.json and database/docs/cairn-case-creation-use-cases-v32.json.
Case deletion with a 7-day hold is UC-END-13, which those specs rely on. Each test names what it covers. Rules
that need no database are in test_account_lifecycle_rules.py.

Skipped unless CAIRN_TEST_MONGODB_URI points at a scratch MongoDB replica set. Fake data only.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import pytest

from cairn_api import maintenance, outbound
from cairn_api.copy_store import load_break_copy, load_case_copy, load_copy, load_subscription_copy

from .conftest import active_case, answer, as_user, expire_trial, new_draft, register, start_journey, user_id

COPY = load_copy()
CASE_COPY = load_case_copy()
BREAK_COPY = load_break_copy()
SUB_COPY = load_subscription_copy()
# Choices with no quiet hours, so a test never depends on the time of day it runs.
ALWAYS = {"quiet_hours_start": "00:00", "quiet_hours_end": "00:00"}


def prefs_row(api, subject):
    row = api.db.notification_preferences.find_one({"_id": user_id(api, subject)})
    return None if row is None else (row["channels"], row["frequency"], row["due_date_lead"], row["inactivity_after"])


def set_prefs(api, subject, body):
    r = api.patch("/v1/me/notification-preferences", json=body, headers=as_user(subject))
    assert r.status_code == 200, r.text
    return r.json()


def outbox(api, email, *, with_service: bool = False):
    """Queued confirmations. The setup welcome and Settings changes are left out unless asked for."""
    return [(r["action_type"], r["user_id"])
            for r in api.db.action_confirmation_outbox.find({"email": email}, sort=[("queued_at", 1)])
            if with_service or r["action_type"] not in ("setup_complete", "settings_changed")]


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


def send_outbound(api, mailer, push=None):
    return outbound.run_once(api.jobs_db, mailer, COPY, CASE_COPY, break_copy=BREAK_COPY, sub_copy=SUB_COPY,
                             push=push)


def due_tomorrow(api, case_id):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    api.db.case_tasks.update_many({"case_id": UUID(case_id)}, {"$set": {"due_on": tomorrow}})


def add_conversation(api, case_id, text):
    api.db.context_items.insert_one({"case_id": UUID(case_id), "item_key": "CONVO_SUMMARY", "payload": {"text": text},
                                     "updated_at": datetime.now(timezone.utc), "expires_at": None})


def named_case(api, subject, name="Dan", **answers):
    return active_case(api, subject, {"display_name": name, "place_of_death": {"jurisdiction": "NH"}, **answers})


# ------------------------------------------------------------------ UC-CASE-19 confirm how Cairn keeps in touch

def test_uc19_reads_back_the_account_choices_and_asks_only_lead_time_and_inactivity(api):
    register(api, "nt01")
    cid = new_draft(api, "nt01")["case"]["id"]
    body = api.get(f"/v1/cases/{cid}/keep-in-touch", headers=as_user("nt01")).json()
    assert body["opening"] == CASE_COPY["notifications_intro"].format(
        channels="email and inside Cairn", frequency="only when something is due")
    assert [q["id"] for q in body["questions"]] == ["due_date_lead", "inactivity_after"]
    assert body["questions"][0]["prompt"] == CASE_COPY["lead_time_question"]
    assert body["questions"][1]["prompt"] == CASE_COPY["inactivity_question"]
    assert [o["value"] for o in body["questions"][0]["options"]] == ["day_before", "three_days", "one_week"]
    assert [o["value"] for o in body["questions"][1]["options"]] == ["off", "three_days", "one_week", "two_weeks"]
    assert body["change_link"]["label"] == CASE_COPY["change_how_you_hear"]
    # Channels and frequency are never asked here (account D-13).
    assert "channels" not in [q["id"] for q in body["questions"]]


def test_uc19_saves_lead_time_and_inactivity_on_the_account(api):
    register(api, "nt02")
    cid = new_draft(api, "nt02")["case"]["id"]
    r = api.put(f"/v1/cases/{cid}/keep-in-touch", json={"due_date_lead": "one_week", "inactivity_after": "one_week"},
                headers=as_user("nt02"))
    assert r.status_code == 200 and r.json()["next_step"]["action"] == "preview_journey"
    assert prefs_row(api, "nt02") == (["email", "in_app"], "due_only", "one_week", "one_week")
    assert r.json()["preferences"]["journey_confirmed"] is True


def test_uc19_skip_keeps_the_open_03_defaults(api):
    register(api, "nt03")
    cid = new_draft(api, "nt03")["case"]["id"]
    api.put(f"/v1/cases/{cid}/keep-in-touch", json={"skip": True}, headers=as_user("nt03"))
    assert prefs_row(api, "nt03") == (["email", "in_app"], "due_only", "three_days", "off")


def test_uc19_with_no_reminders_both_questions_are_skipped(api):
    register(api, "nt04")
    set_prefs(api, "nt04", {"stop_all_reminders": True})
    cid = new_draft(api, "nt04")["case"]["id"]
    body = api.get(f"/v1/cases/{cid}/keep-in-touch", headers=as_user("nt04")).json()
    assert body["questions"] == [] and body["next_step"]["prompt"] == CASE_COPY["keep_in_touch_none"]


def test_uc19_another_user_cannot_read_or_set_it_through_someone_elses_case(api):
    register(api, "nt05")
    register(api, "nt05x")
    cid = new_draft(api, "nt05")["case"]["id"]
    assert api.get(f"/v1/cases/{cid}/keep-in-touch", headers=as_user("nt05x")).status_code == 403
    assert api.put(f"/v1/cases/{cid}/keep-in-touch", json={"skip": True},
                   headers=as_user("nt05x")).status_code == 403


def test_uc18_a_second_journey_uses_the_same_account_choices(api):
    """UC-CASE-18 v3: no per-case choices. The read-back shows the account's."""
    register(api, "nt06")
    named_case(api, "nt06")
    set_prefs(api, "nt06", {"frequency": "weekly"})
    second = new_draft(api, "nt06")["case"]["id"]
    body = api.get(f"/v1/cases/{second}/keep-in-touch", headers=as_user("nt06")).json()
    assert "a short weekly summary" in body["opening"]


# ------------------------------------------------------------------ UC-REG-17 change setup choices later

def test_reg17_each_choice_is_read_back_saved_and_confirmed(api):
    register(api, "st01")
    r = set_prefs(api, "st01", {"frequency": "daily", "quiet_hours_start": "22:00", "quiet_hours_end": "07:30"})
    assert r["preferences"]["frequency"] == "daily"
    assert r["acknowledgment"].startswith("Saved. A confirmation is on its way to s•••1@example.test.")
    assert "10 PM" in r["preferences"]["readback"] and "7:30 AM" in r["preferences"]["readback"]
    assert outbox(api, "st01@example.test", with_service=True)[-1][0] == "settings_changed"


def test_reg17_stop_all_reminders_is_one_step_with_no_persuasion(api):
    register(api, "st02")
    r = set_prefs(api, "st02", {"stop_all_reminders": True})
    assert r["acknowledgment"] == COPY["notify_stopped"]
    assert prefs_row(api, "st02")[1] == "none"
    for word in ("sure", "miss", "why"):
        assert word not in r["acknowledgment"].lower()


def test_reg17_always_free_on_a_read_only_account(api):
    register(api, "st03")
    named_case(api, "st03")
    expire_trial(api, "st03")
    assert set_prefs(api, "st03", {"inactivity_after": "two_weeks"})["preferences"]["inactivity_after"] == "two_weeks"


def test_reg17_by_chat_stop_texting_me_and_email_me_instead(api):
    register(api, "st04")
    h = as_user("st04")
    r = api.post("/v1/me/messages", json={"text": "Stop texting me"}, headers=h).json()
    assert r["intent"] == "stop_notifications" and prefs_row(api, "st04")[1] == "none"
    r = api.post("/v1/me/messages", json={"text": "Email me instead"}, headers=h).json()
    assert r["intent"] == "change_notifications" and r["proposal"]["email"] is True
    assert r["next_step"]["action"] == "confirm_notification_change"  # read back, nothing saved yet
    r = api.post("/v1/me/messages", json={"text": "Can you text me?"}, headers=h).json()
    assert r["intent"] == "sms_not_available"


# ------------------------------------------------------------------ what goes out (account D-13, D-14)

def test_the_trial_note_is_emailed_whatever_the_reminder_choices(api):
    """Account D-14: a service notice, emailed a week before, and shown in Cairn."""
    register(api, "tn01")
    set_prefs(api, "tn01", {"stop_all_reminders": True})
    named_case(api, "tn01")
    api.db.trial_reminders.update_one({"user_id": user_id(api, "tn01")},
                                      {"$set": {"due_at": datetime.now(timezone.utc) - timedelta(minutes=1)}})
    mailer = FakeMailer()
    send_outbound(api, mailer)
    [(subject, body)] = [m for m in mailer.to("tn01@example.test") if m[0] == COPY["email_subject_trial"]]
    assert body.startswith("Your free time with Cairn ends in a week, on ")


def test_uc12_confirmation_says_the_note_goes_to_the_sign_in_email(api):
    register(api, "tn02")
    cid = new_draft(api, "tn02")["case"]["id"]
    answer(api, "tn02", cid, "place_of_death", {"jurisdiction": "NH"})
    started = start_journey(api, "tn02", cid)
    assert "tn02@example.test" in started["confirmation"]
    assert "Taking a break doesn't pause a subscription." in started["confirmation"]


def test_reminders_are_short_private_and_at_the_chosen_pace(api):
    register(api, "nt10")
    cid, _ = named_case(api, "nt10", name="Zebulon")
    set_prefs(api, "nt10", {**ALWAYS, "frequency": "daily"})
    due_tomorrow(api, cid)
    mailer = FakeMailer()
    send_outbound(api, mailer)
    send_outbound(api, mailer)
    sent = [m for m in mailer.to("nt10@example.test") if m[0] == COPY["email_subject_notification"]]
    assert sent == [(COPY["email_subject_notification"], CASE_COPY["notification_example"])]
    assert "Zebulon" not in json.dumps(mailer.sent)


def test_browser_notifications_are_empty_and_a_gone_subscription_is_dropped(api):
    """UC-REG-15 browser channel: the push carries no text. A 410 takes browser off (permission revoked)."""
    from cairn_api.webpush import PushError
    register(api, "nt11")
    cid, _ = named_case(api, "nt11")
    set_prefs(api, "nt11", {**ALWAYS, "channels": {"email": False, "browser": True, "browser_permission": "granted",
                                                   "browser_push_endpoint": "https://push.example.test/sub/nt11"}})
    due_tomorrow(api, cid)

    class GonePush:
        def __init__(self):
            self.sent = []

        def send(self, endpoint):
            self.sent.append(endpoint)
            raise PushError("gone", 410)

    push = GonePush()
    send_outbound(api, FakeMailer(), push)
    assert push.sent == ["https://push.example.test/sub/nt11"]
    row = api.db.notification_preferences.find_one({"_id": user_id(api, "nt11")})
    assert row["channels"] == ["in_app"] and row["browser_push_endpoint"] is None


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
    [(subject, body)] = [m for m in mailer.to("dc02@example.test") if m[1] == COPY["email_case_deleted_now"]]
    assert "Zebulon" not in subject + body
    assert outbox(api, "dc02@example.test", with_service=True) == []  # every address is purged once sent
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
    sent = [m for m in up.to("dc03@example.test") if m[1] == COPY["email_case_deleted_now"]]
    assert len(sent) == 1 and outbox(api, "dc03@example.test") == []


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


# ------------------------------------------------------------------ UC-ACCT-01 delete my account (UC-REG-15 before)

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


def test_reg15_a_subscription_is_cancelled_right_away_with_no_refund_said_first(api):
    """UC-SUB-16 and SUB-D-05. The note is information, not a question."""
    register(api, "da02")
    api.db.users.update_one({"idp_subject": "da02"}, {"$set": {"subscription_status": "active"}})
    note = api.get("/v1/me/deletion", headers=as_user("da02")).json()["subscription_note"]
    assert note["text"] == SUB_COPY["delete_account_with_subscription"] and "?" not in note["text"]


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
    api.patch("/v1/me/notification-preferences", json={"frequency": "daily"}, headers=h)
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
    for collection in ("cases", "case_intake_answers", "case_tasks", "notification_log", "context_items",
                       "deceased"):
        field = "_id" if collection == "cases" else "case_id"
        assert api.db[collection].count_documents({field: {"$in": ids}}) == 0, collection
    for collection in ("users", "notification_preferences", "consents", "trial_reminders"):
        field = "_id" if collection in ("users", "notification_preferences") else "user_id"
        assert api.db[collection].count_documents({field: uid}) == 0, collection
    # Exactly one confirmation, with nothing that links it to the account, then the address is purged.
    assert outbox(api, email, with_service=True) == [("account_deleted", None)]
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
    set_prefs(api, "dx02", {"frequency": "daily"})
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
    assert data["notification_preferences"]["channels"] == ["email", "in_app"]
    assert data["subscription"]["subscription_status"] == "none" and "stripe" not in json.dumps(data["subscription"])
    assert data["profile"]["adult_attested"] is True
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
    assert (r.json()["profile"]["status"], r.json()["profile"]["access"]) == ("setup_complete", "read_only")


def test_reg16_asked_in_chat(api):
    register(api, "dx04")
    r = api.post("/v1/me/messages", json={"text": "Can I get a copy of everything you have about me?"},
                 headers=as_user("dx04")).json()
    assert r["intent"] == "download_data" and r["next_step"]["action"] == "download_data"
    assert r["next_step"]["prompt"] == COPY["export_explanation"]


# ------------------------------------------------------------------ pausing (DEC-07, UC-BRK-05)

def test_pausing_says_the_free_days_keep_counting_and_offers_to_change_notifications(api):
    register(api, "pz01")
    cid, _ = named_case(api, "pz01")
    before = api.db.users.find_one({"idp_subject": "pz01"})["trial_ends_at"]
    r = api.post(f"/v1/cases/{cid}/journey/pause", json={"pause_days": 14}, headers=as_user("pz01")).json()
    texts = [n["text"] for n in r["notes"]]
    assert any(t.startswith("Your free days keep counting while you rest.") for t in texts)
    assert CASE_COPY["pause_notifications_offer"] in texts
    assert api.db.users.find_one({"idp_subject": "pz01"})["break_started_at"] is not None  # a break on the account
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
