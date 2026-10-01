#!/usr/bin/env python3
"""Makes a MongoDB database match database/db/schema.py. Safe to run again.

  CAIRN_ADMIN_MONGODB_URI=mongodb+srv://admin@cluster.example/ python3 database/db/apply.py [--db cairn]

The URI must belong to a database administrator (dbAdmin and userAdmin on the
Cairn database, or Atlas's Atlas admin). Never the app, jobs, or loader user.
Never commit it.

In order:
1. Creates each collection with its validator, or updates the validator with collMod.
2. Creates the indexes. An index whose options changed is reported, not dropped.
3. Creates or updates the cairnApp, cairnJobs, and cairnLoader roles. Atlas
   doesn't allow createRole from a driver, so with --print-roles the roles are
   printed as Atlas Admin API bodies instead (see database/README.md).
4. Adds each app setting that doesn't exist yet. Existing values are kept.
5. Runs any data migration in MIGRATIONS that hasn't run, and records it in
   schema_migrations with its checksum.

The deployment must be a replica set (Atlas always is). The API runs every
request in one multi-document transaction.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import schema  # noqa: E402

# Data migrations, in order: (name, function taking the Database). Never edit one
# that has run anywhere. Add a new one instead. apply() refuses a changed checksum.
MIGRATIONS: list[tuple[str, object]] = []


def _client(uri: str):
    from pymongo import MongoClient
    return MongoClient(uri, uuidRepresentation="standard", tz_aware=True)


def apply_collections(db, log=print) -> None:
    existing = set(db.list_collection_names())
    for name, validator in schema.COLLECTIONS.items():
        if name in existing:
            db.command("collMod", name, validator=validator, validationLevel="strict", validationAction="error")
            log(f"update {name}")
        else:
            db.create_collection(name, validator=validator, validationLevel="strict", validationAction="error")
            log(f"create {name}")


def apply_indexes(db, log=print) -> None:
    for coll, keys, options in schema.INDEXES:
        current = db[coll].index_information().get(options["name"])
        if current is None:
            db[coll].create_index(keys, **options)
            log(f"index  {options['name']}")
        elif list(current["key"]) != [tuple(k) for k in keys] or bool(current.get("unique")) != bool(
                options.get("unique")):
            log(f"WARNING index {options['name']} differs from schema.py. Drop it by hand, then run again.")


def apply_roles(db, log=print) -> None:
    from pymongo.errors import OperationFailure
    for name in schema.ROLES:
        doc = schema.role_document(name, db.name)
        try:
            db.command("createRole", name, **doc)
            log(f"role   {name} created")
        except OperationFailure as exc:
            if exc.code != 51002:  # RoleAlreadyExists
                raise
            db.command("updateRole", name, **doc)
            log(f"role   {name} updated")


def atlas_roles(db_name: str) -> list[dict]:
    """The roles as Atlas Admin API customDBRoles bodies."""
    return [{"roleName": name, "actions": [
        {"action": action.upper(), "resources": [{"db": db_name, "collection": coll}]}
        for coll, actions in schema.ROLES[name].items() for action in actions]} for name in schema.ROLES]


def apply_settings(db, log=print) -> None:
    now = datetime.now(timezone.utc)
    for key, (value, description) in schema.SETTINGS.items():
        result = db.app_settings.update_one(
            {"_id": key}, {"$setOnInsert": {"value": value, "description": description, "updated_at": now}},
            upsert=True)
        if result.upserted_id is not None:
            log(f"setting {key} = {value}")


def apply_migrations(db, log=print) -> None:
    import hashlib
    import inspect
    for name, fn in MIGRATIONS:
        digest = hashlib.sha256(inspect.getsource(fn).encode()).hexdigest()
        row = db.schema_migrations.find_one({"_id": name})
        if row:
            if row["checksum"] != digest:
                raise SystemExit(f"ERROR: migration {name} was modified after it ran. Add a new migration instead.")
            continue
        fn(db)
        db.schema_migrations.insert_one({"_id": name, "checksum": digest, "applied_at": datetime.now(timezone.utc)})
        log(f"migrate {name}")
    db.schema_migrations.update_one(
        {"_id": f"schema_v{schema.SCHEMA_VERSION}"},
        {"$set": {"checksum": schema.checksum(), "applied_at": datetime.now(timezone.utc)}}, upsert=True)


def apply(db, *, roles: bool = True, log=print) -> None:
    hello = db.client.admin.command("hello")
    if not hello.get("setName") and hello.get("msg") != "isdbgrid":
        raise SystemExit("MongoDB must be a replica set (Atlas always is). Start mongod with --replSet.")
    apply_collections(db, log)
    apply_indexes(db, log)
    if roles:
        apply_roles(db, log)
    apply_settings(db, log)
    apply_migrations(db, log)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=os.environ.get("CAIRN_MONGODB_DB", "cairn"))
    ap.add_argument("--skip-roles", action="store_true", help="Leave roles alone (managed in Atlas).")
    ap.add_argument("--print-roles", action="store_true",
                    help="Print the roles as Atlas Admin API bodies and exit. Needs no connection.")
    args = ap.parse_args()
    if args.print_roles:
        import json
        print(json.dumps(atlas_roles(args.db), indent=2))
        return 0
    uri = os.environ.get("CAIRN_ADMIN_MONGODB_URI")
    if not uri:
        print("Set CAIRN_ADMIN_MONGODB_URI to an administrator connection string.", file=sys.stderr)
        return 2
    with _client(uri) as client:
        apply(client[args.db], roles=not args.skip_roles)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
