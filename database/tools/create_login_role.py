"""Create or update the login role the API connects as, and make it a member of cairn_app.

Migrations only create NOLOGIN group roles (0001). The login role, its password,
and network access are provisioned per environment. This does that for a new
environment, as the OWNER role, after the migrations have run.

  DATABASE_URL=postgresql://owner@host/cairn \\
  CAIRN_LOGIN_PASSWORD=... python3 database/tools/create_login_role.py [--role cairn_api_login]

The password comes from CAIRN_LOGIN_PASSWORD, never the command line, so it
doesn't show up in the process list or shell history. With --generate it makes
a random one and prints the application connection string instead.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from psycopg import sql


def app_url(owner_url: str, role: str, password: str) -> str:
    """The owner connection string with the login role's credentials in place of the owner's."""
    parts = urlsplit(owner_url)
    host = parts.hostname or "localhost"
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{quote(role, safe='')}:{quote(password, safe='')}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def provision(owner_url: str, role: str, password: str, group: str = "cairn_app") -> None:
    with psycopg.connect(owner_url, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        # Password is sent as a literal in the statement. Nothing here logs statements.
        conn.execute(sql.SQL(verb + " ROLE {} WITH LOGIN NOBYPASSRLS PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(password)))
        conn.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(group), sql.Identifier(role)))
        db = conn.execute("SELECT current_database()").fetchone()[0]
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(db), sql.Identifier(role)))
        print(f"{verb.lower()}d role {role}, member of {group}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--role", default="cairn_api_login")
    parser.add_argument("--group", default="cairn_app", help="cairn_app for the API, cairn_loader for the loader")
    parser.add_argument("--generate", action="store_true", help="make a random password and print the app URL")
    args = parser.parse_args()

    owner_url = os.environ.get("DATABASE_URL")
    if not owner_url:
        print("Set DATABASE_URL to the owner connection string.", file=sys.stderr)
        return 2
    password = secrets.token_urlsafe(24) if args.generate else os.environ.get("CAIRN_LOGIN_PASSWORD")
    if not password:
        print("Set CAIRN_LOGIN_PASSWORD, or pass --generate.", file=sys.stderr)
        return 2
    provision(owner_url, args.role, password, args.group)
    if args.generate:
        print(app_url(owner_url, args.role, password))
    return 0


if __name__ == "__main__":
    sys.exit(main())
