# Written by scripts/unasync.py from drivine/connection/oracle/oracle_cursor.py:
# change that, and run the script again.
from __future__ import annotations

from typing import Any, TypeVar

from drivine.connection.oracle.oracle_result_mapper import OracleResultMapper
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.sync.cursor.buffered_cursor import BufferedCursor

T = TypeVar("T")


class OracleCursor(BufferedCursor[T]):
    """Reads a batch at a time from an open Oracle cursor."""

    def __init__(
        self, spec: CursorSpecification[T], cursor: Any, result_mapper: OracleResultMapper, layout: bool = False
    ) -> None:
        super().__init__(spec.batch)
        self.spec = spec
        self.layout = layout
        """Whether its rows are read in the store layout: see OracleResultMapper.map_query_results."""
        self.cursor = cursor
        self.result_mapper = result_mapper
        description: list[Any] = cursor.description or []
        self._columns = [str(it[0]) for it in description]

    def read(self, count: int) -> tuple[list[T], bool]:
        rows = self.cursor.fetchmany(count)
        return self.result_mapper.map_query_results(rows, self._columns, self.spec, self.layout), len(rows) < count

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.cursor.close()
