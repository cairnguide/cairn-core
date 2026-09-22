-- 0005: helper functions used by row-level security policies and by the app.
--
-- SECURITY DEFINER functions run with the privileges of the owner role that ran
-- this migration. Row-level security is ENABLED (not FORCED) on tables, so the
-- owner bypasses it inside these functions. This is what avoids policy recursion.
-- Consequence: never connect the application as the owner role.

-- The application sets the current user once per transaction:
--   SELECT set_config('app.user_id', '<uuid>', true);
-- An unset or empty value yields NULL, which matches no rows (fail closed).
CREATE FUNCTION cairn.current_user_id() RETURNS uuid
LANGUAGE sql STABLE AS $$
  SELECT nullif(current_setting('app.user_id', true), '')::uuid
$$;

CREATE FUNCTION cairn.is_case_member(p_case uuid, p_roles text[] DEFAULT NULL)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
  SELECT EXISTS (
    SELECT 1 FROM cairn.case_members m
    WHERE m.case_id = p_case
      AND m.user_id = cairn.current_user_id()
      AND m.status = 'active'
      AND (p_roles IS NULL OR m.role = ANY (p_roles))
  )
$$;

CREATE FUNCTION cairn.is_case_creator(p_case uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
  SELECT EXISTS (
    SELECT 1 FROM cairn.cases c
    WHERE c.id = p_case AND c.created_by = cairn.current_user_id()
  )
$$;

CREATE FUNCTION cairn.case_has_members(p_case uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM cairn.case_members m WHERE m.case_id = p_case)
$$;

CREATE FUNCTION cairn.case_of_deceased(p_deceased uuid) RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
  SELECT d.case_id FROM cairn.deceased d WHERE d.id = p_deceased
$$;

-- Sign-up and sign-in bootstrap. These run before the user id is known, so they
-- are the only way the application can create or look up a user.
CREATE FUNCTION cairn.register_user(
  p_idp_subject text, p_email text, p_first_name text, p_last_name text,
  p_phone text DEFAULT NULL
) RETURNS uuid
LANGUAGE sql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
  INSERT INTO cairn.users (idp_subject, email, first_name, last_name, phone)
  VALUES (p_idp_subject, p_email, p_first_name, p_last_name, p_phone)
  ON CONFLICT (idp_subject) DO UPDATE SET email = EXCLUDED.email
  RETURNING id
$$;

CREATE FUNCTION cairn.resolve_user(p_idp_subject text) RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
  SELECT id FROM cairn.users WHERE idp_subject = p_idp_subject
$$;

-- Rule matching for applies_when. Semantics:
--   {"always": true}             applies to every case
--   otherwise every present key must match (AND), and each key holds a list of
--   accepted values (OR). Keys: veteran_status, has_will, death_state, domicile_state.
CREATE FUNCTION cairn.template_applies(
  p_rules jsonb, p_veteran text, p_will text, p_death_state text, p_domicile_state text
) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN jsonb_typeof(p_rules) IS DISTINCT FROM 'object' THEN false
    WHEN COALESCE((p_rules ->> 'always')::boolean, false) THEN true
    ELSE
      (NOT (p_rules ? 'veteran_status')  OR COALESCE((p_rules -> 'veteran_status')  ? p_veteran, false))
      AND (NOT (p_rules ? 'has_will')    OR COALESCE((p_rules -> 'has_will')        ? p_will, false))
      AND (NOT (p_rules ? 'death_state') OR COALESCE((p_rules -> 'death_state')     ? p_death_state, false))
      AND (NOT (p_rules ? 'domicile_state') OR COALESCE((p_rules -> 'domicile_state') ? p_domicile_state, false))
  END
$$;

-- Creates one case_task per matching template for a case, using the latest active
-- version of each task_key. SECURITY INVOKER on purpose: row-level security still
-- applies, so only a case owner can generate tasks. Safe to call again.
-- Returns the number of tasks created.
CREATE FUNCTION cairn.generate_case_tasks(p_case uuid) RETURNS integer
LANGUAGE plpgsql SECURITY INVOKER SET search_path = cairn, pg_temp AS $$
DECLARE
  n integer;
BEGIN
  -- death_state is required for jurisdiction matching, so a deceased row that
  -- has not completed the UC-6 death-event step yields zero tasks rather than
  -- an error. Call this again once death_state is set.
  INSERT INTO cairn.case_tasks (case_id, template_id, status, due_on)
  SELECT c.id, t.id, 'not_started', c.journey_started_on + t.due_offset_days
  FROM cairn.cases c
  JOIN cairn.deceased d ON d.case_id = c.id
  JOIN LATERAL (
    SELECT DISTINCT ON (tt.task_key) tt.*
    FROM cairn.task_templates tt
    WHERE tt.active
    ORDER BY tt.task_key, tt.version DESC
  ) t ON true
  WHERE c.id = p_case
    AND d.death_state IS NOT NULL
    AND (t.jurisdiction = 'US' OR t.jurisdiction IN (d.death_state, d.domicile_state))
    AND cairn.template_applies(t.applies_when, d.veteran_status, d.has_will,
                               d.death_state, d.domicile_state)
  ON CONFLICT (case_id, template_id) DO NOTHING;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END
$$;

-- Retention enforcement. Deletes cases past purge_after. Cascades remove the
-- case's members, deceased, death event, and tasks. Audit rows are kept.
-- Not granted to the application. Run it from a scheduled job as the owner role.
CREATE FUNCTION cairn.purge_expired_cases() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
DECLARE
  r record;
  n integer := 0;
BEGIN
  FOR r IN SELECT id FROM cairn.cases WHERE purge_after IS NOT NULL AND purge_after < now() LOOP
    DELETE FROM cairn.cases WHERE id = r.id;
    INSERT INTO cairn.audit_events (actor_id, case_id, action, object_type, object_id)
    VALUES (NULL, r.id, 'case_purged', 'case', r.id);
    n := n + 1;
  END LOOP;
  RETURN n;
END
$$;
