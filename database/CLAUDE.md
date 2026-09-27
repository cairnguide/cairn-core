# Cairn MVP database: handoff for Claude Code

Cairn is an AI assistant that walks a family through the logistics of a death, step by step, while protecting their time to grieve. This bundle is the MVP database layer. The MVP covers sign up, creating a case, collecting information about the user and the deceased, and generating a personalized four-week journey of first steps.

The data involved is highly sensitive (identity of a deceased person, place of death, veteran and will status, a partial SSN, and who is authorized to act). Security comes first. Every change must keep the checks in `db/tests/verify.sql` passing.

## Read first

- `docs/cairn-mvp-data-model.md`: the MVP conceptual model, decision log, release gate rules, and example template.
- `docs/cairn-conceptual-data-model.md`: the full future model. The MVP is a strict subset plus a few flag columns.
- `docs/cairn-registration-use-cases.json`: registration and onboarding (UC-REG-01 to UC-REG-14, UC-ACCT-01), spec 1.1.0. Replaces the registration part of UC-1 to UC-13. `docs/registration-gap-audit.md` records what is built and what is open. UC-REG-06 (age confirmation) is not built, by product decision: Cairn does not ask for or store the user's age.
- `docs/cairn-case-creation-use-cases.json`: case creation (UC-CASE-01 to UC-CASE-18), spec 0.3.0. Replaces UC-5 to UC-9. `docs/case-creation-gap-audit.md` maps every acceptance criterion to its test and lists what is open.
- `docs/cairn-case-creation-use-cases-2026-09-25.json` (draft 0.4 changes: UC-CASE-19 to UC-CASE-21, keeping in touch and confirmations) and `docs/cairn-account-use-cases-2026-09-25.json` (UC-REG-15 delete my account, which replaces UC-ACCT-01, and UC-REG-16 download all my data). `docs/account-lifecycle-gap-audit.md` maps them to tests and lists what is open, including UC-END-13, which these specs rely on but don't define.
- `docs/backend-standup-gap-audit.md`: what it takes to stand up the API so a person can register, what was built for local sign-in (test logins, `POST /v1/dev/token`), and the work still to do outside the code.

## Decisions already made (ask the product owner before changing)

1. PostgreSQL 15 or newer holds users, cases, membership, deceased, death events, journey progress, consent, and audit. The design is cloud agnostic.
2. The case is the security boundary. Access is decided by `case_members` and enforced with row-level security, not only in application code.
3. Journey templates and citations are authored as versioned JSON files in `content/` and loaded at deploy into read-only, immutable-per-version tables. A correction is a new version, never an edit. `case_tasks` pins to the exact version a family was shown.
4. A derived journey snapshot in a document store (for AI context assembly) is deferred. Add it only if AI context assembly becomes a measurable cost, task reads dominate load, or templates diverge structurally by state.
5. No document upload at MVP. There is no vault and no object store.
6. Only the last four digits of the SSN are collected. Full SSN and VA file numbers are deferred.
7. Do not persist any inference about a user's emotional state. `cases.tasks_paused_until` lets the product step back from task mode without recording why.
8. The MVP allows one owner per case. `case_members` exists so invitations and co-executors need no schema change later. The `mvp_owner_only` constraint is dropped by a later migration.

## Layout

```
db/migrations/     numbered SQL, applied in order, checksummed
db/optional/       context_items_jsonb.sql (only if context stays in Postgres)
db/apply.sh        migration runner (uses DATABASE_URL, owner role)
db/dev/            development only, never a migration: test_logins.sql (the cairn_dev schema)
db/tests/          verify.sql (security and behavior checks) and run.sh
content/schema/    JSON Schema for template files
content/tasks/     template files, one folder per jurisdiction (us, nh, ...)
content/journeys/  journey selection rules: base paths, add-ons, completed items (case creation spec)
tools/             load_templates.py (validate and load templates), seed_test_db.py (local test logins)
docs/              data model documents
```

## Commands

```bash
# Full test suite against a scratch Postgres server (creates and drops a temp database)
ADMIN_URL=postgresql://admin@localhost/postgres db/tests/run.sh

# Apply migrations to a real database using the OWNER role
DATABASE_URL=postgresql://owner@host/cairn db/apply.sh

# Validate content only (no database). Fails on unreviewed templates by design.
python3 tools/load_templates.py --dry-run

# Load templates. Development only may add --allow-unreviewed.
DATABASE_URL=... python3 tools/load_templates.py --git-release <tag>
```

