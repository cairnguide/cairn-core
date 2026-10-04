# Cairn API (MVP)

HTTP API for the MVP use cases in `database/docs/cairn-mvp-use-cases.md`, the registration use cases in `database/docs/cairn-registration-use-cases.json`, the case creation use cases in `database/docs/cairn-case-creation-use-cases.json`, and the 2026-09-25 changes to both (`database/docs/cairn-*-2026-09-25.json`, audited in `database/docs/account-lifecycle-gap-audit.md`), built on the MongoDB schema in `database/db/schema.py` and the data-access layer in `cairn_api/store.py`. FastAPI generates the contract from the request and response models, which are the only way data enters or leaves the service.

- Swagger UI: `/docs` when running. Static contract: [`openapi.json`](openapi.json) (OpenAPI 3.1).
- Requests reject unknown fields, trim whitespace, and validate against the same rules as the database validators (state codes, date order, lengths, allowed values).
- Errors use RFC 9457 problem details (`application/problem+json`) and never echo submitted values or database messages.
- Any response that moves a conversation forward includes a `next_step` with one prompt, so clients ask one question at a time.

## Use case map

| Use case | Endpoint |
|---|---|
| UC-REG-01, welcome | `GET /v1/welcome` (also `GET /v1/sign-in-methods`) |
| UC-REG-02 to UC-REG-05, create an account or sign in | `POST /v1/registrations` after every Auth0 sign-in |
| UC-REG-06, age | Not built, by product decision. No age is asked or stored |
| UC-REG-07 to UC-REG-10, acknowledgments and declining | `POST /v1/onboarding/acknowledgments/{privacy_terms,trial_terms,ai_notice}` (plus `GET /v1/policies`) |
| UC-REG-11, preferred name | `PUT /v1/onboarding/preferred-name` |
| UC-REG-12, personality (the user's voice) | `PUT /v1/onboarding/personality`, then `POST /v1/onboarding/case-handoff`. Saves `users.voice` |
| UC-REG-13, resume | `GET /v1/onboarding` (and the 200 from `POST /v1/registrations`) |
| UC-REG-14, I need a moment | `GET /v1/onboarding/need-a-moment`. Every onboarding response carries `support` |
| Settings | `GET` and `PATCH /v1/me` |
| UC-REG-15, delete my account (was UC-ACCT-01) | `GET /v1/me/deletion`, then `POST /v1/me/deletion`. The response says the user is signed out |
| UC-REG-16, download all my data | `GET /v1/me/data-export`, then `GET /v1/me/data-export/file` (JSON) |
| UC-REG-15, UC-REG-16, UC-CASE-20 asked in chat | `POST /v1/me/messages`. The client keeps `session` and sends it back |
| UC-CASE-19, how Cairn keeps in touch | `GET /v1/cases/{id}/notification-preferences`, `POST .../readback`, `PUT`, `POST .../push-permission` |
| UC-CASE-20, change it | `GET /v1/me/notification-preferences`, `POST /v1/me/notification-preferences/changes`, or the per-journey `PUT` |
| UC-CASE-21 and UC-END-13, delete a case now or in 7 days | `GET`, `POST`, and `DELETE /v1/cases/{id}/deletion` (the last keeps a held case) |
| UC-CASE-01, start a new case (a draft) | `POST /v1/cases`, then `POST .../intake/continue` (one question at a time) or `POST .../intake/messages` (own words) |
| UC-CASE-01, own words read back | `POST /v1/cases/{id}/intake/messages`, then `POST .../intake/confirmations` |
| UC-CASE-02 to UC-CASE-09, answer, skip, not sure, change | `PUT /v1/cases/{id}/intake/answers/{field}` |
| UC-CASE-02 and UC-CASE-03 preferences | `PUT /v1/cases/{id}/intake/preferences` |
| UC-CASE-10, pause and come back | `POST .../intake/pause`, then `GET /v1/cases/{id}` (resume) and `GET /v1/cases` |
| UC-CASE-11, review | `GET /v1/cases/{id}/review` |
| UC-CASE-12, the journey that fits | `GET .../journey/preview` (step 3 is `choose_notifications` until a choice is saved), `POST .../journey/start`, `POST .../journey/not-yet` |
| UC-CASE-13, first task | `POST .../journey/first-task` |
| UC-CASE-14, distress | Every intake turn. The client keeps `session` and sends it back. `POST .../intake/continue` resumes |
| UC-CASE-15, sensitive numbers | Every free-text field is redacted as it is parsed |
| UC-CASE-16, attorney referral | `POST .../intake/attorney-referrals`, or detected in `intake/messages` |
| UC-CASE-17, the death hasn't happened yet | `POST .../intake/death-not-yet`, or detected in `intake/messages` |
| UC-CASE-18, second case | `POST /v1/cases` again. Start keeps the existing trial |
| Legal identity, just in time | `PATCH /v1/cases/{id}/deceased`, only once the journey has started |
| UC-9, the four-week journey | `GET /v1/cases/{id}/journey` |
| UC-10, order death certificates | `GET /v1/cases/{id}/tasks/{task_id}`, `POST .../certificate-order` |
| UC-11, notify a bank | `POST .../tasks/{task_id}/institution-notices` |
| UC-12, step back from tasks | `POST /v1/cases/{id}/journey/pause` and `/resume` |
| UC-13, status across the case | `GET /v1/cases/{id}/status` |
| Any task status change or snooze | `PATCH /v1/cases/{id}/tasks/{task_id}` |

## Running

The repository [README](../README.md) has the full setup: `make setup` and `make run` locally or in GitHub Codespaces, and the Cloudflare deployment. The container entry point is `python -m cairn_api.serve`, which runs this API (`CAIRN_PROCESS=api`) or the scheduled jobs service (`CAIRN_PROCESS=jobs`, `cairn_api/jobs.py`). By hand:

```bash
python3 -m venv .venv && .venv/bin/pip install -e 'api[test]'
export MONGODB_URI='mongodb+srv://cairn_api:...@cluster.example.mongodb.net/'   # a user with only the cairnApp role
export CAIRN_MONGODB_DB=cairn              # optional: the database name. cairn by default
export CAIRN_AUTH0_DOMAIN=... CAIRN_AUTH0_AUDIENCE=... CAIRN_CLAIM_NAMESPACE=...   # see auth0/README.md
export CAIRN_TERMS_VERSION=... CAIRN_PRIVACY_VERSION=...
export CAIRN_PRIVACY_POLICY_URL=... CAIRN_TERMS_URL=... CAIRN_JOURNEY_MAP_URL=... CAIRN_SUPPORT_URL=...
export CAIRN_AI_PROVIDER_NAME=...          # named on the privacy step (UC-REG-07)
export CAIRN_REGISTRATION_COPY=...         # optional: a replacement copy file after legal review
export CAIRN_VOICES_DIR=...                # optional: the voices folder. Defaults to the repository's voices/
export CAIRN_CASE_COPY=...                 # optional: a replacement case creation copy file after legal review
export CAIRN_ESTATE_PLAN_MODE=add_on       # optional: OPEN-DECISION-01. Only add_on is built
export CAIRN_PRE_NEED_PATH=not_built       # optional: OPEN-DECISION-05. Only not_built is built
export CAIRN_OVERWHELM_SKIP_THRESHOLD=3    # optional: skips in a row that slow a session down (UC-CASE-14)
export CAIRN_CORS_ORIGINS=...              # optional: browser origins allowed to call the API, comma-separated
export CAIRN_DEV_AUTH_SECRET=...           # development only: turns on POST /v1/dev/token for the test logins
.venv/bin/uvicorn cairn_api.main:app --app-dir api
```

The database must be a MongoDB 7.0 or newer replica set (Atlas always is), because every request runs in one multi-document transaction. Run `database/db/apply.py` on it first, then load the templates. See [database/README.md](../database/README.md). UC-10 and UC-11 store their records in `context_items` (`CERT_ORDER` and `BANK_NOTICES`).

## Tests

```bash
cd api
../.venv/bin/pytest                                          # contract tests, no database
CAIRN_TEST_MONGODB_URI='mongodb://admin:admin@localhost:27017/?replicaSet=rs0' ../.venv/bin/pytest   # everything
```

With `CAIRN_TEST_MONGODB_URI` set, the suite creates a scratch database, applies `database/db/schema.py`, creates one login user per role, loads the templates as the loader user, runs every use case with the API connected as the `cairnApp` user, and then drops the database, its users, and its roles. `tests/test_data_security.py` checks the roles, the validators, the case boundary, read-only accounts, deletion, and the jobs (it replaces `verify.sql`). The URI must belong to an administrator of a scratch replica set with authentication on (`make setup` gives you one).

## Registration copy

Sign-up and onboarding copy lives in [`cairn_api/content/registration-copy.json`](cairn_api/content/registration-copy.json). `spec_copy` is the spec's copy, verbatim, and a test fails if it drifts. `draft_copy` holds strings the spec doesn't supply (personality samples, deletion wording, link labels). Those need product and legal review. To replace copy after legal review, point `CAIRN_REGISTRATION_COPY` at a new file. Acknowledgment versions come from a hash of the exact text, so changed wording is acknowledged again on next sign-in.

## Case creation

Case creation follows `database/docs/cairn-case-creation-use-cases-v2.json` (spec 2.0.0) and, where they differ, the Support and Crisis Plan (`database/docs/cairn-support-crisis-plan.json`). `database/docs/case-creation-v2-gap-audit.md` maps every acceptance criterion to its test.

- A new case is a draft. The 28 free days start only at the first `POST .../journey/start` (DEC-01), inside `store.Session.start_journey`. No payment information is asked for anywhere (DEC-02).
- Nothing about the person who died is collected at creation beyond the spec's data_fields. Legal names, dates of birth, SSNs, account numbers, and medical details never are. Free text is redacted as it is parsed (`RedactedText`), never stored, and only confirmed field values are saved.
- Journey selection rules are template data in `database/content/journeys/journey-selection.json`, loaded into `journey_templates`. `cairn_api/journey_selection.py` only evaluates them.
- Every turn acknowledges first, asks at most one question, and has one `next_step`. Every question offers Skip for now and I'm not sure, and every response carries `read_aloud`.
- Care levels (UC-CASE-14 and the crisis plan) are in a client-held `session` and never stored. Level 2 is two overwhelm signals or three skips in a row. Levels 3 and 4 use the steady_care voice, ask nothing until the user continues, pause the tasks (a care rest, which pauses the free days on an active journey), and show no billing wording. Level 4 says 988 first and adds one to the anonymous SB 243 count.
- New endpoints: `POST .../intake/transcripts` (speech, UC-CASE-22), `POST .../intake/level-2-choice`, `POST .../intake/check-in` (DEC-26-04), and `POST /v1/cases/{id}/take-a-break`. Responses carry `care_level`, `announcements` (the AI reminder, rest offers, and the check-in, for screen readers), and `controls` (the take a break, read aloud, and speak labels).
- Open decisions OPEN-04 to OPEN-10 are `CAIRN_*` settings in `config.py`, with the spec's defaults. The app refuses to start with any other value until the decision is made.
- Free-text extraction (`extraction.py`) and distress signals (`safety.py`) are rule-based stand-ins until a model replaces them. The phrase lists need clinical sign-off (DEC-26-06).

Copy lives in [`cairn_api/content/case-creation-copy.json`](cairn_api/content/case-creation-copy.json), with the same `spec_copy`, `flow_copy`, and `draft_copy` sections as the registration copy, and a test that keeps `spec_copy` verbatim. Point `CAIRN_CASE_COPY` at a reviewed file to replace it.

## Voices

The personality step (UC-REG-12) offers the voices in [`voices/manifest.yaml`](../voices/manifest.yaml). The label, tagline, and sample reply on that screen come from the manifest and each voice's reference response. The choice is stored as `users.voice` and can be changed with `PATCH /v1/me`. "Choose for me" saves the manifest's `default_voice`.

The app loads and checks the folder at startup and refuses to start if it can't: every file must exist inside the folder, each voice file's `id` and onboarding label must match the manifest, every reference response must answer the same user message, and the manifest must list exactly the ids in `schemas.Voice`. The database accepts only those ids too (`VOICES` in `database/db/schema.py`, checked by the users validator), so every stored voice can be loaded. `VoiceCatalog.system_blocks` builds the prompt for a user's stored voice in the same cache order as `voices/assemble_prompt.py`. Nothing calls the model yet.

Adding a voice means a new voice file and manifest entry, a new value in `schemas.Voice`, the id added to `VOICES` in `database/db/schema.py` (then `database/db/apply.py`), and a confirmation string `voice_<id>_confirm` in the copy file. `tests/test_voices.py` fails until all four agree.

## Scheduled jobs (the cairnJobs user, not the app)

| Job | Function | Notes |
|---|---|---|
| Trial status | `maintenance.expire_trials`, job `expire_trials` | Reporting only. Read-only is enforced from `trial_ends_at` directly |
| Outbound email | `python api/scripts/send_outbound.py` or job `outbound` | Every 5 minutes. Deletion confirmations (one each, address purged once sent), trial reminders for users who chose email, the notifications users chose, and a check-in the user said yes to. Every notification and check-in passes a check that it names no person who died and no circumstance. Nothing about tasks is sent during a rest. SMTP, provider not chosen yet. Register the sending domain with Apple's Private Email Relay Service |
| Held case deletion (UC-END-13) | `maintenance.purge_held_cases`, job `purge_held_cases` | At least hourly. Deletes cases whose 7-day hold has ended and queues their confirmation |
| Trial clocks (DEC-26-01) | `maintenance.settle_trial_clocks`, job `settle_trial_clocks` | Hourly. Starts the free days again when a care rest ends on its own, and moves `trial_ends_at` later by the paused time |
| Draft cleanup (DEC-07) | `maintenance.purge_inactive_drafts`, job `purge_inactive_drafts` | At least daily. Deletes drafts idle for `app_settings.draft_retention_days` (28), with their answers and context. Never touches active cases |
| Identity cleanup | `python api/scripts/identity_cleanup.py` or job `identity_cleanup` | Deletes Auth0 users and revokes Apple tokens after account deletion |
| Stale accounts | `maintenance.purge_stale_accounts(db, pending, no_case)` | Periods come from the retention schedule [LEGAL REVIEW REQUIRED] |

On Cloudflare, Cron Triggers in `cloudflare/wrangler.jsonc` run the first five through `cairn_api/jobs.py` (`POST /jobs/{name}` in the private jobs container). Stale account purging isn't scheduled until the retention periods are set.

## Security choices

- Every request runs in one MongoDB transaction (snapshot reads, majority writes) as one user, through `store.Session`. The caller lives on the session, never on the pooled connection. The API connects as a user that holds only the `cairnApp` role.
- The case boundary is enforced in `store.Session`, the only module that reads or writes case data (a test fails if any other module touches MongoDB). Onboarding order, the trial start, and read-only are enforced there too: `advance_onboarding` refuses skipped steps, `start_journey` starts the trial in the same transaction as the first journey and writes `trial_started_at` only while it is unset, and every case write checks the account. `consents` and `audit_events` are append-only. Collection validators and the role's privileges are the backstop (see [database/README.md](../database/README.md)).
- A write that collides with another request on the same document returns 409 `try_again` instead of waiting, because MongoDB aborts the second transaction rather than blocking it. Clients can retry it as is.
- Accounts are created with Google, Apple, or an email address, all through Auth0 (see [auth0/README.md](../auth0/README.md)). Auth0 handles passwords, passkeys, and email confirmation. The API only verifies Auth0's signed token and refuses any other sign-in connection. The email address and sign-in method come from the token, never from the request body, and registration is refused until `email_verified` is true.
- Every write adds an `audit_events` row in the same transaction. Reads of the deceased's details (`GET /v1/cases/{id}`, `/status`) are audited too.
- The service logs only the error class, code, and rule name. It never logs names, dates, free text, or a validator's error details (they include the values it rejected).
- `ssn_last4` is never read or returned. Cause of death is not collected. Pausing has no reason field.
- A missing case and a case you can't access both return the same 403, so case IDs can't be probed.

## Decisions to confirm with the product owner

1. **Accounts aren't linked across sign-in methods.** An email that already has an account gets a 409 naming the method used last time. Linking a second method after signing in with the first (UC-REG-05) isn't built. Support merges accounts with `database/docs/support-account-merge.md`.
2. **Fiduciary flag (UC-4, UC-8).** The spec says not to add a user-level column without sign-off. The relationship picked at case hand-off only shapes the response (`language_profile`). It's stored per case as `case_members.relationship`.
3. **POA role confirmation (UC-3).** `confirm_current_role` offers `named_executor`, `next_of_kin`, and `not_sure` as client routing values only. The note that POA authority ends at death is marked `legal_review_required`. [LEGAL REVIEW REQUIRED]
4. **`context_items` is a MongoDB collection** (open question 1), in the same database as everything else.
5. **Task categories and kinds** (UC-11, UC-13) are keyed by `task_key` in `cairn_api/journey.py`. They should become a `category` field in the template content schema.
6. **Pause length** defaults to 30 days (maximum 90), because `tasks_paused_until` needs an end time. When it expires, the tasks come back on their own.
7. **Case list for fiduciaries** is not built, as UC-8 asks. It should be tracked as its own ticket.
8. **New templates** `order_death_certificates`, `choose_funeral_provider`, and `notify_banks` in `database/content/tasks/us/` are unreviewed drafts, like the existing samples. They need counsel review and `last_verified_on` dates before release.
9. **Crisis plan.** UC-REG-14 must match Trello card 26, which has no written plan yet. The distress phrase list in `onboarding.py` and the draft pause wording need checking against it.
10. **Subscriptions.** Nothing sells a subscription yet. `users.status = 'subscribed'` can only be set by an administrator until billing is built.
