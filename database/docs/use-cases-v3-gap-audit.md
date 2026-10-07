# Use case suite of 2026-10-06: gap audit

Review of `main` (2e19c1c, after the account creation review) against the newest version of every use case file in the Use Cases folder, and what the `use-cases-v3-implementation` branch changes. The spec files are copied into this folder:

| Area | File | Version | Before this branch |
|---|---|---|---|
| Account setup, sign-in, Settings, deletion | `cairn-account-use-cases-v32.json` | 3.2.0 | Registration 1.2.0 and the 2026-09-25 account changes |
| Case creation and the home screen | `cairn-case-creation-use-cases-v32.json` | 3.2.0 | 2.0.0 |
| Support and Crisis Plan (card 26) | `cairn-support-crisis-plan-v32.json` | 3.2.0 | 0.2 |
| Take a break | `cairn-take-a-break-use-cases-v32.json` | 3.2.0 | New |
| Subscription (Stripe) | `cairn-subscription-use-cases-v33.json` | 3.3.0 | New |
| Journey J-EXPECTED-FACILITY | `cairn-journey-J-EXPECTED-FACILITY-use-cases-v11.json` | 1.1.0 | New |
| Journey J-SUDDEN-UNEXPECTED | `cairn-journey-J-SUDDEN-UNEXPECTED-use-cases-v11.json` | 1.1.0 | New |

Older files in this folder stay for history. Earlier audits remain the detail for anything this branch didn't change: `registration-gap-audit.md`, `account-lifecycle-gap-audit.md`, `account-creation-review-gap-audit.md`, `case-creation-v2-gap-audit.md`.

