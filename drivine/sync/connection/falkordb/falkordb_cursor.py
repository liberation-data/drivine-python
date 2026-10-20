# Written by scripts/unasync.py from drivine/connection/falkordb/falkordb_cursor.py:
# change that, and run the script again.
from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from drivine.cursor.cursor_specification import CursorSpecification
from drivine.query.query_specification import QuerySpecification
from drivine.sync.cursor.buffered_cursor import BufferedCursor

if TYPE_CHECKING:
    from drivine.sync.connection.falkordb.falkordb_connection import FalkorDbConnection

T = TypeVar("T")


class FalkorDbCursor(BufferedCursor[T]):
    """Reads a batch at a time with SKIP and LIMIT, as the falkordb client has no streaming read."""

    def __init__(self, spec: CursorSpecification[T], connection: FalkorDbConnection) -> None:
        super().__init__(spec.batch)
        self.spec = spec
        self.connection = connection
        self._page = 0

    def read(self, count: int) -> tuple[list[T], bool]:
        assert self.spec.statement is not None
        page = (
            QuerySpecification[T](self.spec.statement)
            .skip(count * self._page)
            .limit(count + 1)
            .bind(self.spec.parameters)
        )
        rows = self.connection.query_rows(page)
        self._page += 1
        last = len(rows) <= count
        return self.connection.result_mapper.post_process(rows[:count], self.spec), last

    def close(self) -> None:
        self.closed = True
