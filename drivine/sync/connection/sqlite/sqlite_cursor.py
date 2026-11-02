# Written by scripts/unasync.py from drivine/connection/sqlite/sqlite_cursor.py:
# change that, and run the script again.
from __future__ import annotations

import sqlite3
from typing import TypeVar

from drivine.connection.postgres.postgres_result_mapper import PostgresResultMapper
from drivine.connection.sqlite.sqlite_session import SqliteSession
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.sync import concurrency
from drivine.sync.cursor.buffered_cursor import BufferedCursor

T = TypeVar("T")


class SqliteCursor(BufferedCursor[T]):
    """Reads a batch at a time from a `sqlite3` cursor, each fetch on a thread of its own (see SqliteConnection)."""

    def __init__(
        self,
        spec: CursorSpecification[T],
        session: SqliteSession,
        statement: str,
        cursor: sqlite3.Cursor,
        result_mapper: PostgresResultMapper,
    ) -> None:
        super().__init__(spec.batch)
        self.spec = spec
        self.session = session
        self.statement = statement
        self.cursor = cursor
        self.result_mapper = result_mapper

    def read(self, count: int) -> tuple[list[T], bool]:
        rows = concurrency.blocking(self.session.fetch, self.statement, self.cursor, count, on=self.session.thread)
        return self.result_mapper.map_query_results(rows, self.spec), len(rows) < count

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            concurrency.blocking(self.cursor.close, on=self.session.thread)