**Before** is the state on `main`: **Met**, **Partial**, **Missing**, or **New** (the use case didn't exist). **Now**: **Done** means built and covered by an automated test. **Client** means the API exposes what's needed and the rest can only be met in the web client. **Open** means not done, with the reason. The full suite passed before (559 tests) and after (707 tests, on MongoDB 8.0), with 95% line coverage of `cairn_api`.

New tests: `test_subscription.py`, `test_take_a_break.py`, `test_journey_use_cases.py`, `test_webpush.py`. Rewritten for the v3 specs: `test_registration_onboarding.py`, `test_account_lifecycle.py`, `test_account_lifecycle_rules.py`, `test_case_creation_rules.py`. Updated: `test_data_security.py`, `test_case_creation.py`, `test_case_creation_v2.py`, `test_voices.py`, `test_jobs.py`.

## Summary: account setup (account spec 3.2.0)

| Use case | Before | Now | Still open |
|---|---|---|---|
| UC-REG-01 Welcome | Partial | Done, Client | WCAG 2.2 AA, 44x44 targets (client). Support resources link and Take a break added |
| UC-REG-02 Google | Met | Done | |
| UC-REG-03 Apple | Partial | Done, Open | Apple relay registration (operations), as before |
| UC-REG-04 Email | Partial | Done, Client | The landing page that uses the token only on Continue (copy in `GET /v1/welcome`, page in the client). Auth0 link settings (operations) |
| UC-REG-05 Account exists | Met | Done | |
| UC-REG-06 Adult confirmation | Removed | Done | Legal review of the age gate approach (OPEN-09) |
| UC-REG-07 Privacy and Terms | Met | Done | Legal review items |
| UC-REG-08 Free trial | Partial | Done | The new trial_summary is a new version, so every account acknowledges it again |
| UC-REG-09 AI notice | Met | Done, Client | Counts as the day's AI reminder now. AI label in every chat view (client) |
| UC-REG-10 Decline | Met | Done | |
| UC-REG-11 Preferred name | Partial | Done | 50 characters and no 5 or more digits now enforced. Never pre-filled from a provider |
| UC-REG-12 Voice | Met | Done, Open | Cards use the spec's copy. Voice design review against SB 243 [LEGAL REVIEW REQUIRED] |
| UC-REG-15 Notification choices | Missing | Done, Client | Moved to setup, one choice for the account (D-13). Browser permission request (client) |
| UC-REG-16 Setup complete | Partial | Done | Welcome confirmation queued |
| UC-REG-13 Leave and resume | Met | Done | |
| UC-REG-14 Distress during setup | Partial | Done, Open | Follow-up offer and setup check-in added. Clinical sign-off of the phrase list (DEC-26-06) |
| UC-REG-17 Change setup choices | Partial | Done | Took in case UC-CASE-20 |
| UC-ACCT-01 Delete account | Met | Done | Cancels Stripe first. Level 4 waits a turn |
| UC-REG-18 Returning sign-in (proposed) | New | Done, Client | Method used last shown first (client, local to the browser) |
| UC-REG-19 Sign out and timeout (proposed, D-20 confirmed) | New | Done, Client | The warning and saving typed text as a draft (client). Auth0 session settings (operations) |
| UC-REG-20 Can't get into the email (proposed) | New | Done, Open | The support identity check needs a security review before launch |

## Summary: case creation (case creation spec 3.2.0)

| Use case | Before | Now | Still open |
|---|---|---|---|
| UC-CASE-01 to UC-CASE-09, UC-CASE-11, UC-CASE-15 to UC-CASE-17, UC-CASE-22 | Met | Done | Unchanged by 3.2.0 except UC-CASE-01's precondition (setup complete, which includes the adult answer) |
| UC-CASE-10 (moved to UC-BRK-04) | Met | Done | Draft pause copy now from the Take a break file |
| UC-CASE-12 Start journey | Met | Done | Copy updated (a break never pauses a subscription, the note is emailed a week before) |
| UC-CASE-13 First task | Met | Done | Not today is not a break, and sets no break field |
| UC-CASE-14 Distress | Met | Done | Check-in on the account (crisis plan v3) |
| UC-CASE-18 Second case | Met | Done | Read-only start goes to UC-SUB-07 |
| UC-CASE-19 Confirm how Cairn keeps in touch | Met (v2 shape) | Done | Reads back the account choices, asks only lead time and inactivity |
| UC-CASE-20 (merged into UC-REG-17) | Met | Done | The per-journey routes are gone |
| UC-CASE-21 Confirmations | Met | Done | Setup welcome, Settings change, subscribed, cancelled added |
| UC-CASE-23 AI reminder | Met | Done | Every signed-in session (`GET /v1/home?session_start=true`). Rest offer moved to UC-BRK-06 |
| UC-CASE-24 Under 18 | Met | Done | |
| UC-CASE-25 Home screen (proposed) | New | Done, Client | Layout (client) |

## Summary: Take a break (3.2.0)

| Use case | Before | Now | Still open |
|---|---|---|---|
| UC-BRK-01 On every view | Partial | Done, Client | Every response carries the label and the Support resources link. The view inventory test of the web client (placement, focus, 44x44, reflow) |
| UC-BRK-02 Before sign-in | Missing | Done | `GET /v1/break` |
| UC-BRK-03 During setup | Partial | Done | |
| UC-BRK-04 Draft | Met | Done | |
| UC-BRK-05 Active journey | Partial | Done | The break is on the account now (BRK-D-06) |
| UC-BRK-06 Cairn offers a break | Met | Done | Copy from the Take a break file, draft suffix added |
| UC-BRK-07 Care rest | Met | Done | |
| UC-BRK-08 While on a break | Partial | Done | Resting screen first, editing a task ends the break |
| UC-BRK-09 Break-ending notice | Missing | Done | OPEN-BRK-01 and OPEN-BRK-02 at their defaults |
| UC-BRK-10 Coming back | Partial | Done | |
| UC-BRK-11 Change how long | Missing | Done | |
| UC-BRK-12 Breaks and the sign-out | New | Done | |

## Summary: subscription (3.3.0)

| Use case | Before | Now | Still open |
|---|---|---|---|
| UC-SUB-01 Subscribe prompt | Missing | Done | Once a session is the client's (`subscribe_prompt_seen`). Once a day is the server's |
| UC-SUB-02 Terms | Missing | Done | Final disclosure wording [LEGAL REVIEW REQUIRED] |
| UC-SUB-03 Stripe Checkout | Missing | Done, Open | Stripe Dashboard setup (operations, README "Stripe") |
| UC-SUB-04 Back after paying | Missing | Done | |
| UC-SUB-05 Leaving Checkout | Missing | Done | |
| UC-SUB-06 Subscribe early (proposed) | Missing | Done | |
| UC-SUB-07 New journey while read-only | Partial | Done | |
| UC-SUB-08 Renewal | Missing | Done | |
| UC-SUB-09 Failed renewal | Missing | Done | Stripe's failed-payment emails turned on in the Dashboard (operations) |
| UC-SUB-10 Bank confirmation | Missing | Done | |
| UC-SUB-11 Ends after failed payments | Missing | Done | |
| UC-SUB-12 Payment details and invoices | Missing | Done, Open | Portal configured without retention offers (operations) |
| UC-SUB-13 Cancel | Missing | Done | |
| UC-SUB-14 Undo a cancellation | Missing | Done | |
| UC-SUB-15 Subscribe again | Missing | Done | |
| UC-SUB-16 Delete while subscribed | Missing | Done, Open | Keeping subscription consent records after deletion (OPEN-SUB-04, counsel) |
| UC-SUB-17 Yearly reminder | Missing | Done | Content and timing for state laws [LEGAL REVIEW REQUIRED] |
| UC-SUB-18 Price change notice | Missing | Done, Open | Applying the new price is done in Stripe. Whether a price increase needs fresh consent [LEGAL REVIEW REQUIRED] |
| UC-SUB-19 Help paying | Removed | Removed | SUB-D-13 |
| UC-SUB-20 Disputed charge | Missing | Done | Owner alert is a log line. Route it to whoever owns billing |
| UC-SUB-21 Billing during a break or crisis | Missing | Done | |
| UC-SUB-22 Stripe events | Missing | Done | |
| UC-SUB-23 Settings, Subscription | Missing | Done | |

## Summary: journeys (1.1.0)

| Use case | Before | Now | Still open |
|---|---|---|---|
| UC-JEF-01 to 06, UC-JSU-01 to 06 | Missing (no tests) | Done | Every expected value in both files is a test against `journeys/tools/resolve.py`. The API still selects journeys from `database/content/journeys/journey-selection.json`, not the journeys package (open question 10 in database/CLAUDE.md) |
| change_needed_in_cairn_core (the M-EMPLOYED answer) | Missing | Done | `resolve.answer_question` with `answer_facts` on the module: a no sets both facts |

## Gaps found and what changed

### 1. One account state model (D-19)
- **Before.** `users.status` mixed setup and billing: `active_no_case`, `trial_active`, `read_only`, `subscribed`.
- **Now.** `status` is `pending_onboarding`, `setup_complete`, or `pending_deletion`. `access` (`full`, `read_only`) and `subscription_status` (`none`, `active`, `lapsed`) hold the billing state. `store.effective_access` derives read-only on every read: the free days ended, no active subscription, and no care rest stopping them. The `expire_trials` job keeps the stored `access` current for reporting. The migration maps every account.
- Tests: `test_users_validator`, `test_read_only_reads_drafts_settings_and_deletion_stay_available`, `test_status_mapping`.

### 2. The adult question (UC-REG-06)
- **Before.** Not built, by a product decision recorded before the account spec was rewritten.
- **Now.** Built as the 3.2.0 spec has it: yes or no with its time (`adult_attested`, `adult_attested_at`), never an age or a birthdate. A no stops onboarding for good and collects nothing more. The account is deleted with other unfinished sign-ups after 90 days. Accounts made before the question existed are asked once, like a changed acknowledgment, and nothing else repeats. See "Conflicts resolved" below.
- Tests: `test_adult_yes_is_recorded_with_its_time_and_goes_on`, `test_adult_no_stops_onboarding_and_collects_nothing_more`, `test_an_account_from_before_the_adult_question_is_asked_once`, `test_an_adult_no_stops_onboarding_and_collects_nothing_more`.

### 3. Notification choices for the whole account (D-13)
- **Before.** One `notification_preferences` document per journey, chosen at Start journey, with `in_app_only`, `push`, reasons, and `as_it_happens` or `daily_max` pacing.
- **Now.** One document per account (`_id` is the user id), chosen at setup (UC-REG-15: channels, then frequency), with `email`, `in_app` (always), and `browser` channels, `due_only`, `daily`, `weekly`, or `none`, quiet hours (21:00 to 08:00 in the browser's time zone), a browser push endpoint, and the lead time and inactivity notices confirmed with the first journey (UC-CASE-19). Settings changes each one (UC-REG-17), with Stop all reminders. The reminder job follows channels, frequency, and quiet hours exactly, and sends nothing during a break. The migration keeps each account's most recent journey choice, and an account that never chose keeps nothing outside Cairn.
- Browser notifications are empty Web Push messages signed with VAPID (`webpush.py`). No text goes through the push service. A 404 or 410 takes browser off the user's channels.
- Tests: `test_valid_choices`, `test_invalid_choices`, `test_notification_choice_validator`, `test_notifications_follow_the_choice_and_the_pace`, the `test_uc19_*` and `test_reg17_*` tests, `test_browser_notifications_are_empty_and_a_gone_subscription_is_dropped`.

### 4. Breaks on the account (Take a break 3.2.0)
- **Before.** A rest was per case (`cases.tasks_paused_until`), started from a case, with "the rest of today" ending at midnight.
- **Now.** `users.break_started_at`, `break_until`, `break_notice_at`, `break_notice_sent_at`. Every active journey's `tasks_paused_until` follows the account's break (BRK-D-06). `/v1/me/break` opens the screen for where the user is (S-01 to S-07). The rest of today ends at 11:59 PM local time (BRK-D-07). A normal break longer than 24 hours gets one break-ending notice 24 hours before, moved out of quiet hours, by the chosen channels. A changed break never sends a second one. A care rest stops the free days only while they run, and never schedules a notice outside Cairn (BRK-D-03). No break calls Stripe or changes a subscription field (BRK-D-08). The home screen shows the resting screen first, and editing a task ends the break.
- Tests: `test_take_a_break.py`, `test_a_break_covers_every_journey_and_never_touches_the_subscription`, `test_the_break_ending_notice_goes_once_by_the_chosen_channels`, `test_breaks_never_call_stripe_or_change_a_subscription_field`.

### 5. Check-ins on the account (crisis plan 3.2.0, DEC-26-04)
- **Before.** `cases.check_in_at`, so a check-in couldn't exist during setup, and an email check-in cleared it so it never showed in Cairn.
- **Now.** `users.check_in_at`, `check_in_case_id` (cancels it when that case is deleted), `check_in_by_email` (the setup question's yes to email), and `check_in_sent_at`. It goes once through the account's channels, ignoring frequency and respecting quiet hours, and also shows once in Cairn. Offered once after level 3 or 4 during setup too (AC-26-12).
- Tests: `test_a_check_in_is_on_the_account_and_goes_once_through_the_chosen_channels`, `test_check_in_*` in `test_case_creation_v2.py`, `test_distress_in_free_text_pauses_saves_nothing_and_offers_the_check_in`.

### 6. The trial-ending note (D-14)
- **Before.** Three reminders (day 21, day 27, 3 days before), emailed only to users who chose email for a journey.
- **Now.** One note a week before `trial_ends_at`, emailed to the sign-in email as a service notice and shown in Cairn. During a break it shows only in Cairn, and is marked `skipped_at` so it is never emailed late. An early subscriber gets the first payment note instead. The migration removes unsent old reminders and moves `trial_reminder_days_before` from 3 to 7.
- Tests: `test_the_trial_note_is_a_service_notice_sent_once_and_held_during_a_break`, `test_the_trial_note_is_emailed_whatever_the_reminder_choices`.

### 7. The subscription (subscription 3.3.0)
- **Before.** Nothing sold a subscription. `status = 'subscribed'` was administrator-only.
- **Now.** Stripe Checkout, the customer portal, cancel at period end, undo, and immediate cancellation on account deletion, through `stripe_client.py` (httpx, no SDK). Access changes only from verified webhook events, recorded once by id in `stripe_events` (ids, type, and times only), applied from the subscription's current state in Stripe. The price is one setting used to show and to charge, and the API refuses to start if any copy names another. Jobs retry events and cancellations, keep an early subscriber's first charge at the free days' end, send the yearly reminder, and send a price change notice when one is configured. Nothing about billing at care levels 3 and 4, and no subscribe prompt at level 2 or during a break. Disputes alert the owner and change nothing.
- Tests: `test_subscription.py`.

### 8. Sessions (D-20, UC-REG-19)
- **Before.** Nothing. A token was valid until Auth0's expiry.
- **Now.** `users.last_active_at`, written at most every 15 seconds outside the request's transaction. A token issued before a quiet stretch of more than 5 minutes gets 401 `session_timed_out`. A token older than 30 days too. `POST /v1/me/sign-out` ends the session on Cairn's side. The settings are config with the spec's values, and the welcome and account responses carry them for the client's warning.
- Tests: `test_signing_out_ends_the_session_and_a_new_sign_in_works`, `test_five_minutes_with_no_activity_signs_out`.

### 9. Data boundary at setup
- **Before.** A name shared by Google or Apple was accepted (`name_from_provider`) and kept as a pre-fill. The preferred name allowed 100 characters and any digits.
- **Now.** The request field is gone and the API refuses it (D-16, data_boundary.enforcement). `users.name_prefill` can only be null, and the migration cleared it. The preferred name is at most 50 characters with fewer than 5 digits, and a refused name is never stored or logged.
- Tests: `test_no_name_from_a_sign_in_provider_is_accepted`, `test_a_name_with_numbers_or_too_long_is_refused_and_never_kept`, `test_no_stored_field_matches_anything_never_collected_at_setup`.

### 10. Account actions at care level 4 (AC-26-11)
- **Before.** Only the account chat held a level 4 turn.
- **Now.** Settings changes, notification changes, account deletion, cancelling, and subscribing take `care_level` and change nothing at level 4 in that turn. They work when asked again.
- Tests: `test_settings_wait_at_care_level_4`, `test_cancel_waits_at_level_4_and_a_failed_call_is_retried_by_the_job`, `test_checkout_waits_at_levels_2_to_4_and_says_plainly_when_stripe_is_down`.

### 11. Support resources (crisis plan support_resources)
- **Before.** No page. Crisis lines appeared only inside crisis replies.
- **Now.** `GET /v1/support-resources`, no sign-in, nothing stored or logged with an id. Every setup and break response carries the link label.
- Tests: `test_support_resources_work_signed_out`.

## Conflicts resolved

The account spec asks for every conflict with earlier code to be listed.

1. **UC-REG-06.** `database/CLAUDE.md` recorded "not built, by product decision: Cairn does not ask for or store the user's age". Account spec 3.2.0 (2026-10-06, approved for build) lists it as Active, with only a yes or no and its time stored, and says no birthdate or age is stored. This branch follows the spec. It stores no age, which keeps the earlier decision's intent. Please confirm.
2. **Per-journey notifications (case DEC-08, OPEN-04).** Superseded by account D-13. The per-journey routes and data were removed. Each account keeps its most recent journey choice.
3. **Default channels.** Case v2 defaulted to in Cairn only. Account OPEN-11 (resolved) pre-selects email at setup. Accounts made before this branch that never chose keep in Cairn only, so nobody starts receiving email they didn't pick.
4. **Trial reminders.** OPEN-02's default (3 days before, outbound only with a channel) is replaced by D-14 (a week before, always emailed except during a break).
5. **I need a moment.** Retired by BRK-D-01. `GET /v1/onboarding/need-a-moment` is removed and `Support.need_a_moment_label` became `take_a_break_label`.
6. **Pre-filled preferred name.** Registration 1.x pre-filled it from Google or Apple. Account 3.2.0 forbids it (UC-REG-11, D-16).
7. **Voice confirmation.** Per-voice confirmations (draft copy) are replaced by the spec's one `voice_confirm`.

## Differences between the specs, reported

The instructions ask to report any difference found, and the crisis plan wins on crisis behavior.

1. **Voice labels.** The voice cards use the account spec's copy ("Warm and Patient", "Room to take things in before moving on"). `voices/manifest.yaml`, which drives the conversational voice, still says "Warm & Patient" with a different tagline. The voices folder wasn't changed here. Align the manifest with the spec, or the spec with the manifest.
2. **UC-CASE-19 voice samples.** The case spec's `voice_samples.uc_case_19` were written for the v2 channel question (the spec says so itself). They are kept verbatim in the copy for the card 48 eval set and are not used on the confirm screen.
3. **Read-only banner wording.** UC-SUB-01 shows the banner on every case view, and UC-CASE-25 shows it with Subscribe only at level 1. The banner itself mentions subscribing, so it is left out at levels 2 to 4 (`?care_level=` on the read routes), following the crisis plan.
4. **Trial note during a break.** D-14 says the note shows only in Cairn during a break. UC-CASE-12's confirmation says it is emailed. Both hold: the confirmation is the normal case, and the break rule wins during a break.
5. **Account deletion and subscription consent records.** UC-ACCT-01 and D-08 delete everything. UC-SUB-16 and OPEN-SUB-04 may keep subscription consent records for a legal period. Today everything is deleted, until counsel decides.
6. **Account data_requirements names.** The spec suggests `trial_reminder_sent_at` and `trial_note_at` on the account. The existing `trial_reminders` collection is kept (the spec allows adapting names), with one `trial_ends_soon` row and a `skipped_at` field.
7. **Browser push endpoint.** The spec stores the endpoint only. Web Push payloads need the subscription's keys too, so Cairn sends empty pushes, which need only the endpoint and carry nothing about the user.
8. **Early subscription within 48 hours of the free days' end.** Stripe Checkout refuses a trial end less than 48 hours away, so the first charge then comes up to 2 days after the free days end, never before (UC-SUB-06: no charge before `trial_ends_at`).

## Open decisions and their settings

| Decision | Setting | Built |
|---|---|---|
| OPEN-04 notification scope | `CAIRN_NOTIFICATION_SCOPE` | `account` only (D-13) |
| OPEN-08 draft check-in channel | `CAIRN_DRAFT_CHECK_IN` | `account_channels` only (D-13) |
| OPEN-BRK-01 notice during a care rest | `CAIRN_BREAK_NOTICE_DURING_CARE_REST` | `false` only |
| OPEN-BRK-02 notice with No reminders | `CAIRN_BREAK_NOTICE_WITHOUT_REMINDERS` | `true` (default) or `false` |
| D-20 sign-out | `CAIRN_INACTIVITY_TIMEOUT_SECONDS`, `CAIRN_TIMEOUT_WARNING_SECONDS`, `CAIRN_OVERALL_SESSION_DAYS` | 300, 20, 30 |
| D-04 price | `CAIRN_SUBSCRIPTION_PRICE_CENTS` | 1499 |
| D-14 trial note timing | `app_settings.trial_reminder_days_before` | 7 |
| OPEN-SUB-04 consent retention after deletion | none | Open, counsel |
| OPEN-SUB-05 Stripe receipts | Stripe Dashboard | Default on, outside Cairn's tests |
| OPEN-SUB-08 arbitration | Terms of Use | Open, counsel |

## Policy and document updates the specs require

From case creation 3.2.0 `policy_updates_required`:

- CAIRN-POL-PRIV-01 retention schedule: 28-day draft deletion.
- CAIRN-POL-PRIV-01: optional 'handled by' name as third-party personal data.
- CAIRN-POL-PRIV-01: speech input, audio never kept, transcripts redacted (new in v2).
- Terms of Use: subscription price and web billing now that alpha is web only (Terms v0.1 still references app store billing).
- CAIRN-POL-PRIV-01: account.check_in_at (time only, no reason) and the Support resources page, which is not logged with a user id.

From subscription 3.3.0 `changes_needed_in_other_specs` (the spec changes are in the code already):

- Terms of Use v0.1: Stripe web billing, $14.99, renewal, cancellation, no refunds (SUB-D-05), tax included (SUB-D-12), and that a break doesn't pause or change a paid subscription (SUB-D-15).
- Privacy Policy v0.2: Stripe as a subprocessor and what it receives (SUB-D-10), and that Stripe collects a billing address on its own page that Cairn never receives.
- Take a break `view_inventory`: add the subscription views (subscribe prompt, terms, finishing, Settings subscription, cancel). The API carries the labels. The inventory test lives in the client.

## Work outside the code

- **Stripe Dashboard:** product and tax code (with an accountant), Stripe Tax registrations, a tax-inclusive price confirmed (SUB-D-12), the customer portal without retention offers, failed-payment emails on, and the webhook endpoint with the events in README "Stripe". Secrets: `CAIRN_STRIPE_SECRET_KEY`, `CAIRN_STRIPE_WEBHOOK_SECRET`.
- **Auth0:** session inactivity 5 minutes and re-login after 30 days, refresh token lifetimes to match (auth0/README.md step 7). Scopes `openid` and `email` only.
- **Browser notifications:** generate a VAPID key pair. The private key goes to the jobs container (`CAIRN_VAPID_PRIVATE_KEY`), the public key to the API (`CAIRN_VAPID_PUBLIC_KEY`).
- **Owner alerts:** route `owner alert` ERROR lines from `cairn_api.subscription` and `cairn_api.account` to whoever owns billing.
- **Support:** the identity check before changing a sign-in email (`support-sign-in-email-recovery.md`) needs a security review before launch.
- **Run `database/db/apply.py`** on each database. The `2026-10-06_use_cases_v3` migration reshapes users, notification choices, cases, and trial reminders. The old `cases_check_in_idx` index is no longer described by the schema and can be dropped by hand.
