#!/usr/bin/env python3
"""Copy an existing Cairn PostgreSQL database into MongoDB, once. For the 2026-10-01 move to MongoDB.

  CAIRN_POSTGRES_URL=postgresql://owner@host/cairn \\
  CAIRN_ADMIN_MONGODB_URI=mongodb+srv://admin@cluster.example/ \\
  python3 database/tools/move_from_postgres.py [--db cairn] [--dry-run]

Run database/db/apply.py on the MongoDB database first, so the validators are
in place: every document is checked as it is written, and the whole copy runs
in one transaction, so a bad row stops it with nothing written. The target's
case, user, and template collections must be empty. Stop the API and the jobs
while it runs. Ids, times, and template versions are kept, so case tasks stay
pinned to the version each family was shown.

What changes shape on the way:
* case_members become each case's members array.
* template_citations go inside their template.
* case_tasks carry their template's task_key.
* notification_preferences become one per account, from the account's most recent journey choice (account D-13),
  and the rest of the v3 shape is applied the same way as db/apply.py's use_cases_v3 migration (_v3 below).
* Calendar dates become "YYYY-MM-DD" strings. Times keep millisecond precision.

What is left behind, on purpose:
* users.first_name, last_name, and phone. Legal names stopped being collected
  at sign-up in 0008, and a phone number is never stored in plain text.
* consents.withdrawn_at, which nothing ever set. The copy stops if one is set.
* schema_migrations, which describes the PostgreSQL schema only.

Needs psycopg (pip install 'psycopg[binary]') in addition to tools/requirements.txt.
Prints counts only. Never a value.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime

TABLES = ("users", "consents", "cases", "case_members", "deceased", "case_intake_answers", "task_templates",
          "template_citations", "journey_templates", "case_tasks", "notification_preferences", "notification_log",
          "trial_reminders", "identity_deletion_requests", "action_confirmation_outbox", "action_confirmation_log",
          "audit_events", "context_items", "app_settings")
MUST_BE_EMPTY = ("users", "cases", "task_templates", "journey_templates", "audit_events")


def _dates(row: dict, *fields) -> dict:
    for f in fields:
        if isinstance(row.get(f), date) and not isinstance(row.get(f), datetime):
            row[f] = row[f].isoformat()
    return row


def _id(row: dict, key: str = "id") -> dict:
    row["_id"] = row.pop(key)
    return row


def read_postgres(url: str) -> dict[str, list[dict]]:
    import psycopg
    from psycopg.rows import dict_row
    out = {}
    with psycopg.connect(url, row_factory=dict_row) as conn:
        present = {r["table_name"] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'cairn'")}
        for table in TABLES:
            out[table] = conn.execute(f"SELECT * FROM cairn.{table}").fetchall() if table in present else []
    return out


def _v2_answer(a: dict) -> dict:
    """Case creation spec 2.0.0 names, the same reshaping as db/apply.py's case_creation_v2 migration."""
    if a["field_key"] == "residence_state":
        a["field_key"] = "residence_jurisdiction"
    value = a.get("value")
    if isinstance(value, dict) and "state" in value:
        value["jurisdiction"] = value.pop("state")
    if a["field_key"] == "completed_items" and isinstance(value, list):
        a["value"] = ["bank_insurer_or_employer_notified" if v == "bank_notified" else v for v in value]
    return a


def _v2_task(t: dict) -> dict:
    needs_check = t["status"] == "check_on_this"
    return {**t, "status": "not_started" if needs_check else t["status"], "needs_check": needs_check,
            "probably_not_applicable": False, "handled_by": None}


def transform(src: dict[str, list[dict]]) -> dict[str, list[dict]]:
    if any(c.get("withdrawn_at") for c in src["consents"]):
        raise SystemExit("A consent has withdrawn_at set. The MongoDB schema has no place for it. Stopping.")
    out: dict[str, list[dict]] = {}

    out["users"] = [_id({
        "id": u["id"], "idp_subject": u["idp_subject"], "email": u["email"], "email_lower": u["email"].lower(),
        **{k: u.get(k) for k in ("sign_in_method", "preferred_name", "name_pronunciation", "name_prefill",
                                 "time_zone", "trial_started_at", "trial_ends_at", "created_at")},
        "trial_clock_paused_at": None, "ai_reminder_shown_at": None, "ai_reminder_shown_on": None,
        "voice": u["voice"], "onboarding_step": u["onboarding_step"], "status": u["status"]})
        for u in src["users"]]

    out["consents"] = [_id({k: c[k] for k in ("id", "user_id", "purpose", "policy_version", "granted_at",
                                              "auth_provider", "client")}) for c in src["consents"]]

    members: dict = {}
    for m in src["case_members"]:
        members.setdefault(m["case_id"], []).append(
            {"user_id": m["user_id"], "role": m["role"], "relationship": m["relationship"], "status": m["status"],
             "created_at": m["created_at"]})
    out["cases"] = [_dates(_id({
        **{k: c[k] for k in ("id", "status", "created_by", "created_at", "purge_after", "journey_template_key",
                             "journey_template_version", "journey_started_at", "journey_started_on",
                             "last_intake_step", "last_activity_at", "death_not_yet_occurred", "skip_explainers",
                             "name_fallback", "attorney_triggers", "shown_notices", "tasks_paused_until",
                             "deletion_requested_at")},
        "loss_survivor_resources": False, "secure_now_first": False,
        "members": members.get(c["id"], [])}), "journey_started_on") for c in src["cases"]]

    out["deceased"] = [_dates(_id(dict(d)), "date_of_birth", "date_of_death") for d in src["deceased"]]
    out["case_intake_answers"] = [_v2_answer(dict(a)) for a in src["case_intake_answers"]]

    citations: dict = {}
    for c in src["template_citations"]:
        citations.setdefault(c["template_id"], []).append(_dates(
            {k: c[k] for k in ("authority_name", "url", "jurisdiction", "last_verified_on")}, "last_verified_on"))
    keys = {t["id"]: t["task_key"] for t in src["task_templates"]}
    out["task_templates"] = [_id({**{k: v for k, v in t.items() if k != "applies_when"},
                                  "applies_when": t["applies_when"], "citations": citations.get(t["id"], [])})
                             for t in src["task_templates"]]
    out["journey_templates"] = [_id(dict(t)) for t in src["journey_templates"]]
    out["case_tasks"] = [_dates(_id(_v2_task({**t, "task_key": keys[t["template_id"]]})), "due_on")
                         for t in src["case_tasks"]]
    out["notification_preferences"] = [_id(dict(p), "case_id") for p in src["notification_preferences"]]
    for table in ("notification_log", "trial_reminders", "identity_deletion_requests", "action_confirmation_outbox",
                  "action_confirmation_log", "audit_events"):
        out[table] = [_id(dict(r)) for r in src[table]]
    out["context_items"] = [dict(r) for r in src["context_items"]]
    out["app_settings"] = [{"_id": s["key"], "value": s["value"], "description": s["description"],
                            "updated_at": s["updated_at"]} for s in src["app_settings"]]
    return _v3(out)


