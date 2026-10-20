# Written by scripts/unasync.py from drivine/connection/neo4j/neo4j_connection.py:
# change that, and run the script again.
from __future__ import annotations

import logging
import time
from typing import Any, TypeVar

from neo4j import ManagedTransaction, Result, Session, Transaction

from drivine.connection.neo4j.neo4j_result_mapper import Neo4jResultMapper
from drivine.connection.neo4j.neo4j_spec_compiler import Neo4jSpecCompiler
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.drivine_error import DrivineError
from drivine.logger import log_statement
from drivine.mapper.graph_result_mapper import GraphResultMapper
from drivine.query.query_specification import QuerySpecification
from drivine.sync.connection.connection import Connection
from drivine.sync.connection.neo4j.neo4j_cursor import Neo4jCursor

T = TypeVar("T")

logger = logging.getLogger("drivine.connection")


class Neo4jConnection(Connection):
    """
    A session on a Bolt engine: Neo4j, Memgraph or Neptune.
    """

    def __init__(self, session: Session, session_id: str, result_mapper: GraphResultMapper | None = None):
        self.session = session
        self.session_id = session_id
        self.result_mapper = result_mapper or Neo4jResultMapper()
        self.transaction: Transaction | None = None

    def query(self, spec: QuerySpecification[T]) -> list[T]:
        finalized = spec.finalized_copy("CYPHER")
        compiled = Neo4jSpecCompiler(finalized).compile()
        start = time.perf_counter()
        result = self._run(compiled.statement, compiled.parameters)
        records = [record for record in result]
        log_statement(self.session_id, finalized, start)
        return self.result_mapper.map_query_results(records, finalized)

    def open_cursor(self, spec: CursorSpecification[T]) -> Neo4jCursor[T]:
        finalized = spec.finalized_copy("CYPHER")
        compiled = Neo4jSpecCompiler(finalized).compile()
        start = time.perf_counter()
        result = self._run(compiled.statement, compiled.parameters)
        log_statement(self.session_id, finalized, start)
        return Neo4jCursor(finalized, result, self.result_mapper)

    def _run(self, statement: str, parameters: dict[str, Any]) -> Result:
        runner: Session | Transaction | ManagedTransaction = self.transaction or self.session
        return runner.run(statement, parameters)  # pyright: ignore[reportArgumentType]

    def start_transaction(self) -> None:
        self.transaction = self.session.begin_transaction()

    def commit_transaction(self) -> None:
        if self.transaction is None:
            raise DrivineError("There is no transaction to commit.")
        transaction, self.transaction = self.transaction, None
        transaction.commit()

    def rollback_transaction(self) -> None:
        if self.transaction is None:
            raise DrivineError("There is no transaction to roll back.")
        transaction, self.transaction = self.transaction, None
        transaction.rollback()

    def release(self, error: BaseException | None = None) -> None:
        if error is not None:
            logger.debug("Closing session %s with error: %s", self.session_id, error)
        self.session.close()
