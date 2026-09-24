# Cairn MVP database: handoff for Claude Code

Cairn is an AI assistant that walks a family through the logistics of a death, step by step, while protecting their time to grieve. This bundle is the MVP database layer. The MVP covers sign up, creating a case, collecting information about the user and the deceased, and generating a personalized four-week journey of first steps.

The data involved is highly sensitive (identity of a deceased person, place of death, veteran and will status, a partial SSN, and who is authorized to act). Security comes first. Every change must keep the checks in `db/tests/verify.sql` passing.

## Read first

- `docs/cairn-mvp-data-model.md`: the MVP conceptual model, decision log, release gate rules, and example template.
- `docs/cairn-conceptual-data-model.md`: the full future model. The MVP is a strict subset plus a few flag columns.
- `docs/cairn-registration-use-cases.json`: registration and onboarding (UC-REG-01 to UC-REG-14, UC-ACCT-01), spec 1.1.0. Replaces the registration part of UC-1 to UC-13. `docs/registration-gap-audit.md` records what is built and what is open. UC-REG-06 (age confirmation) is not built, by product decision: Cairn does not ask for or store the user's age.

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
db/tests/          verify.sql (security and behavior checks) and run.sh
content/schema/    JSON Schema for template files
content/tasks/     template files, one folder per jurisdiction (us, nh, ...)
tools/             load_templates.py (validate and load templates)
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
- The trial starts in a trigger on `cases`, so it is always in the same transaction as the first case. `trial_started_at` is set once and a trigger refuses any change, the owner included.
- Read-only after the trial and "no case before onboarding" are restrictive RLS policies calling `cairn.account_can_write()`. Keep them on every new case-scoped table (INSERT and UPDATE), and never on DELETE, so data can always be deleted.
- `consents` is append-only. Rows go only through the cascade from deleting the account (`cairn.delete_my_account`, `cairn.purge_stale_accounts`).
- `audit_events` is append-only and write-only for the app. Do not use `INSERT ... RETURNING` on it because that needs a SELECT policy.
- Template and citation rows cannot be updated or deleted (only the `active` flag can flip). Audit rows cannot be updated, deleted, or truncated.
- `purge_expired_cases()`, `expire_trials()`, `claim_due_trial_reminders()`, and `purge_stale_accounts()` are not granted to the app. Run them from a scheduled job as the owner. `identity_deletion_requests` has no app grants.
- Never log names, dates of birth, SSN digits, or free-text fields. Audit rows hold opaque IDs only.
- Never commit credentials. Logins, passwords, and network rules are provisioned outside these scripts.
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

## Conventions

- Write documentation and comments in plain prose. No em dashes. No semicolons within a sentence.
- Every jurisdiction-specific claim needs a citation to the issuing authority with a URL, and anything that needs an attorney is flagged as such. Cairn never presents itself as a substitute for legal counsel.
- Mark items needing attorney or compliance review with `[LEGAL REVIEW REQUIRED]`.
- Sample content in `content/tasks/` is illustrative and unreviewed. Do not ship it to real users.
