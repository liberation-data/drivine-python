from __future__ import annotations

from typing import Any, TypeVar

from drivine.connection.postgres.postgres_result_mapper import PostgresResultMapper
from drivine.cursor.buffered_cursor import BufferedCursor
from drivine.cursor.cursor_specification import CursorSpecification

T = TypeVar("T")


class PostgresCursor(BufferedCursor[T]):
    """Reads a batch at a time from a server-side cursor of Postgres."""

    def __init__(self, spec: CursorSpecification[T], cursor: Any, result_mapper: PostgresResultMapper) -> None:
        super().__init__(spec.batch)
        self.spec = spec
        self.cursor = cursor
        self.result_mapper = result_mapper

    async def read(self, count: int) -> tuple[list[T], bool]:
        rows = await self.cursor.fetchmany(count)
        return self.result_mapper.map_query_results(rows, self.spec), len(rows) < count

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            await self.cursor.close()
