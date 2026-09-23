-- 0007: sign-in methods (Google, Apple, or email) for account creation.
--
-- Identity is brokered by Auth0 (open question 6, resolved). Auth0 owns
-- passwords, passkeys, email confirmation, and the Google and Apple
-- federation. Cairn records only which method created the account, so a
-- person who returns with a different method can be told which one to use.

ALTER TABLE cairn.users
  ADD COLUMN sign_in_method text
    CHECK (sign_in_method IS NULL OR sign_in_method IN ('google', 'apple', 'email'));

COMMENT ON COLUMN cairn.users.sign_in_method IS
  'How the account was created. NULL only for accounts created before 0007.';

-- Replaces the 0005 version with one extra, optional parameter. Callers that
-- pass four or five arguments keep working. The method is set on first
-- registration only and never overwritten by a later sign-in.
DROP FUNCTION cairn.register_user(text, text, text, text, text);

CREATE FUNCTION cairn.register_user(
  p_idp_subject text, p_email text, p_first_name text, p_last_name text,
  p_phone text DEFAULT NULL, p_sign_in_method text DEFAULT NULL
) RETURNS uuid
LANGUAGE sql SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
  INSERT INTO cairn.users (idp_subject, email, first_name, last_name, phone, sign_in_method)
  VALUES (p_idp_subject, p_email, p_first_name, p_last_name, p_phone, p_sign_in_method)
  ON CONFLICT (idp_subject) DO UPDATE SET email = EXCLUDED.email
  RETURNING id
$$;

-- Which method already owns an email address, or NULL if none does.
-- The application calls this only with the verified email from the caller's
-- own token, so it answers "which method did I use" and not "does this other
-- person have an account". It returns the method only, never an id or a name.
CREATE FUNCTION cairn.sign_in_method_for_email(p_email text) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = cairn, pg_temp AS $$
  SELECT coalesce(u.sign_in_method, 'unknown')
  FROM cairn.users u
  WHERE lower(u.email) = lower(p_email)
$$;

REVOKE ALL ON FUNCTION cairn.register_user(text, text, text, text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION cairn.sign_in_method_for_email(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
  cairn.register_user(text, text, text, text, text, text),
  cairn.sign_in_method_for_email(text)
TO cairn_app;
