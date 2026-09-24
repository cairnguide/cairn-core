-- OPTIONAL, companion to context_items_jsonb.sql. Apply it after that file:
--   db/apply.sh context_items_jsonb context_items_read_only
--
-- Adds the read-only rule from migration 0008 (D-05) to context_items. 0008
-- does this itself when context_items already exists. On a fresh database the
-- optional table is created after 0008, so this file adds the policies then.
-- Safe in both cases.

DROP POLICY IF EXISTS context_items_insert_needs_writable_account ON cairn.context_items;
DROP POLICY IF EXISTS context_items_update_needs_writable_account ON cairn.context_items;

CREATE POLICY context_items_insert_needs_writable_account ON cairn.context_items AS RESTRICTIVE
  FOR INSERT TO cairn_app WITH CHECK (cairn.account_can_write());
CREATE POLICY context_items_update_needs_writable_account ON cairn.context_items AS RESTRICTIVE
  FOR UPDATE TO cairn_app USING (cairn.account_can_write()) WITH CHECK (cairn.account_can_write());
