# Case creation gap audit: UC-CASE-01 to UC-CASE-18

> **2026-10-01:** the database moved from PostgreSQL to MongoDB. SQL tables, row-level security policies, and functions named below now live in `database/db/schema.py` (collections, validators, roles) and `api/cairn_api/store.py` (the case boundary). `database/CLAUDE.md`, "The move to MongoDB", maps each one.

Audit of the case creation code on `journey-templates` (commit 27ffef6) against `cairn-case-creation-use-cases.json` spec 0.3.0 (copied to `docs/cairn-case-creation-use-cases.json`), and what this change adds.

**Before** is the state at 27ffef6: **Met**, **Partial**, **Conflict** (the code did the opposite of the spec), or **Missing**. **Now**: **Done** means built and covered by an automated test. **Client** means the API exposes what's needed but the check can only be met in the app UI. **Open** means not done, with the reason.

Tests: `api/tests/test_case_creation.py` (database, as the cairnApp user) and `api/tests/test_case_creation_rules.py` (no database). Database invariants: `api/tests/test_data_security.py`. Criteria tagged [SAFETY] or [PRIVACY] are release blockers, and every one has a test.

## Summary

| Use case | Before | Now | Still open |
|---|---|---|---|
| UC-CASE-01 Start a new case | Conflict | Done | Free-text extraction is rule-based until a model does it |
| UC-CASE-02 Who you are to them | Partial | Done | POA copy and citation need legal review (OPEN-DECISION-03) |
| UC-CASE-03 What to call them | Missing | Done | |
| UC-CASE-04 When and where | Partial | Done | Outside US is out of scope (stubbed as specified) |
| UC-CASE-05 How it happened | Missing | Done | Signal lists need Trello card 26 |
| UC-CASE-06 Military service | Partial | Done | |
| UC-CASE-07 Will or estate plan | Partial | Done | No-will copy needs legal review (OPEN-DECISION-03) |
| UC-CASE-08 Already done | Missing | Done | |
| UC-CASE-09 Skip, unsure, change | Partial | Done | |
| UC-CASE-10 Pause and come back | Missing | Done, Open | Schedule `purge_inactive_drafts()` daily. Privacy policy update |
| UC-CASE-11 Review | Missing | Done, Client | Edit per line (client renders the returned action) |
| UC-CASE-12 Start the journey | Conflict | Done, Open | Reminder email sender and billing are not built |
| UC-CASE-13 First task | Missing | Done | |
| UC-CASE-14 Distress | Missing | Done, Open | Thresholds and human escalation come from card 26 |
| UC-CASE-15 Sensitive numbers | Missing | Done, Client | Mask in the UI immediately (client uses `masked_text`) |
| UC-CASE-16 Attorney referral | Partial | Done | Detection of triggers in chat is rule-based |
| UC-CASE-17 Not died yet | Missing | Done | Pre-need path is not built (OPEN-DECISION-05) |
| UC-CASE-18 Second case | Partial | Done | Subscription purchase is not built |

## What conflicted with the spec before this change

- **The trial started when the first case was created**, in a trigger on `cases` (registration D-03). DEC-01 moves it to the first **Start journey**. Migration 0010 drops the trigger and `cairn.start_journey` starts the trial in the same transaction that moves the first case from draft to active. `trial_started_at` is still written once and never reset. This supersedes the registration spec's UC-REG-08 criteria "trial_started_at is set only when the first case is created" and "copy.case_created_trial_start is shown when the first case is saved". `case_created_trial_start` stays in the registration copy file verbatim but is no longer shown.
- **Case creation required the deceased's legal first and last name, and accepted a date of birth.** Both are on `never_collect_at_case_creation`. `POST /v1/cases` now takes nothing about the person who died, the database refuses a `deceased` row while the case is a draft, and legal identity can only be added once the journey has started (`PATCH /v1/cases/{id}/deceased`, just in time).
- **The onboarding hand-off asked "what was their legal first name?"** It now says only that a case will start.
- **There was no draft.** Cases were `active` from creation. There was no `journey_started_at`, no pinned journey template, no `last_activity_at`, and no cleanup.

## Entity and field mapping

