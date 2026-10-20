# Written by scripts/unasync.py from drivine/connection/neo4j/neo4j_cursor.py:
# change that, and run the script again.
from __future__ import annotations

from typing import TypeVar

from neo4j import Result

from drivine.cursor.cursor_specification import CursorSpecification
from drivine.mapper.graph_result_mapper import GraphResultMapper
from drivine.sync.cursor.buffered_cursor import BufferedCursor

T = TypeVar("T")


class Neo4jCursor(BufferedCursor[T]):
    """
    Streams the driver's result as it arrives, the driver fetching ahead as it is read.
    """

    def __init__(self, spec: CursorSpecification[T], result: Result, result_mapper: GraphResultMapper):
        super().__init__(spec.batch)
        self.spec = spec
        self.result = result
        self.result_mapper = result_mapper

    def read(self, count: int) -> tuple[list[T], bool]:
        records = self.result.fetch(count)
        return self.result_mapper.map_query_results(records, self.spec), len(records) < count

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.result.consume()
