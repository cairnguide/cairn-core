-- Development only: test logins for POST /v1/dev/token (api/cairn_api/dev_auth.py).
--
-- This is not a migration. apply.sh never reads this folder, so a production
-- database has no cairn_dev schema and no test logins. tools/seed_test_db.py
-- applies it, as the owner, to a local or Codespaces database. Safe to run again.
--
-- Cairn never stores real passwords (Auth0 owns sign-in). These are fake
-- credentials for fake accounts, and the check below keeps them on example.test.
CREATE SCHEMA IF NOT EXISTS cairn_dev;
REVOKE ALL ON SCHEMA cairn_dev FROM PUBLIC;

CREATE TABLE IF NOT EXISTS cairn_dev.test_logins (
  username         text PRIMARY KEY CHECK (username = lower(username)),
  password_hash    text NOT NULL,   -- pbkdf2_sha256$<iterations>$<salt>$<hash>, never the password
  idp_subject      text NOT NULL UNIQUE,
  email            text NOT NULL UNIQUE CHECK (email LIKE '%@example.test'),
  sign_in_strategy text NOT NULL DEFAULT 'email'
                   CHECK (sign_in_strategy IN ('google-oauth2', 'apple', 'auth0', 'email'))
);
REVOKE ALL ON cairn_dev.test_logins FROM PUBLIC;

-- The app role can look up one login by name and nothing else. It has no grant on the table.
CREATE OR REPLACE FUNCTION cairn_dev.test_login(p_username text)
RETURNS TABLE (password_hash text, idp_subject text, email text, sign_in_strategy text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = cairn_dev, pg_temp AS $$
  SELECT t.password_hash, t.idp_subject, t.email, t.sign_in_strategy
  FROM cairn_dev.test_logins t
  WHERE t.username = lower(p_username)
$$;
REVOKE ALL ON FUNCTION cairn_dev.test_login(text) FROM PUBLIC;
GRANT USAGE ON SCHEMA cairn_dev TO cairn_app;
GRANT EXECUTE ON FUNCTION cairn_dev.test_login(text) TO cairn_app;
