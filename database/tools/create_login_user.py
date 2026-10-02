"""Create or update a MongoDB login user that holds exactly one Cairn role.

database/db/apply.py creates the roles (cairnApp, cairnJobs, cairnLoader). The
login users, their passwords, and network access are provisioned per
environment. This does that for a self-managed deployment (development, CI),
as an administrator, after apply.py has run.

  CAIRN_ADMIN_MONGODB_URI=mongodb://admin@host/?replicaSet=rs0 \\
  CAIRN_LOGIN_PASSWORD=... python3 database/tools/create_login_user.py --user cairn_api --role cairnApp

The password comes from CAIRN_LOGIN_PASSWORD, never the command line, so it
doesn't show up in the process list or shell history. With --generate it makes
a random one and prints the user's connection string instead.

On Atlas, create database users in the Atlas UI, the Atlas CLI
(atlas dbusers create), or the Admin API instead, each with one custom role.
Atlas doesn't allow createUser from a driver. See database/README.md.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

ROLES = ("cairnApp", "cairnJobs", "cairnLoader")


def user_uri(admin_uri: str, user: str, password: str, db: str) -> str:
    """The administrator's connection string with this user's credentials, authenticating against db."""
    parts = urlsplit(admin_uri)
    host = parts.netloc.rsplit("@", 1)[-1]
    query = dict(parse_qsl(parts.query))
    query["authSource"] = db
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}"
    return urlunsplit((parts.scheme, netloc, f"/{db}", urlencode(query), parts.fragment))


def provision(admin_uri: str, db_name: str, user: str, password: str, role: str) -> None:
    from pymongo import MongoClient
    with MongoClient(admin_uri) as client:
        db = client[db_name]
        exists = db.command("usersInfo", user)["users"]
        if exists:
            db.command("updateUser", user, pwd=password, roles=[role])
        else:
            db.command("createUser", user, pwd=password, roles=[role])
        print(f"{'updated' if exists else 'created'} user {user} with role {role} on {db_name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--user", default="cairn_api")
    parser.add_argument("--role", default="cairnApp", choices=ROLES)
    parser.add_argument("--db", default=os.environ.get("CAIRN_MONGODB_DB", "cairn"))
    parser.add_argument("--generate", action="store_true", help="make a random password and print the user's URI")
    args = parser.parse_args()

    admin_uri = os.environ.get("CAIRN_ADMIN_MONGODB_URI")
    if not admin_uri:
        print("Set CAIRN_ADMIN_MONGODB_URI to an administrator connection string.", file=sys.stderr)
        return 2
    password = secrets.token_urlsafe(24) if args.generate else os.environ.get("CAIRN_LOGIN_PASSWORD")
    if not password:
        print("Set CAIRN_LOGIN_PASSWORD, or pass --generate.", file=sys.stderr)
        return 2
    provision(admin_uri, args.db, args.user, password, args.role)
    if args.generate:
        print(user_uri(admin_uri, args.user, password, args.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())
