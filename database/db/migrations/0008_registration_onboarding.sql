-- 0008: registration and onboarding (UC-REG-01 to UC-REG-14, UC-ACCT-01).
--
-- Source: database/docs/cairn-registration-use-cases.json (spec 1.1.0).
-- UC-REG-06 (age confirmation) is not built, by product decision. The
-- spec's account.age_confirmed_at is not stored and there is no age step.
-- Maps the spec's "account" onto cairn.users and its "consent_record" onto
-- cairn.consents rather than adding parallel tables:
--   spec auth_provider       -> users.sign_in_method (0007)
--   spec provider_subject_id -> users.idp_subject (the Auth0 subject, unique)
--   spec consent_type        -> consents.purpose
--   spec document_version    -> consents.policy_version
--   spec accepted_at         -> consents.granted_at
--
-- The 28-day free trial starts when the account's first case is created, in
-- the same transaction, and is never reset (D-02 to D-05). After it ends the
-- account is read-only unless subscribed. Read-only is enforced here with
-- restrictive row-level security policies, not only in the API.

-- ---------------------------------------------------------------- users: account fields

-- Legal names are no longer collected at sign-up (UC-REG-11). They stay as
-- nullable columns for accounts created before this migration.
ALTER TABLE cairn.users ALTER COLUMN first_name DROP NOT NULL;
ALTER TABLE cairn.users ALTER COLUMN last_name DROP NOT NULL;

ALTER TABLE cairn.users
  ADD COLUMN preferred_name     text CHECK (preferred_name IS NULL OR length(preferred_name) BETWEEN 1 AND 100),
  ADD COLUMN name_pronunciation text CHECK (name_pronunciation IS NULL OR length(name_pronunciation) BETWEEN 1 AND 200),
  -- A name shared by Google or Apple, kept only to pre-fill UC-REG-11. Cleared
  -- once the user saves a preferred name.
  ADD COLUMN name_prefill       text CHECK (name_prefill IS NULL OR length(name_prefill) BETWEEN 1 AND 100),
  ADD COLUMN personality        text NOT NULL DEFAULT 'steady'
                                  CHECK (personality IN ('gentle', 'steady', 'straightforward')),
  -- IANA zone name, used to show trial dates in the user's local time.
  ADD COLUMN time_zone          text CHECK (time_zone IS NULL OR length(time_zone) BETWEEN 1 AND 64),
  ADD COLUMN onboarding_step    text NOT NULL DEFAULT 'account_created'
                                  CHECK (onboarding_step IN ('account_created', 'privacy_terms_accepted',
                                    'trial_terms_accepted', 'ai_notice_accepted', 'preferred_name_saved', 'complete')),
  ADD COLUMN status             text NOT NULL DEFAULT 'pending_onboarding'
                                  CHECK (status IN ('pending_onboarding', 'active_no_case', 'trial_active',
                                    'read_only', 'subscribed', 'pending_deletion')),
  ADD COLUMN trial_started_at   timestamptz,
  ADD COLUMN trial_ends_at      timestamptz,
  -- Exactly 28 days (D-02), counted in hours so daylight saving time never shifts it.
  ADD CONSTRAINT trial_is_28_days CHECK (
    (trial_started_at IS NULL AND trial_ends_at IS NULL)
    OR trial_ends_at = trial_started_at + interval '672 hours');

COMMENT ON COLUMN cairn.users.name_prefill IS
  'Name shared by Google or Apple. Pre-fill for UC-REG-11 only. Never saved as preferred_name without confirmation.';
COMMENT ON COLUMN cairn.users.trial_started_at IS
  'Set once, in the transaction that creates the first case. Never reset.';
COMMENT ON COLUMN cairn.users.status IS
  'Lifecycle status. read_only is also derived from trial_ends_at, see cairn.effective_account_status.';

