-- 0010: case creation, intake answers, and starting a journey (UC-CASE-01 to UC-CASE-18).
--
-- Source: database/docs/cairn-case-creation-use-cases.json (spec 0.3.0).
-- Extends existing tables. Nothing is renamed or dropped. Mapping from the spec:
--   spec case.status draft|active           -> cases.status (draft added, now the default)
--   spec case.status read_only              -> derived, not stored. An active case reads as
--                                              read_only when the account is read-only
--                                              (cairn.effective_case_status). The spec says the
--                                              account-level flag drives it.
--   spec journey_template_key / _version    -> cases.journey_template_key / _version (new)
--   spec journey_started_at                 -> cases.journey_started_at (new). journey_started_on
--                                              stays as the date tasks count from.
--   spec last_intake_step, last_activity_at -> cases (new)
--   spec draft_expires_at                   -> derived from last_activity_at and app_settings
--   spec intake_answers                     -> cairn.case_intake_answers (new), one row per field
--   spec journey_task.status todo           -> case_tasks.status not_started (existing value)
--   spec journey_task.status check_on_this,
--        not_today                          -> case_tasks.status (added)
--   spec account_trial.*                    -> users.trial_* (0008). trial_ends_soon reminder added.
--   spec user_role                          -> case_intake_answers.user_role. case_members.relationship
--                                              becomes nullable and is not set for new cases.
--
-- DEC-01 changes when the trial starts: at the first Start journey, not at the
-- first case. Creating or editing a draft never starts it. The 0008 trigger on
-- cases is dropped and cairn.start_journey takes its place.

-- ---------------------------------------------------------------- settings