Install loader dependencies with `pip install -r tools/requirements.txt`.

## Security invariants (do not weaken)

- The application connects as a login role that is a member of `cairn_app`. It never connects as the owner role and never has BYPASSRLS. SECURITY DEFINER helper functions rely on the owner bypassing RLS, so RLS is ENABLED and not FORCED. Do not add FORCE without redesigning the helpers.
- Per transaction, set the caller with `SELECT set_config('app.user_id', '<uuid>', true)`. Use the transaction-local form. An unset user matches no rows.
- Also run `SET search_path = cairn, pg_temp` on each connection. Role-level settings are not inherited through membership.
- `cairn_app` has column-level grants only and no direct INSERT on `users`. Sign up and sign in go through `cairn.create_account` and `cairn.resolve_user`. The app cannot write `onboarding_step`, `status`, or the trial columns. Onboarding moves only through `cairn.advance_onboarding`, one step at a time.
- The trial starts in `cairn.start_journey`, in the same transaction that moves the account's first case from draft to active (DEC-01, migration 0010). Creating or editing a draft never starts it. `trial_started_at` is set once and a trigger refuses any change, the owner included.
- Only `cairn.start_journey` moves a case out of draft. The app has no grant on `cases.status` or the journey start columns, and `cases_guard` refuses going back to draft or changing `journey_started_at`.
- Read-only after the trial and "no case before onboarding" are restrictive RLS policies. Active cases use `cairn.account_can_write()`. Drafts stay editable on a read-only account (UC-CASE-18), so case-scoped tables use `cairn.case_writable(case_id)`. Keep one of them on every new case-scoped table (INSERT and UPDATE), and never on DELETE, so data can always be deleted. The one exception is `notification_preferences`: changing how Cairn keeps in touch is always free, including on a read-only account (D-2026-09-25-F1).
- Deleting, downloading, and changing notifications never check `account_can_write`, onboarding, or acknowledgments. `cairn.request_case_deletion`, `cairn.cancel_case_deletion`, and `cairn.delete_my_account` check ownership only.
- Nothing is sent outside the app unless the user chose it (`notification_preferences`, default `in_app_only`), except the one confirmation of a deletion the user asked for (`action_confirmation_outbox`). Outbound text never names the person who died, the circumstance, task details, or anything deleted. The outbox holds an address only until it is sent. `action_confirmation_log` and `notification_log` never hold content or addresses. `notification_preferences` is never used for marketing or advertising.
- SMS is not an accepted channel. Adding it needs card 50's decision, legal review of the consent wording, and a migration for the encrypted number with its key management. Never store a phone number in plain text.
- Legal identity is never collected on a draft. Restrictive policies refuse `deceased` inserts and updates while the case is a draft.
- `case_intake_answers` holds only the spec's data_fields, and `cairn.intake_value_valid` checks each value's shape. Never add a free-text column to it. Circumstance stores the enum only.
- Nothing about a user's distress is stored. Safety modes live in the client-held session (decision 7).
- `consents` is append-only. Rows go only through the cascade from deleting the account (`cairn.delete_my_account`, `cairn.purge_stale_accounts`).
- `audit_events` is append-only and write-only for the app. Do not use `INSERT ... RETURNING` on it because that needs a SELECT policy.
- Template and citation rows cannot be updated or deleted (only the `active` flag can flip). Audit rows cannot be updated, deleted, or truncated.
- `purge_expired_cases()`, `expire_trials()`, `claim_due_trial_reminders()`, `purge_stale_accounts()`, `purge_inactive_drafts()`, `purge_held_cases()`, `claim_action_confirmations()`, `complete_action_confirmation()`, `release_action_confirmation()`, and `claim_due_notifications()` are not granted to the app. Run them from a scheduled job as the owner (`api/scripts/send_outbound.py` for the sending ones). `identity_deletion_requests`, `action_confirmation_outbox`, and `action_confirmation_log` have no app grants. `app_settings` is read-only for the app.
- Never log names, dates of birth, SSN digits, or free-text fields. Audit rows hold opaque IDs only.
- Never commit credentials. Logins, passwords, and network rules are provisioned outside these scripts.
- The only exception is the fake test logins in `db/dev/test_logins.sql`, documented in `README.md`. They belong to the `cairn_dev` schema, which is never a migration and never exists outside a local or Codespaces database. Never move them into `db/migrations/`, never grant the app anything on `cairn_dev` beyond `cairn_dev.test_login`, and never pass `CAIRN_DEV_AUTH_SECRET` to a deployed API.
- Never edit an applied migration. `apply.sh` fails on a checksum change. Add a new migration.
- Tests and fixtures use fake data only (`example.test` domains, obviously fake names).

