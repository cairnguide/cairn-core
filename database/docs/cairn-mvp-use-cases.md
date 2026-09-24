# Cairn MVP Use Case Scenarios

Written for the four MVP personas: surviving spouse, adult child acting as executor, power of attorney, and professional fiduciary. Each scenario follows the acknowledge-first, one-question-at-a-time standard from the product design, and each ends with the user knowing their next action.

Schema references point to `db/migrations/0002_core_tables.sql`, `0003_journey_tables.sql`, and the functions in `0005_functions.sql`.

## Registration

> Superseded. Registration and onboarding now follow `cairn-registration-use-cases.json` (UC-REG-01 to UC-REG-14 and UC-ACCT-01). The persona-specific parts below (the POA note, fiduciary language, new or existing case) now happen at the hand-off to case creation. They are kept for history.

**UC-1: Surviving spouse creates an account**
As a surviving spouse, when I open Cairn for the first time after my husband's death, I will provide my name, email, and a password (or sign in with a passkey), confirm my email, and accept the terms of use and privacy policy, resulting in an authenticated account and a welcome message that acknowledges my loss before asking anything else.

**UC-2: Adult child acting as executor creates an account**
As an adult child who has been named executor, when I sign up to organize my mother's affairs, I will register with my own name and email, confirm my email, and accept the terms of use and privacy policy, resulting in an authenticated account and a prompt asking, one question at a time, whether I am starting a new case or picking up one already in progress.

**UC-3: Power of attorney creates an account**
As a person who held power of attorney for someone who has since died, when I register for Cairn, I will provide my name and email, confirm my email, and accept the terms, resulting in an authenticated account and a plain-language note that POA authority generally ends at death, with a next step to confirm my current role (such as executor or next of kin) before I continue.

**UC-4: Professional fiduciary creates an account**
As a professional fiduciary, when I sign up to manage a client's case, I will register with my professional name and email, confirm my email, and accept the terms, resulting in an authenticated account and a prompt to identify myself as a fiduciary so later screens use fiduciary-appropriate language rather than assuming a family relationship.

## Creation of a case / addition of the deceased

**UC-5: Surviving spouse starts a case and records identifying information**
As a surviving spouse, when I am ready to begin, I will be asked one question at a time for my spouse's legal name, date of birth, and state of legal residence, resulting in a new case created with the deceased's identity on file and a clear next step to record the details of the death.

**UC-6: Adult child records the death event**
As an adult child acting as executor, when I have my mother's identifying information entered, I will be asked for the date of death, the state and county where she died, and the type of place (hospital, hospice, home, or other facility), resulting in a completed death event record and a next step to answer a few short questions about veteran status and whether a will exists.

**UC-7: Power of attorney records veteran and will status**
As a person who held power of attorney, when I am asked about the deceased's veteran status and whether a will or trust exists, I will answer "yes," "no," or "unknown" to each question, one at a time, with the option to skip and come back later, resulting in that information saved to the case and an acknowledgment that "unknown" is a fine answer for now.

**UC-8: Professional fiduciary sets up a case for a client**
As a professional fiduciary, when I open a new case for a client's estate, I will enter the deceased's identifying information, the death event details, and the estate facts I already have on file, resulting in a complete case record and a note that any answer I don't have yet can be filled in later without blocking the journey from starting.

## Getting started on their journey

**UC-9: Surviving spouse sees her first-month journey**
As a surviving spouse, when I finish entering my husband's information, I will be shown a four-week journey with the first week's tasks in front, starting with ordering death certificates and arranging the funeral, resulting in a clear view of what to do first and a single next action rather than a long list to sort through.

**UC-10: Adult child completes the death certificate task**
As an adult child acting as executor, when I am ready to order death certificates, I will open the "order death certificates" task and follow the guidance for my mother's state, including a citation to the state's vital records office, resulting in the task marked as ordered with the number of copies and expected arrival date saved, and the next task in the queue surfaced.

**UC-11: Power of attorney notifies a bank**
As a person handling the estate, when I reach the task to notify my father's bank, I will open the task, see the plain-language explanation of what to say and what to bring, and mark the task as done once I've called, resulting in that institution recorded as notified and the journey automatically moving to the next task.

