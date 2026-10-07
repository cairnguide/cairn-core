"""Database access. One MongoDB transaction per request, as the calling user.

The API connects as a login user that holds only the cairnApp role (see
database/db/schema.py). Every request runs in one multi-document transaction
(snapshot reads, majority writes), so a request's changes land together or
not at all. The deployment must be a replica set. Atlas always is.

Who the caller is lives on the Session, not on the connection, so nothing
leaks between pooled connections. store.Session enforces the case boundary.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Iterator
from uuid import UUID

from pymongo import MongoClient, ReadPreference
from pymongo.errors import PyMongoError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from .errors import RuleViolation, map_db_error
from .store import Session, end_session, session_activity

__all__ = ["Database", "Session", "client_for"]


def client_for(uri: str, *, app_name: str, min_size: int = 0, max_size: int = 10) -> MongoClient:
    """A client with Cairn's conventions: standard UUIDs and timezone-aware UTC datetimes."""
    return MongoClient(uri, uuidRepresentation="standard", tz_aware=True, appname=app_name,
                       minPoolSize=min_size, maxPoolSize=max_size, connect=False)


class Database:
    def __init__(self, uri: str, db_name: str, min_size: int = 1, max_size: int = 10):
        self.client = client_for(uri, app_name="cairn-api", min_size=min_size, max_size=max_size)
        self.db = self.client[db_name]
        # Immutable content (template versions), shared across requests.
        self._cache: dict = {}

    def open(self) -> None:
        self.client.admin.command("ping")

    def close(self) -> None:
        self.client.close()

    def check_activity(self, idp_subject: str, issued_at: datetime | None, now: datetime, *, timeout: timedelta,
                       every: timedelta) -> bool:
        """D-20. Outside any request transaction, so recording activity never conflicts with one."""
        try:
            return session_activity(self.db, idp_subject, issued_at, now, timeout=timeout, every=every)
        except PyMongoError as exc:
            raise map_db_error(exc) from None

    def end_session(self, idp_subject: str, now: datetime, timeout: timedelta) -> None:
        """UC-REG-19 Sign out. Marks the session as quiet since before now - timeout, so the token that signed out
        stops working. A new sign-in issues a newer token, which works."""
        try:
            end_session(self.db, idp_subject, now - timeout - timedelta(seconds=1))
        except PyMongoError as exc:
            raise map_db_error(exc) from None

    @contextmanager
    def session(self, idp_subject: str | None = None, user_id: UUID | None = None) -> Iterator[Session]:
        """Open a transaction. Resolves the user from the IdP subject when given.

        Database errors and data rule refusals are translated into ApiError here, so no
        document values, and no validation details, ever reach a response or a log line.
        """
        try:
            with self.client.start_session(causal_consistency=True) as cs:
                with cs.start_transaction(read_concern=ReadConcern("snapshot"),
                                          write_concern=WriteConcern("majority"),
                                          read_preference=ReadPreference.PRIMARY):
                    s = Session(self.db, cs, cache=self._cache)
                    if user_id is None and idp_subject is not None:
                        user_id = s.resolve_user(idp_subject)
                    s.user_id = user_id
                    yield s
        except (PyMongoError, RuleViolation) as exc:
            raise map_db_error(exc) from None
