-- 0003: journey tables. Templates and citations are loaded from the content
-- repository at deploy time and are immutable per version.

CREATE FUNCTION cairn.forbid_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'TRUNCATE is not allowed on %', TG_TABLE_NAME
    USING ERRCODE = 'restrict_violation';
END
$$;

CREATE TABLE cairn.task_templates (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_key                text NOT NULL CHECK (task_key ~ '^[a-z][a-z0-9_]{2,63}$'),
  version                 integer NOT NULL CHECK (version >= 1),
  title                   text NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
  plain_summary           text NOT NULL CHECK (length(plain_summary) BETWEEN 1 AND 600),
  journey_week            integer NOT NULL CHECK (journey_week BETWEEN 1 AND 4),
  sort_order              integer NOT NULL DEFAULT 0,
  due_offset_days         integer NOT NULL CHECK (due_offset_days BETWEEN 0 AND 90),
  jurisdiction            text NOT NULL CHECK (jurisdiction ~ '^(US|[A-Z]{2})$'),
  applies_when            jsonb NOT NULL DEFAULT '{"always": true}'::jsonb
                            CHECK (jsonb_typeof(applies_when) = 'object'),
  attorney_referral       boolean NOT NULL DEFAULT false,
  attorney_referral_note  text,
  counsel_reviewed_at     timestamptz,
  content_hash            text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
  git_release             text NOT NULL CHECK (length(git_release) BETWEEN 1 AND 200),
  active                  boolean NOT NULL DEFAULT true,
  loaded_at               timestamptz NOT NULL DEFAULT now(),
  UNIQUE (task_key, version),
  CONSTRAINT attorney_referral_needs_note
    CHECK (NOT attorney_referral OR attorney_referral_note IS NOT NULL)
);
CREATE INDEX task_templates_active_idx ON cairn.task_templates (task_key, version DESC) WHERE active;

CREATE TABLE cairn.template_citations (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  template_id      uuid NOT NULL REFERENCES cairn.task_templates (id) ON DELETE RESTRICT,
  authority_name   text NOT NULL CHECK (length(authority_name) BETWEEN 1 AND 200),
  url              text NOT NULL CHECK (url ~ '^https://'),
  jurisdiction     text NOT NULL CHECK (jurisdiction ~ '^(US|[A-Z]{2})$'),
  last_verified_on date
);
CREATE INDEX template_citations_template_idx ON cairn.template_citations (template_id);

CREATE TABLE cairn.case_tasks (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  case_id       uuid NOT NULL REFERENCES cairn.cases (id) ON DELETE CASCADE,
  -- Pinned to one immutable template version, which records what the family was shown.
  template_id   uuid NOT NULL REFERENCES cairn.task_templates (id) ON DELETE RESTRICT,
  status        text NOT NULL DEFAULT 'not_started'
                  CHECK (status IN ('not_started', 'in_progress', 'done', 'skipped', 'not_applicable')),
  due_on        date,
  snoozed_until timestamptz,
  completed_at  timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (case_id, template_id),
  CONSTRAINT done_needs_completed_at CHECK (status <> 'done' OR completed_at IS NOT NULL)
);
CREATE INDEX case_tasks_case_status_idx ON cairn.case_tasks (case_id, status);
-- Supports "which families are still on an old template version" queries.
CREATE INDEX case_tasks_template_idx ON cairn.case_tasks (template_id);

CREATE TRIGGER case_tasks_touch BEFORE UPDATE ON cairn.case_tasks
  FOR EACH ROW EXECUTE FUNCTION cairn.touch_updated_at();

-- Template rows are immutable except for the active flag. Corrections are new versions.
CREATE FUNCTION cairn.task_templates_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'task_templates rows are immutable and cannot be deleted'
      USING ERRCODE = 'restrict_violation';
  END IF;
  IF (to_jsonb(OLD) - 'active') IS DISTINCT FROM (to_jsonb(NEW) - 'active') THEN
    RAISE EXCEPTION 'task_templates rows are immutable. Publish a new version instead.'
      USING ERRCODE = 'restrict_violation';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER task_templates_immutable BEFORE UPDATE OR DELETE ON cairn.task_templates
  FOR EACH ROW EXECUTE FUNCTION cairn.task_templates_guard();

CREATE FUNCTION cairn.template_citations_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'template_citations rows are immutable'
    USING ERRCODE = 'restrict_violation';
END
$$;

CREATE TRIGGER template_citations_immutable BEFORE UPDATE OR DELETE ON cairn.template_citations
  FOR EACH ROW EXECUTE FUNCTION cairn.template_citations_guard();

CREATE TRIGGER task_templates_no_truncate BEFORE TRUNCATE ON cairn.task_templates
  FOR EACH STATEMENT EXECUTE FUNCTION cairn.forbid_truncate();
CREATE TRIGGER template_citations_no_truncate BEFORE TRUNCATE ON cairn.template_citations
  FOR EACH STATEMENT EXECUTE FUNCTION cairn.forbid_truncate();