**UC-12: Any user steps back from tasks when overwhelmed**
As a user in the middle of my four-week journey, when I feel too overwhelmed to continue with tasks right now, I will choose an option to pause the journey rather than close the app, resulting in Cairn stepping back from task mode, offering a calmer check-in instead, and holding my progress exactly where I left it until I'm ready to resume.

**UC-13: Professional fiduciary tracks status across a case**
As a professional fiduciary, when I return to Cairn after a few days away, I will open the case and see a single view of what's done, what's in progress, and what's next across certificates, agencies, and financial institutions, resulting in a clear, current status without having to reconstruct it from notes or memory.

## Notes

- UC-3 and UC-7 both touch the POA-after-death question flagged for legal review in `CLAUDE.md`. They are written as scenarios the product should handle gracefully, not as confirmation that the legal question is settled.
- UC-12 intentionally records only that the journey is paused, with no reason or emotional state stored, consistent with the design note in the data model about not persisting inferences about a user's mental state.
- These align with the roles and relationship values already in the MVP schema (`spouse`, `child`, `power_of_attorney`, `fiduciary`) and with the four-week journey structure from the data model documents.

---

## Acceptance criteria: UC-5 through UC-8 (case and deceased creation)

These expand the four case-creation scenarios into criteria Claude Code can build and test against. They assume the schema and functions already in this repository (`cairn.cases`, `cairn.case_members`, `cairn.deceased`, `cairn.register_user`, `cairn.generate_case_tasks`). Note: `deceased` and `death_events` were originally separate tables and are now merged into a single `deceased` table, since they are one-to-one and always used together. UC-6 below reflects that merge.

### UC-5: Surviving spouse starts a case and records identifying information

**Preconditions**
- The user has an authenticated session and a row in `cairn.users` (created through `cairn.register_user` at sign-up).
- The application has called `SET search_path = cairn, pg_temp` and `SELECT set_config('app.user_id', '<uuid>', true)` for this transaction.

**Flow**
1. Application creates the case: `INSERT INTO cairn.cases (created_by) VALUES (:user_id) RETURNING id`.
2. Application immediately inserts the first owner membership: `INSERT INTO cairn.case_members (case_id, user_id, relationship, role, status) VALUES (:case_id, :user_id, 'spouse', 'owner', 'active')`.
3. UI asks, one field at a time: legal first name, legal middle name (optional), legal last name, date of birth (optional), state of legal residence.
4. Application inserts the deceased row: `INSERT INTO cairn.deceased (case_id, legal_first_name, legal_middle_name, legal_last_name, date_of_birth, domicile_state) VALUES (...)`.
5. UI advances to the death event questions (UC-6).

**Acceptance criteria**
- A case cannot be created without `created_by` set to the authenticated user (enforced by the `cases_insert` row-level security policy).
- The case-creation step and the first-owner membership insert happen in the same database transaction, so a case is never left without an owner.
- A second membership row cannot be inserted for this case until the product supports invitations. Row-level security policy `case_members_insert_first_owner` will reject any attempt while the case already has a member.
- `deceased.case_id` is unique. A second `INSERT` against the same case fails with a unique-violation, so the UI must use `UPDATE` for edits after the first save.
- `legal_first_name` and `legal_last_name` are required (`NOT NULL`, 1 to 100 characters). Empty strings are rejected by the length check.
- `legal_middle_name` is optional and may be null.
- `date_of_birth`, if provided, cannot be in the future (`date_of_birth <= current_date`). If the UI allows skipping, store null and let the user return later.
- `domicile_state`, if provided, must be a two-letter uppercase state code (`cairn.state_code` domain). Validate client-side before submit and surface the database error in plain language if it slips through.
- On success, the response includes the new `case_id` so the client can proceed to UC-6 without a second round trip.
- Every insert in this flow should be followed by an `audit_events` row (`case_created`, `deceased_added`) written as the acting user. Do this as a second statement in the same transaction, not a separate request.
- No field in this flow is logged in plaintext application logs. Treat `legal_first_name`, `legal_last_name`, and `date_of_birth` as sensitive in logging middleware.