def _v3(out: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """The use case suite of 2026-10-06, as db/apply.py's use_cases_v3 migration applies it: one account state model
    (D-19), notification choices for the whole account (D-13), the account's new fields, and one trial note."""
    owners = {c["_id"]: c["created_by"] for c in out["cases"]}
    channel = {"email": "email", "push": None, "in_app_only": "in_app"}
    frequency = {"as_it_happens": "due_only", "daily_max": "daily", "weekly_max": "weekly"}
    lead = {1: "day_before", 3: "three_days", 7: "one_week"}
    inactivity = {3: "three_days", 7: "one_week", 14: "two_weeks"}
    newest: dict = {}
    for p in sorted(out["notification_preferences"], key=lambda p: p["updated_at"]):
        if p["_id"] in owners:
            newest[owners[p["_id"]]] = p
    out["notification_preferences"] = [{
        "_id": user_id, "channels": ["email", "in_app"] if "email" in p["channels"] else ["in_app"],
        "frequency": frequency[p["frequency"]] if p["reasons"] else "none",
        "quiet_hours_start": "21:00", "quiet_hours_end": "08:00", "browser_push_endpoint": None,
        "due_date_lead": lead.get(p["due_date_lead_days"], "three_days"),
        "inactivity_after": inactivity.get(p["inactivity_days"], "off"),
        "journey_confirmed_at": p["updated_at"], "updated_at": p["updated_at"]}
        for user_id, p in newest.items() if any(channel[c] for c in p["channels"])]
    statuses = {"pending_onboarding": "pending_onboarding", "pending_deletion": "pending_deletion"}
    for u in out["users"]:
        old = u["status"]
        u.update({"status": statuses.get(old, "setup_complete"),
                  "access": "read_only" if old == "read_only" else "full",
                  "subscription_status": "active" if old == "subscribed" else "none", "name_prefill": None,
                  "adult_attested": None, "adult_attested_at": None, "last_active_at": None, "check_in_at": None,
                  "check_in_case_id": None, "check_in_by_email": False, "check_in_sent_at": None,
                  "break_started_at": None, "break_until": None, "break_notice_at": None, "break_notice_sent_at": None,
                  "subscribe_prompt_shown_on": None, "price_notice_sent_for": None, "stripe_customer_id": None,
                  "stripe_subscription_id": None, "stripe_checkout_session_id": None, "current_period_end": None,
                  "cancel_at_period_end": False, "billing_notice": "none", "subscribed_at": None,
                  "annual_reminder_due_at": None, "cancel_requested_at": None})
    out["trial_reminders"] = [{**r, "skipped_at": None} for r in out["trial_reminders"]
                              if r["kind"] == "trial_ends_soon" or r["email_sent_at"] is not None]
    for row in out["app_settings"]:
        if row["_id"] == "trial_reminder_days_before" and row["value"] == 3:
            row["value"] = 7
    return out


def write_mongo(db, docs: dict[str, list[dict]]) -> None:
    for name in MUST_BE_EMPTY:
        if db[name].estimated_document_count():
            raise SystemExit(f"MongoDB collection {name} isn't empty. Copy into a fresh database.")

    def run(cs):
        for name, rows in docs.items():
            if name == "app_settings":
                for r in rows:  # apply.py seeded the defaults. The values in use win.
                    db.app_settings.replace_one({"_id": r["_id"]}, r, upsert=True, session=cs)
            elif rows:
                db[name].insert_many(rows, ordered=True, session=cs)

    with db.client.start_session() as cs:
        cs.with_transaction(run)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=os.environ.get("CAIRN_MONGODB_DB", "cairn"))
    ap.add_argument("--dry-run", action="store_true", help="Read and reshape everything, write nothing.")
    args = ap.parse_args()
    pg, mongo = os.environ.get("CAIRN_POSTGRES_URL"), os.environ.get("CAIRN_ADMIN_MONGODB_URI")
    if not pg or (not mongo and not args.dry_run):
        print("Set CAIRN_POSTGRES_URL and CAIRN_ADMIN_MONGODB_URI.", file=sys.stderr)
        return 2
    docs = transform(read_postgres(pg))
    for name, rows in docs.items():
        print(f"{name:28} {len(rows)}")
    if args.dry_run:
        return 0
    from pymongo import MongoClient
    with MongoClient(mongo, uuidRepresentation="standard", tz_aware=True) as client:
        write_mongo(client[args.db], docs)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
