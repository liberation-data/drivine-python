from __future__ import annotations

from typing import TypeVar

from drivine.cursor.cursor import Cursor
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.manager.non_transactional_persistence_manager import NonTransactionalPersistenceManager
from drivine.manager.persistence_manager import PersistenceManager
from drivine.manager.transactional_persistence_manager import TransactionalPersistenceManager
from drivine.query.query_specification import QuerySpecification
from drivine.transaction.transaction import current_transaction

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

    async def query(self, spec: QuerySpecification[T]) -> list[T]:
        return await self._delegate().query(spec)

    async def open_cursor(self, spec: CursorSpecification[T]) -> Cursor[T]:
        return await self._delegate().open_cursor(spec)
