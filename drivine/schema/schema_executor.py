from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from drivine.backend.cypher_grammar import FALKORDB_GRAMMAR, NEO4J_GRAMMAR
from drivine.backend.postgres.postgres_statement import value_of
from drivine.connection.connection_provider import ConnectionProvider
from drivine.connection.database_type import DatabaseType
from drivine.drivine_error import DrivineError
from drivine.manager.contention import when_not_contended
from drivine.query.query_specification import QuerySpecification
from drivine.schema.native_operation import NativeOperation

_ORACLE_CONTENDED = re.compile(r"ORA-(00060|08177)\b")


def _contended(failure: BaseException) -> bool:
    """
    A transient error over Bolt, FalkorDB turning a write away while another is in progress, or an Oracle deadlock or
    serialization failure.
    """
    cause: BaseException | None = failure
    for _ in range(10):
        if cause is None:
            return False
        if NEO4J_GRAMMAR.contended(cause) or FALKORDB_GRAMMAR.contended(cause) or _ORACLE_CONTENDED.search(str(cause)):
            return True
        cause = cause.__cause__
    return False


class SchemaExecutor:
    """
    Runs schema statements on a connection of their own, in auto-commit: schema DDL can't run inside a data transaction.
    """

    def __init__(self, provider: ConnectionProvider, prepare: Callable[[], Awaitable[None]] | None = None) -> None:
        """
        `prepare` is what is done once before a statement runs, as an object manager makes its store first: a listing
        reads the catalog the store keeps.
        """
        self.provider = provider
        self.prepare = prepare

    async def run(self, statement: str | NativeOperation, bindings: Mapping[str, Any] | None = None) -> list[Any]:
        """
        Runs a statement, again after a pause when the engine turned it away because another write was in progress.
        """
        if self.prepare is not None:
            await self.prepare()
        return await when_not_contended(lambda: self._run_once(statement, bindings or {}), _contended)

    async def _run_once(self, statement: str | NativeOperation, bindings: Mapping[str, Any]) -> list[Any]:
        if isinstance(statement, NativeOperation):
            return await self._native(statement)
        connection = await self.provider.connect()
        try:
            rows = await connection.query(QuerySpecification[Any](statement).bind(bindings))
        except Exception as error:
            await connection.release(error)
            raise DrivineError.with_root_cause(error) from error
        await connection.release()
        # Postgres gives a row as a record of its columns, where the graph engines give one column as its value.
        return [value_of(it) for it in rows] if self.provider.type is DatabaseType.POSTGRES else rows

    async def _native(self, operation: NativeOperation) -> list[Any]:
        constraint = getattr(self.provider, "constraint", None)
        if constraint is None:
            raise DrivineError(f"{operation.native} is a FalkorDB command.")
        await constraint(
            "CREATE" if operation.native == "CONSTRAINT_CREATE" else "DROP", operation.label, operation.properties
        )
        return []
