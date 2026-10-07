"""Seed a local development database with test logins. Development only.

Creates the dev-only cairn_dev database and two test logins that
POST /v1/dev/token accepts when the API runs with CAIRN_DEV_AUTH_SECRET set.
See database/README.md for the usernames and password.

  test.user   has an account already, at the start of onboarding. Signing in
              and calling POST /v1/registrations resumes it (200).
  new.user    has a login but no account. POST /v1/registrations creates the
              account (201), so the whole sign-up flow can be tried.

cairn_dev is a separate database (the Cairn database's name plus "_dev"), so
apply.py and schema.py never create it, and a production deployment has no
test logins. The API's login user gets one extra role, cairnDevTestLogins,
that can read cairn_dev.test_logins and nothing else. create_login_user.py
removes it again whenever it resets that user, and setup runs this script
afterwards.

Run as an administrator, after apply.py and create_login_user.py:

  CAIRN_ADMIN_MONGODB_URI='mongodb://admin:admin@localhost:27017/?replicaSet=rs0' \\
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

DEFAULT_PASSWORD = "cairn-local-test-password"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
PBKDF2_ITERATIONS = 600_000
ROLE = "cairnDevTestLogins"

TEST_LOGINS = [
    {"username": "test.user", "email": "test.user@example.test",
     "idp_subject": "email|cairn-dev-test-user", "with_account": True},
    {"username": "new.user", "email": "new.user@example.test",
     "idp_subject": "email|cairn-dev-new-user", "with_account": False},
]

# Cairn never stores real passwords (Auth0 owns sign-in). These are fake credentials for fake
# accounts, and the validator keeps them on example.test.
TEST_LOGIN_SCHEMA = {"$jsonSchema": {
    "bsonType": "object", "additionalProperties": False,
    "required": ["_id", "password_hash", "idp_subject", "email", "sign_in_strategy"],
    "properties": {
        "_id": {"bsonType": "string", "pattern": "^[a-z0-9._-]+$"},  # the username, lowercase
        "password_hash": {"bsonType": "string", "pattern": r"^pbkdf2_sha256\$"},  # never the password
        "idp_subject": {"bsonType": "string"},
        "email": {"bsonType": "string", "pattern": r"@example\.test$"},
        "sign_in_strategy": {"enum": ["google-oauth2", "apple", "auth0", "email"]},
    },
}}


def dev_db_name(db_name: str) -> str:
    return f"{db_name}_dev"


def hash_password(password: str) -> str:
    """The format verify_password in api/cairn_api/dev_auth.py reads."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def _store():
    try:
        from cairn_api import store
    except ImportError:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "api"))
        from cairn_api import store
    return store


def _apply_dev_database(client, db_name: str, api_user: str) -> None:
    dev = client[dev_db_name(db_name)]
    if "test_logins" in dev.list_collection_names():
        dev.command("collMod", "test_logins", validator=TEST_LOGIN_SCHEMA, validationLevel="strict")
    else:
        dev.create_collection("test_logins", validator=TEST_LOGIN_SCHEMA, validationLevel="strict")
    dev.test_logins.create_index("idp_subject", unique=True, name="test_logins_subject_uq")
    dev.test_logins.create_index("email", unique=True, name="test_logins_email_uq")
    privileges = [{"resource": {"db": dev.name, "collection": "test_logins"}, "actions": ["find"]}]
    if dev.command("rolesInfo", ROLE)["roles"]:
        dev.command("updateRole", ROLE, privileges=privileges, roles=[])
    else:
        dev.command("createRole", ROLE, privileges=privileges, roles=[])
    client[db_name].command("grantRolesToUser", api_user, roles=[{"role": ROLE, "db": dev.name}])


def delete_account(db, cs, idp_subject: str) -> bool:
    """Removes a test account and its cases, like Session.delete_my_account but without the
    confirmation email or the Auth0 deletion request, since neither exists for a test login."""
    user = db.users.find_one({"idp_subject": idp_subject}, {"_id": 1}, session=cs)
    if user is None:
        return False
    uid = user["_id"]
    for case in db.cases.find({"created_by": uid}, {"_id": 1}, session=cs):
        _store().delete_case(db, cs, case["_id"])
    db.cases.update_many({"members.user_id": uid}, {"$pull": {"members": {"user_id": uid}}}, session=cs)
    for name in ("action_confirmation_outbox", "consents", "trial_reminders"):
        db[name].delete_many({"user_id": uid}, session=cs)
    db.notification_preferences.delete_one({"_id": uid}, session=cs)
    db.users.delete_one({"_id": uid}, session=cs)
    return True


def seed(admin_uri: str, db_name: str, password: str, reset: bool = False,
         api_user: str = "cairn_api") -> list[str]:
    """Creates the dev database and writes the test logins. Returns one line per action taken."""
    from pymongo import MongoClient
    store = _store()
    done = []
    with MongoClient(admin_uri, uuidRepresentation="standard", tz_aware=True) as client:
        _apply_dev_database(client, db_name, api_user)
        logins, db = client[dev_db_name(db_name)].test_logins, client[db_name]
        for login in TEST_LOGINS:
            logins.replace_one({"_id": login["username"]}, {
                "password_hash": hash_password(password), "idp_subject": login["idp_subject"],
                "email": login["email"], "sign_in_strategy": "email"}, upsert=True)
            with client.start_session() as cs, cs.start_transaction():
                if reset and delete_account(db, cs, login["idp_subject"]):
                    done.append(f"deleted the {login['username']} account")
                s = store.Session(db, cs, cache={})
                exists = s.resolve_user(login["idp_subject"]) is not None
                if login["with_account"] and not exists:
                    s.create_account(login["idp_subject"], login["email"], "email", time_zone="America/New_York")
                    done.append(f"created the {login['username']} account (onboarding not started)")
                elif not login["with_account"] and exists:
                    done.append(f"{login['username']} has registered already. Run with --reset to register again")
            done.append(f"login {login['username']} ({login['email']}) is ready")
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reset", action="store_true", help="delete the test accounts first")
    parser.add_argument("--api-user", default="cairn_api", help="the API's login user, which may read the logins")
    parser.add_argument("--db", default=os.environ.get("CAIRN_MONGODB_DB", "cairn"))
    parser.add_argument("--allow-remote", action="store_true",
                        help="allow a database host other than this machine (a scratch server only)")
    args = parser.parse_args()

    admin_uri = os.environ.get("CAIRN_ADMIN_MONGODB_URI")
    if not admin_uri:
        print("Set CAIRN_ADMIN_MONGODB_URI to an administrator connection string.", file=sys.stderr)
        return 2
    hosts = {h.rsplit(":", 1)[0].strip("[]") or "localhost"
             for h in urlsplit(admin_uri).netloc.rsplit("@", 1)[-1].split(",")}
    if urlsplit(admin_uri).scheme != "mongodb" or not hosts <= LOCAL_HOSTS:
        if not args.allow_remote:
            print(f"Refusing to add test logins on {', '.join(sorted(hosts))}. Test logins are for a local "
                  "database only. Pass --allow-remote for a scratch server that holds no real data.",
                  file=sys.stderr)
            return 2
    for line in seed(admin_uri, args.db, os.environ.get("CAIRN_TEST_PASSWORD") or DEFAULT_PASSWORD, args.reset,
                     args.api_user):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
