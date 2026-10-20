# Written by scripts/unasync.py from drivine/manager/delegating_persistence_manager.py:
# change that, and run the script again.
from __future__ import annotations

from typing import TypeVar

from drivine.cursor.cursor_specification import CursorSpecification
from drivine.query.query_specification import QuerySpecification
from drivine.sync.cursor.cursor import Cursor
from drivine.sync.manager.non_transactional_persistence_manager import NonTransactionalPersistenceManager
from drivine.sync.manager.persistence_manager import PersistenceManager
from drivine.sync.manager.transactional_persistence_manager import TransactionalPersistenceManager
from drivine.sync.transaction.transaction import current_transaction

T = TypeVar("T")


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
        self.provider = transactional.provider

    def _delegate(self) -> PersistenceManager:
        return self.transactional if current_transaction() is not None else self.non_transactional

    def query(self, spec: QuerySpecification[T]) -> list[T]:
        return self._delegate().query(spec)

    def open_cursor(self, spec: CursorSpecification[T]) -> Cursor[T]:
        return self._delegate().open_cursor(spec)
