# Registration gap audit: UC-REG-01 to UC-REG-14 and UC-ACCT-01

> **2026-10-05:** `account-creation-review-gap-audit.md` closes the open UC-REG-04, UC-REG-05, UC-REG-10, UC-REG-13, and UC-REG-14 code gaps. Unfinished sign-ups are deleted after 90 days (D-2026-10-05-R1). Every email now goes through Twilio SendGrid, and text messages have a Twilio sender, still not offered (OPEN-05).

> **2026-10-01:** the database moved from PostgreSQL to MongoDB. SQL tables, row-level security policies, and functions named below now live in `database/db/schema.py` (collections, validators, roles) and `api/cairn_api/store.py` (the case boundary). `database/CLAUDE.md`, "The move to MongoDB", maps each one.

Audit of the registration code on `main` (commit 9201759) against `cairn-registration-use-cases.json` spec 1.1.0 (copied to `docs/cairn-registration-use-cases.json`), and what this branch changes.

**Before** is the state of `main`: **Met**, **Partial**, or **Missing**. **Now** is the state on this branch: **Done** means built and covered by tests. **Client** means this repo exposes what's needed but the check can only be met in the app UI, which isn't in this repo. **Open** means not done yet, with the reason given.

## Summary

| Use case | Before | Now | Still open |
|---|---|---|---|
| UC-REG-01 Welcome | Partial | Done, Client | WCAG 2.2 AA, 44x44 pt targets, not relying on color alone (client) |
| UC-REG-02 Google | Partial | Done | |
| UC-REG-03 Apple | Partial | Done, Open | Relay domain registration, SPF, DKIM, delivery checks (operations) |
| UC-REG-04 Email | Partial | Done, Open | Auth0 passwordless settings must be applied to the tenant |
| UC-REG-05 Account exists | Partial | Done, Open | Linking a second sign-in method is not built |
| UC-REG-06 Age | Missing | Removed | Not built, by product decision. See below |
| UC-REG-07 Privacy and Terms | Partial | Done | Legal review items |
| UC-REG-08 Free trial | Missing | Done, Open | Reminder email sender, subscription purchase and billing |
| UC-REG-09 AI notice | Missing | Done, Client | AI label in every chat view (client). Legal review |
| UC-REG-10 Decline | Missing | Done, Open | Retention period and scheduler for cleanup |
| UC-REG-11 Preferred name | Missing | Done | |
| UC-REG-12 Personality | Missing | Done, Open | Review of draft sample replies. No chat yet to apply the tone |
| UC-REG-13 Resume | Missing | Done, Open | Cleanup period for accounts with no case |
| UC-REG-14 Distress | Missing | Done, Open | Trello card 26 has no written crisis plan yet |
| UC-ACCT-01 Delete account | Missing | Done, Open | Schedule the cleanup worker and verify the Apple token source in Auth0 |

Nothing on `main` fully met a use case. The closest were UC-REG-02 and UC-REG-03 (Auth0 token verification on the server, relay addresses accepted) and UC-REG-05 (a 409 that named the method used before, and no automatic linking).

## Per use case

### UC-REG-01 Welcome
- Before: `GET /v1/sign-in-methods` had the three button labels. There was no acknowledgment, no sign-in link, and no "not ready" link.
- Now: `GET /v1/welcome` returns `welcome_acknowledgment`, three equally weighted options, "Already have an account? Sign in.", and `welcome_not_ready_link` pointing at `CAIRN_JOURNEY_MAP_URL`. It needs no sign-in and asks for nothing. `?oauth_cancelled=true` adds `oauth_cancelled`.
- Client: contrast, target size, and color-independence are app UI checks.