## Changes made while writing the scripts (already reflected in the docs)

- `task_templates.attorney_referral_note` added. A template that refers to an attorney must carry its wording.
- `audit_events.actor_id` is nullable (system actions) and `actor_id` and `case_id` have no foreign keys, so history survives account deletion and case purges.
- `consents` is keyed to the user only (no `case_id`).
- The loader also enforces that `due_offset_days` falls within the template's `journey_week`.
- `context_items` ships as an optional migration because the MVP has not yet decided where that store lives.
- `deceased` and `death_events` were merged into a single `deceased` table on 2026-09-22, since they are a one-to-one, always-together relationship. This also let a `date_of_death >= date_of_birth` check constraint be added directly, closing the gap noted below. If any environment already ran the pre-merge migrations, see the note under Migrations below before re-running `apply.sh`.

## Open questions for the product owner (do not guess)

1. Where does `CONTEXT_ITEM` live at MVP? Postgres JSONB (the optional migration) or a separate document store?
2. How should a state-specific template replace a generic US one for the same real-world task? Today a US template and an NH template both generate tasks if both match. A `supersedes` field may be needed.
3. Post-death authority: a power of attorney generally does not survive the principal's death. The relationship is self-declared today and only owners exist. Needs legal review before invitations ship. [LEGAL REVIEW REQUIRED]
4. Retention periods for `cases.purge_after`, `audit_events`, accounts that never finish onboarding (UC-REG-10), and accounts that finish but never create a case (UC-REG-13) are not set. `purge_stale_accounts` takes the last two as required arguments. They come from the privacy policy after counsel review. [LEGAL REVIEW REQUIRED]
5. Is application-layer encryption of `ssn_last4` wanted on top of encryption at rest?
6. Resolved: Auth0 is the identity provider. Accounts are created with Google, Apple, or an email address, with a passwordless magic link as the default (D-10). Migration `0007_sign_in_methods.sql` records the method on `users.sign_in_method`. Tenant setup is in `auth0/README.md`. Multi-factor and passkey requirements live in Auth0.
7. Templates with `domicile_state` rules do not match when `domicile_state` is null. Is that the intended behavior?
8. UC-REG-14 must match the crisis plan on Trello card 26, which has no written plan yet.
9. Subscriptions and billing are not designed. `users.status = 'subscribed'` is owner-only until they are.
10. Case creation decisions to confirm are listed in `docs/case-creation-gap-audit.md`: three trial reminders in one week and whether to wire the `journeys/` package into the loader. The registration trial copy now says the days begin with the first journey (UC-REG-08 change, 2026-09-25).
11. Keeping in touch and deletion decisions to confirm are listed in `docs/account-lifecycle-gap-audit.md`. Card 50 holds the open questions: SMS in the MVP, per journey or account-wide preferences, whether the trial reminder follows notification choices (Q1), the file format of the download (Q14), and whether confirmations follow SMS or push. UC-END-13 (the 7-day hold) is built from what the specs rely on and needs its own spec.

Resolved: `date_of_death` before `date_of_birth` is now rejected by the database directly (`death_not_before_birth` check constraint on `deceased`), now that the two dates live on one row.

## Migrations and the deceased/death_events merge

Migration `0002_core_tables.sql` was edited in place to merge `deceased` and `death_events` rather than layered as a new migration, since nothing indicates this schema has been applied anywhere beyond this repository's own test runs. `apply.sh` tracks each file by checksum, so if you have already run these migrations against a real database (including a personal dev database), running `apply.sh` again will fail on the checksum for `0002_core_tables.sql`, `0005_functions.sql`, and `0006_rls_and_grants.sql`. Drop and recreate that database, or write the merge as a new migration (`0007`) instead, which is the correct approach for any environment that has already shipped this schema.

## Suggested next tasks

1. Add CI that runs `db/tests/run.sh` against a Postgres 15 and 16 service container and runs `tools/load_templates.py --dry-run`.
2. Build the data-access layer: connection setup (search_path and `app.user_id`), sign up and sign in with `register_user` and `resolve_user`, and case creation as one transaction (case, first owner, deceased, death event, `generate_case_tasks`, audit event).
3. Emit audit events for reads of sensitive records (deceased details), not only writes.
4. Convert the existing journey map into template files, with a pull request review that includes counsel sign-off before a release tag.
5. Infrastructure outside this repo: database logins and secrets, encryption at rest, backups with restore drills, pgaudit, private networking.
6. Keep `docs/` in sync when the schema changes.

