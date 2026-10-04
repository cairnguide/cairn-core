# Case creation gap audit: spec 2.0.0

Audit of the case creation code on `main` (8c1ce97, which implements spec 0.3.0 plus the 2026-09-25 changes) against `cairn-case-creation-use-cases-v2.json` spec 2.0.0. The spec is copied to `docs/cairn-case-creation-use-cases-v2.json`. It defers to the Support and Crisis Plan (card 26), copied to `docs/cairn-support-crisis-plan.json`. Where the two differ, the crisis plan wins, and the differences are listed below.

Following `instructions_for_claude_code`, this change compares v2 with the version already built and changes only what v2 changes. Existing collections are extended. No collection or field is dropped. One field key and one enum value are renamed by a forward migration (see "Decisions to confirm").

**Before** is the state at 8c1ce97: **Met**, **Partial**, **Conflict** (the code did the opposite of v2), or **Missing**. **Now**: **Done** means built and covered by an automated test. **Client** means the API returns what the screen needs, but the check can only be met in the web app. **Open** means not done, with the reason.

Tests:
- `api/tests/test_case_creation_v2.py`: the v2 additions, against MongoDB.
- `api/tests/test_case_creation.py`: against MongoDB.
- `api/tests/test_case_creation_rules.py`: no database.
- `api/tests/test_data_security.py`: the database invariants.

[SAFETY], [PRIVACY], and [LEGAL] items are release blockers. Each one has a test.

## Summary

| Use case | Before | Now | Still open |
|---|---|---|---|
| UC-CASE-01 Start a new case | Met | Done | Transcripts follow the typed text path |
| UC-CASE-02 Who you are to them | Met | Done | |
| UC-CASE-03 What to call them | Met | Done | |
| UC-CASE-04 When and where | Partial (50 states only) | Done | 56-entry picker. The DMV follows where they lived |
| UC-CASE-05 How it happened | Partial | Done | `loss_survivor_resources` is set only when the user says it |
| UC-CASE-06 Military service | Conflict (steps removed on "no") | Done | |
| UC-CASE-07 Will or estate plan | Met | Done | |
| UC-CASE-08 Already done | Partial | Done | "Someone else is handling it" and the new checklist items |
| UC-CASE-09 Skip, unsure, change | Conflict (threshold) | Done | |
| UC-CASE-10 Pause and come back | Partial | Done | The draft card shows the v2 draft notice |
| UC-CASE-11 Review | Met | Done | |
| UC-CASE-12 Start the journey | Partial | Done, Open | Billing is not built. Price wording needs counsel |
| UC-CASE-13 First task | Partial | Done | The secure-now step goes first when pets or dependents come up |
| UC-CASE-14 Distress | Conflict (levels) | Done, Open | The phrase list needs clinical sign-off (DEC-26-06). The model layer comes with the model |
| UC-CASE-15 Sensitive numbers | Partial | Done | Partial SSNs and spoken digits |
| UC-CASE-16 Attorney referral | Partial | Done | `early_property_disposal` trigger |
| UC-CASE-17 Not died yet | Met | Done | |
| UC-CASE-18 Second case | Partial | Done | Subscribed copy |
| UC-CASE-19 Keeping in touch | Partial | Done, Client | Browser permission timing is a client rule |
| UC-CASE-20 Change keeping in touch | Met | Done | |
| UC-CASE-21 Confirmations | Met | Done | |
| UC-CASE-22 Answer by speaking | Missing | Done, Client | Speech to text runs in the browser. The API takes transcripts only |
| UC-CASE-23 AI reminder and rest offer | Missing | Done, Client | Screen reader announcement is a client rule |
| UC-CASE-24 Under 18 | Missing | Done, Open | OPEN-09 needs legal review |

## What v2 changed, and where it is built

### Journey template (card 6)
- `journey-selection.json` is now version 2. Every path always includes:
  - first hours
  - the death certificate
  - what to secure now (card 59, `secure_home_and_identity`)
  - funeral research only (card 52, `choose_funeral_provider` v3)
  - all 10 government agencies (card 15)
  - banks, credit cards, mortgages and loans, life insurers, and employers (card 16)
- Eleven new task templates are in `database/content/tasks/us/`. Their citations were checked against USA.gov "report a death" and each agency's own page. They are unreviewed drafts until counsel signs them off.
- Add-ons can now `recommend_tasks` and `mark_probably_not_applicable`. Notes can attach to more than one task. The loader checks that each named task is in the journey.

