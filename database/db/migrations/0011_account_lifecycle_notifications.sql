-- 0011: keeping in touch, confirmations, case deletion with a hold, and account deletion
-- (UC-CASE-19 to UC-CASE-21, UC-REG-15, UC-REG-16).
--
-- Sources: database/docs/cairn-case-creation-use-cases-2026-09-25.json (draft 0.4 changes)
-- and database/docs/cairn-account-use-cases-2026-09-25.json. Mapping from the specs:
--   spec notification_preferences (per journey) -> cairn.notification_preferences, keyed by case_id.
--                                                  The spec's journey_id is the case id: one journey per case.
--   spec notification_preferences.sms_*         -> not stored. SMS is an open question (card 50) and needs
--                                                  legal review, so 'sms' is not an accepted channel yet.
--   spec action_confirmation_log                -> cairn.action_confirmation_log (no content, no user id).
--                                                  The address waits in cairn.action_confirmation_outbox
--                                                  only until the one confirmation is sent.
--   spec 7-day hold (UC-END-13, UC-CASE-10)     -> cases.deletion_requested_at and the
--                                                  case_deletion_hold_days setting (7).
--
-- D-2026-09-25-F1: closing, downloading, deleting, and changing notification
-- preferences are always free, including on a read-only account. So
-- notification_preferences deliberately has no case_writable restrictive
-- policy, and the deletion functions never check account_can_write.

-- ---------------------------------------------------------------- settings

ALTER TABLE cairn.app_settings DROP CONSTRAINT app_settings_key_check;
ALTER TABLE cairn.app_settings ADD CONSTRAINT app_settings_key_check
  CHECK (key IN ('draft_retention_days', 'trial_reminder_days_before', 'case_deletion_hold_days'));
