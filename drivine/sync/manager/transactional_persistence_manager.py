# Written by scripts/unasync.py from drivine/manager/transactional_persistence_manager.py:
# change that, and run the script again.
from __future__ import annotations

from typing import TypeVar

from drivine.cursor.cursor_specification import CursorSpecification
from drivine.drivine_error import DrivineError
from drivine.query.query_specification import QuerySpecification
from drivine.sync.connection.connection_provider import ConnectionProvider
from drivine.sync.cursor.cursor import Cursor
from drivine.sync.manager.persistence_manager import PersistenceManager
from drivine.sync.transaction.transaction import Transaction, current_transaction

T = TypeVar("T")


class TransactionalPersistenceManager(PersistenceManager):
    """
    Runs each statement in the current transaction, and raises without one.
    """

    def __init__(self, provider: ConnectionProvider, database: str) -> None:
        self.provider = provider
        self.database = database
        self.type = provider.type

    def query(self, spec: QuerySpecification[T]) -> list[T]:
        return self._transaction().query(spec, self.provider)

    def open_cursor(self, spec: CursorSpecification[T]) -> Cursor[T]:
        return self._transaction().open_cursor(spec, self.provider)

    @staticmethod
    def _transaction() -> Transaction:
        transaction = current_transaction()
        if transaction is None:
            raise DrivineError(
                "TransactionalPersistenceManager requires a transaction. Mark the method @transactional(), run it "
                "in `with transaction()`, or use the delegating PersistenceManager."
            )
        return transaction
