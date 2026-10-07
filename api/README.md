# Cairn API (MVP)

HTTP API for the Cairn use case suite of 2026-10-06, built on the MongoDB schema in `database/db/schema.py` and the data-access layer in `cairn_api/store.py`. The specs it follows are in `database/docs/`: account setup 3.2.0 (`cairn-account-use-cases-v32.json`), case creation 3.2.0 (`cairn-case-creation-use-cases-v32.json`), the Support and Crisis Plan 3.2.0 (`cairn-support-crisis-plan-v32.json`), Take a break 3.2.0 (`cairn-take-a-break-use-cases-v32.json`), and the subscription 3.3.0 (`cairn-subscription-use-cases-v33.json`). `database/docs/use-cases-v3-gap-audit.md` maps each use case to its code and tests. FastAPI generates the contract from the request and response models, which are the only way data enters or leaves the service.

- Swagger UI: `/docs` when running. Static contract: [`openapi.json`](openapi.json) (OpenAPI 3.1).
- Requests reject unknown fields, trim whitespace, and validate against the same rules as the database validators (state codes, date order, lengths, allowed values).
- Errors use RFC 9457 problem details (`application/problem+json`) and never echo submitted values or database messages.
- Any response that moves a conversation forward includes a `next_step` with one prompt, so clients ask one question at a time.

## Use case map