### Statuses (card 50)
- `case_tasks.status` adds `handled_elsewhere`.
- `case_tasks` adds the flags `needs_check`, `probably_not_applicable`, and `handled_by`. `check_on_this` becomes `not_started` with `needs_check` true, through a migration. The old value stays in the enum so nothing old is refused.
- Responses carry `journey_status` (open, done, not_needed, handled_elsewhere).
- Case responses carry `account_access` instead of the read_only case status.
- `pending_deletion` is derived from the 7-day hold.

### Trial and care rest (card 26, DEC-26-01)
- `users.trial_clock_paused_at` is set when a care rest begins on an active journey. When the user comes back, or the rest ends, `trial_ends_at` and any unsent trial reminder move later by exactly the paused time.
- This runs in `Session.settle_trial_clock`, called on resume, on `require_ready`, and by the `settle_trial_clocks` job.
- The users validator now holds `trial_ends_at >= trial_started_at + 672h` instead of exactly 672h.
- A normal rest never pauses the free days, and Cairn says so before it starts.

### Crisis plan levels
`safety.py` was rewritten to the crisis plan's four levels:

| Level | Starts on |
|---|---|
| 2 | Two overwhelm signals, or three skips in a row |
| 3 | Any one signal |
| 4 | Any one signal, including ending language that isn't about the paperwork |

When it happens:
- Levels 3 and 4 pause the tasks on their own (a care rest).
- Level 4 replies with 988 first, adds the Veterans Crisis Line (text 838255) for a veteran and the Crisis Text Line, and asks about suicide directly when the words were unclear.
- Each level 4 referral adds one to an anonymous monthly count, `safety_referral_counts`, for SB 243 reporting.

### Follow-up check-in (DEC-26-04)
- Cairn asks once, never in the first level 4 reply.
- On a yes, `cases.check_in_at` is set. The check-in goes by email if the user chose email for that journey (job `outbound`). Otherwise it shows in Cairn the next time they open the case (OPEN-08).
- It never names the person who died or the circumstance, and it is sent once.

### Billing during a crisis
- At care levels 3 and 4, the preview shows no notice, price, or trial wording. Start journey returns 409 `not_now`.
- The pre-button notice and confirmation now come in first-case, existing-trial, and subscribed versions.
- The first-case confirmation has the $14.99 disclosure, the reminder channel clause, and `legal_review_required`.

### Speech (UC-CASE-22)
- `POST /intake/transcripts` takes a transcript or `unclear`. There is no audio field or endpoint.
- Transcripts are redacted as they are parsed, with spoken digits normalized first. Crisis detection runs on the transcript before it is shown back.

### Long sessions (UC-CASE-23)
- The AI reminder comes at session start (at most once a day) and every 3 hours, at every level. At level 4 it comes after 988. This uses `users.ai_reminder_shown_at` and `users.ai_reminder_shown_on`.
- Rest offers come after 45 active minutes, or after the circumstance answer. Each is offered once per session, at level 1 only.
- Session timing is kept in the client-held session.

### Under 18 (UC-CASE-24)
- A first-person statement stops the intake questions and offers 988.
- Nothing about age is stored. Distress handling runs first.

### Jurisdictions (DEC-COV)
- 56 jurisdictions are accepted, with full names in the copy layer.
- `place_of_death.state` is now `place_of_death.jurisdiction`, and `residence_state` is now `residence_jurisdiction`. A migration renames both.
- The DMV step follows where they lived. When neither is known it asks just in time. An unverified jurisdiction adds "Please confirm with the office." to the certificate step, not a separate task (OPEN-06).

### Copy
- `case-creation-copy.json` 2.0.0 holds the v2 copy and the crisis plan scripts verbatim.
- The copy is web-only ("select", "browser notifications").
- `legal_review_keys` flags the price wording.
- A test keeps reminders and offers at grade 8 or below.

### Open decisions
OPEN-04 to OPEN-10 are settings with the spec's defaults. Any other value is refused at startup (`config.py`).

## Acceptance criteria and their tests

Test files are abbreviated: `v2` is `test_case_creation_v2.py`, `cc` is `test_case_creation.py`, `rules` is `test_case_creation_rules.py`, and `ds` is `test_data_security.py`.

