# Account lifecycle and keeping in touch: gap audit

> **2026-10-05:** `account-creation-review-gap-audit.md` closes the email provider gap (follow-up 5) and covers linked sign-ins in UC-REG-15 and UC-REG-16. Every email now goes through Twilio SendGrid, and text messages have a Twilio sender, still not offered (OPEN-05).

> **2026-10-01:** the database moved from PostgreSQL to MongoDB. SQL tables, row-level security policies, and functions named below now live in `database/db/schema.py` (collections, validators, roles) and `api/cairn_api/store.py` (the case boundary). `database/CLAUDE.md`, "The move to MongoDB", maps each one.

Audit of `journey-templates` (commit 61aa4c4) against the two specs dated 2026-09-25, and what this change adds:

- `cairn-account-use-cases-2026-09-25.json`: UC-REG-15 (delete my account), UC-REG-16 (download all my data), and changes to UC-REG-07 and UC-REG-08.
- `cairn-case-creation-use-cases-2026-09-25.json` (draft 0.4): UC-CASE-19 to UC-CASE-21 (keeping in touch and confirmations), and changes to UC-CASE-10, UC-CASE-12, and UC-CASE-18.

**Before** is the state at 61aa4c4: **Met**, **Partial**, **Conflict** (the code did the opposite of the spec), or **Missing**. **Now**: **Done** means built and covered by an automated test. **Client** means the API exposes what's needed but the check can only be met in the app UI. **Open** means not done, with the reason.

Tests: `api/tests/test_account_lifecycle.py` (database, as the cairnApp user) and `api/tests/test_account_lifecycle_rules.py` (no database). Database invariants: the 0011 sections of `api/tests/test_data_security.py`. Schema: migration `0011_account_lifecycle_notifications.sql`.

## Summary

| Use case | Before | Now | Still open |
|---|---|---|---|
| UC-REG-15 Delete my account | Partial (UC-ACCT-01) | Done, Client, Open | Sign-out is client. Email provider and relay domain. Q12, Q13 |
| UC-REG-16 Download all my data | Missing | Done, Open | File format (Q14): JSON only today |
| UC-REG-08 change (trial wording) | Conflict | Done | Everyone re-acknowledges the new wording once |
| UC-REG-07 change (no marketing) | Met | Done | Nothing to build. Recorded on the table |
| UC-CASE-19 Choose how Cairn keeps in touch | Missing | Done, Client, Open | SMS (card 50). Push delivery. Per journey or account-wide |
| UC-CASE-20 Change how Cairn keeps in touch | Missing | Done, Open | Chat recognition is rule-based. UC-END-08 and UC-END-11 flows |
| UC-CASE-21 Confirmations | Missing | Done, Open | Email provider. Q on following SMS or push |
| UC-CASE-10 change (delete a draft) | Missing | Done, Open | UC-END-13 isn't in these specs. Built from what they rely on |
| UC-CASE-12 change (step 3, copy) | Conflict | Done | Trial reminder follows the choice (Q1) |
| UC-CASE-18 change (same as first case) | Missing | Done | |
| D-2026-09-25-P1 (pausing and the trial) | Missing | Done | |
| D-2026-09-25-F1 (always free) | Partial | Done | |

## What conflicted with the specs before this change

- **Nothing was ever sent, and nothing was ever chosen.** There were no notification preferences at all. `cairn.claim_due_trial_reminders` returned every due reminder for email regardless of any choice, which D-2026-09-25-N1 forbids. It now returns only reminders for users who chose email for a journey. No sender existed for anything. `cairn_api/outbound.py` is the first one.
- **UC-ACCT-01's deletion screen offered a "Keep my account" button and asked "Do you want to delete...?"** UC-REG-15 wants one button and no persuasion. There was no confirmation email, no masked destination, no subscription note, and no sign-out signal. Cases in a hold didn't exist, so they couldn't be included.
- **The registration trial wording** said the 28 days begin "when you start your first case". DEC-01 moved that to the first journey, which was decision 2 in the case creation audit. UC-REG-08's new wording fixes it.
- **The pre-button notice** didn't say the days keep counting while paused (D-2026-09-25-P1). The confirmation promised "We'll remind you" with no channel.
- **No case could be deleted through the API.** The database allowed it, but no endpoint existed, so UC-CASE-10's "delete a draft any time" and UC-CASE-21's confirmations had nothing to hang on.

## Entity and field mapping (migration 0011)

