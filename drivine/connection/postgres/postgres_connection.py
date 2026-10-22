from __future__ import annotations

import itertools
import logging
import time
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from drivine.backend.sql.sql_json import to_json
from drivine.connection.connection import Connection
from drivine.connection.postgres.postgres_cursor import PostgresCursor
from drivine.connection.postgres.postgres_result_mapper import PostgresResultMapper
from drivine.connection.postgres.postgres_spec_compiler import PostgresSpecCompiler
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.logger import log_statement
from drivine.query.query_specification import QuerySpecification

T = TypeVar("T")

logger = logging.getLogger("drivine.connection")

_CURSORS = itertools.count(1)
"""Numbers the server-side cursors, which a connection names."""


class PostgresConnection(Connection):
    """
    A connection to Postgres, through psycopg. Outside a transaction each statement commits itself, as a statement does
    on the graph engines. A statement without parameters is sent as it is, so it may hold several statements, or a
    block.
    """

    def __init__(self, connection: Any, release: Any, session_id: str) -> None:
        self.connection = connection
        self._release = release
        self.session_id = session_id
        self.result_mapper = PostgresResultMapper()
        self._in_transaction = False

    async def query(self, spec: QuerySpecification[T]) -> list[T]:
        finalized = spec.finalized_copy("SQL")
        compiled = PostgresSpecCompiler(finalized).compile()
        start = time.perf_counter()
        async with self.connection.cursor() as cursor:
            await cursor.execute(compiled.statement, _bindable(compiled.parameters))
            rows: list[Any] = await cursor.fetchall() if cursor.description is not None else []
        log_statement(self.session_id, finalized, start)
        return self.result_mapper.map_query_results(rows, finalized)

    async def open_cursor(self, spec: CursorSpecification[T]) -> PostgresCursor[T]:
        """A server-side cursor: held past the statement outside a transaction, where each statement commits itself."""
        finalized = spec.finalized_copy("SQL")
        compiled = PostgresSpecCompiler(finalized).compile()
        cursor = self.connection.cursor(name=f"drivine_{next(_CURSORS)}", withhold=not self._in_transaction)
        await cursor.execute(compiled.statement, _bindable(compiled.parameters))
        return PostgresCursor(finalized, cursor, self.result_mapper)

    async def start_transaction(self) -> None:
        await self.connection.execute("BEGIN")
        self._in_transaction = True

    async def commit_transaction(self) -> None:
        self._in_transaction = False
        await self.connection.execute("COMMIT")

    async def rollback_transaction(self) -> None:
        self._in_transaction = False
        await self.connection.execute("ROLLBACK")

    async def release(self, error: BaseException | None = None) -> None:
        if error is not None:
            logger.debug("Releasing Postgres connection %s with error: %s", self.session_id, error)
        try:
            if self._in_transaction:
                self._in_transaction = False
                await self.connection.execute("ROLLBACK")
        finally:
            await self._release(self.connection)


def _bindable(parameters: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """
    The values as psycopg binds them, or None for none, so a statement without parameters is sent as it is: a dict as
    `jsonb`, as Drivine's statements take a map, its values written as a save writes them (a date as ISO 8601 text, an
    Enum as its value; see storable), and a list of numbers some of which are whole as floats, as psycopg
    binds a list of one type (a vector, say, `[1, 0.5]`). Everything else psycopg adapts itself.
    """
    if not parameters:
        return None
    from psycopg.types.json import Jsonb

    def bindable(value: Any) -> Any:
        if isinstance(value, dict):
            return Jsonb(value, dumps=to_json)
        if isinstance(value, list):
            items = cast(list[Any], value)
            return [float(it) for it in items] if _mixed_numbers(items) else items
        return value

    return {key: bindable(value) for key, value in parameters.items()}


def _mixed_numbers(values: list[Any]) -> bool:
    """Whether the values are numbers, whole and not."""
    numbers = all(isinstance(it, (int, float)) and not isinstance(it, bool) for it in values)
    return numbers and any(isinstance(it, float) for it in values) and any(isinstance(it, int) for it in values)
