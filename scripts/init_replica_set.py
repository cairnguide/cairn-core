#!/usr/bin/env python3
"""Waits for a local MongoDB and makes it a single-node replica set. Development and CI only.

  python3 scripts/init_replica_set.py 'mongodb://admin:admin@localhost:27017/?replicaSet=rs0'

The API runs every request in a multi-document transaction, which needs a
replica set. Atlas is always one. A local mongod started with --replSet rs0
is one only after replSetInitiate, which this runs once. Safe to run again.
Never point it at a shared or production deployment.
"""
from __future__ import annotations

import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pymongo import MongoClient
from pymongo.errors import OperationFailure, PyMongoError

NOT_YET_INITIALIZED = 94


def main(uri: str, wait_seconds: int = 90) -> int:
    parts = urlsplit(uri)
    query = dict(parse_qsl(parts.query))
    replica_set = query.pop("replicaSet", "rs0")
    query["directConnection"] = "true"
    direct = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    member = parts.netloc.rsplit("@", 1)[-1]

    deadline = time.monotonic() + wait_seconds
    with MongoClient(direct, serverSelectionTimeoutMS=2000) as client:
        while True:
            try:
                client.admin.command("ping")
                break
            except PyMongoError:
                if time.monotonic() > deadline:
                    print("MongoDB didn't answer in time.", file=sys.stderr)
                    return 1
                time.sleep(1)
        try:
            client.admin.command("replSetGetStatus")
        except OperationFailure as exc:
            if exc.code != NOT_YET_INITIALIZED:
                raise
            client.admin.command("replSetInitiate", {"_id": replica_set, "members": [{"_id": 0, "host": member}]})
            print(f"initiated replica set {replica_set} on {member}")
        while not client.admin.command("hello").get("isWritablePrimary"):
            if time.monotonic() > deadline:
                print("The replica set has no primary yet.", file=sys.stderr)
                return 1
            time.sleep(1)
    print("MongoDB is ready")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
