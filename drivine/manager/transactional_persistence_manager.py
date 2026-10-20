from __future__ import annotations

from typing import TypeVar

from drivine.connection.connection_provider import ConnectionProvider
from drivine.cursor.cursor import Cursor
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.drivine_error import DrivineError
from drivine.manager.persistence_manager import PersistenceManager
from drivine.query.query_specification import QuerySpecification
from drivine.transaction.transaction import Transaction, current_transaction

T = TypeVar("T")


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
