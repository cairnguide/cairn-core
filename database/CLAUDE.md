# Cairn MVP database: handoff for Claude Code

Cairn is an AI assistant that walks a family through the logistics of a death, step by step, while protecting their time to grieve. This bundle is the MVP database layer. The MVP covers sign up, creating a case, collecting information about the user and the deceased, and generating a personalized four-week journey of first steps.

The data involved is highly sensitive (identity of a deceased person, place of death, veteran and will status, a partial SSN, and who is authorized to act). Security comes first. Every change must keep the checks in `api/tests/test_data_security.py` passing, along with the rest of the API suite.

The database moved from PostgreSQL to MongoDB on 2026-10-01 (see "The move to MongoDB" below). [README.md](README.md) is the operations guide: Atlas setup, roles, users, and moving existing data.

## Read first

- `docs/cairn-mvp-data-model.md`: the MVP conceptual model, decision log, release gate rules, and example template.
- `docs/cairn-conceptual-data-model.md`: the full future model. The MVP is a strict subset plus a few flag columns.
- `docs/cairn-registration-use-cases.json`: registration and onboarding (UC-REG-01 to UC-REG-14, UC-ACCT-01), spec 1.1.0. Replaces the registration part of UC-1 to UC-13. `docs/registration-gap-audit.md` records what is built and what is open. UC-REG-06 (age confirmation) is not built, by product decision: Cairn does not ask for or store the user's age.
- `docs/cairn-case-creation-use-cases-v2.json`: case creation (UC-CASE-01 to UC-CASE-24), spec 2.0.0, the one built. It supersedes the earlier case creation files below and defers to `docs/cairn-support-crisis-plan.json` (card 26) for crisis levels, rests, and the check-in. `docs/case-creation-v2-gap-audit.md` maps every criterion to its test, lists where the two specs differ, and lists the decisions to confirm.
- `docs/cairn-case-creation-use-cases.json`: case creation (UC-CASE-01 to UC-CASE-18), spec 0.3.0, superseded by v2. Replaces UC-5 to UC-9. `docs/case-creation-gap-audit.md` maps every acceptance criterion to its test and lists what is open.
- `docs/cairn-case-creation-use-cases-2026-09-25.json` (draft 0.4 changes: UC-CASE-19 to UC-CASE-21, keeping in touch and confirmations) and `docs/cairn-account-use-cases-2026-09-25.json` (UC-REG-15 delete my account, which replaces UC-ACCT-01, and UC-REG-16 download all my data). `docs/account-lifecycle-gap-audit.md` maps them to tests and lists what is open, including UC-END-13, which these specs rely on but don't define.
- `docs/account-creation-review-gap-audit.md`: the 2026-10-05 review of UC-REG-01 to UC-REG-16 against the code. Closes the UC-REG-04, 05, 10, 13, and 14 gaps, moves every email to Twilio SendGrid, and adds a Twilio sender for text messages that isn't offered yet (OPEN-05).
- `docs/backend-standup-gap-audit.md`: what it takes to stand up the API so a person can register, what was built for local sign-in (test logins, `POST /v1/dev/token`), and the work still to do outside the code.

## Decisions already made (ask the product owner before changing)

1. MongoDB 7.0 or newer, as a replica set (MongoDB Atlas in production), holds users, cases, membership, deceased, journey progress, consent, and audit. Changed from PostgreSQL 15 by the product owner on 2026-10-01.
2. The case is the security boundary. Access is decided by each case's `members` and enforced in one data-access layer (`api/cairn_api/store.py`) that every read and write goes through, backed by collection validators and least-privilege MongoDB roles. Changed from PostgreSQL row-level security by the product owner on 2026-10-01, because MongoDB has no row-level security.
3. Journey templates and citations are authored as versioned JSON files in `content/` and loaded at deploy into read-only, immutable-per-version collections. A correction is a new version, never an edit. `case_tasks` pins to the exact version a family was shown.
4. A derived journey snapshot in a document store (for AI context assembly) is deferred. Add it only if AI context assembly becomes a measurable cost, task reads dominate load, or templates diverge structurally by state.
5. No document upload at MVP. There is no vault and no object store.
6. Only the last four digits of the SSN are collected. Full SSN and VA file numbers are deferred.
7. Do not persist any inference about a user's emotional state. `cases.tasks_paused_until` lets the product step back from task mode without recording why.
8. The MVP allows one owner per case. Each case's `members` array exists so invitations and co-executors need no schema change later. `MEMBER_ROLES` in `db/schema.py` allows only `owner` until they ship.

