from __future__ import annotations

from typing import TypeVar

from neo4j import AsyncResult

from drivine.cursor.buffered_cursor import BufferedCursor
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.mapper.graph_result_mapper import GraphResultMapper

T = TypeVar("T")


class Neo4jCursor(BufferedCursor[T]):
    """
    Streams the driver's result as it arrives, the driver fetching ahead as it is read.
    """

    def __init__(self, spec: CursorSpecification[T], result: AsyncResult, result_mapper: GraphResultMapper):
        super().__init__(spec.batch)
        self.spec = spec
        self.result = result
        self.result_mapper = result_mapper

    async def read(self, count: int) -> tuple[list[T], bool]:
        records = await self.result.fetch(count)
        return self.result_mapper.map_query_results(records, self.spec), len(records) < count

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            await self.result.consume()
