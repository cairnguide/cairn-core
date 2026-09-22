-- 0006: row-level security policies and least-privilege grants.
-- The case is the security boundary. Access is decided by case_members.

-- Start from nothing, then grant explicitly.
REVOKE ALL ON ALL TABLES IN SCHEMA cairn FROM PUBLIC, cairn_app, cairn_loader;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA cairn FROM PUBLIC;

-- ---------------------------------------------------------------- users
ALTER TABLE cairn.users ENABLE ROW LEVEL SECURITY;
CREATE POLICY users_select_self ON cairn.users FOR SELECT TO cairn_app
  USING (id = cairn.current_user_id());
CREATE POLICY users_update_self ON cairn.users FOR UPDATE TO cairn_app
  USING (id = cairn.current_user_id()) WITH CHECK (id = cairn.current_user_id());
GRANT SELECT ON cairn.users TO cairn_app;
GRANT UPDATE (first_name, last_name, phone) ON cairn.users TO cairn_app;

-- ---------------------------------------------------------------- cases
ALTER TABLE cairn.cases ENABLE ROW LEVEL SECURITY;
CREATE POLICY cases_select ON cairn.cases FOR SELECT TO cairn_app
  USING (created_by = cairn.current_user_id() OR cairn.is_case_member(id));
CREATE POLICY cases_insert ON cairn.cases FOR INSERT TO cairn_app
  WITH CHECK (created_by = cairn.current_user_id());
CREATE POLICY cases_update ON cairn.cases FOR UPDATE TO cairn_app
  USING (cairn.is_case_member(id, ARRAY['owner', 'co_executor']))
  WITH CHECK (cairn.is_case_member(id, ARRAY['owner', 'co_executor']));
CREATE POLICY cases_delete ON cairn.cases FOR DELETE TO cairn_app
  USING (cairn.is_case_member(id, ARRAY['owner']));
GRANT SELECT, DELETE ON cairn.cases TO cairn_app;
GRANT INSERT (created_by, journey_started_on, purge_after) ON cairn.cases TO cairn_app;
GRANT UPDATE (status, tasks_paused_until, purge_after) ON cairn.cases TO cairn_app;

-- ---------------------------------------------------------------- case_members
-- The first owner row can only be created by the case creator, for themselves,
-- and only while the case has no members. Updates and deletes are not granted at MVP.
ALTER TABLE cairn.case_members ENABLE ROW LEVEL SECURITY;
CREATE POLICY case_members_select ON cairn.case_members FOR SELECT TO cairn_app
  USING (user_id = cairn.current_user_id() OR cairn.is_case_member(case_id));
CREATE POLICY case_members_insert_first_owner ON cairn.case_members FOR INSERT TO cairn_app
  WITH CHECK (
    user_id = cairn.current_user_id()
    AND role = 'owner'
    AND status = 'active'
    AND cairn.is_case_creator(case_id)
    AND NOT cairn.case_has_members(case_id)
  );
GRANT SELECT ON cairn.case_members TO cairn_app;
GRANT INSERT (case_id, user_id, relationship, role, status) ON cairn.case_members TO cairn_app;

-- ---------------------------------------------------------------- deceased
ALTER TABLE cairn.deceased ENABLE ROW LEVEL SECURITY;
CREATE POLICY deceased_select ON cairn.deceased FOR SELECT TO cairn_app
  USING (cairn.is_case_member(case_id));
CREATE POLICY deceased_insert ON cairn.deceased FOR INSERT TO cairn_app
  WITH CHECK (cairn.is_case_member(case_id, ARRAY['owner', 'co_executor']));
CREATE POLICY deceased_update ON cairn.deceased FOR UPDATE TO cairn_app
  USING (cairn.is_case_member(case_id, ARRAY['owner', 'co_executor']))
  WITH CHECK (cairn.is_case_member(case_id, ARRAY['owner', 'co_executor']));
GRANT SELECT ON cairn.deceased TO cairn_app;
GRANT INSERT (case_id, legal_first_name, legal_middle_name, legal_last_name, date_of_birth,
              ssn_last4, domicile_state, veteran_status, has_will) ON cairn.deceased TO cairn_app;
GRANT UPDATE (legal_first_name, legal_middle_name, legal_last_name, date_of_birth,
              ssn_last4, domicile_state, veteran_status, has_will) ON cairn.deceased TO cairn_app;

-- ---------------------------------------------------------------- death_events
ALTER TABLE cairn.death_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY death_events_select ON cairn.death_events FOR SELECT TO cairn_app
  USING (cairn.is_case_member(cairn.case_of_deceased(deceased_id)));