## Layout

```
db/schema.py       collections, validators, indexes, roles, and settings: the whole schema in one place
db/apply.py        makes a database match schema.py (CAIRN_ADMIN_MONGODB_URI, an administrator)
content/schema/    JSON Schema for template files
content/tasks/     template files, one folder per jurisdiction (us, nh, ...)
content/journeys/  journey selection rules: base paths, add-ons, completed items (case creation spec)
tools/             load_templates.py, create_login_user.py, move_from_postgres.py, seed_test_db.py (local test logins)
docs/              data model documents
```

The data-access layer is `api/cairn_api/store.py` (one Session per request transaction) and the jobs' side is `api/cairn_api/maintenance.py`.

## Commands

```bash
# The API suite and the data security suite on a scratch replica set (creates and drops its own database)
make test-db        # or: cd api && CAIRN_TEST_MONGODB_URI='mongodb://admin:...@localhost:27017/?replicaSet=rs0' pytest
make db-check       # tests/test_data_security.py only

# Apply the schema to a real database as an administrator. On Atlas add --skip-roles (roles live in Atlas).
CAIRN_ADMIN_MONGODB_URI=... python3 db/apply.py --db cairn
python3 db/apply.py --print-roles     # the roles as Atlas Admin API bodies

# Validate content only (no database). Fails on unreviewed templates by design.
python3 tools/load_templates.py --dry-run

# Load templates as the cairnLoader user. Development only may add --allow-unreviewed.
CAIRN_LOADER_MONGODB_URI=... python3 tools/load_templates.py --git-release <tag>
```

Install the tools' dependencies with `pip install -r tools/requirements.txt`.

## Security invariants (do not weaken)

