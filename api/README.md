# Cairn API (MVP)

HTTP API for the MVP use cases in `database/docs/cairn-mvp-use-cases.md`, the registration use cases in `database/docs/cairn-registration-use-cases.json`, and the case creation use cases in `database/docs/cairn-case-creation-use-cases.json`, built on the schema in `database/db/migrations`. FastAPI generates the contract from the request and response models, which are the only way data enters or leaves the service.

- Swagger UI: `/docs` when running. Static contract: [`openapi.json`](openapi.json) (OpenAPI 3.1).
- Requests reject unknown fields, trim whitespace, and validate against the same rules as the database (state codes, date order, lengths, allowed values).
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
| UC-ACCT-01, delete account | `GET /v1/me/deletion`, then `POST /v1/me/deletion` |
| UC-CASE-01, start a new case (a draft) | `POST /v1/cases`, then `POST .../intake/continue` (one question at a time) or `POST .../intake/messages` (own words) |
| UC-CASE-01, own words read back | `POST /v1/cases/{id}/intake/messages`, then `POST .../intake/confirmations` |
| UC-CASE-02 to UC-CASE-09, answer, skip, not sure, change | `PUT /v1/cases/{id}/intake/answers/{field}` |
| UC-CASE-02 and UC-CASE-03 preferences | `PUT /v1/cases/{id}/intake/preferences` |
| UC-CASE-10, pause and come back | `POST .../intake/pause`, then `GET /v1/cases/{id}` (resume) and `GET /v1/cases` |
| UC-CASE-11, review | `GET /v1/cases/{id}/review` |
| UC-CASE-12, the journey that fits | `GET .../journey/preview`, `POST .../journey/start`, `POST .../journey/not-yet` |
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

```bash
python3 -m venv .venv && .venv/bin/pip install -e 'api[test]'
export DATABASE_URL=postgresql://cairn_api_login@host/cairn   # a login role that is a member of cairn_app
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
.venv/bin/uvicorn cairn_api.main:app --app-dir api
```

The database needs migrations 0001 to 0010 **and** the optional `context_items_jsonb` and `context_items_read_only` migrations, in that order (`db/apply.sh context_items_jsonb context_items_read_only`). UC-10 and UC-11 store their records in `context_items` (`CERT_ORDER` and `BANK_NOTICES`).

## Tests

```bash
cd api
../.venv/bin/pytest                                          # contract tests, no database
CAIRN_TEST_ADMIN_URL=postgresql://admin@localhost/postgres ../.venv/bin/pytest   # plus the use case suite
```

The use case suite creates a scratch database, applies the migrations, loads the templates, runs every use case as `cairn_app` under row-level security, and then drops the database.

## Registration copy

Sign-up and onboarding copy lives in [`cairn_api/content/registration-copy.json`](cairn_api/content/registration-copy.json). `spec_copy` is the spec's copy, verbatim, and a test fails if it drifts. `draft_copy` holds strings the spec doesn't supply (personality samples, deletion wording, link labels). Those need product and legal review. To replace copy after legal review, point `CAIRN_REGISTRATION_COPY` at a new file. Acknowledgment versions come from a hash of the exact text, so changed wording is acknowledged again on next sign-in.

## Case creation

Case creation follows `database/docs/cairn-case-creation-use-cases.json` (spec 0.3.0). `database/docs/case-creation-gap-audit.md` maps every acceptance criterion to its test.

- A new case is a draft. The 28 free days start only at the first `POST .../journey/start` (DEC-01), inside `cairn.start_journey`. No payment information is asked for anywhere (DEC-02).
- Nothing about the person who died is collected at creation beyond the spec's data_fields. Legal names, dates of birth, SSNs, account numbers, and medical details never are. Free text is redacted as it is parsed (`RedactedText`), never stored, and only confirmed field values are saved.
- Journey selection rules are template data in `database/content/journeys/journey-selection.json`, loaded into `journey_templates`. `cairn_api/journey_selection.py` only evaluates them.
- Every turn acknowledges first, asks at most one question, and has one `next_step`. Every question offers Skip for now and I'm not sure, and every response carries `read_aloud`.
- Safety modes (UC-CASE-14) are in a client-held `session` and never stored. In acute_distress and risk_of_harm the voice is steady_care and no question is asked until the user continues.
- Free-text extraction (`extraction.py`) and distress signals (`safety.py`) are rule-based stand-ins until a model and the Trello card 26 crisis plan replace them.

Copy lives in [`cairn_api/content/case-creation-copy.json`](cairn_api/content/case-creation-copy.json), with the same `spec_copy`, `flow_copy`, and `draft_copy` sections as the registration copy, and a test that keeps `spec_copy` verbatim. Point `CAIRN_CASE_COPY` at a reviewed file to replace it.

## Voices

