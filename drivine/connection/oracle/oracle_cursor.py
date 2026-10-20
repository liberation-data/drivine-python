from __future__ import annotations

from typing import Any, TypeVar

from drivine.connection.oracle.oracle_result_mapper import OracleResultMapper
from drivine.cursor.buffered_cursor import BufferedCursor
from drivine.cursor.cursor_specification import CursorSpecification

T = TypeVar("T")


class OracleCursor(BufferedCursor[T]):
    """Reads a batch at a time from an open Oracle cursor."""

    def __init__(self, spec: CursorSpecification[T], cursor: Any, result_mapper: OracleResultMapper) -> None:
        super().__init__(spec.batch)
        self.spec = spec
        self.cursor = cursor
        self.result_mapper = result_mapper
        description: list[Any] = cursor.description or []
        self._columns = [str(it[0]) for it in description]

    async def read(self, count: int) -> tuple[list[T], bool]:
        rows = await self.cursor.fetchmany(count)
        return self.result_mapper.map_query_results(rows, self._columns, self.spec), len(rows) < count

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.cursor.close()
