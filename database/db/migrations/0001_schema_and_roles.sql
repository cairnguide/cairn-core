-- 0001: schema, runtime roles, and baseline privileges.
--
-- Run migrations as an OWNER role that is NOT used by the running application.
-- Login roles, passwords, and network access are provisioned outside these
-- scripts (secrets manager or infrastructure code). Never put credentials here.
--
-- Roles created here are NOLOGIN group roles:
--   cairn_app     runtime application role. Subject to row-level security.
--   cairn_loader  deploy-time content loader. Can only add template versions.
-- Grant them to real login roles, for example:
--   GRANT cairn_app TO my_app_login_role;
--
-- Role-level settings such as search_path are not inherited through membership,
-- so application connections should run: SET search_path = cairn, pg_temp

CREATE SCHEMA IF NOT EXISTS cairn;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cairn_app') THEN
    CREATE ROLE cairn_app NOLOGIN NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cairn_loader') THEN
    CREATE ROLE cairn_loader NOLOGIN NOBYPASSRLS;
  END IF;
END
$$;

REVOKE ALL ON SCHEMA cairn FROM PUBLIC;
GRANT USAGE ON SCHEMA cairn TO cairn_app, cairn_loader;

-- Functions are not executable by PUBLIC unless explicitly granted.
ALTER DEFAULT PRIVILEGES IN SCHEMA cairn REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

-- Tighten the public schema where we are allowed to.
DO $$
BEGIN
  REVOKE CREATE ON SCHEMA public FROM PUBLIC;
EXCEPTION WHEN insufficient_privilege THEN
  RAISE NOTICE 'Could not revoke CREATE on schema public. Do this as the database owner.';
END
$$;

-- Migration tracking. No grants: only the owner role can read or write it.
CREATE TABLE IF NOT EXISTS cairn.schema_migrations (
  filename   text PRIMARY KEY,
  checksum   text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now()
);
