# Account creation review: UC-REG-01 to UC-REG-16 and UC-ACCT-01

Review of the account creation code on `main` (commit 4506ef4, after case creation v2) against both account specs, and what the `account-creation-review` branch changes:

- `cairn-registration-use-cases.json` spec 1.2.0: UC-REG-01 to UC-REG-14 and UC-ACCT-01.
- `cairn-account-use-cases-2026-09-25.json`: UC-REG-15, UC-REG-16, and the changes to UC-REG-07 and UC-REG-08.

It also records the move of every email and text message to Twilio.

**Before** is the state at 4506ef4: **Met**, **Partial**, or **Missing**. **Now** is the state on this branch: **Done** means built and covered by an automated test. **Client** means the API exposes what's needed but the check can only be met in the app UI. **Open** means not done, with the reason.

Earlier audits are still the detail for anything this branch didn't touch: `registration-gap-audit.md` (UC-REG-01 to 14) and `account-lifecycle-gap-audit.md` (UC-REG-15, 16, and the 2026-09-25 changes). The full suite (558 tests, on MongoDB 8.0) passed before and after this change.

New tests: `api/tests/test_account_creation_gaps.py` and `api/tests/test_twilio_client.py`. Live Twilio checks: `api/integration/test_twilio_live.py`, run by hand from `.github/workflows/twilio-integration.yml`.

## Summary

| Use case | Before | Now | Still open |
|---|---|---|---|
| UC-REG-01 Welcome | Met | Done, Client | WCAG 2.2 AA, 44x44 pt targets, color (client) |
| UC-REG-02 Google | Met | Done | |
| UC-REG-03 Apple | Partial | Done, Open | Authenticate the sending domain in SendGrid and register it with Apple's relay (operations). The live email check can now send to a relay address |
| UC-REG-04 Email | Partial | Done, Open | Auth0 tenant: 15-minute link, SendGrid as its email provider (operations) |
| UC-REG-05 Account exists | Partial | Done | Two accounts that both have cases still go to support. Linking rules confirmed (D-2026-10-05-L1) |
| UC-REG-06 Age | Removed | Removed | By product decision. Departs from the spec [LEGAL REVIEW REQUIRED] |
| UC-REG-07 Privacy and Terms | Met | Done | Legal review items |
| UC-REG-08 Free trial | Partial | Done, Open | Subscription purchase and billing. `trial_checkbox` now says "first journey" |
| UC-REG-09 AI notice | Met | Done, Client | AI label in every chat view (client). Legal review |
| UC-REG-10 Decline | Partial | Done | Deleted after 90 days (D-2026-10-05-R1). Add it to the retention schedule in CAIRN-POL-PRIV-01 |
| UC-REG-11 Preferred name | Met | Done | |
| UC-REG-12 Personality | Met | Done, Open | Review of draft sample replies |
| UC-REG-13 Resume | Partial | Done, Open | The no-case retention period [LEGAL REVIEW REQUIRED] |
| UC-REG-14 Distress | Partial | Done, Open | Clinical sign-off of the phrase list (DEC-26-06). Suicide loss confirmed as not a crisis (D-2026-10-05-S1) |
| UC-REG-15 Delete my account | Partial | Done, Client, Open | Sign-out is client. Q12, Q13 drafts |
| UC-REG-16 Download all my data | Met | Done, Open | File format (Q14): JSON only |
| UC-ACCT-01 Delete account | Superseded | Superseded | By UC-REG-15 |

## Gaps found and what changed

### 1. No email provider (UC-REG-03, UC-REG-08, UC-REG-15)
- **Before.** `outbound.py` had only `SmtpMailer`, and every audit listed the provider as not chosen. Deletion confirmations waited in the queue, and trial reminders never left Cairn.
- **Now.** Every email goes through Twilio SendGrid (`twilio_client.SendGridMailer`), the default when `CAIRN_EMAIL_PROVIDER` is unset. It sends plain text to one recipient and turns off open, click, subscription, and analytics tracking on every message, so nothing tracks the reader (D-07). Errors keep the HTTP status and Twilio's error code only, never the address or body. Without `TWILIO_SENDGRID_API_KEY` the `outbound` job answers `skipped`, as before. SMTP stays for local development (`CAIRN_EMAIL_PROVIDER=smtp`).
- Auth0 sends the magic link itself. `auth0/README.md` now points Auth0's email provider at the same SendGrid key, so that email goes through Twilio too.
- Tests: `test_email_goes_to_sendgrid_as_plain_text_with_every_kind_of_tracking_off`, `test_a_refused_email_raises_a_code_and_never_the_address_or_body`, `test_twilio_sendgrid_is_the_default_email_provider`, `test_without_the_sendgrid_key_the_outbound_job_is_skipped`.

