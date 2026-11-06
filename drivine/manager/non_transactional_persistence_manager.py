from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from drivine.connection.connection import Connection
from drivine.connection.connection_provider import ConnectionProvider
from drivine.cursor.cursor import Cursor
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.drivine_error import DrivineError
from drivine.manager.persistence_manager import PersistenceManager
from drivine.query.query_specification import QuerySpecification
from drivine.schema.schema_ddl import ddl_of

if TYPE_CHECKING:
    from drivine.schema.schema_commands import SchemaCommands

T = TypeVar("T")


class NonTransactionalPersistenceManager(PersistenceManager):
    """
    Runs each statement on a connection of its own, which commits it.
    """

    def __init__(self, provider: ConnectionProvider, database: str) -> None:
        self.provider = provider
        self.database = database
        self.type = provider.type
        self.schema_commands: SchemaCommands | None = None
        """
        What runs Neo4j schema DDL given as Cypher to a SQL store (see SchemaCommands): the one the
        PersistenceManagerFactory made for the database, or, for a manager made without one, one of the manager's own,
        made when DDL is first run.
        """

    async def query(self, spec: QuerySpecification[T]) -> list[T]:
        """
        The results of the statement. Neo4j schema DDL (`CREATE INDEX`, `CREATE CONSTRAINT`, `DROP …`,
        `db.awaitIndexes`) given as Cypher to Postgres, Oracle or SQLite is applied through Drivine's schema machinery,
        and gives no rows: see SchemaCommands. A graph engine is sent it as it is.
        """
        ddl = ddl_of(self.type, spec)
        if ddl is not None:
            if self.schema_commands is None:
                from drivine.schema.schema_commands import SchemaCommands

                self.schema_commands = SchemaCommands.standalone(self.provider)
            await self.schema_commands.run(ddl)
            return []
        connection = await self.provider.connect()
        try:
            results = await connection.query(spec)
        except Exception as error:
            await connection.release(error)
            raise DrivineError.with_root_cause(error, spec) from error
        except BaseException as error:
            await connection.release(error)
            raise
        await connection.release()
        return results

    async def open_cursor(self, spec: CursorSpecification[T]) -> Cursor[T]:
        connection = await self.provider.connect()
        try:
            cursor = await connection.open_cursor(spec)
        except BaseException as error:
            await connection.release(error)
            if isinstance(error, Exception):
                raise DrivineError.with_root_cause(error, spec) from error
            raise
        return _ConnectionCursor(cursor, connection)


class _ConnectionCursor(Cursor[T]):
    """
    A cursor outside a transaction, on a connection of its own, which it releases when it is closed.
    """

    def __init__(self, cursor: Cursor[T], connection: Connection) -> None:
        self.cursor = cursor
        self.connection = connection
        self._released = False

    async def __anext__(self) -> T:
        try:
            return await self.cursor.__anext__()
        except StopAsyncIteration:
            raise
        except Exception as error:
            await self.close(error)
            raise DrivineError.with_root_cause(error) from error

    async def close(self, error: BaseException | None = None) -> None:
        if self._released:
            return
        self._released = True
        try:
            await self.cursor.close()
        finally:
            await self.connection.release(error)