-- Accounts created before 0008 already have a case flow behind them. Treat
-- them as having finished onboarding so they are not locked out. They are
-- asked for the acknowledgments on next sign-in because they have no
-- privacy_terms, trial_terms, or ai_notice consent (UC-REG-13).
UPDATE cairn.users u SET
  onboarding_step = 'complete',
  status = CASE WHEN EXISTS (SELECT 1 FROM cairn.cases c WHERE c.created_by = u.id)
                THEN 'trial_active' ELSE 'active_no_case' END,
  trial_started_at = (SELECT min(c.created_at) FROM cairn.cases c WHERE c.created_by = u.id),
  trial_ends_at = (SELECT min(c.created_at) FROM cairn.cases c WHERE c.created_by = u.id) + interval '672 hours';

-- Backfill the sign-in method from the Auth0 subject prefix where 0007 left it null.
UPDATE cairn.users SET sign_in_method = CASE split_part(idp_subject, '|', 1)
    WHEN 'google-oauth2' THEN 'google' WHEN 'apple' THEN 'apple'
    WHEN 'auth0' THEN 'email' WHEN 'email' THEN 'email' END
  WHERE sign_in_method IS NULL;

-- trial_started_at is written once and never changed. This fires for the owner too.
CREATE FUNCTION cairn.users_trial_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.trial_started_at IS NOT NULL
     AND NEW.trial_started_at IS DISTINCT FROM OLD.trial_started_at THEN
    RAISE EXCEPTION 'trial_started_at is set once and never reset'
      USING ERRCODE = 'restrict_violation';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER users_trial_set_once BEFORE UPDATE ON cairn.users
  FOR EACH ROW EXECUTE FUNCTION cairn.users_trial_guard();

-- ---------------------------------------------------------------- consents: append-only ledger

ALTER TABLE cairn.consents DROP CONSTRAINT consents_purpose_check;
ALTER TABLE cairn.consents ADD CONSTRAINT consents_purpose_check
  CHECK (purpose IN ('terms', 'privacy', 'ai_processing',            -- before 0008
                     'privacy_terms', 'trial_terms', 'ai_notice'));  -- UC-REG-07 to UC-REG-09
ALTER TABLE cairn.consents DROP CONSTRAINT consents_policy_version_check;
ALTER TABLE cairn.consents ADD CONSTRAINT consents_policy_version_check
  CHECK (length(policy_version) BETWEEN 1 AND 200);
ALTER TABLE cairn.consents
  ADD COLUMN auth_provider text CHECK (auth_provider IS NULL OR auth_provider IN ('google', 'apple', 'email')),
  ADD COLUMN client        text CHECK (client IS NULL OR length(client) BETWEEN 1 AND 100);

COMMENT ON TABLE cairn.consents IS
  'Append-only. Rows are removed only by the cascade from deleting the account.';

-- Consent rows are never updated. They are deleted only by the ON DELETE
-- CASCADE from cairn.users, which runs inside the referential integrity
-- trigger, so a direct DELETE (trigger depth 1) is refused and a cascaded one
-- (depth 2) is allowed.
CREATE FUNCTION cairn.consents_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' AND pg_trigger_depth() > 1 THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION 'consents is append-only. Rows go only when the account is deleted.'
    USING ERRCODE = 'restrict_violation';
END
$$;

CREATE TRIGGER consents_append_only BEFORE UPDATE OR DELETE ON cairn.consents
  FOR EACH ROW EXECUTE FUNCTION cairn.consents_guard();
CREATE TRIGGER consents_no_truncate BEFORE TRUNCATE ON cairn.consents
  FOR EACH STATEMENT EXECUTE FUNCTION cairn.forbid_truncate();

DROP POLICY consents_update_own ON cairn.consents;
REVOKE UPDATE ON cairn.consents FROM cairn_app;
GRANT INSERT (user_id, purpose, policy_version, auth_provider, client) ON cairn.consents TO cairn_app;

-- ---------------------------------------------------------------- trial reminders