### 2. Text messages had no sender (OPEN-05)
- **Before.** Nothing could send a text. `CAIRN_SMS_ENABLED=true` was refused because nothing was built.
- **Now.** `TwilioClient.send_sms` (Programmable Messaging) and `start_verification` and `check_verification` (Twilio Verify, the spec's "verify by code") are built and tested. **Text messages are not offered to anyone.** The product owner confirmed on 2026-10-05 that text messages are not in the MVP (OPEN-05 stays off). The sender is kept for later. `CAIRN_SMS_ENABLED=true` is still refused, with a message that says why. Turning it on needs: number collection, the consent line, an `sms` channel in `CHANNELS`, encrypted storage of the number, and its deletion path. None of that is in this change.
- Tests: `test_a_text_message_uses_basic_auth_and_the_from_number`, `test_a_trial_account_refusing_an_unverified_number_keeps_twilios_code_only`, `test_verify_starts_a_code_by_text_and_checks_it`, `test_an_expired_or_used_code_is_simply_not_approved`.

### 3. UC-REG-05: no way to add a second sign-in method
- **Before.** The alternate flow "User wants to link a second sign-in method. Require sign-in with the original method before linking." wasn't built. The 409 offered only the old method, and an Apple relay account plus a real-email account always needed support.
- **Now.**
  - `POST /v1/me/sign-in-methods` is signed in with a sign-in that already belongs to the account. The body carries an access token from the new sign-in, verified by the same Auth0 verifier, and its email must be confirmed.
  - The new sign-in is stored in `users.linked_identities`, and `resolve_user` finds the account by either subject. A unique index (`users_linked_subject_uq`) and `store.link_identity` make sure one sign-in never belongs to two accounts.
  - Refused: an unconfirmed email, a method the account already has, and a sign-in or email that belongs to another account. That last one says to contact support.
  - `DELETE /v1/me/sign-in-methods/{method}` removes an added method and queues its Auth0 cleanup. The sign-in the account was created with can't be removed.
  - The 409 `account_exists` keeps the old method as the primary button and adds `link_after_sign_in`.
  - Deleting the account (UC-REG-15) and the unfinished sign-up job queue identity cleanup for every linked sign-in. The download (UC-REG-16) lists `linked_sign_in_methods` but never identity provider ids.
  - Accounts are never linked automatically. Auth0 still holds two users, and Cairn maps both to one account, so no Management API scope is added.
- `support-account-merge.md` now starts with the self-serve path.
- Tests: `test_uc_reg_05_*` and `test_uc_reg_15_and_16_cover_linked_sign_ins`.

### 4. UC-REG-10 and UC-REG-13: the cleanup existed but never ran
- **Before.** `maintenance.purge_stale_accounts` existed, with no job, no schedule, and no way to set the periods.
- **Now.** The `purge_stale_accounts` job runs daily (`30 3 * * *`). An account still in `pending_onboarding` 90 days after it was created is deleted with its consents and identity cleanup (D-2026-10-05-R1). `CAIRN_PENDING_ACCOUNT_RETENTION_DAYS` can override 90. Finished accounts that never created a case are deleted only when `CAIRN_NO_CASE_ACCOUNT_RETENTION_DAYS` is set, because that period isn't decided. A period under one day is refused.
- Tests: `test_uc_reg_10_unfinished_sign_ups_have_90_days`, `test_uc_reg_10_and_13_unfinished_sign_ups_are_deleted_after_the_set_period`, `test_uc_reg_10_a_zero_day_period_is_refused`.

### 5. UC-REG-04: the expired-link and resend copy was unreachable
- **Before.** `email_link_expired`, `email_check_spelling`, `send_new_link`, and `resend_link` were in the copy file, but no response returned them, so the client had to hard-code them.
- **Now.** `GET /v1/welcome` returns `email_sign_in`, with that copy, the 15-minute link lifetime, and the 60-second resend delay.
- Test: `test_uc_reg_04_welcome_carries_the_email_link_copy`.

### 6. UC-REG-14: sign-up used its own phrase list
- **Before.** Sign-up had a separate, older phrase list marked "align with card 26 once written". The crisis plan is now written (`cairn-support-crisis-plan.json`), and case creation follows it in `safety.py`. Sign-up missed ending language (DEC-26-05, "I'm done", "I can't do this anymore") and acute distress ("I can't stop crying").
- **Now.** `onboarding.shows_distress` uses `safety.classify`: level 3 or 4 pauses sign-up, the same as case creation. The acceptance criterion "behavior matches the crisis plan" now holds by construction. The crisis plan treats naming someone who died by suicide as a loss, not a risk, so that alone doesn't pause sign-up. The product owner confirmed this on 2026-10-05 (D-2026-10-05-S1): a case can be about a death by suicide, and that doesn't start the crisis plan.
- Tests: `test_uc_reg_14_sign_up_pauses_on_what_the_crisis_plan_calls_level_3_or_4`, `test_uc_reg_14_ordinary_answers_do_not_pause`, and the existing `test_distress_in_free_text_pauses_and_saves_nothing`.

## Twilio integration testing

- **Every push and pull request:** `api/tests/test_twilio_client.py`, with `httpx.MockTransport`. No network, no trial credit.
- **By hand only:** `.github/workflows/twilio-integration.yml` (`workflow_dispatch`).
  - The credential check always runs and is free. Each box sends at most one text, one Verify code, or one email.
  - A box ticked with a missing secret fails the run, so green means sent.
  - Runs don't overlap. The live tests are outside `testpaths`, so the other workflows never collect them, and the pull request rule against skipped tests isn't affected.
- Secrets and settings are listed in the repository README, "Twilio (email and text messages)".

## Decisions

Confirmed by the product owner on 2026-10-05:

1. **Text messages are not in the MVP** (OPEN-05 stays off). The Twilio sender and Verify client stay, unused, for later. Adding text messages later needs legal review of the consent line first.
2. **Unfinished sign-ups are deleted after 90 days** (D-2026-10-05-R1, UC-REG-10). Built as the job's default. Add it to the retention schedule in CAIRN-POL-PRIV-01.
3. **Linking rules are right for the MVP** (D-2026-10-05-L1): one sign-in per method, so at most three in all. The account email stays the one from the original sign-in, and only added methods can be removed.
4. **A death by suicide is a loss, not a crisis** (D-2026-10-05-S1). Naming one during sign-up or case creation doesn't pause sign-up or start the crisis plan. The user's own risk signals still do.
5. **The trial says "first journey" everywhere.** `trial_checkbox` now reads "I understand that 28 days after I start my first journey, I will need a subscription to keep using Cairn fully." The copy file is 1.4.0 and the registration spec is 1.3.0. Because the text changed, everyone who agreed to the old trial terms is asked once more on next sign-in (UC-REG-13).
6. **The Twilio SendGrid key is set.** `TWILIO_SENDGRID_API_KEY`, `TWILIO_ACCOUNT_SID`, and `TWILIO_AUTH_TOKEN` are organization secrets in `cairnguide`, which the Twilio integration workflow reads the same way as repository secrets.

Still open:

1. **The rest of the live email check's settings.** The email check also needs `CAIRN_EMAIL_FROM` (a sender SendGrid has verified) and `TWILIO_TEST_TO_EMAIL`. The Cloudflare jobs container needs the key too: `npx wrangler secret put TWILIO_SENDGRID_API_KEY`.
2. **The retention period for accounts that finish sign-up but never create a case** (UC-REG-13). [LEGAL REVIEW REQUIRED]
3. **The new linking copy** (`link_after_sign_in`, `sign_in_method_*`) is draft and needs product and legal review.
