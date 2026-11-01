from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from drivine.backend.cypher_grammar import FALKORDB_GRAMMAR, NEO4J_GRAMMAR
from drivine.backend.oracle.oracle_schema_grammar import waits_out_busy_lock
from drivine.backend.postgres.postgres_statement import value_of
from drivine.connection.connection_provider import ConnectionProvider
from drivine.connection.database_type import DatabaseType
from drivine.drivine_error import DrivineError
from drivine.manager.contention import any_cause, ddl_contended, ddl_deadlocked, when_not_contended
from drivine.query.query_specification import QuerySpecification
from drivine.schema.native_operation import NativeOperation

_ORACLE_SERIAL = re.compile(r"ORA-08177\b")


def _contended(failure: BaseException, waited_out_busy: bool) -> bool:
    """
    A transient error over Bolt, FalkorDB turning a write away while another is in progress, an Oracle serialization
    failure, or a SQL store turning DDL away because of another session's (see ddl_contended), as when managers that
    start together each make the indexes of the store's key. A statement that `waited_out_busy`, as an Oracle index's
    block does (see waits_out_busy_lock), is taken as turned away only for a deadlock (see ddl_deadlocked).
    """
    ddl = ddl_deadlocked(failure) if waited_out_busy else ddl_contended(failure)
    return ddl or any_cause(
        failure,
        lambda it: (
            NEO4J_GRAMMAR.contended(it) or FALKORDB_GRAMMAR.contended(it) or bool(_ORACLE_SERIAL.search(str(it)))
        ),
    )


class SchemaExecutor:
    """
    Runs schema statements on a connection of their own, in auto-commit: schema DDL can't run inside a data transaction.
    """

    def __init__(self, provider: ConnectionProvider) -> None:
        self.provider = provider

    async def run(self, statement: str | NativeOperation, bindings: Mapping[str, Any] | None = None) -> list[Any]:
        """
        Runs a statement, again after a pause when the engine turned it away because another write was in progress.
        """
        waited_out_busy = isinstance(statement, str) and waits_out_busy_lock(statement)
        return await when_not_contended(
            lambda: self._run_once(statement, bindings or {}), lambda it: _contended(it, waited_out_busy)
        )

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
