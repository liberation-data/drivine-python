# Written by scripts/unasync.py from drivine/connection/sqlite/sqlite_cursor.py:
# change that, and run the script again.
from __future__ import annotations

from typing import TypeVar

from drivine.connection.postgres.postgres_result_mapper import PostgresResultMapper
from drivine.connection.sqlite.sqlite_session import Rows, SqliteSession
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.cypher.cypher_specification import RESULT
from drivine.sync import concurrency
from drivine.sync.cursor.buffered_cursor import BufferedCursor

T = TypeVar("T")


class SqliteCursor(BufferedCursor[T]):
    """Reads a batch at a time from a statement's rows, each fetch on a thread of its own (see SqliteConnection)."""

    def __init__(
        self,
        spec: CursorSpecification[T],
        session: SqliteSession,
        statement: str,
        cursor: Rows,
        result_mapper: PostgresResultMapper,
        compiled: bool = False,
    ) -> None:
        super().__init__(spec.batch)
        self.spec = spec
        self.session = session
        self.statement = statement
        self.cursor = cursor
        self.result_mapper = result_mapper
        self.compiled = compiled
        """Whether the statement is compiled Cypher, which gives each row as its one column."""

    def read(self, count: int) -> tuple[list[T], bool]:
        rows = concurrency.blocking(self.session.fetch, self.statement, self.cursor, count, on=self.session.thread)
        results = [it[RESULT] for it in rows] if self.compiled else rows
        return self.result_mapper.map_query_results(results, self.spec), len(rows) < count

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            concurrency.blocking(self.cursor.close, on=self.session.thread)
