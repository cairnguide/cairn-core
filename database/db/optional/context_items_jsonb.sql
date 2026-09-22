-- OPTIONAL: context_items as a Postgres JSONB table.
--
-- Use this only if the MVP keeps AI-facing case context in Postgres instead of a
-- separate document store. It follows the CONTEXT_ITEM entity in the data model.
-- Apply after migration 0006:  db/apply.sh context_items_jsonb
--
-- Payload rule: no direct identifiers (no names, SSNs, or account numbers).
-- Reference the person through case_id only.

CREATE TABLE cairn.context_items (
  case_id    uuid NOT NULL REFERENCES cairn.cases (id) ON DELETE CASCADE,
  item_key   text NOT NULL
               CHECK (item_key IN ('CERT_ORDER', 'FUNERAL', 'BANK_NOTICES', 'CONVO_SUMMARY')),
  payload    jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'
                                   AND pg_column_size(payload) <= 32768),
  updated_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz,
  PRIMARY KEY (case_id, item_key)
);

CREATE TRIGGER context_items_touch BEFORE UPDATE ON cairn.context_items
  FOR EACH ROW EXECUTE FUNCTION cairn.touch_updated_at();

ALTER TABLE cairn.context_items ENABLE ROW LEVEL SECURITY;
CREATE POLICY context_items_select ON cairn.context_items FOR SELECT TO cairn_app
  USING (cairn.is_case_member(case_id));
CREATE POLICY context_items_insert ON cairn.context_items FOR INSERT TO cairn_app
  WITH CHECK (cairn.is_case_member(case_id, ARRAY['owner', 'co_executor']));
CREATE POLICY context_items_update ON cairn.context_items FOR UPDATE TO cairn_app
  USING (cairn.is_case_member(case_id, ARRAY['owner', 'co_executor']))
  WITH CHECK (cairn.is_case_member(case_id, ARRAY['owner', 'co_executor']));
GRANT SELECT ON cairn.context_items TO cairn_app;
GRANT INSERT (case_id, item_key, payload, expires_at) ON cairn.context_items TO cairn_app;
GRANT UPDATE (payload, expires_at) ON cairn.context_items TO cairn_app;