-- Values that change without a migration (spec open_decisions and config).
-- Owner writes. The app reads.
CREATE TABLE cairn.app_settings (
  key         text PRIMARY KEY CHECK (key IN ('draft_retention_days', 'trial_reminder_days_before')),
  value       jsonb NOT NULL,
  description text NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT app_settings_value_valid CHECK (
    jsonb_typeof(value) = 'number' AND (
      (key = 'draft_retention_days' AND (value #>> '{}')::numeric BETWEEN 1 AND 365)
      OR (key = 'trial_reminder_days_before' AND (value #>> '{}')::numeric BETWEEN 1 AND 27)))
);

INSERT INTO cairn.app_settings (key, value, description) VALUES
  ('draft_retention_days', '28',
   'DEC-07. A draft case with no activity for this many days is deleted. The pause copy says 28 days, so change the copy too.'),
  ('trial_reminder_days_before', '3',
   'OPEN-DECISION-02. Days before trial_ends_at to schedule the trial_ends_soon reminder.');

CREATE TRIGGER app_settings_touch BEFORE UPDATE ON cairn.app_settings
  FOR EACH ROW EXECUTE FUNCTION cairn.touch_updated_at();

CREATE FUNCTION cairn.setting_int(p_key text) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
  SELECT (value #>> '{}')::integer FROM cairn.app_settings WHERE key = p_key
$$;

-- ---------------------------------------------------------------- journey templates (content)

-- The journey selection rules (base paths by circumstance, add-ons, what
-- completed_items mark done). Authored as content/journeys/journey-selection.json
-- and loaded by tools/load_templates.py. Immutable per version, like task_templates.
CREATE TABLE cairn.journey_templates (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  version             integer NOT NULL UNIQUE CHECK (version >= 1),
  definition          jsonb NOT NULL CHECK (jsonb_typeof(definition) = 'object'),
  counsel_reviewed_at timestamptz,
  content_hash        text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
  git_release         text NOT NULL CHECK (length(git_release) BETWEEN 1 AND 200),
  active              boolean NOT NULL DEFAULT true,
  loaded_at           timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION cairn.journey_templates_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'journey_templates rows are immutable and cannot be deleted'
      USING ERRCODE = 'restrict_violation';
  END IF;
  IF (to_jsonb(OLD) - 'active') IS DISTINCT FROM (to_jsonb(NEW) - 'active') THEN
    RAISE EXCEPTION 'journey_templates rows are immutable. Publish a new version instead.'
      USING ERRCODE = 'restrict_violation';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER journey_templates_immutable BEFORE UPDATE OR DELETE ON cairn.journey_templates
  FOR EACH ROW EXECUTE FUNCTION cairn.journey_templates_guard();
CREATE TRIGGER journey_templates_no_truncate BEFORE TRUNCATE ON cairn.journey_templates
  FOR EACH STATEMENT EXECUTE FUNCTION cairn.forbid_truncate();

-- A short line for UC-CASE-13 on why a task is time-sensitive. Optional.
ALTER TABLE cairn.task_templates
  ADD COLUMN why_now text CHECK (why_now IS NULL OR length(why_now) BETWEEN 1 AND 200);

-- ---------------------------------------------------------------- cases

ALTER TABLE cairn.cases DROP CONSTRAINT cases_status_check;
ALTER TABLE cairn.cases ADD CONSTRAINT cases_status_check
  CHECK (status IN ('draft', 'active', 'paused', 'closed'));
ALTER TABLE cairn.cases ALTER COLUMN status SET DEFAULT 'draft';

-- A draft has no journey yet, so it has no start date.
ALTER TABLE cairn.cases ALTER COLUMN journey_started_on DROP NOT NULL;
ALTER TABLE cairn.cases ALTER COLUMN journey_started_on DROP DEFAULT;

ALTER TABLE cairn.cases
  ADD COLUMN journey_template_key     text CHECK (journey_template_key IS NULL
                                                  OR journey_template_key ~ '^[a-z][a-z0-9_]{2,63}$'),
  ADD COLUMN journey_template_version integer REFERENCES cairn.journey_templates (version),
  ADD COLUMN journey_started_at       timestamptz,
  ADD COLUMN last_intake_step         text CHECK (last_intake_step IS NULL
                                                  OR last_intake_step ~ '^[a-z][a-z0-9_]{2,63}$'),
  ADD COLUMN last_activity_at         timestamptz NOT NULL DEFAULT now(),
  -- UC-CASE-17. The user said the person has not died yet. Start journey is not offered.
  ADD COLUMN death_not_yet_occurred   boolean NOT NULL DEFAULT false,
  -- UC-CASE-02. A professional fiduciary can ask for a shorter pace with no explainers.
  ADD COLUMN skip_explainers          boolean NOT NULL DEFAULT false,
  -- UC-CASE-03. What to say when no display name was given.
  ADD COLUMN name_fallback            text NOT NULL DEFAULT 'your_loved_one'
                                        CHECK (name_fallback IN ('your_loved_one', 'the_person_who_died')),
  -- UC-CASE-16. Reasons the user gave that call for an estate attorney. Facts about
  -- the estate only. Death outside the US and unverified states are derived, not stored.
  ADD COLUMN attorney_triggers        text[] NOT NULL DEFAULT '{}'
                                        CHECK (attorney_triggers <@ ARRAY['contested_will', 'family_disagreement',
                                               'unsure_of_authority', 'multi_state_property']::text[]),
  -- One-time notices already shown for this case (UC-CASE-02 POA note). Only
  -- the keys listed can be stored, so nothing about distress or cause of death
  -- can end up here.
  ADD COLUMN shown_notices            text[] NOT NULL DEFAULT '{}'
                                        CHECK (shown_notices <@ ARRAY['poa_authority_note']::text[]);

-- Cases created before 0010 had their journey from the start.
UPDATE cairn.cases SET journey_started_at = created_at, last_activity_at = created_at;

ALTER TABLE cairn.cases
  ADD CONSTRAINT draft_has_no_journey CHECK (
    status <> 'draft' OR (journey_started_at IS NULL AND journey_template_key IS NULL
                          AND journey_template_version IS NULL AND journey_started_on IS NULL)),
  ADD CONSTRAINT started_case_has_start CHECK (
    status = 'draft' OR (journey_started_at IS NOT NULL AND journey_started_on IS NOT NULL));

CREATE INDEX cases_draft_activity_idx ON cairn.cases (last_activity_at) WHERE status = 'draft';

COMMENT ON COLUMN cairn.cases.status IS
  'draft until Start journey (UC-CASE-12). An active case reads as read_only when the account is read-only.';
COMMENT ON COLUMN cairn.cases.last_activity_at IS
  'Any answer, edit, or open of a draft. A draft idle for draft_retention_days is deleted (DEC-07).';

-- Only cairn.start_journey moves a case out of draft, and a started journey is
-- never unstarted. last_activity_at always takes the server time, so a client
-- cannot push a draft's deletion date forward or back.
CREATE FUNCTION cairn.cases_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status <> 'draft' AND NEW.status = 'draft' THEN
    RAISE EXCEPTION 'a started journey cannot go back to draft' USING ERRCODE = 'restrict_violation';
  END IF;
  IF OLD.journey_started_at IS NOT NULL AND NEW.journey_started_at IS DISTINCT FROM OLD.journey_started_at THEN
    RAISE EXCEPTION 'journey_started_at is set once' USING ERRCODE = 'restrict_violation';
  END IF;
  IF NEW.last_activity_at IS DISTINCT FROM OLD.last_activity_at THEN
    NEW.last_activity_at := now();
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER cases_guard BEFORE UPDATE ON cairn.cases
  FOR EACH ROW EXECUTE FUNCTION cairn.cases_guard();

-- ---------------------------------------------------------------- case_members, deceased

-- user_role is an intake answer now (it can be skipped). The column stays for
-- cases created before 0010.
ALTER TABLE cairn.case_members ALTER COLUMN relationship DROP NOT NULL;

-- Legal names are never collected at case creation (never_collect_at_case_creation).
-- They are collected later, inside the task that needs them. The deceased row is
-- created then, not with the case.
ALTER TABLE cairn.deceased ALTER COLUMN legal_first_name DROP NOT NULL;
ALTER TABLE cairn.deceased ALTER COLUMN legal_last_name DROP NOT NULL;

-- ---------------------------------------------------------------- case_tasks

ALTER TABLE cairn.case_tasks DROP CONSTRAINT case_tasks_status_check;
ALTER TABLE cairn.case_tasks ADD CONSTRAINT case_tasks_status_check
  CHECK (status IN ('not_started', 'check_on_this', 'in_progress', 'done', 'not_today',
                    'skipped', 'not_applicable'));

-- Whether the journey rules currently include this task. Changing an answer on
-- an active case flips this instead of deleting rows, so a task the user has
-- worked on is never lost (UC-CASE-09). Separate from status, which is the user's.
ALTER TABLE cairn.case_tasks ADD COLUMN selected boolean NOT NULL DEFAULT true;

-- ---------------------------------------------------------------- intake answers

-- Shape check for each data_fields key. The API validates first. This is the
-- backstop that keeps anything outside the spec's fields out of the table, for
-- example free text about how someone died.
CREATE FUNCTION cairn.intake_value_valid(p_key text, v jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  keys text[];
  ok boolean;
BEGIN
  IF jsonb_typeof(v) = 'object' THEN
    SELECT array_agg(k ORDER BY k) INTO keys FROM jsonb_object_keys(v) k;
  END IF;
  CASE p_key
    WHEN 'user_role' THEN
      ok := jsonb_typeof(v) = 'string' AND v #>> '{}' IN ('spouse_partner', 'child', 'other_family',
        'named_executor', 'power_of_attorney', 'professional_fiduciary', 'friend', 'other', 'prefer_not_to_say');
    WHEN 'display_name' THEN
      ok := jsonb_typeof(v) = 'string' AND length(v #>> '{}') BETWEEN 1 AND 60
        AND v #>> '{}' !~ '[\x00-\x1f\x7f]';
    WHEN 'date_of_death' THEN
      ok := keys = ARRAY['date', 'precision']
        AND v ->> 'precision' IN ('exact', 'today', 'this_week', 'unknown')
        AND CASE WHEN v ->> 'precision' IN ('exact', 'today')
                 THEN jsonb_typeof(v -> 'date') = 'string' AND (v ->> 'date') ~ '^\d{4}-\d{2}-\d{2}$'
                 ELSE jsonb_typeof(v -> 'date') = 'null' END;
    WHEN 'place_of_death' THEN
      ok := keys = ARRAY['county_or_city', 'outside_us', 'state']
        AND jsonb_typeof(v -> 'outside_us') = 'boolean'
        AND (jsonb_typeof(v -> 'state') = 'null' OR (v ->> 'state') ~ '^[A-Z]{2}$')
        AND (jsonb_typeof(v -> 'county_or_city') = 'null'
             OR (jsonb_typeof(v -> 'county_or_city') = 'string'
                 AND length(v ->> 'county_or_city') BETWEEN 1 AND 100))
        AND NOT ((v -> 'outside_us')::boolean AND jsonb_typeof(v -> 'state') <> 'null');
    WHEN 'residence_state' THEN
      ok := keys = ARRAY['choice', 'state']
        AND v ->> 'choice' IN ('same_as_place_of_death', 'different', 'unknown')
        AND (jsonb_typeof(v -> 'state') = 'null'
             OR (v ->> 'choice' = 'different' AND (v ->> 'state') ~ '^[A-Z]{2}$'));
    WHEN 'circumstance' THEN
      ok := jsonb_typeof(v) = 'string' AND v #>> '{}' IN ('expected_illness_or_hospice', 'sudden_natural',
        'accident_or_unexpected', 'under_investigation', 'prefer_not_to_say');
    WHEN 'veteran_status' THEN
      ok := jsonb_typeof(v) = 'string' AND v #>> '{}' IN ('yes', 'no', 'unknown');
    WHEN 'estate_plan_status' THEN
      ok := jsonb_typeof(v) = 'string' AND v #>> '{}' IN ('yes_location_known', 'yes_location_unknown', 'no', 'unknown');
    WHEN 'completed_items' THEN
      ok := jsonb_typeof(v) = 'array' AND jsonb_array_length(v) >= 1
        AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(v) e
                        WHERE jsonb_typeof(e) <> 'string' OR e #>> '{}' NOT IN ('death_pronounced',
                          'funeral_provider_chosen', 'funeral_home_has_ssn', 'certificates_ordered',
                          'ssa_notified', 'bank_notified', 'other', 'none_or_unsure'))
        AND (SELECT count(DISTINCT e) FROM jsonb_array_elements(v) e) = jsonb_array_length(v)
        AND (NOT v ? 'none_or_unsure' OR jsonb_array_length(v) = 1);
    ELSE
      ok := false;
  END CASE;
  -- A NULL result would pass a CHECK constraint, so anything not proven valid is invalid.
  RETURN coalesce(ok, false);
END
$$;

CREATE TABLE cairn.case_intake_answers (
  case_id      uuid NOT NULL REFERENCES cairn.cases (id) ON DELETE CASCADE,
  field_key    text NOT NULL CHECK (field_key IN ('user_role', 'display_name', 'date_of_death', 'place_of_death',
                 'residence_state', 'circumstance', 'veteran_status', 'estate_plan_status', 'completed_items')),
  answer_state text NOT NULL CHECK (answer_state IN ('answered', 'skipped', 'unsure')),
  value        jsonb,
  -- The user's own words for this one field, from free-text intake, for the
  -- review screen (UC-CASE-11). Never for circumstance, which stores the enum only.
  own_words    text,
  updated_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (case_id, field_key),
  CONSTRAINT value_only_when_answered CHECK ((answer_state = 'answered') = (value IS NOT NULL)),
  CONSTRAINT value_matches_field CHECK (value IS NULL OR cairn.intake_value_valid(field_key, value)),
  CONSTRAINT own_words_allowed CHECK (own_words IS NULL OR (
    field_key <> 'circumstance' AND answer_state = 'answered'
    AND length(own_words) BETWEEN 1 AND 120 AND own_words !~ '[\x00-\x1f\x7f]'))
);

COMMENT ON TABLE cairn.case_intake_answers IS
  'One row per data_fields key. Only the spec''s fields and shapes can be stored. Never log values.';

CREATE TRIGGER case_intake_answers_touch BEFORE UPDATE ON cairn.case_intake_answers
  FOR EACH ROW EXECUTE FUNCTION cairn.touch_updated_at();

-- Any answer counts as activity on the case (DEC-07).
CREATE FUNCTION cairn.intake_answer_activity() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
BEGIN
  UPDATE cairn.cases SET last_activity_at = now() WHERE id = NEW.case_id;
  RETURN NULL;
END
$$;

CREATE TRIGGER case_intake_answers_activity AFTER INSERT OR UPDATE ON cairn.case_intake_answers
  FOR EACH ROW EXECUTE FUNCTION cairn.intake_answer_activity();

-- ---------------------------------------------------------------- write rules

CREATE FUNCTION cairn.case_is_draft(p_case uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM cairn.cases WHERE id = p_case AND status = 'draft')
$$;

-- UC-CASE-18. A read-only account can still create and edit drafts. It can't
-- start a journey without a subscription.
CREATE FUNCTION cairn.account_can_edit_drafts() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
  SELECT COALESCE((
    SELECT u.onboarding_step = 'complete'
       AND cairn.effective_account_status(u.status, u.trial_ends_at)
           IN ('active_no_case', 'trial_active', 'subscribed', 'read_only')
    FROM cairn.users u WHERE u.id = cairn.current_user_id()
  ), false)
$$;

CREATE FUNCTION cairn.case_writable(p_case uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
  SELECT cairn.account_can_write() OR (cairn.account_can_edit_drafts() AND cairn.case_is_draft(p_case))
$$;

CREATE FUNCTION cairn.effective_case_status(p_case_status text, p_account_status text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN p_case_status = 'active' AND p_account_status = 'read_only' THEN 'read_only'
              ELSE p_case_status END
$$;

-- cases: new cases are drafts, and drafts stay editable on a read-only account.
DROP POLICY cases_insert_needs_writable_account ON cairn.cases;
DROP POLICY cases_update_needs_writable_account ON cairn.cases;
CREATE POLICY cases_insert_draft_only ON cairn.cases AS RESTRICTIVE
  FOR INSERT TO cairn_app WITH CHECK (status = 'draft' AND cairn.account_can_edit_drafts());
CREATE POLICY cases_update_needs_writable_account ON cairn.cases AS RESTRICTIVE
  FOR UPDATE TO cairn_app
  USING (cairn.account_can_write() OR (status = 'draft' AND cairn.account_can_edit_drafts()))
  WITH CHECK (cairn.account_can_write() OR (status = 'draft' AND cairn.account_can_edit_drafts()));

DROP POLICY case_members_insert_needs_writable_account ON cairn.case_members;
CREATE POLICY case_members_insert_needs_writable_account ON cairn.case_members AS RESTRICTIVE
  FOR INSERT TO cairn_app WITH CHECK (cairn.case_writable(case_id));

-- The deceased's identity belongs to tasks after the journey starts, never to a draft.
CREATE POLICY deceased_insert_not_draft ON cairn.deceased AS RESTRICTIVE
  FOR INSERT TO cairn_app WITH CHECK (NOT cairn.case_is_draft(case_id));
CREATE POLICY deceased_update_not_draft ON cairn.deceased AS RESTRICTIVE
  FOR UPDATE TO cairn_app USING (NOT cairn.case_is_draft(case_id)) WITH CHECK (NOT cairn.case_is_draft(case_id));

ALTER TABLE cairn.case_intake_answers ENABLE ROW LEVEL SECURITY;
CREATE POLICY case_intake_answers_select ON cairn.case_intake_answers FOR SELECT TO cairn_app
  USING (cairn.is_case_member(case_id));
CREATE POLICY case_intake_answers_insert ON cairn.case_intake_answers FOR INSERT TO cairn_app
  WITH CHECK (cairn.is_case_member(case_id, ARRAY['owner', 'co_executor']));
CREATE POLICY case_intake_answers_update ON cairn.case_intake_answers FOR UPDATE TO cairn_app
  USING (cairn.is_case_member(case_id, ARRAY['owner', 'co_executor']))
  WITH CHECK (cairn.is_case_member(case_id, ARRAY['owner', 'co_executor']));
CREATE POLICY case_intake_answers_insert_writable ON cairn.case_intake_answers AS RESTRICTIVE
  FOR INSERT TO cairn_app WITH CHECK (cairn.case_writable(case_id));
CREATE POLICY case_intake_answers_update_writable ON cairn.case_intake_answers AS RESTRICTIVE
  FOR UPDATE TO cairn_app USING (cairn.case_writable(case_id)) WITH CHECK (cairn.case_writable(case_id));

-- ---------------------------------------------------------------- trial: moves to Start journey (DEC-01)

DROP TRIGGER cases_start_trial ON cairn.cases;
DROP FUNCTION cairn.start_trial_on_first_case();

ALTER TABLE cairn.trial_reminders DROP CONSTRAINT trial_reminders_kind_check;
ALTER TABLE cairn.trial_reminders ADD CONSTRAINT trial_reminders_kind_check
  CHECK (kind IN ('trial_day_21', 'trial_day_27', 'trial_ends_soon'));

-- UC-CASE-12. Moves a draft to active and, on the account's first journey,
-- starts the 28-day trial, all in the caller's transaction. The app then adds
-- the journey tasks in that same transaction. Returns true when this call
-- started the trial. trial_started_at is only ever written when it is null.
-- Refuses read-only accounts (UC-CASE-18) and cases where the person has not
-- died yet (UC-CASE-17). No payment information is involved (DEC-02).
CREATE FUNCTION cairn.start_journey(p_case uuid, p_template_version integer, p_template_key text)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
DECLARE
  uid uuid := cairn.current_user_id();
  c cairn.cases;
  started timestamptz;
  ends timestamptz;
  tz text;
  remind_days integer := cairn.setting_int('trial_reminder_days_before');
BEGIN
  IF uid IS NULL OR NOT cairn.is_case_member(p_case, ARRAY['owner']) THEN
    RAISE EXCEPTION 'not the owner of this case' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF NOT cairn.account_can_write() THEN
    RAISE EXCEPTION 'account cannot start a journey' USING ERRCODE = 'insufficient_privilege';
  END IF;
  SELECT * INTO c FROM cairn.cases WHERE id = p_case FOR UPDATE;
  IF c.status <> 'draft' THEN
    RAISE EXCEPTION 'journey already started' USING ERRCODE = 'object_not_in_prerequisite_state';
  END IF;
  IF c.death_not_yet_occurred THEN
    RAISE EXCEPTION 'journeys start after a death' USING ERRCODE = 'object_not_in_prerequisite_state';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM cairn.journey_templates t
                 WHERE t.version = p_template_version AND t.active
                   AND t.definition -> 'paths' ? p_template_key) THEN
    RAISE EXCEPTION 'unknown journey template' USING ERRCODE = 'invalid_parameter_value';
  END IF;

  SELECT u.time_zone INTO tz FROM cairn.users u WHERE u.id = uid;
  UPDATE cairn.cases SET
    status = 'active',
    journey_template_key = p_template_key,
    journey_template_version = p_template_version,
    journey_started_at = now(),
    journey_started_on = (now() AT TIME ZONE coalesce(tz, 'UTC'))::date,
    last_activity_at = now()
  WHERE id = p_case;

  UPDATE cairn.users SET
    trial_started_at = now(),
    trial_ends_at = now() + interval '672 hours',
    status = CASE WHEN status = 'active_no_case' THEN 'trial_active' ELSE status END
  WHERE id = uid AND trial_started_at IS NULL
  RETURNING trial_started_at, trial_ends_at INTO started, ends;

  IF started IS NOT NULL THEN
    INSERT INTO cairn.trial_reminders (user_id, kind, due_at) VALUES
      (uid, 'trial_day_21', started + interval '504 hours'),
      (uid, 'trial_day_27', started + interval '648 hours'),
      (uid, 'trial_ends_soon', ends - make_interval(days => remind_days));
  END IF;

  INSERT INTO cairn.audit_events (actor_id, case_id, action, object_type, object_id)
  VALUES (uid, p_case, 'journey_started', 'case', p_case);
  RETURN started IS NOT NULL;
END
$$;

-- ---------------------------------------------------------------- draft cleanup job (DEC-07)

-- Hard-deletes drafts with no activity for draft_retention_days, with their
-- intake answers, members, and any conversation text tied to them (context_items
-- cascades). Never touches active cases, accounts, or trial fields. The audit
-- row holds the case id, the owner's user id, and the time only.
-- Not granted to the application. Run it at least daily as the owner role.
CREATE FUNCTION cairn.purge_inactive_drafts() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
DECLARE
  r record;
  n integer := 0;
  retention interval := make_interval(days => cairn.setting_int('draft_retention_days'));
BEGIN
  FOR r IN
    SELECT id, created_by FROM cairn.cases
    WHERE status = 'draft' AND last_activity_at + retention <= now()
    FOR UPDATE SKIP LOCKED
  LOOP
    DELETE FROM cairn.cases WHERE id = r.id AND status = 'draft';
    INSERT INTO cairn.audit_events (actor_id, case_id, action, object_type, object_id)
    VALUES (NULL, r.id, 'draft_case_expired', 'user', r.created_by);
    n := n + 1;
  END LOOP;
  RETURN n;
END
$$;

-- ---------------------------------------------------------------- grants

-- The app creates drafts only. Status, journey fields, and the start date are
-- written by cairn.start_journey.
REVOKE INSERT ON cairn.cases FROM cairn_app;
GRANT INSERT (created_by, purge_after) ON cairn.cases TO cairn_app;
REVOKE UPDATE ON cairn.cases FROM cairn_app;
-- journey_template_key can change on an active case when an answer changes the
-- base path (UC-CASE-09). draft_has_no_journey keeps it null on a draft.
GRANT UPDATE (tasks_paused_until, purge_after, last_intake_step, last_activity_at, death_not_yet_occurred,
              skip_explainers, name_fallback, attorney_triggers, shown_notices, journey_template_key)
  ON cairn.cases TO cairn_app;

GRANT SELECT ON cairn.case_intake_answers TO cairn_app;
GRANT INSERT (case_id, field_key, answer_state, value, own_words) ON cairn.case_intake_answers TO cairn_app;
GRANT UPDATE (answer_state, value, own_words) ON cairn.case_intake_answers TO cairn_app;

GRANT INSERT (case_id, template_id, status, due_on, selected) ON cairn.case_tasks TO cairn_app;
GRANT UPDATE (selected) ON cairn.case_tasks TO cairn_app;

GRANT SELECT ON cairn.app_settings TO cairn_app;

GRANT SELECT ON cairn.journey_templates TO cairn_app;
GRANT SELECT, INSERT ON cairn.journey_templates TO cairn_loader;
GRANT UPDATE (active) ON cairn.journey_templates TO cairn_loader;

REVOKE ALL ON FUNCTION
  cairn.setting_int(text),
  cairn.journey_templates_guard(),
  cairn.cases_guard(),
  cairn.intake_value_valid(text, jsonb),
  cairn.intake_answer_activity(),
  cairn.case_is_draft(uuid),
  cairn.account_can_edit_drafts(),
  cairn.case_writable(uuid),
  cairn.effective_case_status(text, text),
  cairn.start_journey(uuid, integer, text),
  cairn.purge_inactive_drafts()
FROM PUBLIC;

GRANT EXECUTE ON FUNCTION
  cairn.setting_int(text),
  cairn.intake_value_valid(text, jsonb),
  cairn.case_is_draft(uuid),
  cairn.account_can_edit_drafts(),
  cairn.case_writable(uuid),
  cairn.effective_case_status(text, text),
  cairn.start_journey(uuid, integer, text)
TO cairn_app;
