from __future__ import annotations

import logging
import re
import time
from typing import Any, TypeVar

from drivine.backend.sql.sql_graph import DEFAULT_GRAPH
from drivine.connection.connection import Connection
from drivine.connection.oracle.oracle_cursor import OracleCursor
from drivine.connection.oracle.oracle_result_mapper import OracleResultMapper
from drivine.connection.oracle.oracle_spec_compiler import OracleSpecCompiler
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.cypher.cypher_specification import cypher_compiled
from drivine.drivine_error import DrivineError
from drivine.logger import log_statement
from drivine.query.query_specification import QuerySpecification

T = TypeVar("T")

logger = logging.getLogger("drivine.connection")

_BLOCK = re.compile(r"^\s*(DECLARE|BEGIN)\b", re.IGNORECASE)
"""A PL/SQL block, whose rows, if it hands any back, are its first implicit result."""


class OracleConnection(Connection):
    """
    A connection to Oracle. Outside a transaction each statement commits itself, as a statement does on the graph
    engines. A PL/SQL block hands its rows back with `DBMS_SQL.RETURN_RESULT`; the first such set is its result. A
    Cypher statement is compiled to SQL over the graph's tables (see SqlCypherCompiler): a read to a query, and a write
    to a block.
    """

    def __init__(self, connection: Any, release: Any, session_id: str, graph: str = DEFAULT_GRAPH) -> None:
        self.connection = connection
        self._release = release
        self.session_id = session_id
        self.graph = graph
        self.result_mapper = OracleResultMapper()
        self._in_transaction = False

    async def query(self, spec: QuerySpecification[T]) -> list[T]:
        finalized = spec.finalized_copy("SQL")
        compiled = OracleSpecCompiler(cypher_compiled(finalized, "ORACLE", self.graph)[0]).compile()
        start = time.perf_counter()
        self.connection.autocommit = not self._in_transaction
        cursor: Any = self.connection.cursor()
        rows: list[Any] = []
        columns: list[str] = []
        try:
            await cursor.execute(compiled.statement, compiled.parameters, fetch_lobs=False)
            if cursor.description is None and _BLOCK.match(compiled.statement):
                implicit = _implicit_results(cursor)
                cursor.close()
                cursor = implicit[0] if implicit else None
            if cursor is not None and cursor.description is not None:
                rows = await cursor.fetchall()
                columns = [str(it[0]) for it in cursor.description]
        finally:
            if cursor is not None:
                cursor.close()
        log_statement(self.session_id, finalized, start)
        return self.result_mapper.map_query_results(await _read_lobs(rows), columns, finalized)

    async def open_cursor(self, spec: CursorSpecification[T]) -> OracleCursor[T]:
        finalized = spec.finalized_copy("SQL")
        sql = cypher_compiled(finalized, "ORACLE", self.graph)[0]
        compiled = OracleSpecCompiler(sql).compile()
        if sql is not finalized and _BLOCK.match(compiled.statement):
            raise DrivineError(
                "A Cypher statement that writes is a block on Oracle, which hands its rows back whole: run it with "
                "query, not through a cursor.",
                spec,
            )
        self.connection.autocommit = not self._in_transaction
        cursor = self.connection.cursor()
        cursor.arraysize = spec.batch
        await cursor.execute(compiled.statement, compiled.parameters, fetch_lobs=False)
        return OracleCursor(finalized, cursor, self.result_mapper)

    async def start_transaction(self) -> None:
        # Oracle begins a transaction with the first statement that writes; until commit, none commits itself.
        self._in_transaction = True

    async def commit_transaction(self) -> None:
        self._in_transaction = False
        await self.connection.commit()

    async def rollback_transaction(self) -> None:
        self._in_transaction = False
        await self.connection.rollback()

    async def release(self, error: BaseException | None = None) -> None:
        if error is not None:
            logger.debug("Releasing Oracle connection %s with error: %s", self.session_id, error)
        try:
            if self._in_transaction:
                self._in_transaction = False
                await self.connection.rollback()
        finally:
            await self._release(self.connection)


def _implicit_results(cursor: Any) -> list[Any]:
    """
    The result sets a block handed back. A block that handed back none raises DPY-1004 ("no statement executed") in
    python-oracledb's thin mode, rather than giving none.
    """
    import oracledb

    try:
        return list(cursor.getimplicitresults())
    except oracledb.InterfaceError as error:
        if "DPY-1004" in str(error):
            return []
        raise


async def _read_lobs(rows: list[Any]) -> list[Any]:
    """Rows with each LOB read: an implicit result's CLOB comes back as one, whatever the statement asked."""
    if not any(hasattr(value, "read") for row in rows for value in row):
        return rows
    read: list[Any] = []
    for row in rows:
        read.append([await value.read() if hasattr(value, "read") else value for value in row])
    return read