-- Scheduled when the trial starts (day 21 and day 27). Email is sent by a
-- scheduled job as the owner (cairn.claim_due_trial_reminders). The app can
-- read its own rows to show the same reminder in the app.
CREATE TABLE cairn.trial_reminders (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid NOT NULL REFERENCES cairn.users (id) ON DELETE CASCADE,
  kind          text NOT NULL CHECK (kind IN ('trial_day_21', 'trial_day_27')),
  due_at        timestamptz NOT NULL,
  email_sent_at timestamptz,
  UNIQUE (user_id, kind)
);
CREATE INDEX trial_reminders_due_idx ON cairn.trial_reminders (due_at) WHERE email_sent_at IS NULL;

ALTER TABLE cairn.trial_reminders ENABLE ROW LEVEL SECURITY;
CREATE POLICY trial_reminders_select_own ON cairn.trial_reminders FOR SELECT TO cairn_app
  USING (user_id = cairn.current_user_id());
GRANT SELECT ON cairn.trial_reminders TO cairn_app;

-- ---------------------------------------------------------------- identity cleanup queue

-- When an account is deleted, the Auth0 user must be deleted too and, for
-- Apple, the tokens revoked through the Sign in with Apple REST API
-- (UC-ACCT-01). Those calls leave the database, so they are queued here and
-- run by a job as the owner. No grants: the app writes only through
-- cairn.delete_my_account. Rows are deleted once the cleanup has finished.
CREATE TABLE cairn.identity_deletion_requests (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  idp_subject   text NOT NULL CHECK (length(idp_subject) BETWEEN 1 AND 255),
  provider      text CHECK (provider IS NULL OR provider IN ('google', 'apple', 'email')),
  requested_at  timestamptz NOT NULL DEFAULT now(),
  attempts      integer NOT NULL DEFAULT 0,
  last_error    text CHECK (last_error IS NULL OR length(last_error) <= 200)
);
ALTER TABLE cairn.identity_deletion_requests ENABLE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------- account state functions

CREATE FUNCTION cairn.effective_account_status(p_status text, p_trial_ends_at timestamptz)
RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT CASE
    WHEN p_status IN ('subscribed', 'pending_deletion', 'pending_onboarding') THEN p_status
    WHEN p_trial_ends_at IS NOT NULL AND now() >= p_trial_ends_at THEN 'read_only'
    ELSE p_status
  END
$$;

-- True when the calling user may create or change case data: onboarding is
-- complete and the account is not read-only (D-05). Used by the restrictive
-- policies below. Deleting a case or the account never depends on this.
CREATE FUNCTION cairn.account_can_write() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
  SELECT COALESCE((
    SELECT u.onboarding_step = 'complete'
       AND cairn.effective_account_status(u.status, u.trial_ends_at) IN ('active_no_case', 'trial_active', 'subscribed')
    FROM cairn.users u WHERE u.id = cairn.current_user_id()
  ), false)
$$;

-- Sign-up bootstrap (UC-REG-02 to UC-REG-04). Creates the account with status
-- pending_onboarding, or returns the existing one for the same subject.
-- No legal name is collected. A provider-shared name is kept only as a pre-fill.
CREATE FUNCTION cairn.create_account(
  p_idp_subject text, p_email text, p_sign_in_method text,
  p_name_prefill text DEFAULT NULL, p_time_zone text DEFAULT NULL
) RETURNS uuid
LANGUAGE sql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
  INSERT INTO cairn.users (idp_subject, email, sign_in_method, name_prefill, time_zone)
  VALUES (p_idp_subject, p_email, p_sign_in_method, p_name_prefill, p_time_zone)
  ON CONFLICT (idp_subject) DO UPDATE SET email = EXCLUDED.email
  RETURNING id
$$;