## Migration 0008 (registration and onboarding)

Adds the account fields from the registration spec to `users`, makes `consents` append-only with `auth_provider` and `client`, and adds `trial_reminders` and `identity_deletion_requests`. It also adds the onboarding, trial, deletion, and cleanup functions, plus the restrictive read-only policies. `context_items` is optional and created after the numbered migrations on a fresh database, so its read-only policies also ship as `db/optional/context_items_read_only.sql`, applied after `context_items_jsonb`. Accounts that existed before 0008 are marked as having finished onboarding, and their trial counts from their first case.

## Migration 0009 (voices)

Replaces `users.personality` (gentle, steady, straightforward) with `users.voice`, one of the four voices in `voices/manifest.yaml`: `steady_direct` (the default), `warm_patient`, `brisk_businesslike`, and `plain_practical`. The `users_voice_known` check constraint lists the ids. Existing rows keep the closest voice: gentle becomes `warm_patient`, steady becomes `steady_direct`, and straightforward becomes `plain_practical`. The app keeps its column-level UPDATE grant, now on `voice`. Adding a voice needs a new migration that replaces the constraint. Spec 1.2.0 records the change to UC-REG-12.

## Migration 0010 (case creation)

From `docs/cairn-case-creation-use-cases.json` spec 0.3.0. A case starts as a `draft` (the new default). `cairn.start_journey` moves it to `active`, pins `journey_template_key` and `journey_template_version`, sets `journey_started_at`, and on the account's first journey starts the trial and schedules the `trial_day_21`, `trial_day_27`, and `trial_ends_soon` reminders. The 0008 trigger that started the trial on the first case is dropped. Intake answers go in `case_intake_answers` (one row per data_fields key, with `answer_state` answered, skipped, or unsure). Journey selection rules are content (`content/journeys/journey-selection.json`), loaded into the immutable `journey_templates` table. `case_tasks` gains `selected` (whether the rules include the task, separate from the user's status) and the `check_on_this` and `not_today` statuses. `task_templates` gains `why_now`. `deceased` legal names become nullable because they are collected just in time, not at case creation. `case_members.relationship` becomes nullable because the role is an intake answer. `app_settings` holds `draft_retention_days` (28, DEC-07) and `trial_reminder_days_before` (3, OPEN-DECISION-02). `cairn.purge_inactive_drafts()` deletes drafts idle that long and must run at least daily as the owner. Existing cases keep `active` and get `journey_started_at = created_at`.

## Migration 0011 (keeping in touch, confirmations, deletion)

From `docs/cairn-case-creation-use-cases-2026-09-25.json` and `docs/cairn-account-use-cases-2026-09-25.json`. `notification_preferences` has one row per journey, keyed by `case_id`. The shape of a choice is checked by `cairn.notification_choice_valid`: `in_app_only` stands alone, and every other channel needs a reason, with each reason's timing. `start_journey` in the API writes the `in_app_only` default when nothing was chosen. `notification_log` records what was sent and why, without content, and drives the frequency limits. `cairn.claim_due_notifications()` picks journeys due a message by email. A case can be deleted now or held for `app_settings.case_deletion_hold_days` (7) through `cairn.request_case_deletion`, and `cairn.purge_held_cases()` deletes held cases when the hold ends. Each deletion the user asked for queues one confirmation in `action_confirmation_outbox`. The sender purges the address once it's sent and appends to `action_confirmation_log`. `cairn.delete_my_account` is replaced: it drops pending case confirmations and queues one account confirmation with no user id. `cairn.claim_due_trial_reminders` is replaced: it returns reminders only for users who chose email for a journey, and none more than 48 hours overdue. A change to a task's status or snooze now counts as activity on the case (`case_tasks_activity`).

## Conventions

- Write documentation and comments in plain prose. No em dashes. No semicolons within a sentence.
- Every jurisdiction-specific claim needs a citation to the issuing authority with a URL, and anything that needs an attorney is flagged as such. Cairn never presents itself as a substitute for legal counsel.
- Mark items needing attorney or compliance review with `[LEGAL REVIEW REQUIRED]`.
- Sample content in `content/tasks/` is illustrative and unreviewed. Do not ship it to real users.
