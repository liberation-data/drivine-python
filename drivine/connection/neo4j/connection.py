from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any, TypeVar

from neo4j import AsyncManagedTransaction, AsyncResult, AsyncSession, AsyncTransaction, Record
from neo4j.graph import Node, Path, Relationship
from neo4j.time import Date, DateTime, Time

from drivine.connection.provider import Connection
from drivine.cursor.cursor import BufferedCursor
from drivine.errors import DrivineError
from drivine.logger import log_statement
from drivine.mapper.result_mapper import GraphResultMapper
from drivine.query.compiler import CypherSpecCompiler
from drivine.query.specification import CursorSpecification, QuerySpecification

T = TypeVar("T")

logger = logging.getLogger("drivine.connection")


class Neo4jResultMapper(GraphResultMapper):
    """
    A node or relationship is its properties, and Neo4j's temporal values are the standard library's.
    """

    def values(self, record: Any) -> Sequence[Any]:
        return record.values()

    def to_native(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, (Node, Relationship)):
            return {key: self.to_native(it) for key, it in value.items()}
        if isinstance(value, (DateTime, Date, Time)):
            return value.to_native()
        if isinstance(value, list):
            return [self.to_native(it) for it in value]  # pyright: ignore[reportUnknownVariableType]
        if isinstance(value, Record):
            return {key: self.to_native(value[key]) for key in value.keys()}
        if isinstance(value, dict):
            return {key: self.to_native(it) for key, it in value.items()}  # pyright: ignore[reportUnknownVariableType]
        if isinstance(value, Path):
            return [self.to_native(it) for it in value.nodes]
        return value


class Neo4jConnection(Connection):
    """
    A session on a Bolt engine: Neo4j, Memgraph or Neptune.
    """

    def __init__(self, session: AsyncSession, session_id: str, result_mapper: GraphResultMapper | None = None):
        self.session = session
        self.session_id = session_id
        self.result_mapper = result_mapper or Neo4jResultMapper()
        self.transaction: AsyncTransaction | None = None

    async def query(self, spec: QuerySpecification[T]) -> list[T]:
        finalized = spec.finalized_copy("CYPHER")
        compiled = CypherSpecCompiler(finalized).compile()
        start = time.perf_counter()
        result = await self._run(compiled.statement, compiled.parameters)
        records = [record async for record in result]
        log_statement(self.session_id, finalized, start)
        return self.result_mapper.map_query_results(records, finalized)

    async def open_cursor(self, spec: CursorSpecification[T]) -> Neo4jCursor[T]:
        finalized = spec.finalized_copy("CYPHER")
        compiled = CypherSpecCompiler(finalized).compile()
        start = time.perf_counter()
        result = await self._run(compiled.statement, compiled.parameters)
        log_statement(self.session_id, finalized, start)
        return Neo4jCursor(finalized, result, self.result_mapper)

    async def _run(self, statement: str, parameters: dict[str, Any]) -> AsyncResult:
        runner: AsyncSession | AsyncTransaction | AsyncManagedTransaction = self.transaction or self.session
        return await runner.run(statement, parameters)  # pyright: ignore[reportArgumentType]

    async def start_transaction(self) -> None:
        self.transaction = await self.session.begin_transaction()

    async def commit_transaction(self) -> None:
        if self.transaction is None:
            raise DrivineError("There is no transaction to commit.")
        transaction, self.transaction = self.transaction, None
        await transaction.commit()

    async def rollback_transaction(self) -> None:
        if self.transaction is None:
            raise DrivineError("There is no transaction to roll back.")
        transaction, self.transaction = self.transaction, None
        await transaction.rollback()

    async def release(self, error: BaseException | None = None) -> None:
        if error is not None:
            logger.debug("Closing session %s with error: %s", self.session_id, error)
        await self.session.close()


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
