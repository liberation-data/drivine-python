from __future__ import annotations

from typing import Any, TypeVar

from drivine.connection.postgres.postgres_result_mapper import PostgresResultMapper
from drivine.cursor.buffered_cursor import BufferedCursor
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.cypher.cypher_specification import RESULT

T = TypeVar("T")


class PostgresCursor(BufferedCursor[T]):
    """
    Reads a batch at a time from a server-side cursor of Postgres, or from the client's for a Cypher statement that
    writes (see PostgresConnection.open_cursor). A Cypher statement's, `compiled`, gives each row as its one column.
    """

    def __init__(
        self, spec: CursorSpecification[T], cursor: Any, result_mapper: PostgresResultMapper, compiled: bool = False
    ) -> None:
        super().__init__(spec.batch)
        self.spec = spec
        self.cursor = cursor
        self.result_mapper = result_mapper
        self.compiled = compiled

    async def read(self, count: int) -> tuple[list[T], bool]:
        rows: list[Any] = await self.cursor.fetchmany(count)
        results = [it[RESULT] for it in rows] if self.compiled else rows
        return self.result_mapper.map_query_results(results, self.spec), len(rows) < count

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            await self.cursor.close()
