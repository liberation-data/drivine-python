from __future__ import annotations

import logging
import time
from typing import Any, TypeVar

from drivine.connection.auto_commit_transactions import AutoCommitTransactions
from drivine.connection.connection import Connection
from drivine.connection.falkordb.falkordb_cursor import FalkorDbCursor
from drivine.connection.falkordb.falkordb_result_mapper import FalkorDbResultMapper
from drivine.connection.falkordb.falkordb_spec_compiler import FalkorDbSpecCompiler
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.logger import log_statement
from drivine.query.query_specification import QuerySpecification

T = TypeVar("T")

logger = logging.getLogger("drivine.connection")


class FalkorDbConnection(Connection):
    """
    A graph of a FalkorDB server. FalkorDB commits each statement as it runs: transaction boundaries are handled by
    AutoCommitTransactions, according to the connection's TransactionMode.
    """

    def __init__(self, graph: Any, transactions: AutoCommitTransactions, session_id: str) -> None:
        self.graph = graph
        self.transactions = transactions
        self.session_id = session_id
        self.result_mapper = FalkorDbResultMapper()

    async def query(self, spec: QuerySpecification[T]) -> list[T]:
        finalized = spec.finalized_copy("CYPHER")
        return self.result_mapper.post_process(await self.query_rows(finalized), finalized)

    async def query_rows(self, spec: QuerySpecification[Any]) -> list[Any]:
        """The rows of a statement, mapped to Python values but not post-processed."""
        finalized = spec.finalized_copy("CYPHER")
        compiled = FalkorDbSpecCompiler(finalized).compile()
        start = time.perf_counter()
        result = await self.graph.query(compiled.statement, compiled.parameters or None)
        log_statement(self.session_id, finalized, start)
        return self.result_mapper.map_to_native(result.result_set or [])

    async def open_cursor(self, spec: CursorSpecification[T]) -> FalkorDbCursor[T]:
        return FalkorDbCursor(spec.finalized_copy("CYPHER"), self)

    async def start_transaction(self) -> None:
        self.transactions.start()

    async def commit_transaction(self) -> None:
        self.transactions.commit()

    async def rollback_transaction(self) -> None:
        self.transactions.rollback()

    async def release(self, error: BaseException | None = None) -> None:
        # The client is shared by the provider, which closes it in end().
        if error is not None:
            logger.debug("Releasing FalkorDB connection %s with error: %s", self.session_id, error)
