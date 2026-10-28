from __future__ import annotations

import logging
import re
import time
from typing import Any, TypeVar

from drivine.backend.oracle.oracle_stored_functions import OracleStoredFunctions, is_missing
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

    def __init__(
        self, connection: Any, release: Any, session_id: str, graph: str = DEFAULT_GRAPH, database: str = ""
    ) -> None:
        """
        `database` is the database and the user the session is of: whether it may call the graph's functions is
        remembered for them (see OracleStoredFunctions).
        """
        self.connection = connection
        self._release = release
        self.session_id = session_id
        self.graph = graph
        self.database = database
        self.result_mapper = OracleResultMapper()
        self._in_transaction = False

    async def query(self, spec: QuerySpecification[T]) -> list[T]:
        finalized = spec.finalized_copy("SQL")
        sql = await self._compiled(finalized)
        compiled = OracleSpecCompiler(sql).compile()
        start = time.perf_counter()
        self.connection.autocommit = not self._in_transaction
        cursor: Any = self.connection.cursor()
        rows: list[Any] = []
        columns: list[str] = []
        try:
            _prepare(cursor, compiled.statement, sql is not finalized)
            await cursor.execute(compiled.statement, await self._bindable(compiled.parameters), fetch_lobs=False)
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
        sql = await self._compiled(finalized)
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
        _prepare(cursor, compiled.statement, sql is not finalized)
        await cursor.execute(compiled.statement, await self._bindable(compiled.parameters), fetch_lobs=False)
        return OracleCursor(finalized, cursor, self.result_mapper)

    async def _compiled(self, finalized: QuerySpecification[T]) -> QuerySpecification[T]:
        """
        The specification, a Cypher statement compiled for Oracle, calling the functions the graph's store keeps where
        the statement needs one and the session may call it, and compiled again to call none where it may not: see
        OracleStoredFunctions.
        """
        sql, compiled = cypher_compiled(finalized, "ORACLE", self.graph)
        if compiled is None or not compiled.calls_stored_nulls_equal or await self._calls_nulls_equal():
            return sql
        return cypher_compiled(finalized, "ORACLE", self.graph, nulls_equal_stored=False)[0]

    async def _calls_nulls_equal(self) -> bool:
        """
        Whether this session may call the graph's function `<graph>_nulls_equal`, as remembered, or as calling it
        finds. A query that fails rolls back only itself, so an open transaction is left as it was.
        """
        import oracledb

        key = f"{self.database}|{self.graph}"
        known = OracleStoredFunctions.known(key)
        if known is not None:
            return known
        self.connection.autocommit = not self._in_transaction
        cursor = self.connection.cursor()
        try:
            await cursor.execute(OracleStoredFunctions.check(self.graph))
            await cursor.fetchall()
            found = True
        except oracledb.DatabaseError as error:
            if not is_missing(error):
                raise
            found = False
        finally:
            cursor.close()
        return OracleStoredFunctions.found(key, self.graph, found)

    async def _bindable(self, parameters: dict[str, Any]) -> dict[str, Any]:
        """
        The parameters, with each text of more than _MAX_TEXT bytes as a CLOB, as drivine4j binds one, whatever wrote
        the statement: a compiled Cypher statement's parameter is bound as its JSON text, a save's properties as theirs,
        and a schema marker's inventory as one text, any of which may be more than Oracle takes as a VARCHAR2
        (ORA-01460, ORA-01461, or ORA-06502 in a block).
        """
        import oracledb

        bindable = dict(parameters)
        for name, value in parameters.items():
            # A character is at most four bytes of UTF-8, so a text of fewer than a quarter of _MAX_TEXT fits.
            if isinstance(value, str) and len(value) * 4 > _MAX_TEXT and len(value.encode()) > _MAX_TEXT:
                clob: Any = oracledb.DB_TYPE_CLOB  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                bindable[name] = await self.connection.createlob(clob, value)
        return bindable

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


_MAX_TEXT = 32_767
"""The most bytes of text Oracle binds as a VARCHAR2; past it a text is bound as a CLOB."""


def _prepare(cursor: Any, statement: str, compiled_from_cypher: bool) -> None:
    """
    Prepares a statement compiled from Cypher to be left out of the driver's statement cache. Oracle keeps a cursor's
    memory while the driver keeps its statement, and a compiled Cypher statement holds megabytes of it: about 4 MB, and
    over 20 MB where it reads a text past 32 KB. Kept in the driver's cache, 20 statements a session, a few dozen ran
    the instance out of PGA (ORA-04036, liberation-data/drivine4j#313). Closed, its memory goes back. Drivine's own
    statements, a few the same each time, are still kept. The statement is executed as this same text, so it is not
    prepared again: stripped as `execute` strips it before it compares, or a padded one would be prepared again, cached.
    """
    if compiled_from_cypher:
        cursor.prepare(statement.strip(), cache_statement=False)


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