ALTER TABLE cairn.app_settings DROP CONSTRAINT app_settings_value_valid;
ALTER TABLE cairn.app_settings ADD CONSTRAINT app_settings_value_valid CHECK (
  jsonb_typeof(value) = 'number' AND (
    (key = 'draft_retention_days' AND (value #>> '{}')::numeric BETWEEN 1 AND 365)
    OR (key = 'trial_reminder_days_before' AND (value #>> '{}')::numeric BETWEEN 1 AND 27)
    OR (key = 'case_deletion_hold_days' AND (value #>> '{}')::numeric BETWEEN 1 AND 30)));

INSERT INTO cairn.app_settings (key, value, description) VALUES
  ('case_deletion_hold_days', '7',
   'UC-END-13. A case the user chose to delete with a hold is deleted this many days later. The copy says 7 days, so change the copy too.');

-- ---------------------------------------------------------------- cases: deletion with a hold

ALTER TABLE cairn.cases ADD COLUMN deletion_requested_at timestamptz;
CREATE INDEX cases_deletion_requested_idx ON cairn.cases (deletion_requested_at)
  WHERE deletion_requested_at IS NOT NULL;

COMMENT ON COLUMN cairn.cases.deletion_requested_at IS
  'The user chose delete with a hold. Deleted case_deletion_hold_days later by cairn.purge_held_cases. '
  'Written only by cairn.request_case_deletion and cairn.cancel_case_deletion.';

-- ---------------------------------------------------------------- activity on an active journey

-- The inactivity reason (UC-CASE-19) counts from the last thing the user did.
-- Intake answers already count (0010). A change to a task's status counts too.
-- Changes the rules make (the selected flag) do not.
CREATE FUNCTION cairn.case_task_activity() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
BEGIN
  UPDATE cairn.cases SET last_activity_at = now() WHERE id = NEW.case_id;
  RETURN NULL;
END
$$;

CREATE TRIGGER case_tasks_activity AFTER UPDATE OF status, snoozed_until ON cairn.case_tasks
  FOR EACH ROW
  WHEN (OLD.status IS DISTINCT FROM NEW.status OR OLD.snoozed_until IS DISTINCT FROM NEW.snoozed_until)
  EXECUTE FUNCTION cairn.case_task_activity();

-- ---------------------------------------------------------------- notification preferences (UC-CASE-19, UC-CASE-20)

-- The shape rules for a choice. in_app_only stands alone and has no reasons.
-- Any other channel needs at least one reason. Each reason needs its timing,
-- and a timing without its reason is refused.
CREATE FUNCTION cairn.notification_choice_valid(p_channels text[], p_reasons text[],
                                                p_lead_days integer, p_inactivity_days integer)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  ok boolean;
BEGIN
  ok := cardinality(p_channels) >= 1
    AND p_channels <@ ARRAY['email', 'push', 'in_app_only']::text[]
    AND cardinality(p_channels) = (SELECT count(DISTINCT x) FROM unnest(p_channels) x)
    AND p_reasons <@ ARRAY['due_date_upcoming', 'inactivity']::text[]
    AND cardinality(p_reasons) = (SELECT count(DISTINCT x) FROM unnest(p_reasons) x)
    AND CASE WHEN 'in_app_only' = ANY (p_channels)
             THEN cardinality(p_channels) = 1 AND cardinality(p_reasons) = 0
             ELSE cardinality(p_reasons) >= 1 END
    AND (p_lead_days IS NOT NULL) = ('due_date_upcoming' = ANY (p_reasons))
    AND (p_inactivity_days IS NOT NULL) = ('inactivity' = ANY (p_reasons));
  -- A NULL result would pass a CHECK constraint, so anything not proven valid is invalid.
  RETURN coalesce(ok, false);
END
$$;

CREATE TABLE cairn.notification_preferences (
  case_id                 uuid PRIMARY KEY REFERENCES cairn.cases (id) ON DELETE CASCADE,
  channels                text[] NOT NULL DEFAULT ARRAY['in_app_only']::text[],
  reasons                 text[] NOT NULL DEFAULT '{}',
  due_date_lead_days      integer CHECK (due_date_lead_days IS NULL OR due_date_lead_days IN (1, 3, 7)),
  inactivity_days         integer CHECK (inactivity_days IS NULL OR inactivity_days IN (3, 7, 14)),
  frequency               text NOT NULL DEFAULT 'daily_max'
                            CHECK (frequency IN ('as_it_happens', 'daily_max', 'weekly_max')),
  -- What the OS said when the user chose push. Push is never required for the app to work.
  push_permission_granted boolean NOT NULL DEFAULT false,
  updated_at              timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT notification_choice_valid
    CHECK (cairn.notification_choice_valid(channels, reasons, due_date_lead_days, inactivity_days))
);

COMMENT ON TABLE cairn.notification_preferences IS
  'How and when Cairn keeps in touch, per journey (UC-CASE-19). No row, or in_app_only, means nothing is sent '
  'outside the app (D-2026-09-25-N2). Never used for marketing or advertising (UC-REG-07).';

CREATE TRIGGER notification_preferences_touch BEFORE UPDATE ON cairn.notification_preferences
  FOR EACH ROW EXECUTE FUNCTION cairn.touch_updated_at();

-- Always free (D-2026-09-25-F1): no account_can_write or case_writable policy here.
ALTER TABLE cairn.notification_preferences ENABLE ROW LEVEL SECURITY;
CREATE POLICY notification_preferences_select ON cairn.notification_preferences FOR SELECT TO cairn_app
  USING (cairn.is_case_member(case_id));
CREATE POLICY notification_preferences_insert ON cairn.notification_preferences FOR INSERT TO cairn_app
  WITH CHECK (cairn.is_case_member(case_id, ARRAY['owner', 'co_executor']));
CREATE POLICY notification_preferences_update ON cairn.notification_preferences FOR UPDATE TO cairn_app
  USING (cairn.is_case_member(case_id, ARRAY['owner', 'co_executor']))
  WITH CHECK (cairn.is_case_member(case_id, ARRAY['owner', 'co_executor']));

-- What was sent outside the app, and why. No text, no address. Used for the
-- frequency limit, so a reason is never sent twice for the same thing, and
-- for the user's data download (UC-REG-16).
CREATE TABLE cairn.notification_log (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  case_id      uuid NOT NULL REFERENCES cairn.cases (id) ON DELETE CASCADE,
  case_task_id uuid REFERENCES cairn.case_tasks (id) ON DELETE CASCADE,
  reason       text NOT NULL CHECK (reason IN ('due_date_upcoming', 'inactivity')),
  channel      text NOT NULL CHECK (channel IN ('email')),
  sent_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX notification_log_case_idx ON cairn.notification_log (case_id, sent_at);
CREATE INDEX notification_log_task_idx ON cairn.notification_log (case_task_id) WHERE case_task_id IS NOT NULL;

ALTER TABLE cairn.notification_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY notification_log_select ON cairn.notification_log FOR SELECT TO cairn_app
  USING (cairn.is_case_member(case_id));

-- ---------------------------------------------------------------- action confirmations (UC-CASE-21)

-- The one confirmation for something the user just did. The address is kept
-- here only until it is sent (UC-REG-15: send, then purge the address). No
-- foreign key to users: the account_deleted row outlives the account, and it
-- carries no user id at all. No app grants. The app queues rows only through
-- the deletion functions below.
CREATE TABLE cairn.action_confirmation_outbox (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  action_type text NOT NULL
                CHECK (action_type IN ('case_deleted_now', 'case_deleted_after_hold', 'account_deleted')),
  email       text NOT NULL CHECK (length(email) BETWEEN 3 AND 320),
  user_id     uuid,
  queued_at   timestamptz NOT NULL DEFAULT now(),
  claimed_at  timestamptz,
  attempts    integer NOT NULL DEFAULT 0,
  last_error  text CHECK (last_error IS NULL OR length(last_error) <= 200)
);
ALTER TABLE cairn.action_confirmation_outbox ENABLE ROW LEVEL SECURITY;

-- Spec action_confirmation_log. content_stored is false by construction:
-- there is no column that could hold content or say who it went to.
CREATE TABLE cairn.action_confirmation_log (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  action_type text NOT NULL
                CHECK (action_type IN ('case_deleted_now', 'case_deleted_after_hold', 'account_deleted')),
  channel     text NOT NULL DEFAULT 'email' CHECK (channel = 'email'),
  sent_at     timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE cairn.action_confirmation_log ENABLE ROW LEVEL SECURITY;

CREATE FUNCTION cairn.action_confirmation_log_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'action_confirmation_log is append-only' USING ERRCODE = 'restrict_violation';
END
$$;

CREATE TRIGGER action_confirmation_log_append_only BEFORE UPDATE OR DELETE ON cairn.action_confirmation_log
  FOR EACH ROW EXECUTE FUNCTION cairn.action_confirmation_log_guard();
CREATE TRIGGER action_confirmation_log_no_truncate BEFORE TRUNCATE ON cairn.action_confirmation_log
  FOR EACH STATEMENT EXECUTE FUNCTION cairn.forbid_truncate();

-- Internal. Queues the confirmation to the account email as it is right now.
CREATE FUNCTION cairn.queue_action_confirmation(p_action text, p_user uuid) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
  INSERT INTO cairn.action_confirmation_outbox (action_type, email, user_id)
  SELECT p_action, u.email, u.id FROM cairn.users u WHERE u.id = p_user
$$;

-- ---------------------------------------------------------------- case deletion (UC-END-13, UC-CASE-10, UC-CASE-21)

-- Deletes a case now, or holds it for case_deletion_hold_days first. Owner
-- only. Works on drafts, active, and read-only cases alike, and never checks
-- whether the account can write (D-2026-09-25-F1). Deleting now queues one
-- confirmation. A hold queues it when the case is actually deleted. Returns
-- when a held case will be deleted, or NULL when it was deleted now.
CREATE FUNCTION cairn.request_case_deletion(p_case uuid, p_mode text) RETURNS timestamptz
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
DECLARE
  uid uuid := cairn.current_user_id();
  requested timestamptz;
  hold interval := make_interval(days => cairn.setting_int('case_deletion_hold_days'));
BEGIN
  IF uid IS NULL OR NOT cairn.is_case_member(p_case, ARRAY['owner']) THEN
    RAISE EXCEPTION 'not the owner of this case' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF p_mode IS NULL OR p_mode NOT IN ('now', 'hold') THEN
    RAISE EXCEPTION 'unknown deletion mode' USING ERRCODE = 'invalid_parameter_value';
  END IF;
  SELECT deletion_requested_at INTO requested FROM cairn.cases WHERE id = p_case FOR UPDATE;

  IF p_mode = 'now' THEN
    DELETE FROM cairn.cases WHERE id = p_case;
    INSERT INTO cairn.audit_events (actor_id, case_id, action, object_type, object_id)
    VALUES (uid, p_case, 'case_deleted_now', 'case', p_case);
    PERFORM cairn.queue_action_confirmation('case_deleted_now', uid);
    RETURN NULL;
  END IF;

  IF requested IS NULL THEN
    requested := now();
    UPDATE cairn.cases SET deletion_requested_at = requested WHERE id = p_case;
    INSERT INTO cairn.audit_events (actor_id, case_id, action, object_type, object_id)
    VALUES (uid, p_case, 'case_deletion_scheduled', 'case', p_case);
  END IF;
  RETURN requested + hold;
END
$$;

-- Keeps a case the user had set to delete with a hold. Owner only.
CREATE FUNCTION cairn.cancel_case_deletion(p_case uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
DECLARE
  uid uuid := cairn.current_user_id();
BEGIN
  IF uid IS NULL OR NOT cairn.is_case_member(p_case, ARRAY['owner']) THEN
    RAISE EXCEPTION 'not the owner of this case' USING ERRCODE = 'insufficient_privilege';
  END IF;
  UPDATE cairn.cases SET deletion_requested_at = NULL
  WHERE id = p_case AND deletion_requested_at IS NOT NULL;
  IF NOT FOUND THEN
    RETURN false;
  END IF;
  INSERT INTO cairn.audit_events (actor_id, case_id, action, object_type, object_id)
  VALUES (uid, p_case, 'case_deletion_cancelled', 'case', p_case);
  RETURN true;
END
$$;

-- Deletes cases whose hold has ended and queues their confirmation. Not
-- granted to the app. Run it at least hourly as the owner role.
CREATE FUNCTION cairn.purge_held_cases() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
DECLARE
  r record;
  n integer := 0;
  hold interval := make_interval(days => cairn.setting_int('case_deletion_hold_days'));
BEGIN
  FOR r IN
    SELECT id, created_by FROM cairn.cases
    WHERE deletion_requested_at IS NOT NULL AND deletion_requested_at + hold <= now()
    FOR UPDATE SKIP LOCKED
  LOOP
    DELETE FROM cairn.cases WHERE id = r.id;
    INSERT INTO cairn.audit_events (actor_id, case_id, action, object_type, object_id)
    VALUES (NULL, r.id, 'case_deleted_after_hold', 'user', r.created_by);
    PERFORM cairn.queue_action_confirmation('case_deleted_after_hold', r.created_by);
    n := n + 1;
  END LOOP;
  RETURN n;
END
$$;

-- ---------------------------------------------------------------- account deletion (UC-REG-15)

-- Replaces the 0008 version. Everything goes now, in one transaction: every
-- case the user created in any status, including cases in a hold, with their
-- tasks, answers, conversation text, and notification preferences (cascades),
-- then memberships, consents, reminders, and the account. Pending case
-- confirmations are dropped so exactly one confirmation goes out, and that
-- row has no user id. The identity provider cleanup is queued (Apple token
-- revocation, TN3194). Audit rows keep opaque ids only.
CREATE OR REPLACE FUNCTION cairn.delete_my_account() RETURNS void
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
  DELETE FROM cairn.action_confirmation_outbox WHERE user_id = uid;
  INSERT INTO cairn.action_confirmation_outbox (action_type, email, user_id) VALUES ('account_deleted', u.email, NULL);
  INSERT INTO cairn.identity_deletion_requests (idp_subject, provider) VALUES (u.idp_subject, u.sign_in_method);
  DELETE FROM cairn.users WHERE id = uid;
  INSERT INTO cairn.audit_events (actor_id, action, object_type, object_id)
  VALUES (uid, 'account_deleted', 'user', uid);
END
$$;

-- ---------------------------------------------------------------- sending (owner only, not granted)

-- Claims confirmations to send. A claim older than p_stale is claimed again,
-- so a sender that crashed mid-send is retried. That can repeat a message in
-- that one rare case, which is safer than never sending it.
CREATE FUNCTION cairn.claim_action_confirmations(p_limit integer DEFAULT 50,
                                                 p_stale interval DEFAULT interval '15 minutes')
RETURNS TABLE (confirmation_id uuid, action_type text, email text)
LANGUAGE sql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
  WITH due AS (
    SELECT o.id FROM cairn.action_confirmation_outbox o
    WHERE o.claimed_at IS NULL OR o.claimed_at < now() - p_stale
    ORDER BY o.queued_at
    LIMIT p_limit
    FOR UPDATE SKIP LOCKED
  )
  UPDATE cairn.action_confirmation_outbox o SET claimed_at = now(), attempts = o.attempts + 1
  FROM due WHERE o.id = due.id
  RETURNING o.id, o.action_type, o.email
$$;

-- The confirmation went out: purge the address and log the send, without content.
CREATE FUNCTION cairn.complete_action_confirmation(p_id uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
DECLARE
  kind text;
BEGIN
  DELETE FROM cairn.action_confirmation_outbox WHERE id = p_id RETURNING action_type INTO kind;
  IF NOT FOUND THEN
    RETURN false;
  END IF;
  INSERT INTO cairn.action_confirmation_log (action_type, channel) VALUES (kind, 'email');
  RETURN true;
END
$$;

-- Sending failed. Release the claim for the next run, with a short error code.
CREATE FUNCTION cairn.release_action_confirmation(p_id uuid, p_error text) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
  UPDATE cairn.action_confirmation_outbox SET claimed_at = NULL, last_error = left(p_error, 200)
  WHERE id = p_id
$$;

-- UC-CASE-19. Picks the journeys that are due a message by email, logs each
-- pick, and returns it. At most one message per journey per run, and none
-- more often than the journey's frequency allows. A step coming up is sent
-- once per task. Inactivity is sent once per quiet stretch. Nothing is sent
-- for a draft, a paused journey, a case set to be deleted, or a read-only
-- account. Logging at claim time means a failed send is not retried: a missed
-- nudge is better than a repeated one.
CREATE FUNCTION cairn.claim_due_notifications(p_limit integer DEFAULT 100)
RETURNS TABLE (notification_id uuid, reason text, email text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
#variable_conflict use_column
BEGIN
  -- One sender at a time, so two runs never pick the same journey.
  IF NOT pg_try_advisory_xact_lock(hashtext('cairn.claim_due_notifications')) THEN
    RETURN;
  END IF;
  RETURN QUERY
  WITH eligible AS (
    SELECT p.case_id, p.reasons, p.due_date_lead_days, p.inactivity_days, c.last_activity_at, u.email,
           (now() AT TIME ZONE coalesce(u.time_zone, 'UTC'))::date AS today
    FROM cairn.notification_preferences p
    JOIN cairn.cases c ON c.id = p.case_id
    JOIN cairn.users u ON u.id = c.created_by
    WHERE 'email' = ANY (p.channels)
      AND c.status = 'active'
      AND c.deletion_requested_at IS NULL
      AND (c.tasks_paused_until IS NULL OR c.tasks_paused_until <= now())
      AND cairn.effective_account_status(u.status, u.trial_ends_at) IN ('trial_active', 'subscribed')
      AND NOT EXISTS (
        SELECT 1 FROM cairn.notification_log l
        WHERE l.case_id = p.case_id
          AND l.sent_at > now() - CASE p.frequency WHEN 'daily_max' THEN interval '24 hours'
                                                   WHEN 'weekly_max' THEN interval '7 days'
                                                   ELSE interval '0' END)
  ), due_soon AS (
    SELECT DISTINCT ON (e.case_id) e.case_id, e.email, 'due_date_upcoming'::text AS reason, t.id AS task_id
    FROM eligible e JOIN cairn.case_tasks t ON t.case_id = e.case_id
    WHERE 'due_date_upcoming' = ANY (e.reasons)
      AND t.selected
      AND t.status IN ('not_started', 'check_on_this', 'in_progress', 'not_today')
      AND (t.snoozed_until IS NULL OR t.snoozed_until <= now())
      AND t.due_on BETWEEN e.today AND e.today + e.due_date_lead_days
      AND NOT EXISTS (SELECT 1 FROM cairn.notification_log l WHERE l.case_task_id = t.id)
    ORDER BY e.case_id, t.due_on, t.id
  ), idle AS (
    SELECT e.case_id, e.email, 'inactivity'::text AS reason, NULL::uuid AS task_id
    FROM eligible e
    WHERE 'inactivity' = ANY (e.reasons)
      AND e.last_activity_at <= now() - make_interval(days => e.inactivity_days)
      AND NOT EXISTS (SELECT 1 FROM cairn.notification_log l
                      WHERE l.case_id = e.case_id AND l.reason = 'inactivity' AND l.sent_at > e.last_activity_at)
  ), picked AS (
    -- A step coming up comes before inactivity.
    SELECT DISTINCT ON (x.case_id) x.case_id, x.email, x.reason, x.task_id
    FROM (SELECT * FROM due_soon UNION ALL SELECT * FROM idle) x
    ORDER BY x.case_id, x.reason = 'inactivity'
    LIMIT p_limit
  ), logged AS (
    INSERT INTO cairn.notification_log (case_id, case_task_id, reason, channel)
    SELECT picked.case_id, picked.task_id, picked.reason, 'email' FROM picked
    RETURNING notification_log.id, notification_log.case_id
  )
  SELECT logged.id, picked.reason, picked.email FROM logged JOIN picked ON picked.case_id = logged.case_id;
END
$$;

-- Replaces the 0008 version. The trial reminder is always shown in Cairn. It
-- goes out by email only when the user chose email for a journey
-- (UC-CASE-12 change, D-2026-09-25-N1). A reminder more than 48 hours
-- overdue is not sent late with a date that no longer fits.
-- [OPEN QUESTION card 50, Q1] whether the reminder follows notification choices.
CREATE OR REPLACE FUNCTION cairn.claim_due_trial_reminders(p_limit integer DEFAULT 100)
RETURNS TABLE (reminder_id uuid, kind text, email text, preferred_name text,
               trial_ends_at timestamptz, time_zone text)
LANGUAGE sql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
  WITH due AS (
    SELECT r.id FROM cairn.trial_reminders r JOIN cairn.users u ON u.id = r.user_id
    WHERE r.email_sent_at IS NULL AND r.due_at <= now() AND r.due_at > now() - interval '48 hours'
      AND u.status NOT IN ('subscribed', 'pending_deletion') AND now() < u.trial_ends_at
      AND EXISTS (SELECT 1 FROM cairn.cases c JOIN cairn.notification_preferences p ON p.case_id = c.id
                  WHERE c.created_by = u.id AND 'email' = ANY (p.channels))
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

-- ---------------------------------------------------------------- grants

GRANT SELECT ON cairn.notification_preferences TO cairn_app;
GRANT INSERT (case_id, channels, reasons, due_date_lead_days, inactivity_days, frequency, push_permission_granted)
  ON cairn.notification_preferences TO cairn_app;
GRANT UPDATE (channels, reasons, due_date_lead_days, inactivity_days, frequency, push_permission_granted)
  ON cairn.notification_preferences TO cairn_app;
GRANT SELECT ON cairn.notification_log TO cairn_app;

REVOKE ALL ON FUNCTION
  cairn.case_task_activity(),
  cairn.notification_choice_valid(text[], text[], integer, integer),
  cairn.action_confirmation_log_guard(),
  cairn.queue_action_confirmation(text, uuid),
  cairn.request_case_deletion(uuid, text),
  cairn.cancel_case_deletion(uuid),
  cairn.purge_held_cases(),
  cairn.claim_action_confirmations(integer, interval),
  cairn.complete_action_confirmation(uuid),
  cairn.release_action_confirmation(uuid, text),
  cairn.claim_due_notifications(integer)
FROM PUBLIC;

GRANT EXECUTE ON FUNCTION
  cairn.notification_choice_valid(text[], text[], integer, integer),
  cairn.request_case_deletion(uuid, text),
  cairn.cancel_case_deletion(uuid)
TO cairn_app;