Existing tables were extended. Nothing was renamed or dropped (migration `0010_case_creation.sql`).

| Spec | Where it lives |
|---|---|
| case.status draft, active | `cases.status`. `draft` added and made the default. `paused` and `closed` kept |
| case.status read_only | Derived, not stored: an active case on a read-only account (`cairn.effective_case_status`, `CaseOut.status`). The spec says the account flag drives it |
| journey_template_key, journey_template_version | `cases.journey_template_key`, `cases.journey_template_version` (FK to `journey_templates.version`). Version is an integer like every other template version |
| journey_started_at | `cases.journey_started_at`, set only by `cairn.start_journey`, never changed after. `journey_started_on` (the day tasks count from) is now null on drafts |
| last_intake_step | `cases.last_intake_step`: the question the user is on |
| last_activity_at, draft_expires_at | `cases.last_activity_at` (server time only, set by a trigger). `draft_expires_at` is computed from `app_settings.draft_retention_days` |
| intake_answers | New `case_intake_answers`, one row per data_fields key with `answer_state` and `value`. `own_words` keeps the user's words for the review screen, never for circumstance |
| data_fields shapes | `cairn.intake_value_valid` checks every value's shape in the database. The API checks first (`FIELD_VALUE_TYPES`) |
| user_role | `case_intake_answers.user_role`. `case_members.relationship` is now nullable and not set for new cases |
| account_trial.* | `users.trial_*` (0008). Reminders: `trial_day_21`, `trial_day_27`, and new `trial_ends_soon` |
| account_trial.subscription_status | `users.status = 'subscribed'`, owner-only until billing exists |
| payment_method_on_file | There is no such column or field anywhere. Tested |
| journey_task.status todo | `case_tasks.status = 'not_started'` (existing value). `check_on_this` and `not_today` added |
| journey_selection | Template data: `content/journeys/journey-selection.json`, loaded into new `journey_templates` (immutable per version) |
| add-on tasks | Task templates in `content/tasks/us/`. Ten new templates, three version bumps |
| draft_cleanup_job | `cairn.purge_inactive_drafts()`, owner only |
| config | `app_settings` (database) and `Settings` (API). See below |

## Template keys and add-on rules (template data)

`content/journeys/journey-selection.json` holds the spec's `journey_selection` as data. The loader validates it against `content/schema/journey-selection.schema.json` and checks that every task it names has a template and every note and support resource exists. The API only evaluates it (`cairn_api/journey_selection.py`).

| Spec add | Template data |
|---|---|
| base paths | `paths`: `expected_loss`, `sudden_loss`, `sudden_loss_investigation`, `general` |
| va_burial_benefits_step | add-on `va_burial_benefits` adds `notify_va_if_veteran` (v2: one-line description, no records request) |
| Veterans Crisis Line (UC-CASE-06) | add-on `veterans_crisis_line` adds the support resource when veteran_status is yes |
| confirm_named_executor, find_the_will | add-ons `estate_plan_location_known`, `estate_plan_location_unknown` |
| no_will_explainer | add-on `no_will`, attorney flag, legal review |
| poa_authority_note | add-on `power_of_attorney` adds the note, attorney flag, legal review |
| certificate_may_be_pending_note | add-on on `base_path` in the sudden paths, attached to the certificate task |
| certificate_task_waiting_on_place_note | add-on when the place of death state is unknown and the death was in the US |
| change_task ssa_notify | add-on `funeral_home_reports_to_ssa` replaces `notify_social_security` with `confirm_funeral_home_reported_death` |
| UC-CASE-16 triggers | add-ons `death_outside_us`, `attorney_triggers` (task `talk_to_estate_attorney`), `unverified_state` (task `ask_state_vital_records_office`) |
| completed_items | `completed_items` and `check_on_this_when_unsure` |
| mvp_waypoints | `waypoints` and `task_waypoints` |

`verified_states` is empty: no state's content has counsel review yet, so every US place of death adds "Check with the state's vital records office". Add a state there after review.

## Per use case and acceptance criterion