| Criterion | Test |
|---|---|
| 01 Creating a case is a draft, trial untouched | cc `test_uc01_new_case_is_a_draft_and_does_not_start_the_trial` |
| 01 [PRIVACY] Free text and transcripts persist only data_fields | cc `test_uc01_confirmations_refuse_anything_outside_data_fields`, v2 `test_transcript_is_redacted_shown_back_and_never_stored` |
| 04 Picker includes 50 states, DC, and 5 territories | rules `test_uc04_picker_covers_all_56_jurisdictions_by_full_name`, contract `test_state_codes_are_normalized_and_checked` |
| 04 Certificate by place of death, DMV by residence | rules `test_uc04_dmv_follows_where_they_lived_and_defaults_to_the_place_of_death`, cc `test_uc04_certificate_office_uses_place_of_death_not_residence` |
| 05 [PRIVACY] Only the enum persisted | cc `test_uc05_only_the_enum_is_stored_and_free_text_is_never_logged`, `test_uc05_volunteered_suicide_loss_is_acknowledged_and_resources_offered_once` |
| 06 VA and military retiree steps never removed | rules `test_uc06_va_and_military_retiree_steps_are_never_removed`, cc `test_uc06_no_keeps_the_va_steps_visible_as_probably_not_applicable`, use cases `test_late_veteran_answer_adds_va_task_without_losing_progress` |
| 08 handled_by optional, new checklist items | rules `test_uc08_someone_else_handling_it`, `test_uc08_checklist_has_the_secure_now_and_bank_insurer_employer_items`, cc `test_uc08_someone_else_is_handling_it_with_an_optional_name` |
| 09 [SAFETY] Third consecutive skip triggers level 2, not before | rules `test_uc09_third_skip_in_a_row_starts_level_2_and_unsure_does_not_count`, cc `test_uc14_overwhelm_stops_questions_and_offers_a_pause_or_one_small_thing` |
| 10 28-day notice on the draft card | v2 `test_draft_card_says_it_is_deleted_after_28_days` |
| 12 trial_ends_at = start + 28 days + care-rest time | v2 `test_care_rest_pauses_the_free_days_and_moves_the_trial_end_by_exactly_the_paused_time`, `test_a_rest_in_normal_mode_keeps_the_free_days_counting_and_says_so_first` |
| 12 [SAFETY] No trial or price wording at levels 3 and 4 | v2 `test_no_billing_wording_and_no_start_at_levels_3_and_4` |
| 12 [LEGAL] Price wording approved by counsel | rules `test_legal_wording_is_flagged_for_counsel`, cc `test_uc12_preview_then_start_starts_trial_once_in_local_time` (`legal_review_required`) |
| 13 Not today includes the free-days line | v2 `test_not_today_says_the_free_days_keep_counting_on_a_trial` |
| 13 Pets or dependents put the secure-now step first | v2 `test_pets_or_dependents_put_securing_things_first` |
| 14 [SAFETY] Level 4 reply has 988 and no task or question | v2 `test_level_4_says_988_first_asks_directly_and_counts_once_without_ids`, cc `test_uc14_risk_of_harm_first_response_has_988_and_no_task_or_question` |
| 14 [SAFETY] Veterans Crisis Line for veterans | v2 `test_level_4_for_a_veteran_adds_the_veterans_crisis_line_with_the_text_number` |
| 14 [SAFETY] No promise to stay safe, no methods | rules `test_uc14_crisis_copy_makes_no_promises` |
| 14 [PRIVACY] Nothing stored but trial_clock_paused_at and check_in_at | cc `test_uc14_distress_is_never_persisted`, v2 level 4 test |
| 14 [LEGAL] Anonymous monthly SB 243 count | v2 `test_level_4_says_988_first_asks_directly_and_counts_once_without_ids`, account lifecycle `test_reg15_...` |
| 15 [PRIVACY] Partial SSNs and spoken digits | rules `test_uc15_partial_ssns`, `test_uc15_spoken_digit_sequences_are_normalized_before_matching` |
| 16 Attorney triggers incl. early property disposal | rules `test_secure_now_step_has_the_attorney_trigger`, `test_uc16_attorney_triggers_and_unverified_jurisdictions` |
| 18 Trial unchanged by a second start, subscribed copy | v2 `test_second_case_keeps_the_trial_end_date`, `test_a_subscriber_never_sees_trial_wording` |
| 18 [SAFETY] No subscribe prompt at levels 3 and 4 | v2 `test_no_billing_wording_and_no_start_at_levels_3_and_4` |
| 19 [PRIVACY] Outbound passes a name and circumstance check | rules `test_uc19_outbound_text_is_checked_for_names_and_circumstances`, v2 `test_check_in_by_email_is_private_and_sent_once` |
| 22 [PRIVACY] No audio anywhere, transcripts redacted | v2 `test_speech_is_optional_and_labelled`, rules `test_uc22_transcripts_are_redacted_as_they_are_parsed` |
| 22 [SAFETY] Crisis detection on transcripts | v2 `test_crisis_language_in_speech_is_handled_before_any_confirmation` |
| 22 Speech never required, labelled control | v2 `test_speech_is_optional_and_labelled`, `test_unclear_transcript_asks_again_and_never_guesses` |
| 23 [LEGAL] AI reminder at start and every 3 hours | v2 `test_ai_reminder_at_session_start_once_a_day_and_again_after_three_hours`, `test_ai_reminder_is_never_skipped_at_level_4_and_comes_after_988` |
| 23 Rest offer once per trigger, grade 8 | v2 `test_rest_offer_after_a_heavy_answer_once`, `test_rest_offer_after_about_45_minutes_of_active_use`, rules `test_copy_is_about_grade_8` |
| 24 [PRIVACY] No age stored, [SAFETY] distress first | v2 `test_under_18_stops_the_questions_and_stores_no_age`, `test_under_18_with_distress_handles_the_distress_first`, rules `test_uc24_under_18_statements` |
| Crisis plan [TRIAL] paused clocks never expire | v2 `test_expire_trials_never_expires_a_paused_clock` |
| Crisis plan [FOLLOW-UP] only after yes, once | v2 `test_check_in_only_after_yes_and_shown_in_cairn_once_without_email` |
| Forward migration | v2 `test_the_v2_migration_renames_values_and_keeps_every_answer` |
| Open decisions as config | rules `test_open_decisions_are_configuration_with_the_spec_defaults` |