| Use case | Endpoint |
|---|---|
| UC-REG-01, welcome | `GET /v1/welcome` (also `GET /v1/sign-in-methods`). Carries the email link copy and landing page (UC-REG-04), the Support resources and Take a break labels, and the session policy (D-20) |
| UC-REG-02 to UC-REG-05, create an account or sign in | `POST /v1/registrations` after every Auth0 sign-in |
| UC-REG-05, add or remove another way to sign in | `POST /v1/me/sign-in-methods` (signed in the original way, with the new sign-in's token in the body), `DELETE /v1/me/sign-in-methods/{method}` |
| UC-REG-06, are you 18 or older | `POST /v1/onboarding/adult`. Yes or no with its time. Never an age |
| UC-REG-07 to UC-REG-10, acknowledgments and declining | `POST /v1/onboarding/acknowledgments/{privacy_terms,trial_terms,ai_notice}` (plus `GET /v1/policies`) |
| UC-REG-11, preferred name | `PUT /v1/onboarding/preferred-name` |
| UC-REG-12, voice | `PUT /v1/onboarding/personality`. Saves `users.voice` |
| UC-REG-15, notification channels then frequency | `PUT /v1/onboarding/notification-channels`, then `PUT /v1/onboarding/notification-frequency` (finishes setup) |
| UC-REG-16, setup complete | The response to the frequency step. Then `POST /v1/onboarding/case-handoff` (Start a case) or `GET /v1/home` |
| UC-REG-13, resume | `GET /v1/onboarding` (and the 200 from `POST /v1/registrations`) |
| UC-REG-14, distress during setup | `PUT /v1/onboarding/preferred-name` pauses. `GET /v1/onboarding?offer_check_in=true` and `POST /v1/onboarding/check-in` for the follow-up |
| UC-REG-17, Settings | `GET` and `PATCH /v1/me`, `GET` and `PATCH /v1/me/notification-preferences` |
| UC-REG-18, returning sign-in | `POST /v1/registrations`, then `GET /v1/home?session_start=true`, whose `route` says where to go |
| UC-REG-19, sign out and the 5-minute timeout | `POST /v1/me/sign-out`. Every signed-in request checks the session (401 `session_timed_out`) |
| UC-REG-20, can't get into the sign-in email | `GET /v1/sign-in-help?method=` (no sign-in needed) |
| UC-ACCT-01, delete account (UC-REG-15 before) | `GET /v1/me/deletion`, then `POST /v1/me/deletion`. Cancels a Stripe subscription first. The response says the user is signed out |
| UC-REG-16, download all my data | `GET /v1/me/data-export`, then `GET /v1/me/data-export/file` (JSON) |
| Account requests in chat | `POST /v1/me/messages`. The client keeps `session` and sends it back |
| Support resources (crisis plan) | `GET /v1/support-resources` (no sign-in needed, never logged with an id) |
| UC-BRK-02, Take a break before sign-in | `GET /v1/break` |
| UC-BRK-03 to UC-BRK-12, Take a break | `GET`, `POST`, `PUT`, and `DELETE /v1/me/break`, and `POST /v1/cases/{id}/take-a-break` inside a conversation |
| UC-CASE-25, home screen | `GET /v1/home` |
| UC-CASE-01, start a new case (a draft) | `POST /v1/cases`, then `POST .../intake/continue` (one question at a time) or `POST .../intake/messages` (own words) |
| UC-CASE-01, own words read back | `POST /v1/cases/{id}/intake/messages`, then `POST .../intake/confirmations` |
| UC-CASE-02 to UC-CASE-09, answer, skip, not sure, change | `PUT /v1/cases/{id}/intake/answers/{field}` |
| UC-CASE-02 and UC-CASE-03 preferences | `PUT /v1/cases/{id}/intake/preferences` |
| UC-CASE-10 (now UC-BRK-04), pause and come back | `POST .../intake/pause`, then `GET /v1/cases/{id}` (resume) and `GET /v1/cases` |
| UC-CASE-11, review | `GET /v1/cases/{id}/review` |
| UC-CASE-12, the journey that fits | `GET .../journey/preview` (step 3 is `confirm_keep_in_touch` until UC-CASE-19 is confirmed), `POST .../journey/start`, `POST .../journey/not-yet` |
| UC-CASE-13, first task | `POST .../journey/first-task` |
| UC-CASE-14, distress | Every intake turn. The client keeps `session` and sends it back. `POST .../intake/continue` resumes |
| UC-CASE-15, sensitive numbers | Every free-text field is redacted as it is parsed |
| UC-CASE-16, attorney referral | `POST .../intake/attorney-referrals`, or detected in `intake/messages` |
| UC-CASE-17, the death hasn't happened yet | `POST .../intake/death-not-yet`, or detected in `intake/messages` |
| UC-CASE-18, second case | `POST /v1/cases` again. Start keeps the existing trial |
| UC-CASE-19, confirm how Cairn keeps in touch | `GET` and `PUT /v1/cases/{id}/keep-in-touch` |
| UC-CASE-21 and UC-END-13, delete a case now or in 7 days | `GET`, `POST`, and `DELETE /v1/cases/{id}/deletion` (the last keeps a held case) |
| UC-SUB-02 and UC-SUB-03, terms and Stripe Checkout | `GET /v1/me/subscription/terms`, then `POST /v1/me/subscription/checkout` |
| UC-SUB-04 and UC-SUB-05, back from Checkout | `GET /v1/me/subscription/checkout-result?outcome=` |
| UC-SUB-10, UC-SUB-12, payment details and past payments | `POST /v1/me/subscription/portal`, `POST /v1/me/subscription/confirm-payment` |
| UC-SUB-13 and UC-SUB-14, cancel and undo | `POST /v1/me/subscription/cancel`, `POST /v1/me/subscription/undo-cancel` |
| UC-SUB-23, Settings, Subscription | `GET /v1/me/subscription` |
| UC-SUB-22, Stripe events | `POST /v1/stripe/webhook` (signed by Stripe, not in the public contract) |
| Legal identity, just in time | `PATCH /v1/cases/{id}/deceased`, only once the journey has started |
| UC-9, the four-week journey | `GET /v1/cases/{id}/journey` |
| UC-10, order death certificates | `GET /v1/cases/{id}/tasks/{task_id}`, `POST .../certificate-order` |
| UC-11, notify a bank | `POST .../tasks/{task_id}/institution-notices` |
| UC-12, step back from tasks | `POST /v1/cases/{id}/journey/pause` and `/resume` (a break on the account, UC-BRK-05) |
| UC-13, status across the case | `GET /v1/cases/{id}/status` |
| Any task status change or snooze | `PATCH /v1/cases/{id}/tasks/{task_id}`. Editing a task during a break ends the break first |

Removed with the v3 specs: `GET /v1/onboarding/need-a-moment` (Take a break replaced I need a moment, BRK-D-01), and the per-journey notification routes `/v1/cases/{id}/notification-preferences` and `/v1/me/notification-preferences/changes` (one choice for the account, D-13).

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
export CAIRN_BREAK_COPY=... CAIRN_SUBSCRIPTION_COPY=...   # optional: replacement Take a break and subscription copy
export CAIRN_APP_URL=https://app.cairnguide.app           # where Stripe sends the user back
export CAIRN_SUBSCRIPTION_PRICE_CENTS=1499                # D-04. Shown and charged. Every price in the copy must match
export CAIRN_STRIPE_SECRET_KEY=... CAIRN_STRIPE_WEBHOOK_SECRET=... CAIRN_STRIPE_PRODUCT_ID=...   # secrets, from the secret manager
export CAIRN_VAPID_PUBLIC_KEY=...          # optional: browser notifications. Unset hides the browser choice
export CAIRN_INACTIVITY_TIMEOUT_SECONDS=300 CAIRN_TIMEOUT_WARNING_SECONDS=20 CAIRN_OVERALL_SESSION_DAYS=30   # D-20
export CAIRN_DEV_AUTH_SECRET=...           # development only: turns on POST /v1/dev/token for the test logins
.venv/bin/uvicorn cairn_api.main:app --app-dir api
```

The database must be a MongoDB 7.0 or newer replica set (Atlas always is), because every request runs in one multi-document transaction. Run `database/db/apply.py` on it first, then load the templates. See [database/README.md](../database/README.md). UC-10 and UC-11 store their records in `context_items` (`CERT_ORDER` and `BANK_NOTICES`).

## Tests

```bash
cd api
../.venv/bin/pytest                                          # contract tests, no database
CAIRN_TEST_MONGODB_URI='mongodb://admin:admin@localhost:27017/?replicaSet=rs0' ../.venv/bin/pytest   # everything
make coverage                                                # everything, with a coverage report (from the repo root)
```

Coverage of `cairn_api` is measured with pytest-cov, and the pull request workflow fails under the floor in `pyproject.toml` (`[tool.coverage.report] fail_under`). Stripe is a mocked client in tests (`conftest.FakeStripe`), and Stripe events are fixtures signed with the test endpoint secret. As the subscription spec says (SUB-D-09), no test checks that money arrived or that anyone received an email. `tests/test_journey_use_cases.py` runs the journey specs' expected values against `journeys/tools/resolve.py`.

With `CAIRN_TEST_MONGODB_URI` set, the suite creates a scratch database, applies `database/db/schema.py`, creates one login user per role, loads the templates as the loader user, runs every use case with the API connected as the `cairnApp` user, and then drops the database, its users, and its roles. `tests/test_data_security.py` checks the roles, the validators, the case boundary, read-only accounts, deletion, and the jobs (it replaces `verify.sql`). The URI must belong to an administrator of a scratch replica set with authentication on (`make setup` gives you one).

## Copy

User-facing copy lives in four versioned files in [`cairn_api/content/`](cairn_api/content/): `registration-copy.json` (account spec 3.2.0), `case-creation-copy.json` (case creation 3.2.0 and the crisis plan's follow-up), `take-a-break-copy.json` (Take a break 3.2.0), and `subscription-copy.json` (subscription 3.3.0). In each, `spec_copy` is the spec's copy, verbatim, and a test fails if it drifts. `draft_copy` holds strings the specs don't supply. Those need product, clinical, and legal review. To replace a file after review, point `CAIRN_REGISTRATION_COPY`, `CAIRN_CASE_COPY`, `CAIRN_BREAK_COPY`, or `CAIRN_SUBSCRIPTION_COPY` at the new one. Acknowledgment versions, and the subscription terms version, come from a hash of the exact text, so changed wording is acknowledged again. The API refuses to start if any copy names a price other than `CAIRN_SUBSCRIPTION_PRICE_CENTS`.

## Case creation

Case creation follows `database/docs/cairn-case-creation-use-cases-v32.json` (spec 3.2.0) and, where they differ, the Support and Crisis Plan (`database/docs/cairn-support-crisis-plan-v32.json`). Breaks follow `cairn-take-a-break-use-cases-v32.json`. `database/docs/use-cases-v3-gap-audit.md` maps them to their tests, and `case-creation-v2-gap-audit.md` keeps the detail of the 2.0.0 build.

- A new case is a draft. The 28 free days start only at the first `POST .../journey/start` (DEC-01), inside `store.Session.start_journey`. No payment information is asked for anywhere (DEC-02).
- Nothing about the person who died is collected at creation beyond the spec's data_fields. Legal names, dates of birth, SSNs, account numbers, and medical details never are. Free text is redacted as it is parsed (`RedactedText`), never stored, and only confirmed field values are saved.
- Journey selection rules are template data in `database/content/journeys/journey-selection.json`, loaded into `journey_templates`. `cairn_api/journey_selection.py` only evaluates them.
- Every turn acknowledges first, asks at most one question, and has one `next_step`. Every question offers Skip for now and I'm not sure, and every response carries `read_aloud`.
- Care levels (UC-CASE-14 and the crisis plan) are in a client-held `session` and never stored. Level 2 is two overwhelm signals or three skips in a row. Levels 3 and 4 use the steady_care voice, ask nothing until the user continues, pause the tasks (a care rest, which pauses the free days while they run), and show no billing wording. Level 4 says 988 first and adds one to the anonymous SB 243 count. Routes that act on the account take `care_level` and do nothing at level 4 in that turn (AC-26-11).
- A break covers the person, not one case (BRK-D-06). It lives on the account (`break_started_at`, `break_until`, `break_notice_at`), and every active journey's `tasks_paused_until` follows it. No break changes a subscription (BRK-D-08). A check-in the user said yes to lives on the account too (`check_in_at`), so it works during setup.
- New endpoints: `POST .../intake/transcripts` (speech, UC-CASE-22), `POST .../intake/level-2-choice`, `POST .../intake/check-in` (DEC-26-04), and `POST /v1/cases/{id}/take-a-break`. Responses carry `care_level`, `announcements` (the AI reminder, rest offers, and the check-in, for screen readers), and `controls` (the take a break, read aloud, and speak labels).
- Open decisions OPEN-04 to OPEN-10 are `CAIRN_*` settings in `config.py`, with the spec's defaults. The app refuses to start with any other value until the decision is made.
- Free-text extraction (`extraction.py`) and distress signals (`safety.py`) are rule-based stand-ins until a model replaces them. The phrase lists need clinical sign-off (DEC-26-06).

Copy lives in [`cairn_api/content/case-creation-copy.json`](cairn_api/content/case-creation-copy.json), with the same `spec_copy`, `flow_copy`, and `draft_copy` sections as the registration copy, and a test that keeps `spec_copy` verbatim. Point `CAIRN_CASE_COPY` at a reviewed file to replace it.

## Voices

The personality step (UC-REG-12) offers the voices in [`voices/manifest.yaml`](../voices/manifest.yaml). The label, tagline, and sample reply on that screen come from the manifest and each voice's reference response. The choice is stored as `users.voice` and can be changed with `PATCH /v1/me`. "Choose for me" saves the manifest's `default_voice`.

The app loads and checks the folder at startup and refuses to start if it can't: every file must exist inside the folder, each voice file's `id` and onboarding label must match the manifest, every reference response must answer the same user message, and the manifest must list exactly the ids in `schemas.Voice`. The database accepts only those ids too (`VOICES` in `database/db/schema.py`, checked by the users validator), so every stored voice can be loaded. `VoiceCatalog.system_blocks` builds the prompt for a user's stored voice in the same cache order as `voices/assemble_prompt.py`. Nothing calls the model yet.

The voice cards on the setup screen use the account spec's copy for each voice (`voice_<id>_label`, `voice_<id>_description`, `voice_<id>_sample`), and `copy.voice_confirm` follows the choice. Adding a voice means a new voice file and manifest entry, a new value in `schemas.Voice`, the id added to `VOICES` in `database/db/schema.py` (then `database/db/apply.py`), and the three card strings in the copy file. `tests/test_voices.py` fails until they agree.

## Scheduled jobs (the cairnJobs user, not the app)

| Job | Function | Notes |
|---|---|---|
| Access (D-19) | `maintenance.expire_trials`, job `expire_trials` | Daily. Keeps `users.access` current for reporting. Read-only is enforced from the trial and subscription fields directly |
| Outbound | `python api/scripts/send_outbound.py` or job `outbound` | Every 5 minutes. Confirmations of things the user did (setup, Settings, subscribed, cancelled, deletions: one each, address purged once sent), the trial-ending note a week before (D-14, held to Cairn during a break), the reminders users chose (channels, frequency, and quiet hours, none during a break), a check-in the user said yes to, the break-ending notice (UC-BRK-09), and the yearly subscription reminder (UC-SUB-17). Every reminder and check-in passes a check that it names no person who died and no circumstance. Email through Twilio SendGrid (`TWILIO_SENDGRID_API_KEY`). Browser notifications are empty Web Push messages signed with `CAIRN_VAPID_PRIVATE_KEY` (`webpush.py`), skipped when it isn't set |
| Stripe events (UC-SUB-22) | `maintenance.process_stripe_events`, job `process_stripe_events` | Every 15 minutes. Events the webhook recorded but couldn't finish, fetched again from Stripe by id |
| Cancellations (UC-SUB-13) | `maintenance.retry_cancellations`, job `retry_cancellations` | Every 15 minutes. A cancel request recorded while Stripe couldn't be reached |
| Early subscribers (UC-SUB-06) | `maintenance.sync_early_subscriptions`, job `sync_early_subscriptions` | Hourly. Keeps the first charge at the free days' end after a care rest moves it |
| Price change (UC-SUB-18) | job `price_change_notices` | Daily, only while `CAIRN_PRICE_CHANGE_EFFECTIVE_DATE` and `CAIRN_PRICE_CHANGE_NEW_PRICE` are set. Emails each subscriber once, 30 to 7 days before. Applying the price is done in Stripe |
| Held case deletion (UC-END-13) | `maintenance.purge_held_cases`, job `purge_held_cases` | At least hourly. Deletes cases whose 7-day hold has ended and queues their confirmation |
| Trial clocks (DEC-26-01) | `maintenance.settle_trial_clocks`, job `settle_trial_clocks` | Hourly. Starts the free days again when a care rest's break ends on its own, and moves `trial_ends_at` and the trial note later by the paused time |
| Draft cleanup (DEC-07) | `maintenance.purge_inactive_drafts`, job `purge_inactive_drafts` | At least daily. Deletes drafts idle for `app_settings.draft_retention_days` (28), with their answers and context. Never touches active cases |
| Identity cleanup | `python api/scripts/identity_cleanup.py` or job `identity_cleanup` | Deletes Auth0 users and revokes Apple tokens after account deletion |
| Unfinished sign-ups (UC-REG-10, UC-REG-13) | `maintenance.purge_stale_accounts`, job `purge_stale_accounts` | Daily. Deletes accounts still in `pending_onboarding` 90 days after they were created (D-2026-10-05-R1, `CAIRN_PENDING_ACCOUNT_RETENTION_DAYS` overrides it). Finished accounts that never created a case are deleted only if `CAIRN_NO_CASE_ACCOUNT_RETENTION_DAYS` is set, and that period isn't decided [LEGAL REVIEW REQUIRED]. Queues identity cleanup for every sign-in on the account |

On Cloudflare, Cron Triggers in `cloudflare/wrangler.jsonc` run every job above through `cairn_api/jobs.py` (`POST /jobs/{name}` in the private jobs container). A job whose provider isn't set answers `skipped`.

Every email and text message goes through Twilio. See "Twilio (email and text messages)" in the [repository README](../README.md#twilio-email-and-text-messages) for the settings, the GitHub secrets, and the hand-run live checks.

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

1. **Accounts are never linked automatically across sign-in methods.** An email that already has an account gets a 409 naming the method used last time, with an option to add the new method after signing in the old way. `POST /v1/me/sign-in-methods` does that (UC-REG-05): the request is signed in the original way and the body carries the second sign-in's token. One account per sign-in, one sign-in per method. Two accounts that already exist are still merged by support (`database/docs/support-account-merge.md`).
2. **Fiduciary flag (UC-4, UC-8).** The spec says not to add a user-level column without sign-off. The relationship picked at case hand-off only shapes the response (`language_profile`). It's stored per case as `case_members.relationship`.
3. **POA role confirmation (UC-3).** `confirm_current_role` offers `named_executor`, `next_of_kin`, and `not_sure` as client routing values only. The note that POA authority ends at death is marked `legal_review_required`. [LEGAL REVIEW REQUIRED]
4. **`context_items` is a MongoDB collection** (open question 1), in the same database as everything else.
5. **Task categories and kinds** (UC-11, UC-13) are keyed by `task_key` in `cairn_api/journey.py`. They should become a `category` field in the template content schema.
6. **Pause length** defaults to 30 days (maximum 90), because `tasks_paused_until` needs an end time. When it expires, the tasks come back on their own.
7. **Case list for fiduciaries** is not built, as UC-8 asks. It should be tracked as its own ticket.
8. **New templates** `order_death_certificates`, `choose_funeral_provider`, and `notify_banks` in `database/content/tasks/us/` are unreviewed drafts, like the existing samples. They need counsel review and `last_verified_on` dates before release.
9. **Crisis plan.** Written (`database/docs/cairn-support-crisis-plan-v32.json`). The distress phrase list in `safety.py` still needs licensed clinical sign-off (DEC-26-06) before any real user.
10. **Subscriptions.** Built on Stripe (UC-SUB). Before launch: the live Stripe product, tax-inclusive pricing confirmed (SUB-D-12), the product tax code chosen with an accountant, the customer portal configured without retention offers, failed-payment emails turned on in the Stripe Dashboard (SUB-D-08), and the webhook endpoint registered for the events in `subscription.HANDLED`. Keeping subscription consent records after account deletion (OPEN-SUB-04) is open with counsel: today account deletion removes them with everything else.
11. **The 5-minute sign-out (D-20).** Enforced on the server from `users.last_active_at` and the token's issue time. A request after 5 quiet minutes with a token from before then gets 401 `session_timed_out`. Signing in again works. `last_active_at` is written at most every 15 seconds, outside the request's transaction.