-- Moves the calling user's onboarding forward by exactly one step, in the
-- order of the spec's onboarding_sequence. Repeating a step already passed is
-- a no-op, so clients can retry safely. Skipping ahead is refused. Each step
-- checks that its own record exists (the consent row, the preferred name).
CREATE FUNCTION cairn.advance_onboarding(p_to text) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
DECLARE
  steps constant text[] := ARRAY['account_created', 'privacy_terms_accepted',
    'trial_terms_accepted', 'ai_notice_accepted', 'preferred_name_saved', 'complete'];
  consent_for constant jsonb := '{"privacy_terms_accepted": "privacy_terms",
    "trial_terms_accepted": "trial_terms", "ai_notice_accepted": "ai_notice"}';
  uid uuid := cairn.current_user_id();
  u cairn.users;
  cur_i int;
  to_i int := array_position(steps, p_to);
BEGIN
  IF uid IS NULL THEN
    RAISE EXCEPTION 'no current user' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF to_i IS NULL OR to_i = 1 THEN
    RAISE EXCEPTION 'unknown onboarding step' USING ERRCODE = 'invalid_parameter_value';
  END IF;
  SELECT * INTO u FROM cairn.users WHERE id = uid FOR UPDATE;
  cur_i := array_position(steps, u.onboarding_step);
  IF cur_i >= to_i THEN
    RETURN u.onboarding_step;
  END IF;
  IF cur_i <> to_i - 1 THEN
    RAISE EXCEPTION 'onboarding steps must be completed in order'
      USING ERRCODE = 'object_not_in_prerequisite_state';
  END IF;
  IF consent_for ? p_to AND NOT EXISTS (
       SELECT 1 FROM cairn.consents c WHERE c.user_id = uid AND c.purpose = consent_for ->> p_to) THEN
    RAISE EXCEPTION 'acknowledgment not recorded' USING ERRCODE = 'object_not_in_prerequisite_state';
  END IF;
  IF p_to = 'preferred_name_saved' AND u.preferred_name IS NULL THEN
    RAISE EXCEPTION 'preferred name not saved' USING ERRCODE = 'object_not_in_prerequisite_state';
  END IF;

  UPDATE cairn.users SET
    onboarding_step = p_to,
    name_prefill = CASE WHEN p_to = 'preferred_name_saved' THEN NULL ELSE name_prefill END,
    status = CASE WHEN p_to = 'complete' AND status = 'pending_onboarding' THEN 'active_no_case' ELSE status END
  WHERE id = uid;
  RETURN p_to;
END
$$;

-- Starts the trial when the account's first case is inserted. Runs as a
-- trigger so it is always in the same transaction as the case, whatever the
-- caller. Only a null trial_started_at is written (D-03, D-04).
CREATE FUNCTION cairn.start_trial_on_first_case() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
DECLARE
  started timestamptz;
BEGIN
  UPDATE cairn.users SET
    trial_started_at = now(),
    trial_ends_at = now() + interval '672 hours',
    status = CASE WHEN status = 'active_no_case' THEN 'trial_active' ELSE status END
  WHERE id = NEW.created_by AND trial_started_at IS NULL
  RETURNING trial_started_at INTO started;
  IF started IS NOT NULL THEN
    INSERT INTO cairn.trial_reminders (user_id, kind, due_at) VALUES
      (NEW.created_by, 'trial_day_21', started + interval '504 hours'),
      (NEW.created_by, 'trial_day_27', started + interval '648 hours');
  END IF;
  RETURN NULL;
END
$$;

CREATE TRIGGER cases_start_trial AFTER INSERT ON cairn.cases
  FOR EACH ROW EXECUTE FUNCTION cairn.start_trial_on_first_case();

-- UC-ACCT-01. Deletes the calling user's account and personal data now:
-- the cases they created (which cascade to members, deceased, tasks, and
-- context), their memberships, consents, and reminders. Queues the identity
-- provider cleanup. Audit rows keep only opaque ids and are retained.
-- At MVP every case has a single owner, so deleting the creator's cases
-- deletes no one else's data. Revisit when invitations ship.
CREATE FUNCTION cairn.delete_my_account() RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
DECLARE
  uid uuid := cairn.current_user_id();
  u cairn.users;
  r record;