**Out of scope for UC-5:** SSN entry (`ssn_last4` is not asked at this step. If collected, do so as a clearly optional, separately labeled field later in the flow, never bundled with the name fields).

### UC-6: Adult child records the death event

**Preconditions**
- `cairn.deceased` row already exists for this case (from UC-5 or equivalent). Its death-event fields (`date_of_death`, `place_type`, `death_state`, `county`, `facility_name`) are still null at this point.
- The acting user is an active member of the case with role `owner` or `co_executor` (checked by row-level security. MVP has only `owner`).

**Flow**
1. UI asks, one question at a time: date of death, place type (hospital, hospice, home, facility, other), state where death occurred, then optionally county and facility name.
2. Application updates the existing deceased row: `UPDATE cairn.deceased SET date_of_death = :dod, place_type = :pt, death_state = :st, county = :c, facility_name = :f WHERE case_id = :case_id`.
3. On success, UI advances to UC-7 (veteran and will status).

**Acceptance criteria**
- `deceased` and `death_events` were originally separate tables and are now one table, so this step is an `UPDATE` on the row created in UC-5, not an `INSERT` into a second table. Scope the `UPDATE` with `WHERE case_id = :case_id` (or `WHERE id = :deceased_id`) so it cannot touch another case's row, the same way UC-7 scopes its update. Row-level security (`deceased_update`) is the enforced backstop.
- `date_of_death` is required for this step to be considered complete and cannot be in the future (`date_of_death <= current_date`). The column itself is nullable at the database level, since a deceased row can exist before this step runs, so "required" here is an application-level rule, not a `NOT NULL` constraint.
- `date_of_death` cannot be before `date_of_birth`. This is enforced by the database directly (`death_not_before_birth` check constraint on `deceased`), now that both dates live on the same row. A violation raises a `check_violation` that the application should translate into a plain-language message asking the user to recheck the two dates, rather than a generic error.
- `place_type` must be one of `hospital`, `hospice`, `home`, `facility`, `other`.
- `death_state` is required for this step to be considered complete and must be a two-letter state code. This is the field that determines which vital records office issues the certificate, so the UI should make clear this is the state of death, not the state of residence, if they differ. It is also the field `cairn.generate_case_tasks` checks before generating any tasks, so the journey (UC-9) will not populate until this is set.
- `county` and `facility_name` are optional free text, each under the column's length limit. Treat both as sensitive in logs.
- After a successful update, write an `audit_events` row (`death_event_added`) in the same transaction. This is still a distinct audit action from `deceased_added` (UC-5) even though both now write to the same table, so the audit trail reads clearly regardless of the underlying schema.
- This step must succeed only if the acting user has `owner` role on the case (`deceased_update` policy, the same policy UC-5 and UC-7 use). Attempting this as a non-member or a `viewer` returns zero rows affected, and the application should translate that into a permission-denied response rather than a silent no-op.

**Out of scope for UC-6:** cause of death is not collected. Do not add a field for it without a product decision, since it is not required anywhere in the current journey logic.

### UC-7: Power of attorney records veteran and will status

**Preconditions**
- `cairn.deceased` row exists.
- The acting user has `owner` role on the case.

**Flow**
1. UI asks two questions, one at a time, each with three options (yes, no, unknown) and a visible "skip for now" affordance that stores `unknown`.
2. Application updates the existing deceased row: `UPDATE cairn.deceased SET veteran_status = :v, has_will = :w WHERE case_id = :case_id`.
3. On success, UI advances to a summary screen, then to UC-9 (journey generation).