- The API connects as a login user that holds only the `cairnApp` role. Never an administrator, never the jobs or loader user, and no Cairn role ever gets `bypassDocumentValidation` or any action beyond find, insert, update, and remove. `test_no_cairn_role_can_bypass_validation_or_manage_users` checks it.
- Every request is one multi-document transaction (`db.py`, snapshot reads, majority writes) as one user. The caller lives on `store.Session`, never on a pooled connection. An unknown user sees nothing.
- `store.py` is the only module that reads or writes case data. `test_only_the_data_layer_touches_mongodb` fails if another API module imports pymongo or reaches a collection. A new case-scoped collection gets its reads behind the visibility filter (`_visible`) and its writes behind `_case_for_write`, and is added to `CASE_SCOPED` so it is deleted with its case.
- Writes to case data need an owner or co_executor membership and `account_can_write()`. Drafts stay editable on a read-only account (UC-CASE-18) through `case_writable`. Never check either on deletion, so data can always be deleted. The one other exception is notification preferences: changing how Cairn keeps in touch is always free, including on a read-only account (D-2026-09-25-F1).
- The app sets only `ACCOUNT_FIELDS` on users and `CASE_FIELDS` on cases. Sign up and sign in go through `create_account` and `resolve_user`. Onboarding moves only through `advance_onboarding`, one step at a time.
- A second sign-in is added only through `link_identity`, from a request signed in with a sign-in the account already has, and with the new sign-in's token verified (UC-REG-05). Never link automatically. One sign-in belongs to one account (`users_linked_subject_uq`). Deleting an account, or purging an unfinished one, queues identity cleanup for every sign-in on it (`identity_deletions`).
- The trial starts in `start_journey`, in the same transaction that moves the account's first case from draft to active (DEC-01). Creating or editing a draft never starts it. `trial_started_at` is written only with a filter that matches while it is unset, and the users validator holds `trial_ends_at` to at least 672 hours after it. `trial_ends_at` moves later only through a care rest (DEC-26-01): `begin_rest` sets `trial_clock_paused_at`, and `settle_trial_clock` moves the end and any unsent reminder by exactly the paused time. Never move it any other way, and never earlier.
- Only `start_journey` moves a case out of draft, and nothing moves it back. The cases validator refuses a draft with journey fields and a started case without them. `last_activity_at` is always the server's time.
- Deleting, downloading, and changing notifications never check `account_can_write`, onboarding, or acknowledgments. `request_case_deletion`, `cancel_case_deletion`, and `delete_my_account` check ownership only.
- Nothing is sent outside the app unless the user chose it (`notification_preferences`, default `in_app_only`), except the one confirmation of a deletion the user asked for (`action_confirmation_outbox`). Outbound text never names the person who died, the circumstance, task details, or anything deleted. The outbox holds an address only until it is sent. `action_confirmation_log` and `notification_log` never hold content or addresses, and their validators have no field that could. `notification_preferences` is never used for marketing or advertising.
- SMS is not an accepted channel. The Twilio sender and Verify client exist (`api/cairn_api/twilio_client.py`), but adding the channel needs card 50's decision (OPEN-05), legal review of the consent wording, and an encrypted field for the number with its key management. Never store a phone number in plain text.
- Legal identity is never collected on a draft. `save_deceased` refuses a draft.
- `case_intake_answers` holds only the spec's data_fields. `intake_value_valid` in `store.py` and `INTAKE_VALUE_SHAPES` in `db/schema.py` check each value's shape, twice. Never add a free-text field to it. Circumstance stores the enum only. `additionalProperties` stays false on every collection.
- Nothing about a user's distress is stored. Safety modes live in the client-held session (decision 7). A crisis may store only `users.trial_clock_paused_at`, `cases.check_in_at` (after the user says yes), and one added to the anonymous monthly `safety_referral_counts`, which has no user, case, or time of day. `cases.loss_survivor_resources` is set only when the user says the death was by suicide. Nothing about the user's age is stored (UC-CASE-24).
- Speech input never reaches the API as audio. Transcripts are `RedactedText`, like typed text (UC-CASE-22).
- `consents` is append-only: the app role can't update it, and only `delete_my_account` and `purge_stale_accounts` remove rows. (The app role keeps remove on `consents` for account deletion. That rule is in `store.py`, not the role.)
- `audit_events` is append-only, and write-only for the app and the jobs: their roles have insert and no find.
- Template rows are never updated or deleted except the `active` flag, which only the loader changes. The loader role can't delete them. The loader's code is what keeps its updates to `active`.
- The purges, claims, and sends are in `maintenance.py`, run by the jobs container as `cairnJobs`. The app role can't read `identity_deletion_requests`, `action_confirmation_outbox`, or `action_confirmation_log`, can't touch `job_locks`, and reads `app_settings` only.
- Never log names, dates of birth, SSN digits, free-text fields, or a validator's error details (they contain the rejected values). Audit rows hold opaque IDs only.
- Never commit credentials. Logins, passwords, and network rules are provisioned outside these scripts.
- The only exception is the fake test logins that `tools/seed_test_db.py` writes, documented in `README.md`. They belong to the separate `cairn_dev` database, which `db/schema.py` never describes and which never exists outside a local or Codespaces deployment. Never add them to `db/schema.py` or `db/apply.py`, never give the `cairnDevTestLogins` role anything beyond `find` on `cairn_dev.test_logins`, never give `cairnApp` access to `cairn_dev`, and never pass `CAIRN_DEV_AUTH_SECRET` to a deployed API.
- Never edit a data migration in `db/apply.py` that has run anywhere. Apply fails on a checksum change. Add a new one.
- Tests and fixtures use fake data only (`example.test` domains, obviously fake names).

## Changes made while writing the scripts (already reflected in the docs)

- `task_templates.attorney_referral_note` added. A template that refers to an attorney must carry its wording.
- `audit_events.actor_id` is nullable (system actions) and `actor_id` and `case_id` have no foreign keys, so history survives account deletion and case purges.
- `consents` is keyed to the user only (no `case_id`).
- The loader also enforces that `due_offset_days` falls within the template's `journey_week`.
- `context_items` is a collection in the same MongoDB database (open question 1).
- `deceased` and `death_events` were merged into a single `deceased` table on 2026-09-22, since they are a one-to-one, always-together relationship. This also let the database check `date_of_death >= date_of_birth` directly (now the deceased validator and `save_deceased`).

## Open questions for the product owner (do not guess)