### UC-CASE-01 Start a new case
- Creating a case sets draft and does not set or change trial_started_at: `test_uc01_new_case_is_a_draft_and_does_not_start_the_trial`, `test_data_security.py`.
- First message has an acknowledgment and at most one question: same test. The two intake modes are the only options.
- [PRIVACY] Free-text intake persists only data_fields keys: `test_uc01_own_words_read_back_and_only_data_fields_are_saved`, `test_uc01_confirmations_refuse_anything_outside_data_fields`, `test_unknown_intake_fields_are_rejected`. Free text is never stored at all. Proposals are read back and saved only on confirmation, each checked like a button answer, then checked again by the database.
- Built: `POST /v1/cases`, `POST .../intake/messages`, `POST .../intake/confirmations`, `POST .../intake/continue`.

### UC-CASE-02 Who you are to them
- Skipping stores skipped and uses family_member: `test_uc02_skipping_role_stores_skipped_and_defaults_to_family_member`, `test_uc02_skipped_user_role_uses_family_member`.
- POA note renders once per case, with citation: `test_uc02_power_of_attorney_note_renders_once_per_case_with_citation`. Tracked in `cases.shown_notices`, which can only hold that key.
- Professional fiduciary is offered a shorter pace with no explainers, regardless of voice: `test_uc02_professional_fiduciary_is_offered_a_shorter_pace`.
- The onboarding hand-off answer is passed through as `user_role`, so the question isn't asked twice: `test_uc02_role_from_onboarding_handoff_is_not_asked_again`.
- Open [LEGAL REVIEW REQUIRED]: the spec's own note says to replace the alperlaw.com citation with a primary source or uniform-act citation before launch.

### UC-CASE-03 What to call them
- display_name is never used as, or copied into, a legal name: `test_uc03_display_name_is_used_but_never_as_a_legal_name`. It lives only in `case_intake_answers`.
- Skipped uses "your loved one". In plain_practical, "the person who died" is offered: `test_uc03_skipped_name_uses_your_loved_one_or_the_person_who_died`.

### UC-CASE-04 When and where
- Certificate office uses place_of_death.state only: `test_uc04_certificate_office_uses_place_of_death_not_residence`, `test_uc04_certificate_office_uses_place_of_death_only`. The task view's `death_state` now comes from the place of death answer.
- Approximate dates accepted: `test_uc04_approximate_dates_are_accepted`, `test_date_of_death_accepts_approximate_dates_and_refuses_the_future`.
- residence_state is asked only after the user says the death was away from home: `test_uc04_residence_is_not_asked_unless_away_from_home`. That signal is kept in the client-held session, not stored.
- Place unknown adds the note to the certificate task: `test_uc04_place_unknown_adds_the_certificate_note`.
- Outside the US (out of scope): explained kindly, case kept, funeral home and attorney suggested, attorney task added: `test_uc04_outside_us_keeps_the_case_and_suggests_help`.

### UC-CASE-05 How it happened
- [PRIVACY] Only the enum is persisted. Free text is never stored or logged: `test_uc05_only_the_enum_is_stored_and_free_text_is_never_logged` (API, log capture, and a direct database write refused by `intake_value_valid`), `test_uc05_medical_cause_never_becomes_a_circumstance`, `test_data_security.py`.
- No follow-up after prefer_not_to_say, and no painful repeat: `test_uc05_confirm_without_repeating_and_no_follow_up_after_prefer_not_to_say`.
- Explains before asking, in the user's voice (voice_samples_uc_case_05): `test_uc05_explains_before_asking_in_every_voice_and_fiduciary_can_skip_it`.
- Volunteered cause: acknowledged simply, no follow-up, not stored, sensitivity raised for the session, suicide loss resources offered once: `test_uc05_volunteered_suicide_loss_is_acknowledged_and_resources_offered_once`, `test_uc05_suicide_loss_is_a_volunteered_cause_not_a_risk_to_the_user`.

### UC-CASE-06 Military service
- VA step renders with the VA.gov citation: `test_uc06_va_step_with_va_gov_citation_and_crisis_line`. Unknown adds the step without the crisis line: `test_uc06_unknown_adds_the_va_step_but_not_the_crisis_line`.
- No document request during case creation: `test_uc06_uc07_no_document_request_during_case_creation`. `notify_va_if_veteran` v2 drops "gather service records" from the summary.

