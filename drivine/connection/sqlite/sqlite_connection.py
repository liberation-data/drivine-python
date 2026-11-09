from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from drivine import concurrency
from drivine.backend.sql.sql_graph import DEFAULT_GRAPH
from drivine.backend.sqlite.sqlite_graph import CYPHER, JSON_ROWS
from drivine.connection.connection import Connection
from drivine.connection.postgres.postgres_result_mapper import PostgresResultMapper
from drivine.connection.sqlite.sqlite_cursor import SqliteCursor
from drivine.connection.sqlite.sqlite_session import SqliteSession
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.cypher.cypher_specification import RESULT, cypher_compiled
from drivine.drivine_error import DrivineError
from drivine.logger import log_statement
from drivine.query.query_specification import QuerySpecification
from drivine.query.query_specification_compiler import QuerySpecificationCompiler

T = TypeVar("T")

logger = logging.getLogger("drivine.connection")


class SqliteConnection(Connection):
    """
    A connection to a SQLite file, through apsw (see SqliteSession). Outside a transaction each
    statement commits itself, as a statement does on the graph engines. A transaction takes the file's write lock as it
    begins, so one writer works at a time. Rows are dicts of their columns, as on Postgres; a statement that opens with
    `/* drivine:json */` has its text values read as JSON. A Cypher statement is compiled to SQL over the graph's tables
    (see SqliteCypherCompiler), and gives a row as a graph engine gives it: its one item, or a list of its items.

    Each call on the file blocks, so it runs on a thread of its own and the event loop goes on meanwhile (see
    concurrency.blocking); the sync API runs it where it is.
    """

    def __init__(
        self,
        session: SqliteSession,
        release: Callable[[SqliteSession], Awaitable[None]],
        session_id: str,
        graph: str = DEFAULT_GRAPH,
    ) -> None:
        self.session = session
        self.graph = graph
        self._release = release
        self.session_id = session_id
        self.result_mapper = PostgresResultMapper()
        self._cursors: list[SqliteCursor[Any]] = []
        """The cursors opened on the connection: each still open is closed as it is released (see release)."""

    async def query(self, spec: QuerySpecification[T]) -> list[T]:
        finalized = spec.finalized_copy("SQL")
        statement, parameters, compiled = _compiled(finalized, self.graph)
        start = time.perf_counter()
        run = self.session.query_writing if compiled and _writes(statement) else self.session.query
        rows = await concurrency.blocking(run, statement, parameters, on=self.session.thread)
        log_statement(self.session_id, finalized, start)
        if compiled:
            # A compiled statement's row is its one column, as a graph engine's record of one column is its value.
            rows = [it[RESULT] for it in rows]
        return self.result_mapper.map_query_results(rows, finalized)

    async def open_cursor(self, spec: CursorSpecification[T]) -> SqliteCursor[T]:
        finalized = spec.finalized_copy("SQL")
        statement, parameters, compiled = _compiled(finalized, self.graph)
        execute = self.session.execute_writing if compiled and _writes(statement) else self.session.execute
        cursor = await concurrency.blocking(execute, statement, parameters, on=self.session.thread)
        opened = SqliteCursor(finalized, self.session, statement, cursor, self.result_mapper, compiled)
        self._cursors.append(opened)
        return opened

    async def start_transaction(self) -> None:
        await concurrency.blocking(self.session.begin, on=self.session.thread)

    async def commit_transaction(self) -> None:
        await concurrency.blocking(self.session.commit, on=self.session.thread)

    async def rollback_transaction(self) -> None:
        await concurrency.blocking(self.session.rollback, on=self.session.thread)

    async def release(self, error: BaseException | None = None) -> None:
        """
        Closes each cursor still open, rolls back a transaction still open, and gives the connection back. A cursor not
        read to its end holds the file as it was when it was opened, and the connection's next user would read that.
        """
        if error is not None:
            logger.debug("Releasing SQLite connection %s with error: %s", self.session_id, error)
        try:
            cursors, self._cursors = self._cursors, []
            for cursor in cursors:
                await cursor.close()
            if self.session.in_transaction:
                await concurrency.blocking(self.session.rollback, on=self.session.thread)
        finally:
            await self._release(self.session)


def _compiled(spec: QuerySpecification[Any], graph: str) -> tuple[str, Any, bool]:
    """
    The statement as SQLite reads it, with its values, and whether it is compiled Cypher: a Cypher statement is
    compiled to SQL over the graph's tables, opening with `/* drivine:json */`, so its values are read as JSON. A skip
    and a limit are written as SQLite reads them, the limit first, and none (-1) where only a skip is given.
    """
    sql, cypher = cypher_compiled(spec, "SQLITE", graph)
    if cypher is not None:
        assert sql.statement is not None
        sql.statement = dataclasses.replace(sql.statement, text=f"{JSON_ROWS}\n{sql.statement.text}")
        spec = sql
    statement = QuerySpecificationCompiler(spec).statement
    if statement.language != "SQL":
        raise DrivineError(f"{statement.language} is not supported on SQLite.", spec)
    paged = [
        *([f"LIMIT {spec._limit if spec._limit is not None else -1}"] if spec._limit is not None or spec._skip else []),
        *([f"OFFSET {spec._skip}"] if spec._skip else []),
    ]
    return " ".join([statement.text, *paged]), spec.parameters, cypher is not None


def _writes(statement: str) -> bool:
    """Whether the compiled statement writes: a procedure `drivine_cypher` carries out."""
    return f"{CYPHER}(" in statement