**Acceptance criteria**
- Both `veteran_status` and `has_will` default to `unknown` at row creation (UC-5), so skipping this step entirely still leaves the case in a valid, journey-eligible state.
- Only the values `yes`, `no`, `unknown` are accepted (`cairn.tri_state` domain). Any other value is rejected by the database. The UI should never be able to send anything else, so treat a rejection here as a client bug, not a user error to display.
- This step is an `UPDATE`, not an `INSERT`, and must be scoped with `WHERE case_id = :case_id` (or `WHERE id = :deceased_id`) so it cannot affect another case's row. Row-level security (`deceased_update`) is the enforced backstop, but the query should still be scoped correctly for clarity and for efficient index use.
- "Skip for now" must write `unknown`, not leave the field untouched with an ambiguous client-side state, so the server is always the source of truth for what has and hasn't been answered.
- Changing an answer later (for example, a user learns after the fact that the deceased was a veteran) is the same `UPDATE` statement. There is no history kept of prior answers to this field at MVP. If an audit trail of changes to veteran or will status is wanted, that is a new requirement, not covered by the current `audit_events` design, which logs the action and not the before and after values.
- After a successful update, write an `audit_events` row (`deceased_estate_flags_updated`).
- A `has_will` answer of `yes` should prompt (in the UI copy, not the database) a note that will location details can be added later. No additional table exists for this at MVP, since `ESTATE_INFO` is deferred per the data model.

**Out of scope for UC-7:** DD-214 upload, VA file number, will location text, and probate details are all deferred fields belonging to the full model's `VETERAN_INFO` and `ESTATE_INFO` tables, not the MVP schema.

### UC-8: Professional fiduciary sets up a case for a client

**Preconditions**
- The fiduciary has an authenticated account and, per UC-4, is identified as a fiduciary at the account level (this flag lives in the application layer or `users`, is not yet a database field, and should be confirmed with the product owner before Claude Code adds one).

**Flow**
This scenario composes UC-5, UC-6, and UC-7 into a single continuous session rather than three separate visits, since a fiduciary often has more complete information up front.

1. Application creates the case and first owner membership as in UC-5, with `relationship = 'fiduciary'`.
2. UI presents all fields from UC-5, UC-6, and UC-7 in one flow, but each question is still asked one at a time per the product's conversational standard. "All at once" describes the session, not the UI pacing.
3. Any field the fiduciary does not have on hand can be left as its default (`unknown` for the two tri-state fields, null for optional identity and death-event fields) and completed later.
4. On completing the required fields, the journey is generated (UC-9).

**Acceptance criteria**
- `relationship = 'fiduciary'` must be an accepted value in `case_members.relationship`. Confirm this against the current check constraint in `db/migrations/0002_core_tables.sql`. If `fiduciary` is not yet in the allowed list there, add it in a new migration rather than editing the applied one.
- The minimum fields required before `generate_case_tasks` can run meaningfully are: `deceased.legal_first_name`, `deceased.legal_last_name`, `deceased.date_of_death`, `deceased.death_state`. All other fields (domicile state, veteran status, will status, county, facility name) are optional and default to values that still let jurisdiction matching run, though with a smaller matched task set until they're filled in.
- The flow must allow saving partial progress at any point. Every `INSERT` and `UPDATE` in this composed flow is independently valid per the criteria in UC-5, UC-6, and UC-7. Nothing about combining them into one session changes the underlying constraints.
- The UI must not silently apply a default for a required field (name, date of death, death state) without the fiduciary explicitly submitting it. "Unknown" is a valid explicit answer for only `veteran_status` and `has_will`.
- Because a fiduciary may manage multiple cases, the case list view (not part of this use case's acceptance criteria, but a dependency of it) should let them distinguish cases by the deceased's name and case creation date. Confirm this is tracked as a separate ticket rather than built implicitly here.
- After the composed flow completes, `audit_events` should show the same distinct events as the separate flows (`case_created`, `deceased_added`, `death_event_added`, `deceased_estate_flags_updated`), not one combined event, so the audit trail reads consistently regardless of which persona created the case.

### Cross-cutting acceptance criteria for UC-5 through UC-8

- All four flows run under the `cairn_app` role with row-level security enabled. None should require elevated privileges.
- None of the flows write directly to `cairn.users`. Identity is established at registration (`cairn.register_user`), and these flows only reference the existing `user_id`.
- No flow should call `cairn.generate_case_tasks` until at minimum the fields listed in UC-8's second bullet are present. Calling it earlier is not an error, since it simply returns fewer or zero matched tasks, but the UI should wait until UC-7 or UC-8's flow completes before showing the journey, so the first impression is a populated one.
- Every flow described here should have a corresponding case in `db/tests/verify.sql` or a new test file, following the existing pattern (owner setup, act as `cairn_app` with `app.user_id` set, assert both success and permission-denied paths).