### UC-CASE-07 Will or estate plan
- No document upload or request offered: `test_no_document_upload`, `test_uc06_uc07_no_document_request_during_case_creation`.
- No or unknown: reassurance, no_will_explainer with the attorney line, no inheritance rules in chat: `test_uc07_no_will_reassures_and_adds_the_explainer_with_attorney_flag`.
- Family disagreement: no side taken, attorney task added: `test_uc07_family_disagreement_adds_the_attorney_task_without_taking_sides`.

### UC-CASE-08 Already done
- Funeral home chosen and SSA not notified changes the SSA task to "Confirm the funeral home reported the death": `test_uc08_funeral_home_chosen_changes_the_ssa_task`, `test_uc08_funeral_home_reports_to_ssa`.
- Checked items mark tasks done. none_or_unsure makes the relevant tasks check_on_this: `test_uc08_none_or_unsure_marks_relevant_tasks_check_on_this`, `test_uc08_completed_items_mark_done_and_unsure_is_check_on_this`.

### UC-CASE-09 Skip, I'm not sure, change
- answer_state is answered, skipped, or unsure: `test_uc09_answer_states_and_skipped_questions_are_not_asked_again`, `test_every_answer_can_be_skipped_or_unsure_without_a_value`. Every question offers both: `test_every_question_offers_skip_and_not_sure_and_free_text`.
- A skipped question is not asked again: same test. Any answer row, whatever its state, takes the question out of the queue.
- Changing an answer recomputes the journey and states the change in one line: `test_uc09_changing_an_answer_says_what_changed_in_one_line`.
- On an active case, add-on tasks change without losing progress on unaffected tasks: `test_uc09_change_on_active_case_keeps_progress`, `test_late_veteran_answer_adds_va_task_without_losing_progress`. Rules unselect tasks (`case_tasks.selected`) rather than delete them, and a task in progress or done stays visible.

### UC-CASE-10 Pause and come back later
- Pausing never starts the free period. No draft reminder is sent: `test_uc10_pause_saves_says_28_days_and_never_starts_trial_or_reminders`. There is no draft reminder at all, so nothing can be sent without an opt-in.
- [PRIVACY] Idle drafts are fully deleted with answers and conversation text. Active and read-only cases are never touched: `test_uc10_cleanup_deletes_idle_drafts_only`, `test_data_security.py`.
- Any answer, edit, or open resets the 28 days: `test_uc10_any_answer_edit_or_open_resets_the_28_days`.
- The pause message says so: same pause test. Resume greets and says where they left off: `test_uc10_return_greets_and_says_where_they_left_off`.
- Open (operations): run `SELECT cairn.purge_inactive_drafts();` as the owner at least daily. Open (policy): add the 28-day draft deletion to the retention schedule in CAIRN-POL-PRIV-01 (`policy_updates_required`). [LEGAL REVIEW REQUIRED]

### UC-CASE-11 Review
- Own words shown where free text was given, except circumstance, which shows its label: `test_uc11_review_uses_own_words_except_circumstance`. Two groups, Edit on every line, "Does this look right?".

### UC-CASE-12 Start the journey
- trial_started_at set once, at the first Start journey, never by drafts or edits. trial_ends_at is exactly 28 days later: `test_uc12_preview_then_start_starts_trial_once_in_local_time`, `test_trial_starts_at_the_first_start_journey_not_at_case_creation`, `test_data_security.py`.
- [PRIVACY] No payment form, field, or SDK: `test_uc12_start_journey_never_asks_for_payment`, `test_no_payment_code_in_the_api`.
- Pre-button notice visible before Start is enabled: Start requires the notice's version, and a stale one is refused (409) without starting anything. Same test.
- Confirmation shows the end date in the user's time zone, and the trial_ends_soon reminder is scheduled 3 days before the end: same test.
- Subscription prompt only at or after trial_ends_at: `test_uc12_no_subscription_prompt_before_the_trial_ends`.
- Not yet keeps the draft: `test_uc12_not_yet_keeps_the_draft_and_the_trial_unstarted`.

### UC-CASE-13 First task
- Never auto-starts a task. Not today leaves no task in progress. Any other task opens without comment: `test_uc13_first_task_choice_never_auto_starts_and_not_today_leaves_none_in_progress`. The "why now" line comes from the new `why_now` template field.