Criteria unchanged from 0.3.0 keep the tests listed in `case-creation-gap-audit.md`.

## Where the crisis plan and v2 differ (the crisis plan wins)

Reported to the developer as the instructions ask:

1. **Level 2 entry.** v2 UC-CASE-14 lists overwhelm as a mode a single phrase can start. The crisis plan (DEC-26-03) needs two signals in one conversation, or three skips in a row. Built to the crisis plan.
2. **Raised sensitivity no longer lowers the skip threshold.** 0.3.0 lowered it after a volunteered cause. The crisis plan sets three skips for everyone, so the lowering is gone.
3. **Ending language.** "I can't do this anymore" and "I'm done" are level 4 under DEC-26-05, with the direct question. "I can't do this" stays level 3. "I'm done with these questions" is not a crisis.
4. **Rest copy.** The crisis plan's rest choices, clock notes, and return note are used verbatim. v2 has only the 45-minute offer.
5. **Check-in in the first level 4 reply.** The crisis plan puts 988 first with nothing else to answer, so the check-in question waits for the next turn or for the user's return.

## Decisions to confirm with the product owner

1. **Renames.** The `residence_state` field key, the `state` value key, and the `bank_notified` item value were renamed by a forward migration (`2026-10-04_case_creation_v2`), so the stored data matches v2's names. No collection or field was dropped. If renames should have been asked about first, the migration can be replaced by aliases before it runs anywhere shared.
2. **The bank, insurer, or employer item** marks three tasks done: banks, life insurers, and employer and pension.
3. **"Until I come back"** is stored as a rest 3,650 days long, because `tasks_paused_until` needs an end time.
4. **`in_cairn_only`.** v2 names the default `in_cairn_only`. The stored value stays `in_app_only`, with the same meaning.
5. **The hosting platform.** DEC-PLAT says CloudFront. The repository deploys to Cloudflare. Nothing in this change depends on it.
6. **Buying a subscription** is in the MVP under DEC-SUB, but no payment provider has been chosen. The price is shown, and `users.status = 'subscribed'` can still be set only by an administrator.
7. **The phrase lists** in `safety.py` need licensed clinical sign-off (DEC-26-06) before any real user.

## Policy updates required

These are tracked here and are not code:

- CAIRN-POL-PRIV-01 retention schedule: 28-day draft deletion.
- CAIRN-POL-PRIV-01: optional 'handled by' name as third-party personal data.
- CAIRN-POL-PRIV-01: speech input, audio never kept, transcripts redacted (new in v2).
- Terms of Use: subscription price and web billing now that alpha is web only (Terms v0.1 still references app store billing).

## Not built (out_of_scope_mvp)

The following are not built, as the spec says:
- document upload and a vault
- the journey after week 4
- the pre-need path
- deaths outside the US (shown the out-of-scope message)
- text messages (OPEN-05 is off, and turning it on is refused)
