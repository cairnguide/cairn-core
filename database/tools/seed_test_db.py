"""Seed a local development database with test logins. Development only.

Creates the dev-only cairn_dev schema (db/dev/test_logins.sql) and two test
logins that POST /v1/dev/token accepts when the API runs with
CAIRN_DEV_AUTH_SECRET set. See database/README.md for the usernames and password.

  test.user   has an account already, at the start of onboarding. Signing in
              and calling POST /v1/registrations resumes it (200).
  new.user    has a login but no account. POST /v1/registrations creates the
              account (201), so the whole sign-up flow can be tried.

Run as the OWNER role, after the migrations:

  DATABASE_URL=postgresql://postgres:postgres@localhost:5432/cairn \\
    python3 database/tools/seed_test_db.py [--reset]

--reset deletes both test accounts and everything they created first, so
new.user can register again. The password is CAIRN_TEST_PASSWORD if set,
otherwise the documented default. The script refuses any database host other
than this machine unless --allow-remote is given. Never run it against real data.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import pathlib
import secrets
import sys
from urllib.parse import urlsplit

import psycopg

DEV_SQL = pathlib.Path(__file__).resolve().parents[1] / "db" / "dev" / "test_logins.sql"
DEFAULT_PASSWORD = "cairn-local-test-password"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
PBKDF2_ITERATIONS = 600_000

TEST_LOGINS = [
    {"username": "test.user", "email": "test.user@example.test",
     "idp_subject": "email|cairn-dev-test-user", "with_account": True},
    {"username": "new.user", "email": "new.user@example.test",
     "idp_subject": "email|cairn-dev-new-user", "with_account": False},
]


def hash_password(password: str) -> str:
    """The format verify_password in api/cairn_api/dev_auth.py reads."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def delete_account(conn: psycopg.Connection, idp_subject: str) -> bool:
    """Removes a test account and its cases, like cairn.delete_my_account but without the
    confirmation email or the Auth0 deletion request, since neither exists for a test login."""
    row = conn.execute("SELECT id FROM cairn.users WHERE idp_subject = %s", (idp_subject,)).fetchone()
    if row is None:
        return False
    uid = row[0]
    conn.execute("DELETE FROM cairn.cases WHERE created_by = %s", (uid,))
    conn.execute("DELETE FROM cairn.case_members WHERE user_id = %s", (uid,))
    conn.execute("DELETE FROM cairn.action_confirmation_outbox WHERE user_id = %s", (uid,))
    conn.execute("DELETE FROM cairn.users WHERE id = %s", (uid,))
    return True


def seed(owner_url: str, password: str, reset: bool = False) -> list[str]:
    """Applies the dev schema and writes the test logins. Returns one line per action taken."""
    done = []
    with psycopg.connect(owner_url) as conn:
        conn.execute(DEV_SQL.read_text())
        for login in TEST_LOGINS:
            if reset and delete_account(conn, login["idp_subject"]):
                done.append(f"deleted the {login['username']} account")
            conn.execute(
                "INSERT INTO cairn_dev.test_logins (username, password_hash, idp_subject, email) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (username) DO UPDATE SET "
                "password_hash = EXCLUDED.password_hash, idp_subject = EXCLUDED.idp_subject, email = EXCLUDED.email",
                (login["username"], hash_password(password), login["idp_subject"], login["email"]))
            exists = conn.execute("SELECT cairn.resolve_user(%s)", (login["idp_subject"],)).fetchone()[0]
            if login["with_account"] and not exists:
                conn.execute("SELECT cairn.create_account(%s, %s, 'email', NULL, 'America/New_York')",
                             (login["idp_subject"], login["email"]))
                done.append(f"created the {login['username']} account (onboarding not started)")
            elif not login["with_account"] and exists:
                done.append(f"{login['username']} has registered already. Run with --reset to register again")
            done.append(f"login {login['username']} ({login['email']}) is ready")
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reset", action="store_true", help="delete the test accounts first")
    parser.add_argument("--allow-remote", action="store_true",
                        help="allow a database host other than this machine (a scratch server only)")
    args = parser.parse_args()

    owner_url = os.environ.get("DATABASE_URL")
    if not owner_url:
        print("Set DATABASE_URL to the owner connection string.", file=sys.stderr)
        return 2
    host = urlsplit(owner_url).hostname or "localhost"
    if host not in LOCAL_HOSTS and not args.allow_remote:
        print(f"Refusing to add test logins on {host}. Test logins are for a local database only. "
              "Pass --allow-remote for a scratch server that holds no real data.", file=sys.stderr)
        return 2
    for line in seed(owner_url, os.environ.get("CAIRN_TEST_PASSWORD") or DEFAULT_PASSWORD, args.reset):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