### UC-CASE-14 Distress
- [SAFETY] risk_of_harm: first response has 988 and no task or question: `test_uc14_risk_of_harm_first_response_has_988_and_no_task_or_question`.
- [SAFETY] Veteran cases include the Veterans Crisis Line: `test_uc14_veteran_cases_include_the_veterans_crisis_line`.
- [SAFETY] acute_distress asks nothing until the user asks to continue, whether they type or tap: `test_uc14_acute_distress_asks_nothing_until_the_user_continues`.
- Overwhelm (phrases or repeated skips) stops the questions and offers a pause or one small thing: `test_uc14_overwhelm_stops_questions_and_offers_a_pause_or_one_small_thing`.
- Every voice converges on steady_care, no promises about crisis lines, nothing about distress stored: `test_uc14_steady_care_in_acute_distress_and_risk_of_harm`, `test_uc14_crisis_copy_makes_no_promises`, `test_uc14_distress_is_never_persisted`.
- How: the mode lives in `IntakeSession`, which the client keeps and sends back. The server never stores it (database/CLAUDE.md decision 7).
- Open: signal definitions, thresholds, long-gap detection, and the human escalation path come from Trello card 26, which isn't written. The phrase lists in `cairn_api/safety.py` are a broad placeholder.

### UC-CASE-15 Sensitive numbers
- [PRIVACY] Redacted server-side before any write, log line, or reply: `test_uc15_sensitive_numbers_are_redacted_before_storage_logs_and_reply`, `test_uc15_redaction_runs_during_request_parsing`, `test_uc15_every_free_text_request_field_is_redacted`. Free-text request fields use the `RedactedText` type, so handlers never see the raw value. There are no analytics events and no model calls yet. When they arrive, they only ever receive the redacted text. A log filter redacts again as defense in depth.
- [PRIVACY] Replies never contain the value or any part of it: same test.
- Unit tests: SSN with and without dashes, 13 to 19 digit cards with Luhn, labeled account numbers: `test_uc15_*` in the rules file.
- Client: mask the user's own bubble immediately. The response carries `masked_text` to replace it with.

### UC-CASE-16 Something Cairn should not guide alone
- Never blocks the journey. The attorney task carries the referral line: `test_uc16_attorney_trigger_never_blocks_the_journey`, `test_uc16_attorney_triggers_and_unverified_states`, `test_uc16_unverified_state_adds_the_state_office_task`.
- Triggers are recorded in `cases.attorney_triggers` (contested will, family disagreement, unsure of authority, property in more than one state). Death outside the US and unverified states come from the answers.

### UC-CASE-17 The death has not happened yet
- Start journey not offered and no trial start: `test_uc17_death_not_yet_offers_a_draft_and_never_starts_the_journey` (API, and `start_journey` refused in the database).
- "Come back later" keeps the draft and shows the pause copy. It deletes nothing. See decisions to confirm.

### UC-CASE-18 Second case
- trial_started_at and trial_ends_at unchanged by a second Start journey. The existing end date is shown: `test_uc18_second_case_acknowledges_another_loss_and_keeps_the_trial`.
- Read-only accounts can create and edit a draft but can't start without a subscription. The subscription prompt shows only there: `test_uc18_read_only_account_can_draft_but_not_start_without_a_subscription`, `test_data_security.py`.

## Global rules

| Rule | How it's met |
|---|---|
| Voice, steady_care override | `IntakeTurnResponse.voice`. The circumstance question uses the voice samples |
| No timers, no required order | Any field can be answered at any time (`PUT .../intake/answers/{field}`). Nothing times out except the 28-day draft deletion the spec asks for |
| Skip for now and I'm not sure on every question | `Question.skip`, `Question.not_sure`. Tested for every field |
| Never ask the same question twice in one session | A question with any answer row is never suggested again |
| Missing answers never block the journey | Selection always returns a path. Tested with no answers at all |
| Free text: extract, read back, confirm | `intake/messages` then `intake/confirmations` |
| Comfort-first turn contract | Every turn has an acknowledgment or statements, at most one question, and exactly one `next_step` |
| Large tap targets, free text, no color alone | Every option has a text label. Preview statuses have text labels (`status_label`). Rendering is client |
| Read this to me | Every case creation response carries `read_aloud` |
| Legal: citations next to statements, attorney line | `Note.source_urls`, `Note.attorney_line`, task citations and `attorney_line` |