| Spec | Where it lives |
|---|---|
| notification_preferences.journey_id | `notification_preferences.case_id`, the primary key. One journey per case, so the case id is the journey id |
| channels, reasons, due_date_lead_days, inactivity_days, frequency, push_permission_granted, updated_at | Same names. Shape rules in `cairn.notification_choice_valid` (CHECK) and `NotificationChoice` (API) |
| channels value `sms`, sms_number, sms_consent_at | Not stored. The database refuses `sms` until card 50 decides SMS for the MVP and legal review signs off on consent wording. The encrypted number and its key management come with that migration |
| Default in_app_only if skipped | Column defaults. `start_journey` also writes the default row when nothing was chosen |
| action_confirmation_log | `action_confirmation_log (id, action_type, channel, sent_at)`. content_stored is false by construction: no column can hold content or say who it went to. Append-only |
| (the address until it's sent) | `action_confirmation_outbox`. No app grants. The row is deleted once sent, which purges the address (UC-REG-15 step 7) |
| 7-day hold (UC-END-13) | `cases.deletion_requested_at` plus `app_settings.case_deletion_hold_days` (7). `deletion_scheduled_for` is computed on read |
| (what was sent and why) | `notification_log (case_id, case_task_id, reason, channel, sent_at)`, no text and no address. Drives frequency limits and appears in the download |

## Per use case and acceptance point

### UC-REG-15 Delete my account
- Step 1, what will be deleted in plain words (account, every case, every task, all conversation text, notification settings): `deletion_explanation`, `test_reg15_explains_everything_shows_where_the_confirmation_goes_and_offers_one_button`.
- Step 2, a store subscription is information only, with a link to each store's instructions: `test_reg15_a_store_subscription_is_information_only`. Cairn doesn't record which store, so both links are shown.
- Step 3, where the confirmation goes, masked: same test as step 1. `mask_email` keeps the domain, so an Apple relay address is recognizable.
- Step 4, one button, "Delete my account and everything in it". No reason and no retention offer: same test, plus `test_deletion_copy_never_asks_why_or_tries_to_keep_the_user`.
- Step 5, delete immediately, cases in a hold included, with tasks, answers, conversation text, and notification preferences: `test_reg15_deletes_everything_now_including_held_cases_and_sends_one_confirmation`, `test_data_security.py`.
- Step 6, Apple token revocation (TN3194): queued as before. `cairn_api/identity_cleanup.py` does the REST call. Same test.
- Step 7, one email, then purge the address: pending case confirmations are dropped so exactly one goes out. The row carries no user id and is deleted once sent. Same test, `test_data_security.py`.
- Step 8, sign out: the response has `signed_out: true` and `next_step.action = signed_out`, and every later call gets `registration_required`. **Client**: clear the session and return to the signed-out state.
- Alternate flow, risk of harm in chat [SAFETY]: `POST /v1/me/messages`. The first turn is 988, the Veterans Crisis Line for a veteran, and 911, with no account action. Asked again, the main flow runs with no extra questions and the crisis lines stay on screen: `test_reg15_asked_in_chat_with_a_risk_of_harm_signal_puts_safety_first_then_proceeds`. Nothing about it is stored (decision 7). The turn is tracked in the client-held `AccountChatSession`.
- Alternate flow, read-only account: `test_reg15_a_read_only_account_can_delete_for_free`.
- Rule, logged without deleted content: audit rows hold ids only. The confirmation log has no content column (`test_data_security.py`).
- Rule, always available from Settings (5.1.1(v)): no gate on onboarding, acknowledgments, or status: `test_always_available_even_when_an_acknowledgment_changed`.

### UC-REG-16 Download all my data
- One-sentence explanation: `GET /v1/me/data-export`, `test_reg16_explains_in_one_sentence`.
- Generate and offer the file: `GET /v1/me/data-export/file` returns JSON as an attachment with `Cache-Control: no-store`. It includes the profile, acknowledgments, reminders, and every case with its answers, the person's legal identity (never the SSN digits), tasks, a summary, notification settings and history, and conversation text: `test_reg16_the_download_has_everything_and_never_a_sensitive_number`.
- Never SSNs, account numbers, or card numbers: `deceased.ssn_last4` is never selected, and every free-text value is redacted again on the way out. The export schema has no field for any of them: `test_the_download_has_no_field_for_sensitive_numbers`.
- Read-only, and a case in a 7-day hold is included until deleted: `test_reg16_is_free_on_a_read_only_account_and_never_includes_another_users_data`, `test_hold_keeps_the_case_for_7_days_can_be_cancelled_and_confirms_when_deleted`.
- Asked in chat: `test_reg16_asked_in_chat`.
- Open, Q14: the file format. JSON covers Article 20 portability. A printable page or PDF would be an addition. "Hand-off summary" isn't defined in any spec yet, so each case carries a `summary` of where things stand. Replace it when the hand-off summary (UC-END) is specified.

### UC-CASE-19 Choose how and when Cairn keeps in touch
- Explains that the user decides and can change it any time: `test_uc19_setup_explains_then_offers_shortcuts_and_questions_in_order`.
- Channel, reasons (only for a channel other than in_app_only), timing per reason, frequency, readback and confirm: the same test, then `test_uc19_readback_before_saving_stores_nothing`. The API returns the questions in order with the condition for each, and the client asks one at a time.
- Shortcut "Keep it simple for me" (email, due_date_upcoming, 3 days, daily_max): `test_uc19_keep_it_simple_uses_the_draft_preset`, `test_keep_it_simple_matches_the_spec_shortcut`.
- Skipped or in_app_only means nothing is sent outside the app: `test_uc19_skipped_or_never_asked_is_in_app_only_and_nothing_is_sent`.
- Email shown masked, works with Apple private relay: `test_uc19_email_is_shown_masked_and_works_with_apple_private_relay`. **Open (operations)**: register the sending domain with Apple's Private Email Relay Service, with SPF and DKIM.
- Push: the OS prompt comes only after the user picks push (`request_push_permission`). Push is never required, and declining takes push off the channels: `test_uc19_push_prompt_only_after_push_is_chosen_and_never_required`. **Client**: show the OS prompt when told and post the result. **Open**: push delivery isn't built (no APNs or FCM provider and no device tokens), so a push choice sends nothing yet. The confirmation's reminder sentence only promises the outside-the-app reminder when email is chosen, so it never promises a push that can't arrive.
- SMS: not offered and refused by the API and the database. **Open**: card 50 and legal review.
- Distress: the setup happens inside UC-CASE-12, where UC-CASE-14 already applies.
- Rules. Notification text is short and private: `test_uc19_notifications_are_short_private_and_at_the_chosen_pace`, `test_outbound_messages_are_short_private_and_never_repeat_content`. Nothing is sent that wasn't chosen: the sender only reads `notification_preferences` and the confirmations queue. Never used for marketing: no code path reads preferences except the sender and the user's own reads, noted on the table.
- Pace: at most one message per journey per run. `daily_max` and `weekly_max` are rolling 24-hour and 7-day windows. A step coming up is sent once per task. Inactivity is sent once per quiet stretch. Nothing goes to drafts, paused journeys, cases set to be deleted, or read-only accounts: `test_data_security.py`.
- Data: per journey (`case_id`). Open question card 50: per journey or account-wide.

### UC-CASE-20 Change how Cairn keeps in touch
- Read back in one line and confirm, then apply at once: `test_uc20_a_change_is_read_back_in_one_line_and_applied_on_yes`.
- stop_everything sets in_app_only in one step with no follow-up: `test_uc20_stop_everything_is_one_step_on_every_journey`. With no scope it applies to every journey, because asking which one would be a follow-up question.
- Several journeys: asks which one by display name and offers "All of them": `test_uc20_with_several_journeys_asks_which_by_name_and_offers_all`.
- In chat ("Stop texting me", "Email me instead"): `test_uc20_by_chat_stop_texting_me_and_email_me_instead`.
- Always free, and turning notifications off never changes the journey: `test_uc20_always_free_on_a_read_only_account`, and the journey comparison in the readback test.
- From pausing (UC-END-08): the pause response offers it (`pause_notifications_offer`). Reopening (UC-END-11) isn't built. See below.

### UC-CASE-21 Confirmations for something the user just did
- Where it goes, shown before confirming: `test_uc21_where_the_confirmation_goes_is_shown_before_confirming`, and step 3 of UC-REG-15.
- Exactly one confirmation after the action: `test_uc21_delete_now_sends_exactly_one_private_confirmation_then_purges_the_address`, `test_uc21_a_failed_send_is_retried_and_still_sent_only_once`. A crash between sending and recording could repeat one message after 15 minutes. That's documented in the function, and a repeat is safer than never confirming.
- By email, short, private, never repeating deleted content: `test_outbound_messages_are_short_private_and_never_repeat_content`.
- Logged without content: `action_confirmation_log`, `test_data_security.py`.
- Open, card 50: should confirmations follow an SMS or push choice instead of email?

### Changes to existing use cases
- **UC-REG-08.** `trial_summary` is the new wording, verbatim. The text changed, so everyone who agreed to the old wording is asked to acknowledge it again on next sign-in (UC-REG-13). That's by design. `trial_checkbox` still says "start my first case" because the spec didn't change it. See the decisions list.
- **UC-REG-07.** No change. The table comment on `notification_preferences` records that preferences are never used for marketing or advertising. No phone numbers are stored.
- **UC-CASE-10.** There is no draft reminder (there never was). A draft can be deleted now or with a 7-day hold, for free: `test_case_deletion_is_free_on_drafts_and_read_only_accounts_and_owner_only`. Open: warn before a draft is auto-deleted at 28 days (card 50).
- **UC-CASE-12.** Step 3 is the notification setup. The preview's `next_step` is `choose_notifications` until a choice or skip is saved, then Start journey: `test_uc12_preview_then_start_starts_trial_once_in_local_time`. The pre-button notice is the new wording, and the confirmation ends with the reminder sentence that fits the choice: `test_uc12_confirmation_says_the_reminder_goes_the_way_the_user_chose`.
- **UC-CASE-18.** "Use the same as [first case name]" is offered first, and each journey keeps its own: `test_uc18_offers_the_same_as_the_first_case_first_and_each_journey_keeps_its_own`.
- **D-2026-09-25-P1.** Pausing says the free days keep counting and doesn't touch the trial: `test_pausing_says_the_free_days_keep_counting_and_offers_to_change_notifications`.

## Gaps these specs depend on but don't define

1. **UC-END-13 (delete a case, now or with a 7-day hold)** isn't in either spec, but UC-CASE-10, UC-CASE-21, UC-REG-15, and UC-REG-16 all rely on it. The mechanics are built as the specs describe them: delete now, or delete after 7 days. Things assumed that need confirming: the hold can be cancelled ("Keep this case"), the case stays usable during the hold, and the copy (`case_deletion_*`) is draft.
2. **UC-END-08 (pause) and UC-END-11 (reopen)** aren't in these specs. Pausing uses the existing UC-12 pause, now with the P1 note and the offer to change notifications. Closing and reopening a journey aren't built: `cases.status` allows `closed`, but nothing sets it.
3. **Hand-off summary** (UC-REG-16) isn't defined. See above.
4. **Push delivery** needs a provider, device token storage, and its own deletion path. Deleting the account would have to delete tokens (UC-REG-15 step 5, "stored tokens"). Today the only stored tokens are Apple's, in Auth0, and those are revoked.
5. **The email provider** isn't chosen. `SmtpMailer` works with any SMTP provider. `api/scripts/send_outbound.py` is the job.

## Decisions to confirm with the product owner

1. **Trial reminder follows the choice (Q1).** Built as the UC-CASE-12 change reads: always in Cairn, and by email only when the user chose email for a journey. If Q1 lands on "always sent", drop the `EXISTS` clause in `claim_due_trial_reminders`.
2. **Which journey's choice governs the trial reminder.** Today it's any journey with email. The alternative is only the journey that started the trial.
3. **stop_everything from chat applies to all journeys** without asking which one, because the spec says no follow-up question. From Settings with a scope, it applies to that scope.
4. **Email me instead keeps the existing reasons and pace** where there are some. Otherwise it proposes the Keep it simple values. It is always read back first.
5. **Inactivity counts from** the last intake answer, task status change, snooze, or opening the journey.
6. **Read-only accounts get no notifications**, because there is nothing to act on except subscribing, and that would be a nudge. Confirmations still go.
7. **`trial_checkbox`** still says "28 days after I start my first case". Should it say "journey" like the new summary? Resolved 2026-10-05: it says "my first journey" (`account-creation-review-gap-audit.md`).
8. **Q12 and Q13 (card 50)** are built as their drafts say: no hold for account deletion, and the address is purged right after the confirmation is sent.
9. **`deceased.ssn_last4` exists** (decision 6 in `database/CLAUDE.md`), while UC-REG-16 says no SSNs are stored. It is never exported, and no endpoint writes it. Consider dropping the column if last-four digits are no longer planned.

## Follow-ups outside this change

- Schedule `python api/scripts/send_outbound.py` every 5 minutes and `SELECT cairn.purge_held_cases();` at least hourly, both as the owner, next to the other jobs in `api/README.md`.
- Add the 7-day hold and the post-send address purge to the retention schedule in CAIRN-POL-PRIV-01. [LEGAL REVIEW REQUIRED]
- Product and legal review of the new draft copy: notification setup, deletion, confirmations, and the account chat replies.
