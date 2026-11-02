from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from drivine import concurrency
from drivine.connection.connection import Connection
from drivine.connection.postgres.postgres_result_mapper import PostgresResultMapper
from drivine.connection.sqlite.sqlite_cursor import SqliteCursor
from drivine.connection.sqlite.sqlite_session import SqliteSession
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.drivine_error import DrivineError
from drivine.logger import log_statement
from drivine.query.query_specification import QuerySpecification
from drivine.query.query_specification_compiler import QuerySpecificationCompiler

T = TypeVar("T")

logger = logging.getLogger("drivine.connection")

CYPHER_REFUSED = (
    "Cypher is not compiled to SQLite's SQL yet (liberation-data/drivine-python#93): send SQL over the graph's tables, "
    "or use an object manager."
)


class SqliteConnection(Connection):
    """
    A connection to a SQLite file, through the `sqlite3` module (see SqliteSession). Outside a transaction each
    statement commits itself, as a statement does on the graph engines. A transaction takes the file's write lock as it
    begins, so one writer works at a time. Rows are dicts of their columns, as on Postgres; a statement that opens with
    `/* drivine:json */` has its text values read as JSON.

    Each call on the file blocks, so it runs on a thread of its own and the event loop goes on meanwhile (see
    concurrency.blocking); the sync API runs it where it is.
    """

    def __init__(
        self, session: SqliteSession, release: Callable[[SqliteSession], Awaitable[None]], session_id: str
    ) -> None:
        self.session = session
        self._release = release
        self.session_id = session_id
        self.result_mapper = PostgresResultMapper()

    async def query(self, spec: QuerySpecification[T]) -> list[T]:
        finalized = spec.finalized_copy("SQL")
        statement, parameters = _compiled(finalized)
        start = time.perf_counter()
        rows = await concurrency.blocking(self.session.query, statement, parameters)
        log_statement(self.session_id, finalized, start)
        return self.result_mapper.map_query_results(rows, finalized)

    async def open_cursor(self, spec: CursorSpecification[T]) -> SqliteCursor[T]:
        finalized = spec.finalized_copy("SQL")
        statement, parameters = _compiled(finalized)
        cursor = await concurrency.blocking(self.session.execute, statement, parameters)
        return SqliteCursor(finalized, self.session, statement, cursor, self.result_mapper)

    async def start_transaction(self) -> None:
        await concurrency.blocking(self.session.begin)

    async def commit_transaction(self) -> None:
        await concurrency.blocking(self.session.commit)

    async def rollback_transaction(self) -> None:
        await concurrency.blocking(self.session.rollback)

    async def release(self, error: BaseException | None = None) -> None:
        if error is not None:
            logger.debug("Releasing SQLite connection %s with error: %s", self.session_id, error)
        try:
            if self.session.in_transaction:
                await concurrency.blocking(self.session.rollback)
        finally:
            await self._release(self.session)


def _compiled(spec: QuerySpecification[Any]) -> tuple[str, Any]:
    """
    The statement as SQLite reads it, with its values: a Cypher statement is refused until it is compiled. A skip and a
    limit are written as SQLite reads them, the limit first, and none (-1) where only a skip is given.
    """
    if spec.statement is not None and spec.statement.language == "CYPHER":
        raise DrivineError(CYPHER_REFUSED, spec)
    statement = QuerySpecificationCompiler(spec).statement
    if statement.language != "SQL":
        raise DrivineError(f"{statement.language} is not supported on SQLite.", spec)
    paged = [
        *([f"LIMIT {spec._limit if spec._limit is not None else -1}"] if spec._limit is not None or spec._skip else []),
        *([f"OFFSET {spec._skip}"] if spec._skip else []),
    ]
    return " ".join([statement.text, *paged]), spec.parameters