## Configuration (open_decisions and config)

| Item | Where | Default |
|---|---|---|
| OPEN-DECISION-01 estate plan as add-on or trailhead | `CAIRN_ESTATE_PLAN_MODE` | `add_on`. Only this is built. Startup refuses anything else |
| OPEN-DECISION-02 days before the end for the reminder | `app_settings.trial_reminder_days_before` | 3 |
| OPEN-DECISION-03 POA and no-will wording | `content/case-creation-copy.json`, flagged legal review | spec draft copy |
| OPEN-DECISION-05 pre-need path | `CAIRN_PRE_NEED_PATH` | `not_built`. Startup refuses anything else |
| draft_retention_days (DEC-07) | `app_settings.draft_retention_days` | 28 |
| trial_length_days | `trial_is_28_days` check on `users` (0008) | 28. A confirmed decision (DEC-03), so it stays a constraint |
| overwhelm skip threshold | `CAIRN_OVERWHELM_SKIP_THRESHOLD` | 3, pending card 26 |

`app_settings` rows change with an UPDATE by the owner role, with no migration. The pause and draft notices say "28 days" verbatim from the spec, so a different retention period needs new copy too.

## Out of scope for MVP

| Item | Fallback |
|---|---|
| Document upload | No endpoint, no file field. Tested |
| Multi-user case access | Unchanged: one owner per case (`mvp_owner_only`) |
| Deaths outside the US | Explained, case kept, funeral home and attorney suggested (UC-CASE-04) |
| Journey beyond week 4 | Template weeks are limited to 1 to 4 by schema and database check |
| Pre-need planning | UC-CASE-17 draft only. Config refuses any other value |

## Decisions to confirm with the product owner

1. **Three trial reminders.** Registration UC-REG-08 asks for reminders on day 21 and day 27. This spec asks for one at `trial_ends_at` minus 3 days (day 25). All three are scheduled so both specs' criteria hold. That is three messages in one week to someone who is grieving. Should `trial_ends_soon` replace one or both of the registration reminders?
2. **Registration trial copy.** `trial_summary` (registration spec, verbatim) says the 28 days "begin when you start your first case". Under DEC-01 they begin at Start journey. A new registration spec version could say "when you start your first journey". Resolved 2026-09-25: the UC-REG-08 change says "first journey". See `account-lifecycle-gap-audit.md`.
3. **"Come back later" in UC-CASE-17** keeps the draft (with the pause copy and its 28-day notice) rather than deleting it. Deleting on that button seemed too destructive to assume.
4. **POA note at hand-off and in the case.** Onboarding's case hand-off still shows its own POA note (`messages.POA_ENDS_AT_DEATH`). A case then shows the spec's note once. The two have different wording, so legal review has two texts to approve. Consider using only the spec's.
5. **Case status read_only is derived, not stored**, so a subscription restores write access without touching cases. Say if a stored value is wanted for reporting.
6. **The `journeys/` package** (the richer step library, modules, and state overlays from 27ffef6) is not wired into the database yet. It uses a different fact vocabulary (for example `death_circumstance: sudden_unexpected`) from this spec's data_fields. This change extended the loaded `content/` templates instead. Bringing `journeys/` in needs a loader and a mapping from these data_fields to its case facts.
7. **Verified states.** `verified_states` is empty, so every US case gets "Check with the state's vital records office". Add states as counsel reviews them.

## Follow-ups outside this change

- Schedule `cairn.purge_inactive_drafts()` daily (owner role), next to the other jobs in `api/README.md`.
- Add the draft deletion period to CAIRN-POL-PRIV-01. [LEGAL REVIEW REQUIRED]
- Replace the rule-based extraction and distress detection with the model and the card 26 plan. Model input must use the redacted text only.
- Counsel review of the new task templates and `journey-selection.json` before a release tag. The loader's release gate fails until `counsel_reviewed_at` and `last_verified_on` are set.