### UC-REG-02 and UC-REG-03 Google and Apple
- Before: tokens were verified on the server (Auth0 verifies Google and Apple, the API verifies Auth0's RS256 token). But registration also required a legal first and last name plus policy versions, and the account had no onboarding status.
- Now: `POST /v1/registrations` creates the account in `pending_onboarding` with the email and method from the token only. A name the provider shares goes in `name_from_provider`, is stored only as `users.name_prefill`, and is cleared once the preferred name is saved. The response is the Privacy Policy and Terms acknowledgment.
- Apple relay addresses are accepted (tested). Open (operations): register the outbound email domain with Apple's Private Email Relay Service, set up SPF and DKIM, and confirm delivery of verification, reminder, and support mail. Cairn sends at most two reminder emails per account, far below the 100 per day relay limit.

### UC-REG-04 Email
- Before: email sign-up used Auth0's password database connection.
- Now: the default email connection is Auth0 passwordless `email` (magic link). `email_check_inbox` is the response until the link is used. Draft copy covers the expired-link and 60-second resend states.
- Open (tenant setup, see `auth0/README.md`): set the link lifetime to 15 minutes and use link mode. Auth0 codes are single use. If passwords stay enabled, apply the NIST SP 800-63B-4 settings listed there.

### UC-REG-05 Account already exists
- Before: 409 `account_exists` with the method name, in different wording.
- Now: `copy.account_exists` is used verbatim with the provider filled in, and `next_step` offers that provider as the primary button. Accounts are never linked automatically (tested).
- Open: linking a second method after signing in with the original one is not built. It needs an Auth0 account-linking decision. The support path to merge an Apple relay account with a real-email account is in `docs/support-account-merge.md`.

### UC-REG-06 Age
- Removed by product decision on 2026-09-24. Cairn does not ask for or store the user's age. There is no age step, no `age_confirmed_at` column, and no `age_confirmed` onboarding step. Onboarding goes from account creation straight to UC-REG-07. `age_question` and `age_under_18` stay in the copy file's `spec_copy` only because that section mirrors the spec verbatim. They are never shown.
- This departs from the approved spec (1.1.0), which lists the age step in the onboarding sequence and flags it for legal review. The spec and legal review should be updated to match. [LEGAL REVIEW REQUIRED]

### UC-REG-07 Privacy Policy and Terms
- Before: registration required the current terms and privacy versions and wrote `terms` and `privacy` consent rows. There was no summary or checkbox, no sign-in method or client on the record, and rows could be updated (`withdrawn_at`).
- Now: the screen shows `privacy_terms_summary`, links to both documents, names the AI provider (`CAIRN_AI_PROVIDER_NAME`), and shows an unchecked checkbox. Agreeing needs the `document_version` that was shown and appends a `privacy_terms` consent row with version, time, sign-in method, and client. `consents` is now append-only: a trigger refuses UPDATE, DELETE, and TRUNCATE, except the cascade from deleting the account. No case, and so no data for the AI, can be created until onboarding is complete.
- Open: the legal review items (align with CAIRN-POL-PRIV-01, and require the same promise in subprocessor contracts).

### UC-REG-08 Free trial
- Now:
  - `users.trial_started_at` and `trial_ends_at` are written by an AFTER INSERT trigger on `cases`, so the trial always starts in the same transaction as the first case, and only if it hasn't started.
  - A guard trigger stops anyone, the owner included, from changing `trial_started_at` once set.
  - A CHECK keeps `trial_ends_at` at exactly 672 hours (28 days) after the start.
  - Reminders are scheduled at +21 and +27 days in `trial_reminders` and shown in the app (`notes` on `/v1/me` and on case reads).
  - `case_created_trial_start` is shown on the first case with the local end date.
  - After `trial_ends_at` the account is read-only: restrictive RLS policies block inserts and updates on cases, members, deceased, tasks, and context items. Reads, Settings, case deletion, and account deletion still work.
  - The API returns `account_read_only` with `read_only_banner` and a subscribe next step.
  - No payment method is requested anywhere.
- Open: an email sender for reminders (`cairn.claim_due_trial_reminders` returns them, and nothing sends them yet). Subscription purchase, the pre-purchase screen, and billing are not built. `status = 'subscribed'` can only be set by the owner role for now.

### UC-REG-09 AI notice
- Now: the screen shows `ai_notice`, then `ai_notice_legal` and `ai_checkbox`, and records an `ai_notice` consent. From then on `account.ai_label` is `AI guide`.
- Client: show the label in every chat view. There is no chat in this repo yet.
- Open: the legal review items.

### UC-REG-10 Decline
- Now: `agreed: false` records nothing, shows `decline_acknowledgment`, and offers to read it again, the journey map, and support. The account stays `pending_onboarding`.
- Open: `cairn.purge_stale_accounts(pending_older_than, no_case_older_than)` exists and is owner-only. The periods have no defaults because the retention schedule isn't set `[LEGAL REVIEW REQUIRED]`, and no scheduler runs it yet.

### UC-REG-11 Preferred name
- Before: legal first and last name were required at sign-up.
- Now: `PUT /v1/onboarding/preferred-name` saves `preferred_name` and an optional `name_pronunciation`. The pre-fill is shown for the user to confirm and is never saved without that. Legal names are no longer collected (the columns are now nullable).

### UC-REG-12 Personality
- Now (spec 1.2.0): the four voices in `voices/manifest.yaml`, each with its label, tagline, and reference response to the same situation. "Choose for me" selects `steady_direct`. The choice is saved as `users.voice` (migration 0009), finishes onboarding (`active_no_case`), and confirms in that voice using the preferred name. `PATCH /v1/me` changes it any time. The app refuses to start if any voice can't be loaded, and the database accepts only voices in the manifest. The crisis and AI-disclosure responses don't read the voice (tested identical), and `core.md` is the same prompt block for every voice.
- Open: the reference responses and confirmations are draft copy that needs review. The reference responses make factual claims about death certificates and need the same legal content review as the journey templates before real users see them. Nothing calls the model yet, so the stored voice only reaches the prompt builder in tests. [LEGAL REVIEW REQUIRED]

### UC-REG-13 Resume
- Now: every sign-in calls `POST /v1/registrations`. A returning user gets 200, `resume_onboarding`, and the first incomplete step, or a gentle hand-off to case creation if onboarding is done and there's no case. Acknowledgment versions come from a hash of the exact text shown plus the policy versions. If any changes, only that acknowledgment is asked again, and case, journey, and task routes return 409 `acknowledgment_required` until it's done. The trial doesn't start before the first case.

### UC-REG-14 Distress
- Now: every onboarding response carries `support` (`need_a_moment_control` and `crisis_resource`). `GET /v1/onboarding/need-a-moment` needs no sign-in, stores nothing, and sets no timers. Free text in onboarding (the preferred name and pronunciation) is checked for distress phrases. On a match nothing is saved, the flow pauses with the 988 resource, and progress already made is kept. The matched text is never stored or logged, and no emotional-state inference is saved (decision 7).
- Open: Trello card 26 has only a description, not a written plan. The phrase list and the draft acknowledgments need to be checked against it once it's written.

### UC-ACCT-01 Delete account
- Superseded on 2026-09-25 by UC-REG-15 (one button, a masked confirmation address, one confirmation email, cases in a hold, and safety first in chat). See `account-lifecycle-gap-audit.md`.
- Before: only a case could be deleted.
- Now: `GET /v1/me/deletion` explains, and `POST /v1/me/deletion {"confirm": true}` runs `cairn.delete_my_account()`. That deletes the cases the user created (cascading to deceased, tasks, and context), their memberships, consents, reminders, and the account itself, in one transaction and in any status. Audit rows keep opaque ids only. The identity cleanup is queued, and `cairn_api.identity_cleanup` deletes the Auth0 user and, for Apple, revokes the refresh token through `https://appleid.apple.com/auth/revoke`.
- Open: schedule the worker with owner credentials and Auth0 Management API and Apple keys. Confirm the tenant exposes the Apple refresh token (see the note in `identity_cleanup.py`).

## Data requirements mapping

| Spec | Implemented as |
|---|---|
| account.auth_provider | `users.sign_in_method` (0007), backfilled from the subject prefix in 0008 |
| account.provider_subject_id | `users.idp_subject` (Auth0 subject, which includes the provider) |
| account.preferred_name, name_pronunciation, personality, onboarding_step, status, trial_started_at, trial_ends_at | Same names on `users` (0008) |
| account.age_confirmed_at | Not stored. UC-REG-06 was removed |
| consent_record | `consents`: consent_type is `purpose`, document_version is `policy_version`, accepted_at is `granted_at`, plus new `auth_provider` and `client` |
| reminders | `trial_reminders` (0008) |
| Display trial_end_date in local time | `users.time_zone` (IANA), sent at registration or in Settings. Falls back to UTC |

## Breaking API changes

- `POST /v1/registrations` takes `name_from_provider` and `time_zone` only, and returns the onboarding screen. Names, phone, policy versions, and relationship are no longer accepted there. The relationship moved to `POST /v1/onboarding/case-handoff`.
- `GET /v1/me` returns `{account, notes}`.
- Case, journey, and task routes need finished onboarding and current acknowledgments. Writes also need a writable account.
- New required settings: `CAIRN_PRIVACY_POLICY_URL`, `CAIRN_TERMS_URL`, `CAIRN_JOURNEY_MAP_URL`, `CAIRN_SUPPORT_URL`, `CAIRN_AI_PROVIDER_NAME`. `CAIRN_AUTH0_EMAIL_CONNECTION` now defaults to `email`.
- Accounts that existed before 0008 are treated as having finished onboarding, and their trial counts from their first case. They are asked for the three new acknowledgments on next sign-in.