BEGIN
  SELECT * INTO u FROM cairn.users WHERE id = uid FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no current user' USING ERRCODE = 'insufficient_privilege';
  END IF;
  FOR r IN SELECT id FROM cairn.cases WHERE created_by = uid LOOP
    DELETE FROM cairn.cases WHERE id = r.id;
    INSERT INTO cairn.audit_events (actor_id, case_id, action, object_type, object_id)
    VALUES (uid, r.id, 'case_deleted_with_account', 'case', r.id);
  END LOOP;
  DELETE FROM cairn.case_members WHERE user_id = uid;
  INSERT INTO cairn.identity_deletion_requests (idp_subject, provider) VALUES (u.idp_subject, u.sign_in_method);
  DELETE FROM cairn.users WHERE id = uid;
  INSERT INTO cairn.audit_events (actor_id, action, object_type, object_id)
  VALUES (uid, 'account_deleted', 'user', uid);
END
$$;

-- ---------------------------------------------------------------- scheduled jobs (owner only, not granted)

-- Marks trials that have ended as read_only for reporting. Enforcement does
-- not wait for this job: cairn.account_can_write compares against trial_ends_at.
CREATE FUNCTION cairn.expire_trials() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
DECLARE n integer;
BEGIN
  UPDATE cairn.users SET status = 'read_only'
  WHERE status = 'trial_active' AND trial_ends_at <= now();
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END
$$;

-- Returns trial reminders that are due and marks them sent (at most once).
-- Skips subscribed accounts and trials that have already ended.
CREATE FUNCTION cairn.claim_due_trial_reminders(p_limit integer DEFAULT 100)
RETURNS TABLE (reminder_id uuid, kind text, email text, preferred_name text,
               trial_ends_at timestamptz, time_zone text)
LANGUAGE sql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
  WITH due AS (
    SELECT r.id FROM cairn.trial_reminders r JOIN cairn.users u ON u.id = r.user_id
    WHERE r.email_sent_at IS NULL AND r.due_at <= now()
      AND u.status NOT IN ('subscribed', 'pending_deletion') AND now() < u.trial_ends_at
    ORDER BY r.due_at
    LIMIT p_limit
    FOR UPDATE OF r SKIP LOCKED
  ), marked AS (
    UPDATE cairn.trial_reminders r SET email_sent_at = now() FROM due WHERE r.id = due.id
    RETURNING r.id, r.kind, r.user_id
  )
  SELECT m.id, m.kind, u.email, u.preferred_name, u.trial_ends_at, u.time_zone
  FROM marked m JOIN cairn.users u ON u.id = m.user_id
$$;

-- Deletes accounts that stopped before finishing onboarding (UC-REG-10), and
-- optionally accounts that finished but never created a case (UC-REG-13).
-- The periods come from the retention schedule and have no defaults on
-- purpose. [LEGAL REVIEW REQUIRED]
CREATE FUNCTION cairn.purge_stale_accounts(p_pending_older_than interval,
                                           p_no_case_older_than interval DEFAULT NULL)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
DECLARE
  r record;
  n integer := 0;
BEGIN
  FOR r IN
    SELECT u.id, u.idp_subject, u.sign_in_method FROM cairn.users u
    WHERE NOT EXISTS (SELECT 1 FROM cairn.cases c WHERE c.created_by = u.id)
      AND NOT EXISTS (SELECT 1 FROM cairn.case_members m WHERE m.user_id = u.id)
      AND ((u.status = 'pending_onboarding' AND u.created_at < now() - p_pending_older_than)
        OR (p_no_case_older_than IS NOT NULL AND u.status = 'active_no_case'
            AND u.created_at < now() - p_no_case_older_than))
  LOOP
    INSERT INTO cairn.identity_deletion_requests (idp_subject, provider) VALUES (r.idp_subject, r.sign_in_method);
    DELETE FROM cairn.users WHERE id = r.id;
    INSERT INTO cairn.audit_events (actor_id, action, object_type, object_id)
    VALUES (NULL, 'stale_account_purged', 'user', r.id);
    n := n + 1;
  END LOOP;
  RETURN n;
