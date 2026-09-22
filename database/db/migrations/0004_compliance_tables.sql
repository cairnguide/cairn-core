-- 0004: consent ledger and append-only audit trail.

CREATE TABLE cairn.consents (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        uuid NOT NULL REFERENCES cairn.users (id) ON DELETE CASCADE,
  purpose        text NOT NULL CHECK (purpose IN ('terms', 'privacy', 'ai_processing')),
  policy_version text NOT NULL CHECK (length(policy_version) BETWEEN 1 AND 50),
  granted_at     timestamptz NOT NULL DEFAULT now(),
  withdrawn_at   timestamptz,
  CONSTRAINT withdrawn_after_granted CHECK (withdrawn_at IS NULL OR withdrawn_at >= granted_at)
);
CREATE INDEX consents_user_idx ON cairn.consents (user_id, purpose);

-- Audit rows hold opaque identifiers only and no PII.
-- actor_id and case_id intentionally have no foreign keys so that history survives
-- account deletion and case purges. actor_id is NULL for system actions.
-- Retention period is a policy decision. [LEGAL REVIEW REQUIRED]
CREATE TABLE cairn.audit_events (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  actor_id    uuid,
  case_id     uuid,
  action      text NOT NULL CHECK (length(action) BETWEEN 1 AND 100),
  object_type text CHECK (object_type IS NULL OR length(object_type) <= 100),
  object_id   uuid,
  occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_events_case_idx ON cairn.audit_events (case_id, occurred_at);
CREATE INDEX audit_events_actor_idx ON cairn.audit_events (actor_id, occurred_at);

CREATE FUNCTION cairn.audit_events_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit_events is append-only'
    USING ERRCODE = 'restrict_violation';
END
$$;

CREATE TRIGGER audit_events_append_only BEFORE UPDATE OR DELETE ON cairn.audit_events
  FOR EACH ROW EXECUTE FUNCTION cairn.audit_events_guard();
CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON cairn.audit_events
  FOR EACH STATEMENT EXECUTE FUNCTION cairn.forbid_truncate();