The personality step (UC-REG-12) offers the voices in [`voices/manifest.yaml`](../voices/manifest.yaml). The label, tagline, and sample reply on that screen come from the manifest and each voice's reference response. The choice is stored as `users.voice` and can be changed with `PATCH /v1/me`. "Choose for me" saves the manifest's `default_voice`.

The app loads and checks the folder at startup and refuses to start if it can't: every file must exist inside the folder, each voice file's `id` and onboarding label must match the manifest, every reference response must answer the same user message, and the manifest must list exactly the ids in `schemas.Voice`. The database accepts only those ids too (`users_voice_known`, migration 0009), so every stored voice can be loaded. `VoiceCatalog.system_blocks` builds the prompt for a user's stored voice in the same cache order as `voices/assemble_prompt.py`. Nothing calls the model yet.

Adding a voice means a new voice file and manifest entry, a new value in `schemas.Voice`, a migration that replaces `users_voice_known`, and a confirmation string `voice_<id>_confirm` in the copy file. `tests/test_voices.py` fails until all four agree.

## Scheduled jobs (owner role, not the app)

| Job | Function | Notes |
|---|---|---|
| Trial status | `cairn.expire_trials()` | Reporting only. Read-only is enforced from `trial_ends_at` directly |
| Trial reminder email | `cairn.claim_due_trial_reminders(limit)` | Returns due reminders (day 21, day 27, and `trial_ends_soon`) and marks them sent. Render with `account.reminder_text`. No email sender yet |
| Draft cleanup (DEC-07) | `cairn.purge_inactive_drafts()` | At least daily. Deletes drafts idle for `app_settings.draft_retention_days` (28), with their answers and context. Never touches active cases |
| Identity cleanup | `python api/scripts/identity_cleanup.py` | Deletes Auth0 users and revokes Apple tokens after account deletion |
| Stale accounts | `cairn.purge_stale_accounts(pending, no_case)` | Periods come from the retention schedule [LEGAL REVIEW REQUIRED] |

## Security choices

- Every request runs in one transaction with `app.user_id` set through the transaction-local `set_config`, and each pooled connection runs `SET search_path = cairn, pg_temp` and `SET ROLE cairn_app`.
- Onboarding order, the trial start, and read-only are enforced in the database: `cairn.advance_onboarding` refuses skipped steps, a trigger on `cases` starts the trial in the same transaction as the first case and never resets it, and restrictive row-level security policies block case writes until onboarding is complete and after the trial ends. `consents` is append-only.
- Accounts are created with Google, Apple, or an email address, all through Auth0 (see [auth0/README.md](../auth0/README.md)). Auth0 handles passwords, passkeys, and email confirmation. The API only verifies Auth0's signed token and refuses any other sign-in connection. The email address and sign-in method come from the token, never from the request body, and registration is refused until `email_verified` is true.
- Every write adds an `audit_events` row in the same transaction. Reads of the deceased's details (`GET /v1/cases/{id}`, `/status`) are audited too.
- The service logs only the SQL error class and constraint name. It never logs names, dates, or free text.
- `ssn_last4` is never read or returned. Cause of death is not collected. Pausing has no reason field.
- A missing case and a case you can't access both return the same 403, so case IDs can't be probed.

## Decisions to confirm with the product owner

1. **Accounts aren't linked across sign-in methods.** An email that already has an account gets a 409 naming the method used last time. Linking a second method after signing in with the first (UC-REG-05) isn't built. Support merges accounts with `database/docs/support-account-merge.md`.
2. **Fiduciary flag (UC-4, UC-8).** The spec says not to add a user-level column without sign-off. The relationship picked at case hand-off only shapes the response (`language_profile`). It's stored per case as `case_members.relationship`.
3. **POA role confirmation (UC-3).** `confirm_current_role` offers `named_executor`, `next_of_kin`, and `not_sure` as client routing values only. The note that POA authority ends at death is marked `legal_review_required`. [LEGAL REVIEW REQUIRED]
4. **`context_items` stays in Postgres** (open question 1). The optional migration is required as described above.
5. **Task categories and kinds** (UC-11, UC-13) are keyed by `task_key` in `cairn_api/journey.py`. They should become a `category` field in the template content schema.
6. **Pause length** defaults to 30 days (maximum 90), because `tasks_paused_until` needs an end time. When it expires, the tasks come back on their own.
7. **Case list for fiduciaries** is not built, as UC-8 asks. It should be tracked as its own ticket.
8. **New templates** `order_death_certificates`, `choose_funeral_provider`, and `notify_banks` in `database/content/tasks/us/` are unreviewed drafts, like the existing samples. They need counsel review and `last_verified_on` dates before release.
9. **Crisis plan.** UC-REG-14 must match Trello card 26, which has no written plan yet. The distress phrase list in `onboarding.py` and the draft pause wording need checking against it.
10. **Subscriptions.** Nothing sells a subscription yet. `users.status = 'subscribed'` can only be set by the owner role until billing is built.