CREATE POLICY death_events_insert ON cairn.death_events FOR INSERT TO cairn_app
  WITH CHECK (cairn.is_case_member(cairn.case_of_deceased(deceased_id), ARRAY['owner', 'co_executor']));
CREATE POLICY death_events_update ON cairn.death_events FOR UPDATE TO cairn_app
  USING (cairn.is_case_member(cairn.case_of_deceased(deceased_id), ARRAY['owner', 'co_executor']))
  WITH CHECK (cairn.is_case_member(cairn.case_of_deceased(deceased_id), ARRAY['owner', 'co_executor']));
GRANT SELECT ON cairn.death_events TO cairn_app;
GRANT INSERT (deceased_id, date_of_death, place_type, facility_name, city, county, death_state)
  ON cairn.death_events TO cairn_app;
GRANT UPDATE (date_of_death, place_type, facility_name, city, county, death_state)
  ON cairn.death_events TO cairn_app;

-- ---------------------------------------------------------------- case_tasks
ALTER TABLE cairn.case_tasks ENABLE ROW LEVEL SECURITY;
CREATE POLICY case_tasks_select ON cairn.case_tasks FOR SELECT TO cairn_app
  USING (cairn.is_case_member(case_id));
CREATE POLICY case_tasks_insert ON cairn.case_tasks FOR INSERT TO cairn_app
  WITH CHECK (cairn.is_case_member(case_id, ARRAY['owner', 'co_executor']));
CREATE POLICY case_tasks_update ON cairn.case_tasks FOR UPDATE TO cairn_app
  USING (cairn.is_case_member(case_id, ARRAY['owner', 'co_executor']))
  WITH CHECK (cairn.is_case_member(case_id, ARRAY['owner', 'co_executor']));
GRANT SELECT ON cairn.case_tasks TO cairn_app;
GRANT INSERT (case_id, template_id, status, due_on) ON cairn.case_tasks TO cairn_app;
GRANT UPDATE (status, due_on, snoozed_until, completed_at) ON cairn.case_tasks TO cairn_app;

-- ---------------------------------------------------------------- consents
ALTER TABLE cairn.consents ENABLE ROW LEVEL SECURITY;
CREATE POLICY consents_select_own ON cairn.consents FOR SELECT TO cairn_app
  USING (user_id = cairn.current_user_id());
CREATE POLICY consents_insert_own ON cairn.consents FOR INSERT TO cairn_app
  WITH CHECK (user_id = cairn.current_user_id());
CREATE POLICY consents_update_own ON cairn.consents FOR UPDATE TO cairn_app
  USING (user_id = cairn.current_user_id()) WITH CHECK (user_id = cairn.current_user_id());
GRANT SELECT ON cairn.consents TO cairn_app;
GRANT INSERT (user_id, purpose, policy_version) ON cairn.consents TO cairn_app;
GRANT UPDATE (withdrawn_at) ON cairn.consents TO cairn_app;

-- ---------------------------------------------------------------- audit_events
-- The application can write audit rows as itself and cannot read them back.
-- Note: INSERT ... RETURNING would need a SELECT policy, so do not use RETURNING here.
ALTER TABLE cairn.audit_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY audit_insert_as_self ON cairn.audit_events FOR INSERT TO cairn_app
  WITH CHECK (actor_id = cairn.current_user_id());
GRANT INSERT (actor_id, case_id, action, object_type, object_id) ON cairn.audit_events TO cairn_app;

-- ---------------------------------------------------------------- content tables
-- Reference content. Read-only for the app. The loader can add versions only.
GRANT SELECT ON cairn.task_templates, cairn.template_citations TO cairn_app;
GRANT SELECT, INSERT ON cairn.task_templates, cairn.template_citations TO cairn_loader;
GRANT UPDATE (active) ON cairn.task_templates TO cairn_loader;

-- ---------------------------------------------------------------- functions
-- The purge function is intentionally not granted to the application.
GRANT EXECUTE ON FUNCTION
  cairn.current_user_id(),
  cairn.is_case_member(uuid, text[]),
  cairn.is_case_creator(uuid),
  cairn.case_has_members(uuid),
  cairn.case_of_deceased(uuid),
  cairn.register_user(text, text, text, text, text),
  cairn.resolve_user(text),
  cairn.template_applies(jsonb, text, text, text, text),
  cairn.generate_case_tasks(uuid)
TO cairn_app;
