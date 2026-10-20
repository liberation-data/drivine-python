from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal, TypeVar

from drivine.connection.database_type import DatabaseType
from drivine.connection.provider import Connection, ConnectionProvider
from drivine.connection.registry import DEFAULT, DatabaseRegistry
from drivine.cursor.cursor import Cursor
from drivine.errors import DrivineError
from drivine.query.specification import CursorSpecification, QuerySpecification
from drivine.transaction.transaction import Transaction, current_transaction

T = TypeVar("T")

PersistenceManagerType = Literal["TRANSACTIONAL", "NON_TRANSACTIONAL", "DELEGATING"]


class PersistenceManager(ABC):
    """
    Runs statements against one database, and maps their results.
    """

    database: str
    """The name the database is registered under."""
    type: DatabaseType

    @abstractmethod
    async def query(self, spec: QuerySpecification[T]) -> list[T]:
        """The results of the statement."""

    async def execute(self, spec: QuerySpecification[Any]) -> None:
        """Runs the statement, and disregards any results."""
        await self.query(spec)

    async def get_one(self, spec: QuerySpecification[T]) -> T:
        """The statement's one result. Raises `DrivineError` for none or several."""
        results = await self.query(spec)
        if len(results) != 1:
            raise DrivineError(f"Expected exactly one result, received {len(results)}.", spec)
        return results[0]

    async def maybe_get_one(self, spec: QuerySpecification[T]) -> T | None:
        """The statement's one result, or None. Raises `DrivineError` for several."""
        results = await self.query(spec)
        if len(results) > 1:
            raise DrivineError(f"Expected one result, received {len(results)}.", spec)
        return results[0] if results else None

    @abstractmethod
    async def open_cursor(self, spec: CursorSpecification[T]) -> Cursor[T]:
        """A cursor over the statement's results, read `batch_size` at a time. Close it, or use it with `async with`."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}(database={self.database!r}, type={self.type.value})"


class NonTransactionalPersistenceManager(PersistenceManager):
    """
    Runs each statement on a connection of its own, which commits it.
    """

    def __init__(self, provider: ConnectionProvider, database: str) -> None:
        self.provider = provider
        self.database = database
        self.type = provider.type

    async def query(self, spec: QuerySpecification[T]) -> list[T]:
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


class TransactionalPersistenceManager(PersistenceManager):
    """
    Runs each statement in the current transaction, and raises without one.
    """

    def __init__(self, provider: ConnectionProvider, database: str) -> None:
        self.provider = provider
        self.database = database
        self.type = provider.type

    async def query(self, spec: QuerySpecification[T]) -> list[T]:
        return await self._transaction().query(spec, self.provider)

    async def open_cursor(self, spec: CursorSpecification[T]) -> Cursor[T]:
        return await self._transaction().open_cursor(spec, self.provider)

    @staticmethod
    def _transaction() -> Transaction:
        transaction = current_transaction()
        if transaction is None:
            raise DrivineError(
                "TransactionalPersistenceManager requires a transaction. Mark the method @transactional(), run it "
                "in `async with transaction()`, or use the delegating PersistenceManager."
            )
        return transaction


class DelegatingPersistenceManager(PersistenceManager):
    """
    Runs each statement in the current transaction if there is one, and on its own if not. The default.
    """

    def __init__(
        self, transactional: TransactionalPersistenceManager, non_transactional: NonTransactionalPersistenceManager
    ):
        self.transactional = transactional
        self.non_transactional = non_transactional
        self.database = transactional.database
        self.type = transactional.type

    def _delegate(self) -> PersistenceManager:
        return self.transactional if current_transaction() is not None else self.non_transactional

    async def query(self, spec: QuerySpecification[T]) -> list[T]:
        return await self._delegate().query(spec)

    async def open_cursor(self, spec: CursorSpecification[T]) -> Cursor[T]:
        return await self._delegate().open_cursor(spec)


class PersistenceManagerFactory:
    """
    Makes the persistence managers for each registered database, once.
    """

    def __init__(self, registry: DatabaseRegistry) -> None:
        self.registry = registry
        self._managers: dict[str, DelegatingPersistenceManager] = {}

    def get(self, database: str = DEFAULT, type: PersistenceManagerType = "DELEGATING") -> PersistenceManager:
        manager = self._managers.get(database)
        if manager is None:
            provider = self.registry.connection_provider(database)
            if provider is None:
                raise DrivineError(f"No database is registered under name: {database}")
            manager = DelegatingPersistenceManager(
                TransactionalPersistenceManager(provider, database),
                NonTransactionalPersistenceManager(provider, database),
            )
            self._managers[database] = manager
        if type == "DELEGATING":
            return manager
        if type == "TRANSACTIONAL":
            return manager.transactional
        if type == "NON_TRANSACTIONAL":
            return manager.non_transactional
        raise DrivineError(f"Invalid PersistenceManagerType: {type}")


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