1. Resolved by the move to MongoDB: `CONTEXT_ITEM` is the `context_items` collection, alongside everything else.
2. How should a state-specific template replace a generic US one for the same real-world task? Today a US template and an NH template both generate tasks if both match. A `supersedes` field may be needed.
3. Post-death authority: a power of attorney generally does not survive the principal's death. The relationship is self-declared today and only owners exist. Needs legal review before invitations ship. [LEGAL REVIEW REQUIRED]
4. Retention periods for `cases.purge_after`, `audit_events`, accounts that never finish onboarding (UC-REG-10), and accounts that finish but never create a case (UC-REG-13) are not set. The daily `purge_stale_accounts` job reads the last two from `CAIRN_PENDING_ACCOUNT_RETENTION_DAYS` and `CAIRN_NO_CASE_ACCOUNT_RETENTION_DAYS`, which have no defaults, and is skipped until the first is set. They come from the privacy policy after counsel review. [LEGAL REVIEW REQUIRED]
5. Is application-layer encryption of `ssn_last4` wanted on top of encryption at rest?
6. Resolved: Auth0 is the identity provider. Accounts are created with Google, Apple, or an email address, with a passwordless magic link as the default (D-10). Migration `0007_sign_in_methods.sql` records the method on `users.sign_in_method`. Tenant setup is in `auth0/README.md`. Multi-factor and passkey requirements live in Auth0.
7. Templates with `domicile_state` rules do not match when `domicile_state` is null. Is that the intended behavior?
8. Resolved: the crisis plan is written (`docs/cairn-support-crisis-plan.json`), and sign-up now reads free text with the same detector as case creation (`safety.classify`). Clinical sign-off of the phrase list (DEC-26-06) is still pending.
9. Subscriptions and billing are not designed. `users.status = 'subscribed'` is administrator-only until they are.
10. Case creation decisions to confirm are listed in `docs/case-creation-gap-audit.md`: three trial reminders in one week and whether to wire the `journeys/` package into the loader. The registration trial copy now says the days begin with the first journey (UC-REG-08 change, 2026-09-25).
11. Keeping in touch and deletion decisions to confirm are listed in `docs/account-lifecycle-gap-audit.md`. Card 50 holds the open questions: SMS in the MVP, per journey or account-wide preferences, whether the trial reminder follows notification choices (Q1), the file format of the download (Q14), and whether confirmations follow SMS or push. UC-END-13 (the 7-day hold) is built from what the specs rely on and needs its own spec.

12. Atlas network access from Cloudflare Containers (see README.md, Atlas setup step 4). Whether a fixed-egress proxy is needed before production.
13. Two requests that change the same document at the same moment: MongoDB aborts the second transaction instead of making it wait, and the API answers 409 `try_again`. Should the API retry once on its own?

Resolved: `date_of_death` before `date_of_birth` is rejected by the database directly (the deceased validator), now that the two dates live on one document.

## The move to MongoDB (2026-10-01)

The product owner moved the database from PostgreSQL to MongoDB (Atlas in production), with access control in the application's data-access layer. The SQL migrations, `apply.sh`, `verify.sql`, and `create_login_role.py` were removed. Their history is in git. Where each PostgreSQL protection went:

| PostgreSQL | MongoDB |
|---|---|
| CHECK constraints, domains | `$jsonSchema` and `$expr` validators in `db/schema.py`, for every writer |
| Column grants, `cairn_app`, `cairn_loader`, the owner | The `cairnApp`, `cairnLoader`, and `cairnJobs` roles in `db/schema.py` |
| Row-level security policies | `store.Session`: `_visible`, `_member`, `_case_for_write`, `account_can_write`, `case_writable` |
| SECURITY DEFINER functions | `store.Session` methods with the same names (`start_journey`, `advance_onboarding`, `delete_my_account`, ...) |
| Owner-only functions | `maintenance.py`, run by the jobs as `cairnJobs` |
| Triggers (activity, trial set once, guards) | The same rules inside the `store.Session` writes |
| `case_members`, `template_citations` | Embedded arrays: a case's `members`, a template's `citations` |
| `verify.sql` | `api/tests/test_data_security.py` |

`tools/move_from_postgres.py` copies an existing PostgreSQL database across once (README.md).

What the database itself no longer enforces, because MongoDB can't: that the app reads only its own cases (the roles are per collection, not per document), that `trial_started_at` is never reset (validators can't see the old document), and that consents and template rows are never changed by code that holds the privilege. Those rules live in `store.py` and the loader, and the tests check them.

## Suggested next tasks

1. Decide open questions 12 and 13 (Atlas network access, retrying write conflicts).
2. Decide whether the API should take on MongoDB Client-Side Field Level Encryption for `ssn_last4` and the legal names (open question 5).
3. Emit audit events for reads of sensitive records (deceased details), not only writes.
4. Convert the existing journey map into template files, with a pull request review that includes counsel sign-off before a release tag.
5. Infrastructure outside this repo: Atlas users and secrets, encryption at rest, backups with restore drills, Atlas database auditing, network access.
6. Keep `docs/` in sync when the schema changes.

## History: the PostgreSQL migrations (removed 2026-10-01)

These notes record why the data model looks the way it does. The rules they describe now live in `db/schema.py` and `store.py`, under the same names where possible.

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
