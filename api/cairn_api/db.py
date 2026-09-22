"""Database access. One transaction per request, scoped to the calling user.

Every transaction sets app.user_id with the transaction-local form of
set_config, so an unset user matches no rows and nothing leaks between pooled
connections. search_path is set once per connection because role-level
settings are not inherited through membership.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .errors import ApiError, map_db_error


class Database:
    def __init__(self, url: str, session_role: str | None, min_size: int = 1, max_size: int = 10):
        self._session_role = session_role
        self.pool = ConnectionPool(
            url,
            min_size=min_size,
            max_size=max_size,
            configure=self._configure,
            kwargs={"row_factory": dict_row},
            open=False,
        )

    def _configure(self, conn: psycopg.Connection) -> None:
        conn.autocommit = True
        conn.execute("SET search_path = cairn, pg_temp")
        if self._session_role:
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(self._session_role)))

    def open(self) -> None:
        self.pool.open(wait=True)

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def session(self, idp_subject: str | None = None, user_id: UUID | None = None) -> Iterator[Session]:
        """Open a transaction. Resolves the user from the IdP subject when given.

        Database errors are translated into ApiError here so no SQL text, and no
        failing row values, ever reach a response or a log line.
        """
        try:
            with self.pool.connection() as conn:
                with conn.transaction():
                    if user_id is None and idp_subject is not None:
                        row = conn.execute("SELECT cairn.resolve_user(%s) AS id", (idp_subject,)).fetchone()
                        user_id = row["id"] if row else None
                    if user_id is not None:
                        conn.execute("SELECT set_config('app.user_id', %s, true)", (str(user_id),))
                    yield Session(conn, user_id)
        except psycopg.Error as exc:
            raise map_db_error(exc) from None


@dataclass
class Session:
    conn: psycopg.Connection
    user_id: UUID | None

    def require_user(self) -> UUID:
        if self.user_id is None:
            raise ApiError(403, "registration_required", "Please finish creating your account first.")
        return self.user_id

    def one(self, query: str, params: tuple | dict = ()) -> dict | None:
        return self.conn.execute(query, params).fetchone()

    def all(self, query: str, params: tuple | dict = ()) -> list[dict]:
        return self.conn.execute(query, params).fetchall()

    def audit(self, action: str, case_id: UUID | None = None,
              object_type: str | None = None, object_id: UUID | None = None) -> None:
        """Append an audit row as the acting user, in the current transaction.

        No RETURNING clause. The app role cannot read audit rows, by design.
        Audit rows hold opaque ids only, never names or free text.
        """
        self.conn.execute(
            "INSERT INTO cairn.audit_events (actor_id, case_id, action, object_type, object_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (self.require_user(), case_id, action, object_type, object_id),
        )
