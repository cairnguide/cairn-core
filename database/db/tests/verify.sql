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

-- Alice creates a case, becomes owner, records the deceased and the death, gets tasks.
SELECT set_config('app.user_id', :'alice', true);
INSERT INTO cairn.cases (created_by) VALUES (:'alice'::uuid) RETURNING id AS case_a \gset
INSERT INTO cairn.case_members (case_id, user_id, relationship, role)
  VALUES (:'case_a'::uuid, :'alice'::uuid, 'spouse', 'owner');
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
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.cases', 0);
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.deceased', 0);
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.case_members', 0);
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.case_tasks', 0);
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.consents', 0);
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.users', 1);  -- only himself
-- Writes against Alice's case are rejected or affect nothing.
SELECT pg_temp.expect_fail(format($q$INSERT INTO cairn.case_members (case_id,user_id,relationship,role) VALUES (%L,%L,'child','owner')$q$, :'case_a', :'bob'));
SELECT pg_temp.expect_fail(format($q$INSERT INTO cairn.deceased (case_id, legal_first_name, legal_last_name) VALUES (%L,'E','V')$q$, :'case_a'));
SELECT pg_temp.expect_fail(format($q$INSERT INTO cairn.cases (created_by) VALUES (%L)$q$, :'alice'));
SELECT pg_temp.expect_fail(format($q$INSERT INTO cairn.audit_events (actor_id, case_id, action) VALUES (%L,%L,'forged')$q$, :'alice', :'case_a'));
SELECT pg_temp.expect_true('cannot generate tasks for another case', cairn.generate_case_tasks(:'case_a'::uuid) = 0);
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
  DELETE FROM cairn.cases;                                 GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN RAISE EXCEPTION 'Bob deleted % cases', n; END IF;
END $$;

-- ------------------------------------------------------------ Alice still sees her own case
SELECT set_config('app.user_id', :'alice', true);
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.cases', 1);
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.deceased', 1);
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.case_tasks', 5);
SELECT pg_temp.expect_fail($q$INSERT INTO cairn.task_templates (task_key,version,title,plain_summary,journey_week,due_offset_days,jurisdiction,content_hash,git_release) VALUES ('zzz',1,'x','x',1,1,'US',repeat('9',64),'t')$q$);

-- ------------------------------------------------------------ back to owner: immutability and retention
RESET ROLE;
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
SELECT pg_temp.expect_count(format('SELECT 1 FROM cairn.audit_events WHERE case_id = %L', :'case_a'), 2);

-- A case owner can delete their own case through the app (erasure request path).
SET LOCAL ROLE cairn_app;
SELECT set_config('app.user_id', :'bob', true);
INSERT INTO cairn.cases (created_by) VALUES (:'bob'::uuid) RETURNING id AS case_b \gset
INSERT INTO cairn.case_members (case_id, user_id, relationship, role) VALUES (:'case_b'::uuid, :'bob'::uuid, 'child', 'owner');
INSERT INTO cairn.deceased (case_id, legal_first_name, legal_last_name) VALUES (:'case_b'::uuid, 'Eve', 'Baker');
DELETE FROM cairn.cases WHERE id = :'case_b'::uuid;
SELECT pg_temp.expect_count('SELECT 1 FROM cairn.deceased', 0);
RESET ROLE;

ROLLBACK;
\echo All checks passed.
