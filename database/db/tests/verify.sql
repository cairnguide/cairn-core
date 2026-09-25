-- Security and behavior checks. Run as the owner role against a scratch database.
-- Everything happens inside one transaction that is rolled back at the end.
\set ON_ERROR_STOP on
\set QUIET on
BEGIN;

-- Test helpers (temporary, live only in this session).
CREATE FUNCTION pg_temp.expect_fail(q text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  BEGIN
    EXECUTE q;
  EXCEPTION WHEN OTHERS THEN
    RETURN;
  END;
  RAISE EXCEPTION 'EXPECTED FAILURE BUT SUCCEEDED: %', q;
END $$;

CREATE FUNCTION pg_temp.expect_count(q text, want bigint) RETURNS void LANGUAGE plpgsql AS $$
DECLARE got bigint;
BEGIN
  EXECUTE 'SELECT count(*) FROM (' || q || ') s' INTO got;
  IF got IS DISTINCT FROM want THEN
    RAISE EXCEPTION 'COUNT MISMATCH (want %, got %): %', want, got, q;
  END IF;
END $$;

CREATE FUNCTION pg_temp.expect_true(label text, cond boolean) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  IF cond IS NOT TRUE THEN RAISE EXCEPTION 'ASSERTION FAILED: %', label; END IF;
END $$;

-- Walks the current user through onboarding with the real functions (0008).
CREATE FUNCTION pg_temp.onboard() RETURNS void LANGUAGE plpgsql AS $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['privacy_terms', 'trial_terms', 'ai_notice'] LOOP
    INSERT INTO cairn.consents (user_id, purpose, policy_version, auth_provider, client)
      VALUES (cairn.current_user_id(), t, 'test', 'email', 'verify/1');
    PERFORM cairn.advance_onboarding(CASE t WHEN 'privacy_terms' THEN 'privacy_terms_accepted'
      WHEN 'trial_terms' THEN 'trial_terms_accepted' ELSE 'ai_notice_accepted' END);
  END LOOP;
  UPDATE cairn.users SET preferred_name = 'Tester' WHERE id = cairn.current_user_id();
  PERFORM cairn.advance_onboarding('preferred_name_saved');
  PERFORM cairn.advance_onboarding('complete');
END $$;

-- ------------------------------------------------------------ owner setup: templates
INSERT INTO cairn.task_templates
  (task_key, version, title, plain_summary, journey_week, due_offset_days, jurisdiction,
   applies_when, content_hash, git_release)
VALUES
  ('t_always',  1, 'Always',   'x', 1, 3,  'US', '{"always": true}',                    repeat('a', 64), 'test'),
  ('t_vet',     1, 'Veteran',  'x', 3, 17, 'US', '{"veteran_status": ["yes","unknown"]}', repeat('b', 64), 'test'),
  ('t_nh',      1, 'NH only', 'x', 2, 10, 'NH', '{"always": true}',                    repeat('c', 64), 'test'),
  ('t_ca',      1, 'CA only', 'x', 2, 10, 'CA', '{"always": true}',                    repeat('d', 64), 'test'),
  ('t_nowill',  1, 'No will',  'x', 4, 25, 'US', '{"has_will": ["no"], "domicile_state": ["NH"]}', repeat('e', 64), 'test'),
  ('t_old',     1, 'Old ver',  'x', 1, 3,  'US', '{"always": true}',                    repeat('f', 64), 'test'),
  ('t_old',     2, 'New ver',  'x', 1, 3,  'US', '{"always": true}',                    repeat('1', 64), 'test');
UPDATE cairn.task_templates SET active = false WHERE task_key = 't_old' AND version = 1;
-- Isolate from any content loaded earlier by the loader test.
UPDATE cairn.task_templates SET active = false WHERE git_release <> 'test';
INSERT INTO cairn.template_citations (template_id, authority_name, url, jurisdiction)
SELECT id, 'Test Authority', 'https://example.test/a', 'US' FROM cairn.task_templates WHERE task_key = 't_always';
-- Journey selection rules (0010). start_journey only accepts an active version with the named path.
INSERT INTO cairn.journey_templates (version, definition, content_hash, git_release)
VALUES (9001, '{"paths": {"general": {"tasks": []}}}', repeat('7', 64), 'test');

-- ------------------------------------------------------------ template_applies unit checks
SELECT pg_temp.expect_true('always applies', cairn.template_applies('{"always": true}', 'no','no','NH','NH'));
SELECT pg_temp.expect_true('veteran yes matches', cairn.template_applies('{"veteran_status":["yes"]}', 'yes','no','NH','NH'));
SELECT pg_temp.expect_true('veteran no does not match', NOT cairn.template_applies('{"veteran_status":["yes"]}', 'no','no','NH','NH'));
SELECT pg_temp.expect_true('AND across keys', NOT cairn.template_applies('{"veteran_status":["yes"],"has_will":["no"]}', 'yes','yes','NH','NH'));
SELECT pg_temp.expect_true('null value is false', NOT cairn.template_applies('{"has_will":["no"]}', 'yes',NULL,'NH','NH'));

-- ------------------------------------------------------------ app flow as cairn_app
SET LOCAL ROLE cairn_app;
SET LOCAL search_path = cairn, pg_temp;

-- Sign up (no user id known yet).
SELECT cairn.register_user('sub-alice', 'alice@example.test', 'Alice', 'Anders') AS alice \gset
SELECT cairn.register_user('sub-bob',   'bob@example.test',   'Bob',   'Baker')  AS bob   \gset
SELECT pg_temp.expect_true('resolve_user works', cairn.resolve_user('sub-alice') = :'alice'::uuid);
SELECT pg_temp.expect_true('register_user is idempotent',
  cairn.register_user('sub-alice', 'alice@example.test', 'Alice', 'Anders') = :'alice'::uuid);

-- The app cannot write users directly or read audit rows.
SELECT pg_temp.expect_fail($q$INSERT INTO cairn.users (idp_subject,email,first_name,last_name) VALUES ('x','x@example.test','X','X')$q$);
SELECT pg_temp.expect_fail($q$SELECT * FROM cairn.audit_events$q$);
SELECT pg_temp.expect_fail($q$SELECT * FROM cairn.schema_migrations$q$);
SELECT pg_temp.expect_fail($q$SELECT cairn.purge_expired_cases()$q$);

-- No user set: everything is invisible.
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.users', 0);

-- ------------------------------------------------------------ onboarding (0008)
SELECT set_config('app.user_id', :'alice', true);
SELECT pg_temp.expect_true('new account is pending onboarding',
  (SELECT status = 'pending_onboarding' AND onboarding_step = 'account_created' AND trial_started_at IS NULL
          AND voice = 'steady_direct'
   FROM cairn.users WHERE id = :'alice'::uuid));
-- No case until onboarding is finished.
SELECT pg_temp.expect_fail(format($q$INSERT INTO cairn.cases (created_by) VALUES (%L)$q$, :'alice'));
-- Steps go in order, and acknowledgment steps need their consent row. There is no age step.
SELECT pg_temp.expect_fail($q$SELECT cairn.advance_onboarding('age_confirmed')$q$);
SELECT pg_temp.expect_fail($q$SELECT cairn.advance_onboarding('privacy_terms_accepted')$q$);
SELECT pg_temp.expect_fail($q$SELECT cairn.advance_onboarding('trial_terms_accepted')$q$);
SELECT pg_temp.expect_fail($q$SELECT cairn.advance_onboarding('bogus')$q$);
INSERT INTO cairn.consents (user_id, purpose, policy_version) VALUES (:'alice'::uuid, 'privacy_terms', 'test');
SELECT pg_temp.expect_true('first step after account creation is privacy_terms',
  cairn.advance_onboarding('privacy_terms_accepted') = 'privacy_terms_accepted');
SELECT pg_temp.expect_true('advance is idempotent',
  cairn.advance_onboarding('privacy_terms_accepted') = 'privacy_terms_accepted');
SELECT pg_temp.expect_fail($q$SELECT cairn.advance_onboarding('ai_notice_accepted')$q$);
-- The app cannot set onboarding, status, or trial columns directly.
SELECT pg_temp.expect_fail($q$UPDATE cairn.users SET onboarding_step = 'complete'$q$);
SELECT pg_temp.expect_fail($q$UPDATE cairn.users SET status = 'subscribed'$q$);
SELECT pg_temp.expect_fail($q$UPDATE cairn.users SET trial_started_at = NULL$q$);
-- Voice (0009). The app sets its own voice, only to a voice in voices/manifest.yaml.
UPDATE cairn.users SET voice = 'brisk_businesslike' WHERE id = :'alice'::uuid;
SELECT pg_temp.expect_true('app can set its own voice',
  (SELECT voice = 'brisk_businesslike' FROM cairn.users WHERE id = :'alice'::uuid));
SELECT pg_temp.expect_fail($q$UPDATE cairn.users SET voice = 'gentle'$q$);
SELECT pg_temp.expect_fail($q$UPDATE cairn.users SET voice = NULL$q$);
SELECT pg_temp.expect_fail($q$SELECT personality FROM cairn.users$q$);
UPDATE cairn.users SET voice = 'steady_direct' WHERE id = :'alice'::uuid;
-- Owner-only jobs are not callable by the app.
SELECT pg_temp.expect_fail($q$SELECT cairn.expire_trials()$q$);
SELECT pg_temp.expect_fail($q$SELECT * FROM cairn.claim_due_trial_reminders(10)$q$);
SELECT pg_temp.expect_fail($q$SELECT cairn.purge_stale_accounts('1 day', NULL)$q$);
SELECT pg_temp.expect_fail($q$SELECT * FROM cairn.identity_deletion_requests$q$);
-- Finish onboarding from the start. Reset first so the helper runs every step.
RESET ROLE;
UPDATE cairn.users SET onboarding_step = 'account_created' WHERE id = :'alice'::uuid;
SET LOCAL ROLE cairn_app;
SELECT pg_temp.onboard();
SELECT pg_temp.expect_true('onboarding complete, no trial yet',
  (SELECT status = 'active_no_case' AND onboarding_step = 'complete' AND trial_started_at IS NULL
          AND name_prefill IS NULL FROM cairn.users WHERE id = :'alice'::uuid));
-- Consents are append-only for the app.
SELECT pg_temp.expect_fail($q$UPDATE cairn.consents SET policy_version = 'x'$q$);
SELECT pg_temp.expect_fail($q$DELETE FROM cairn.consents$q$);
SELECT set_config('app.user_id', :'bob', true);
SELECT pg_temp.onboard();

-- Alice creates a case. It starts as a draft (0010) and does not start the trial (DEC-01).
SELECT set_config('app.user_id', :'alice', true);
INSERT INTO cairn.cases (created_by) VALUES (:'alice'::uuid) RETURNING id AS case_a \gset
INSERT INTO cairn.case_members (case_id, user_id, relationship, role)
  VALUES (:'case_a'::uuid, :'alice'::uuid, 'spouse', 'owner');
SELECT pg_temp.expect_true('a new case is a draft with no journey',
  (SELECT status = 'draft' AND journey_started_at IS NULL AND journey_started_on IS NULL
   FROM cairn.cases WHERE id = :'case_a'::uuid));
SELECT pg_temp.expect_true('a draft does not start the trial',
  (SELECT trial_started_at IS NULL FROM cairn.users WHERE id = :'alice'::uuid));
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.trial_reminders', 0);

-- ------------------------------------------------------------ case creation (0010)
-- Only cairn.start_journey moves a case out of draft. The app can't write status or journey fields.
SELECT pg_temp.expect_fail(format($q$UPDATE cairn.cases SET status = 'active' WHERE id = %L$q$, :'case_a'));
SELECT pg_temp.expect_fail(format($q$UPDATE cairn.cases SET journey_started_at = now() WHERE id = %L$q$, :'case_a'));
SELECT pg_temp.expect_fail(format($q$INSERT INTO cairn.cases (created_by, status) VALUES (%L, 'active')$q$, :'alice'));
SELECT pg_temp.expect_fail($q$UPDATE cairn.app_settings SET value = '1'$q$);
SELECT pg_temp.expect_fail($q$SELECT cairn.purge_inactive_drafts()$q$);
-- Legal identity is never collected on a draft (never_collect_at_case_creation).
SELECT pg_temp.expect_fail(format($q$INSERT INTO cairn.deceased (case_id, legal_first_name, legal_last_name) VALUES (%L,'E','V')$q$, :'case_a'));
-- Intake answers: only the spec's fields and shapes. Nothing else, and no free text for circumstance.
INSERT INTO cairn.case_intake_answers (case_id, field_key, answer_state, value, own_words) VALUES
  (:'case_a'::uuid, 'display_name', 'answered', '"Dan"', 'Dan'),
  (:'case_a'::uuid, 'circumstance', 'answered', '"sudden_natural"', NULL),
  (:'case_a'::uuid, 'veteran_status', 'skipped', NULL, NULL),
  (:'case_a'::uuid, 'date_of_death', 'answered', '{"precision": "this_week", "date": null}', NULL),
  (:'case_a'::uuid, 'place_of_death', 'answered', '{"state": "NH", "county_or_city": null, "outside_us": false}', NULL),
  (:'case_a'::uuid, 'completed_items', 'answered', '["funeral_provider_chosen", "bank_notified"]', NULL);
SELECT pg_temp.expect_fail(format($q$INSERT INTO cairn.case_intake_answers (case_id, field_key, answer_state, value) VALUES (%L, 'cause_of_death', 'answered', '"x"')$q$, :'case_a'));
SELECT pg_temp.expect_fail(format($q$INSERT INTO cairn.case_intake_answers (case_id, field_key, answer_state, value) VALUES (%L, 'user_role', 'answered', '"cousin"')$q$, :'case_a'));
SELECT pg_temp.expect_fail(format($q$UPDATE cairn.case_intake_answers SET value = '"he had cancer"' WHERE case_id = %L AND field_key = 'circumstance'$q$, :'case_a'));
SELECT pg_temp.expect_fail(format($q$UPDATE cairn.case_intake_answers SET own_words = 'a heart attack' WHERE case_id = %L AND field_key = 'circumstance'$q$, :'case_a'));
SELECT pg_temp.expect_fail(format($q$UPDATE cairn.case_intake_answers SET value = '{"precision": "exact", "date": null}' WHERE case_id = %L AND field_key = 'date_of_death'$q$, :'case_a'));
SELECT pg_temp.expect_fail(format($q$UPDATE cairn.case_intake_answers SET value = '{"state": "NH", "county_or_city": null, "outside_us": false, "ssn": "1"}' WHERE case_id = %L AND field_key = 'place_of_death'$q$, :'case_a'));
SELECT pg_temp.expect_fail(format($q$UPDATE cairn.case_intake_answers SET value = '{"state": "NH", "county_or_city": null, "outside_us": true}' WHERE case_id = %L AND field_key = 'place_of_death'$q$, :'case_a'));
SELECT pg_temp.expect_fail(format($q$UPDATE cairn.case_intake_answers SET value = '["none_or_unsure", "bank_notified"]' WHERE case_id = %L AND field_key = 'completed_items'$q$, :'case_a'));
SELECT pg_temp.expect_fail(format($q$UPDATE cairn.case_intake_answers SET value = '"yes"' WHERE case_id = %L AND field_key = 'veteran_status'$q$, :'case_a'));
SELECT pg_temp.expect_fail(format($q$UPDATE cairn.cases SET shown_notices = ARRAY['suicide_loss'] WHERE id = %L$q$, :'case_a'));
SELECT pg_temp.expect_fail(format($q$UPDATE cairn.cases SET attorney_triggers = ARRAY['grief'] WHERE id = %L$q$, :'case_a'));
-- start_journey refuses unknown templates and a death that hasn't happened yet (UC-CASE-17).
SELECT pg_temp.expect_fail(format($q$SELECT cairn.start_journey(%L, 9001, 'nonexistent_path')$q$, :'case_a'));
SELECT pg_temp.expect_fail(format($q$SELECT cairn.start_journey(%L, 424242, 'general')$q$, :'case_a'));
UPDATE cairn.cases SET death_not_yet_occurred = true WHERE id = :'case_a'::uuid;
SELECT pg_temp.expect_fail(format($q$SELECT cairn.start_journey(%L, 9001, 'general')$q$, :'case_a'));
UPDATE cairn.cases SET death_not_yet_occurred = false WHERE id = :'case_a'::uuid;
SELECT pg_temp.expect_true('still a draft, still no trial',
  (SELECT c.status = 'draft' AND u.trial_started_at IS NULL FROM cairn.cases c JOIN cairn.users u ON u.id = c.created_by
   WHERE c.id = :'case_a'::uuid));

-- Start journey: active, pinned, and the first journey starts the 28-day trial in this transaction.
SELECT pg_temp.expect_true('first start_journey starts the trial', cairn.start_journey(:'case_a'::uuid, 9001, 'general'));
SELECT pg_temp.expect_true('case is active and pinned',
  (SELECT status = 'active' AND journey_template_key = 'general' AND journey_template_version = 9001
          AND journey_started_at = now() AND journey_started_on IS NOT NULL FROM cairn.cases WHERE id = :'case_a'::uuid));
SELECT pg_temp.expect_fail(format($q$SELECT cairn.start_journey(%L, 9001, 'general')$q$, :'case_a'));
-- Identity fields (UC-5) then death event fields (UC-6) as two statements on
-- the same row, matching the two-step UX now that the tables are merged.
INSERT INTO cairn.deceased (case_id, legal_first_name, legal_last_name, date_of_birth, ssn_last4,
                            domicile_state, veteran_status, has_will)
  VALUES (:'case_a'::uuid, 'Dan', 'Anders', '1950-01-01', '1234', 'NH', 'yes', 'no')
  RETURNING id AS dec_a \gset
UPDATE cairn.deceased SET date_of_death = current_date - 2, place_type = 'hospital', death_state = 'NH'
  WHERE id = :'dec_a'::uuid;
INSERT INTO cairn.consents (user_id, purpose, policy_version) VALUES (:'alice'::uuid, 'privacy', 'v0');
INSERT INTO cairn.audit_events (actor_id, case_id, action) VALUES (:'alice'::uuid, :'case_a'::uuid, 'case_created');

-- always + vet + nh + nowill(has_will no, domicile NH) + t_old v2 = 5. CA excluded. Old version excluded.
-- The first Start journey started the 28-day trial in the same transaction and scheduled reminders:
-- day 21, day 27, and trial_reminder_days_before (3) before the end.
SELECT pg_temp.expect_true('trial started with the first journey',
  (SELECT status = 'trial_active' AND trial_started_at = now() AND trial_ends_at = now() + interval '672 hours'
   FROM cairn.users WHERE id = :'alice'::uuid));
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.trial_reminders', 3);
SELECT pg_temp.expect_true('reminders at day 21, day 27, and 3 days before the end',
  (SELECT array_agg(due_at - now() ORDER BY kind) = ARRAY[interval '504 hours', interval '648 hours', interval '600 hours']
   FROM cairn.trial_reminders));

SELECT pg_temp.expect_true('generate_case_tasks creates 5', cairn.generate_case_tasks(:'case_a'::uuid) = 5);
SELECT pg_temp.expect_true('generate_case_tasks is idempotent', cairn.generate_case_tasks(:'case_a'::uuid) = 0);
SELECT pg_temp.expect_count(format('SELECT 1 FROM cairn.case_tasks ct JOIN cairn.task_templates t ON t.id = ct.template_id WHERE ct.case_id = %L AND t.task_key = ''t_ca''', :'case_a'), 0);
SELECT pg_temp.expect_count(format('SELECT 1 FROM cairn.case_tasks ct JOIN cairn.task_templates t ON t.id = ct.template_id WHERE ct.case_id = %L AND t.task_key = ''t_old'' AND t.version = 1', :'case_a'), 0);
SELECT pg_temp.expect_true('due_on = start + offset',
  (SELECT ct.due_on FROM cairn.case_tasks ct JOIN cairn.task_templates t ON t.id = ct.template_id
    WHERE ct.case_id = :'case_a'::uuid AND t.task_key = 't_vet') = current_date + 17);

-- A second owner cannot be added to a case that already has members.
SELECT pg_temp.expect_fail(format($q$INSERT INTO cairn.case_members (case_id,user_id,relationship,role) VALUES (%L,%L,'child','owner')$q$, :'case_a', :'alice'));

-- Constraint checks.
SELECT pg_temp.expect_fail(format($q$UPDATE cairn.deceased SET ssn_last4 = '12345' WHERE case_id = %L$q$, :'case_a'));
SELECT pg_temp.expect_fail(format($q$UPDATE cairn.case_tasks SET status = 'done' WHERE case_id = %L$q$, :'case_a'));
SELECT pg_temp.expect_fail($q$UPDATE cairn.users SET email = 'changed@example.test'$q$);
-- date_of_death cannot precede date_of_birth. Dan's date_of_birth is 1950-01-01
-- and date_of_death is current_date - 2, so moving birth to today violates it.
SELECT pg_temp.expect_fail(format($q$UPDATE cairn.deceased SET date_of_birth = current_date WHERE id = %L$q$, :'dec_a'));

-- Alice can complete a task properly.
UPDATE cairn.case_tasks SET status = 'done', completed_at = now()
  WHERE case_id = :'case_a'::uuid
    AND template_id = (SELECT id FROM cairn.task_templates WHERE task_key = 't_always');

-- ------------------------------------------------------------ isolation: Bob sees nothing of Alice's
SELECT set_config('app.user_id', :'bob', true);
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.trial_reminders', 0);
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.cases', 0);
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.deceased', 0);
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.case_members', 0);
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.case_tasks', 0);
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.case_intake_answers', 0);
SELECT pg_temp.expect_count(format('SELECT 1 FROM cairn.consents WHERE user_id = %L', :'alice'), 0);
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.users', 1);  -- only himself
-- Writes against Alice's case are rejected or affect nothing.
SELECT pg_temp.expect_fail(format($q$INSERT INTO cairn.case_members (case_id,user_id,relationship,role) VALUES (%L,%L,'child','owner')$q$, :'case_a', :'bob'));
SELECT pg_temp.expect_fail(format($q$INSERT INTO cairn.deceased (case_id, legal_first_name, legal_last_name) VALUES (%L,'E','V')$q$, :'case_a'));
SELECT pg_temp.expect_fail(format($q$INSERT INTO cairn.cases (created_by) VALUES (%L)$q$, :'alice'));
SELECT pg_temp.expect_fail(format($q$INSERT INTO cairn.audit_events (actor_id, case_id, action) VALUES (%L,%L,'forged')$q$, :'alice', :'case_a'));
SELECT pg_temp.expect_true('cannot generate tasks for another case', cairn.generate_case_tasks(:'case_a'::uuid) = 0);
SELECT pg_temp.expect_fail(format($q$SELECT cairn.start_journey(%L, 9001, 'general')$q$, :'case_a'));
SELECT pg_temp.expect_fail(format($q$INSERT INTO cairn.case_intake_answers (case_id, field_key, answer_state) VALUES (%L, 'user_role', 'skipped')$q$, :'case_a'));
DO $$
DECLARE n bigint;
BEGIN
  UPDATE cairn.deceased SET legal_first_name = 'Hacked';  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN RAISE EXCEPTION 'Bob updated % deceased rows', n; END IF;
  -- Same table, same policy: death-event columns are covered by the identical check.
  UPDATE cairn.deceased SET death_state = 'CA';           GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN RAISE EXCEPTION 'Bob updated % death_state rows', n; END IF;
  UPDATE cairn.case_tasks SET status = 'skipped';         GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN RAISE EXCEPTION 'Bob updated % task rows', n; END IF;
  UPDATE cairn.case_intake_answers SET answer_state = 'skipped', value = NULL;  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN RAISE EXCEPTION 'Bob updated % intake answers', n; END IF;
  DELETE FROM cairn.cases;                                 GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN RAISE EXCEPTION 'Bob deleted % cases', n; END IF;
END $$;

-- ------------------------------------------------------------ Alice still sees her own case
SELECT set_config('app.user_id', :'alice', true);
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.cases', 1);
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.deceased', 1);
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.case_tasks', 5);
SELECT pg_temp.expect_fail($q$INSERT INTO cairn.task_templates (task_key,version,title,plain_summary,journey_week,due_offset_days,jurisdiction,content_hash,git_release) VALUES ('zzz',1,'x','x',1,1,'US',repeat('9',64),'t')$q$);

-- A second journey never moves the trial clock (DEC-04).
SELECT trial_started_at AS alice_trial FROM cairn.users WHERE id = :'alice'::uuid \gset
INSERT INTO cairn.cases (created_by) VALUES (:'alice'::uuid) RETURNING id AS case_a2 \gset
INSERT INTO cairn.case_members (case_id, user_id, relationship, role)
  VALUES (:'case_a2'::uuid, :'alice'::uuid, 'spouse', 'owner');
SELECT pg_temp.expect_true('second start_journey does not start a trial',
  NOT cairn.start_journey(:'case_a2'::uuid, 9001, 'general'));
SELECT pg_temp.expect_true('second case keeps trial_started_at',
  (SELECT trial_started_at = :'alice_trial'::timestamptz FROM cairn.users WHERE id = :'alice'::uuid));
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.trial_reminders', 3);

-- ------------------------------------------------------------ back to owner: immutability and retention
RESET ROLE;
-- A started journey never goes back to draft, not even for the owner.
SELECT pg_temp.expect_fail(format($q$UPDATE cairn.cases SET status = 'draft', journey_started_at = NULL, journey_template_key = NULL, journey_template_version = NULL, journey_started_on = NULL WHERE id = %L$q$, :'case_a'));
SELECT pg_temp.expect_fail(format($q$UPDATE cairn.cases SET journey_started_at = now() - interval '1 day' WHERE id = %L$q$, :'case_a'));
-- last_activity_at always takes the server time.
UPDATE cairn.cases SET last_activity_at = now() + interval '100 days' WHERE id = :'case_a'::uuid;
SELECT pg_temp.expect_true('last_activity_at cannot be pushed forward',
  (SELECT last_activity_at = now() FROM cairn.cases WHERE id = :'case_a'::uuid));
-- The trial start is never reset, not even by the owner.
SELECT pg_temp.expect_fail(format($q$UPDATE cairn.users SET trial_started_at = NULL, trial_ends_at = NULL WHERE id = %L$q$, :'alice'));
SELECT pg_temp.expect_fail(format($q$UPDATE cairn.users SET trial_ends_at = now() WHERE id = %L$q$, :'alice'));
-- Consents are append-only for the owner too, except the cascade from deleting the account.
SELECT pg_temp.expect_fail($q$UPDATE cairn.consents SET policy_version = 'x'$q$);
SELECT pg_temp.expect_fail($q$DELETE FROM cairn.consents$q$);
SELECT pg_temp.expect_fail($q$TRUNCATE cairn.consents$q$);
-- Reminders due now are claimed once for email.
UPDATE cairn.trial_reminders SET due_at = now() - interval '1 minute'
  WHERE user_id = :'alice'::uuid AND kind = 'trial_day_21';
SELECT pg_temp.expect_count('SELECT * FROM cairn.claim_due_trial_reminders(10)', 1);
SELECT pg_temp.expect_count('SELECT * FROM cairn.claim_due_trial_reminders(10)', 0);

-- ------------------------------------------------------------ read-only after the trial (D-05)
-- Move Alice's trial into the past. The guard trigger is lifted only inside this rolled-back test.
ALTER TABLE cairn.users DISABLE TRIGGER users_trial_set_once;
UPDATE cairn.users SET trial_started_at = now() - interval '29 days',
                       trial_ends_at = now() - interval '29 days' + interval '672 hours'
  WHERE id = :'alice'::uuid;
ALTER TABLE cairn.users ENABLE TRIGGER users_trial_set_once;
SET LOCAL ROLE cairn_app;
SELECT set_config('app.user_id', :'alice', true);
SELECT pg_temp.expect_true('account is read-only', NOT cairn.account_can_write());
SELECT pg_temp.expect_true('effective status is read_only',
  (SELECT cairn.effective_account_status(status, trial_ends_at) = 'read_only' FROM cairn.users WHERE id = :'alice'::uuid));
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.cases', 2);          -- still readable
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.case_tasks', 5);
-- UC-CASE-18. A read-only account can create and edit a draft, but can't start its journey.
INSERT INTO cairn.cases (created_by) VALUES (:'alice'::uuid) RETURNING id AS case_a3 \gset
INSERT INTO cairn.case_members (case_id, user_id, role) VALUES (:'case_a3'::uuid, :'alice'::uuid, 'owner');
INSERT INTO cairn.case_intake_answers (case_id, field_key, answer_state) VALUES (:'case_a3'::uuid, 'user_role', 'skipped');
UPDATE cairn.cases SET last_intake_step = 'display_name' WHERE id = :'case_a3'::uuid;
SELECT pg_temp.expect_fail(format($q$SELECT cairn.start_journey(%L, 9001, 'general')$q$, :'case_a3'));
DELETE FROM cairn.cases WHERE id = :'case_a3'::uuid;
SELECT pg_temp.expect_fail(format($q$INSERT INTO cairn.deceased (case_id, legal_first_name, legal_last_name) VALUES (%L,'E','V')$q$, :'case_a2'));
SELECT pg_temp.expect_fail(format($q$INSERT INTO cairn.context_items (case_id, item_key, payload) VALUES (%L,'CERT_ORDER','{}')$q$, :'case_a'));
DO $$
DECLARE n bigint;
BEGIN
  UPDATE cairn.deceased SET legal_first_name = 'Changed';  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN RAISE EXCEPTION 'read-only account updated % deceased rows', n; END IF;
  UPDATE cairn.case_tasks SET status = 'skipped';          GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN RAISE EXCEPTION 'read-only account updated % task rows', n; END IF;
  UPDATE cairn.cases SET tasks_paused_until = now();       GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN RAISE EXCEPTION 'read-only account updated % cases', n; END IF;
  -- An active case's answers are read-only too. Only drafts stay editable.
  UPDATE cairn.case_intake_answers SET answer_state = 'unsure', value = NULL, own_words = NULL;
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN RAISE EXCEPTION 'read-only account updated % answers on an active case', n; END IF;
END $$;
-- Settings stay editable and a case can still be deleted.
UPDATE cairn.users SET voice = 'warm_patient' WHERE id = :'alice'::uuid;
DELETE FROM cairn.cases WHERE id = :'case_a2'::uuid;
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.cases', 1);
RESET ROLE;
-- A subscribed account can write again.
UPDATE cairn.users SET status = 'subscribed' WHERE id = :'alice'::uuid;
SET LOCAL ROLE cairn_app;
SELECT set_config('app.user_id', :'alice', true);
SELECT pg_temp.expect_true('subscribed account can write', cairn.account_can_write());
RESET ROLE;
SELECT pg_temp.expect_true('expire_trials skips subscribed accounts', cairn.expire_trials() = 0);
SELECT pg_temp.expect_fail($q$UPDATE cairn.task_templates SET title = 'changed' WHERE task_key = 't_always'$q$);
SELECT pg_temp.expect_fail($q$DELETE FROM cairn.task_templates WHERE task_key = 't_always'$q$);
SELECT pg_temp.expect_fail($q$UPDATE cairn.template_citations SET url = 'https://example.test/b'$q$);
SELECT pg_temp.expect_fail($q$DELETE FROM cairn.template_citations$q$);
SELECT pg_temp.expect_fail($q$TRUNCATE cairn.task_templates CASCADE$q$);
SELECT pg_temp.expect_fail($q$UPDATE cairn.audit_events SET action = 'tampered'$q$);
SELECT pg_temp.expect_fail($q$DELETE FROM cairn.audit_events$q$);
SELECT pg_temp.expect_fail($q$TRUNCATE cairn.audit_events$q$);
-- The active flag alone may change.
UPDATE cairn.task_templates SET active = false WHERE task_key = 't_ca';

-- Retention: an expired case is purged, its data cascades away, its audit trail stays.
UPDATE cairn.cases SET purge_after = now() - interval '1 day' WHERE id = :'case_a'::uuid;
SELECT pg_temp.expect_true('purge removes one case', cairn.purge_expired_cases() = 1);
SELECT pg_temp.expect_count(format('SELECT 1 FROM cairn.deceased WHERE case_id = %L', :'case_a'), 0);
SELECT pg_temp.expect_count(format('SELECT 1 FROM cairn.case_tasks WHERE case_id = %L', :'case_a'), 0);
-- case_created (by the test), journey_started (by start_journey), and case_purged.
SELECT pg_temp.expect_count(format('SELECT 1 FROM cairn.audit_events WHERE case_id = %L', :'case_a'), 3);

-- A case owner can delete their own case through the app (erasure request path).
SET LOCAL ROLE cairn_app;
SELECT set_config('app.user_id', :'bob', true);
INSERT INTO cairn.cases (created_by) VALUES (:'bob'::uuid) RETURNING id AS case_b \gset
INSERT INTO cairn.case_members (case_id, user_id, relationship, role) VALUES (:'case_b'::uuid, :'bob'::uuid, 'child', 'owner');
INSERT INTO cairn.case_intake_answers (case_id, field_key, answer_state, value) VALUES (:'case_b'::uuid, 'display_name', 'answered', '"Eve"');
SELECT pg_temp.expect_true('bob starts his journey', cairn.start_journey(:'case_b'::uuid, 9001, 'general'));
INSERT INTO cairn.deceased (case_id, legal_first_name, legal_last_name) VALUES (:'case_b'::uuid, 'Eve', 'Baker');
DELETE FROM cairn.cases WHERE id = :'case_b'::uuid;
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.deceased', 0);
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.case_intake_answers', 0);

-- ------------------------------------------------------------ account deletion (UC-ACCT-01)
INSERT INTO cairn.cases (created_by) VALUES (:'bob'::uuid) RETURNING id AS case_b2 \gset
INSERT INTO cairn.case_members (case_id, user_id, relationship, role) VALUES (:'case_b2'::uuid, :'bob'::uuid, 'child', 'owner');
INSERT INTO cairn.case_intake_answers (case_id, field_key, answer_state, value) VALUES (:'case_b2'::uuid, 'display_name', 'answered', '"Eve"');
SELECT cairn.delete_my_account();
RESET ROLE;
SELECT pg_temp.expect_count(format('SELECT 1 FROM cairn.users WHERE id = %L', :'bob'), 0);
SELECT pg_temp.expect_count(format('SELECT 1 FROM cairn.consents WHERE user_id = %L', :'bob'), 0);
SELECT pg_temp.expect_count(format('SELECT 1 FROM cairn.trial_reminders WHERE user_id = %L', :'bob'), 0);
SELECT pg_temp.expect_count(format('SELECT 1 FROM cairn.cases WHERE id = %L', :'case_b2'), 0);
SELECT pg_temp.expect_count(format('SELECT 1 FROM cairn.deceased WHERE case_id = %L', :'case_b2'), 0);
SELECT pg_temp.expect_count(format('SELECT 1 FROM cairn.case_intake_answers WHERE case_id = %L', :'case_b2'), 0);
SELECT pg_temp.expect_count($q$SELECT 1 FROM cairn.identity_deletion_requests WHERE idp_subject = 'sub-bob'$q$, 1);
SELECT pg_temp.expect_count(format($q$SELECT 1 FROM cairn.audit_events WHERE actor_id = %L AND action = 'account_deleted'$q$, :'bob'), 1);

-- ------------------------------------------------------------ draft cleanup (DEC-07, UC-CASE-10)
-- Drafts idle for draft_retention_days are deleted with their answers and conversation text.
-- Active cases, accounts, and trial fields are never touched. The audit row has ids only.
SET LOCAL ROLE cairn_app;
SELECT cairn.create_account('sub-dave', 'dave@example.test', 'email') AS dave \gset
SELECT set_config('app.user_id', :'dave', true);
SELECT pg_temp.onboard();
INSERT INTO cairn.cases (created_by) VALUES (:'dave'::uuid) RETURNING id AS old_draft \gset
INSERT INTO cairn.case_members (case_id, user_id, role) VALUES (:'old_draft'::uuid, :'dave'::uuid, 'owner');
INSERT INTO cairn.case_intake_answers (case_id, field_key, answer_state, value) VALUES (:'old_draft'::uuid, 'display_name', 'answered', '"Fakey"');
INSERT INTO cairn.context_items (case_id, item_key, payload) VALUES (:'old_draft'::uuid, 'CONVO_SUMMARY', '{"text": "fake"}');
INSERT INTO cairn.cases (created_by) VALUES (:'dave'::uuid) RETURNING id AS new_draft \gset
INSERT INTO cairn.case_members (case_id, user_id, role) VALUES (:'new_draft'::uuid, :'dave'::uuid, 'owner');
INSERT INTO cairn.cases (created_by) VALUES (:'dave'::uuid) RETURNING id AS old_active \gset
INSERT INTO cairn.case_members (case_id, user_id, role) VALUES (:'old_active'::uuid, :'dave'::uuid, 'owner');
SELECT pg_temp.expect_true('dave starts a journey', cairn.start_journey(:'old_active'::uuid, 9001, 'general'));
RESET ROLE;
SELECT trial_started_at AS dave_trial FROM cairn.users WHERE id = :'dave'::uuid \gset
ALTER TABLE cairn.cases DISABLE TRIGGER cases_guard;
UPDATE cairn.cases SET last_activity_at = now() - interval '28 days' WHERE id IN (:'old_draft'::uuid, :'old_active'::uuid);
UPDATE cairn.cases SET last_activity_at = now() - interval '27 days 23 hours' WHERE id = :'new_draft'::uuid;
ALTER TABLE cairn.cases ENABLE TRIGGER cases_guard;
SELECT pg_temp.expect_true('purge removes the idle draft only', cairn.purge_inactive_drafts() = 1);
SELECT pg_temp.expect_count(format('SELECT 1 FROM cairn.cases WHERE id = %L', :'old_draft'), 0);
SELECT pg_temp.expect_count(format('SELECT 1 FROM cairn.case_intake_answers WHERE case_id = %L', :'old_draft'), 0);
SELECT pg_temp.expect_count(format('SELECT 1 FROM cairn.context_items WHERE case_id = %L', :'old_draft'), 0);
SELECT pg_temp.expect_count(format('SELECT 1 FROM cairn.cases WHERE id IN (%L, %L)', :'new_draft', :'old_active'), 2);
SELECT pg_temp.expect_true('account and trial untouched',
  (SELECT trial_started_at = :'dave_trial'::timestamptz FROM cairn.users WHERE id = :'dave'::uuid));
SELECT pg_temp.expect_count(format($q$SELECT 1 FROM cairn.audit_events WHERE case_id = %L AND action = 'draft_case_expired'
  AND actor_id IS NULL AND object_type = 'user' AND object_id = %L$q$, :'old_draft', :'dave'), 1);
-- An answer is activity: it moves a draft's deletion date out again.
ALTER TABLE cairn.cases DISABLE TRIGGER cases_guard;
UPDATE cairn.cases SET last_activity_at = now() - interval '30 days' WHERE id = :'new_draft'::uuid;
ALTER TABLE cairn.cases ENABLE TRIGGER cases_guard;
INSERT INTO cairn.case_intake_answers (case_id, field_key, answer_state) VALUES (:'new_draft'::uuid, 'user_role', 'unsure');
SELECT pg_temp.expect_true('purge keeps a draft with new activity', cairn.purge_inactive_drafts() = 0);

-- ------------------------------------------------------------ stale account cleanup (UC-REG-10, UC-REG-13)
SET LOCAL ROLE cairn_app;
SELECT cairn.create_account('sub-carol', 'carol@example.test', 'apple', 'Carol', NULL) AS carol \gset
RESET ROLE;
SELECT pg_temp.expect_true('fresh pending account is kept', cairn.purge_stale_accounts('1 day') = 0);
UPDATE cairn.users SET created_at = now() - interval '2 days' WHERE id = :'carol'::uuid;
SELECT pg_temp.expect_true('stale pending account is purged', cairn.purge_stale_accounts('1 day') = 1);
SELECT pg_temp.expect_count(format('SELECT 1 FROM cairn.users WHERE id = %L', :'carol'), 0);

ROLLBACK;
\echo All checks passed.