END
$$;

-- ---------------------------------------------------------------- grants on users

GRANT UPDATE (preferred_name, name_pronunciation, name_prefill, personality, time_zone)
  ON cairn.users TO cairn_app;

-- ---------------------------------------------------------------- read-only enforcement (D-05)
-- Restrictive policies are ANDed with the existing permissive ones, so each
-- write below now also needs an account that has finished onboarding and is
-- not read-only. Reads are untouched. Deleting a case stays available.

CREATE POLICY cases_insert_needs_writable_account ON cairn.cases AS RESTRICTIVE
  FOR INSERT TO cairn_app WITH CHECK (cairn.account_can_write());
CREATE POLICY cases_update_needs_writable_account ON cairn.cases AS RESTRICTIVE
  FOR UPDATE TO cairn_app USING (cairn.account_can_write()) WITH CHECK (cairn.account_can_write());

CREATE POLICY case_members_insert_needs_writable_account ON cairn.case_members AS RESTRICTIVE
  FOR INSERT TO cairn_app WITH CHECK (cairn.account_can_write());

CREATE POLICY deceased_insert_needs_writable_account ON cairn.deceased AS RESTRICTIVE
  FOR INSERT TO cairn_app WITH CHECK (cairn.account_can_write());
CREATE POLICY deceased_update_needs_writable_account ON cairn.deceased AS RESTRICTIVE
  FOR UPDATE TO cairn_app USING (cairn.account_can_write()) WITH CHECK (cairn.account_can_write());

CREATE POLICY case_tasks_insert_needs_writable_account ON cairn.case_tasks AS RESTRICTIVE
  FOR INSERT TO cairn_app WITH CHECK (cairn.account_can_write());
CREATE POLICY case_tasks_update_needs_writable_account ON cairn.case_tasks AS RESTRICTIVE
  FOR UPDATE TO cairn_app USING (cairn.account_can_write()) WITH CHECK (cairn.account_can_write());

-- context_items is an optional migration. If it is already here, protect it
-- now. A fresh database applies it after this file, so the same policies also
-- ship as db/optional/context_items_read_only.sql, which must follow it.
DO $$
BEGIN
  IF to_regclass('cairn.context_items') IS NOT NULL THEN
    CREATE POLICY context_items_insert_needs_writable_account ON cairn.context_items AS RESTRICTIVE
      FOR INSERT TO cairn_app WITH CHECK (cairn.account_can_write());
    CREATE POLICY context_items_update_needs_writable_account ON cairn.context_items AS RESTRICTIVE
      FOR UPDATE TO cairn_app USING (cairn.account_can_write()) WITH CHECK (cairn.account_can_write());
  END IF;
END
$$;

-- ---------------------------------------------------------------- function grants

REVOKE ALL ON FUNCTION
  cairn.effective_account_status(text, timestamptz),
  cairn.account_can_write(),
  cairn.create_account(text, text, text, text, text),
  cairn.advance_onboarding(text),
  cairn.start_trial_on_first_case(),
  cairn.delete_my_account(),
  cairn.expire_trials(),
  cairn.claim_due_trial_reminders(integer),
  cairn.purge_stale_accounts(interval, interval),
  cairn.users_trial_guard(),
  cairn.consents_guard()
FROM PUBLIC;

GRANT EXECUTE ON FUNCTION
  cairn.effective_account_status(text, timestamptz),
  cairn.account_can_write(),
  cairn.create_account(text, text, text, text, text),
  cairn.advance_onboarding(text),
  cairn.delete_my_account()
TO cairn_app;
