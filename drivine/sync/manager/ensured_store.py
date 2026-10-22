# Written by scripts/unasync.py from drivine/manager/ensured_store.py:
# change that, and run the script again.
from __future__ import annotations

from typing import Any, TypeVar
from weakref import WeakKeyDictionary

from drivine.cursor.cursor_specification import CursorSpecification
from drivine.query.query_specification import QuerySpecification
from drivine.sync import concurrency
from drivine.sync.cursor.cursor import Cursor
from drivine.sync.manager.persistence_manager import PersistenceManager
from drivine.sync.schema.schema_executor import SchemaExecutor

T = TypeVar("T")

_made: WeakKeyDictionary[Any, set[str]] = WeakKeyDictionary()
"""The stores each database's `ensuring` statement has made, by its connection provider."""

_making: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
"""The lock each database's stores are made under, by its connection provider: made once, however many managers ask."""


class EnsuredStore(PersistenceManager):
    """
    A persistence manager whose statements each run once `ensuring` has made the store. The statement runs on a
    connection of its own, outside the transaction a statement may be in, as schema statements run: a transaction that
    rolled back would take the store with it. One that fails is run again by the next statement.
    """

    def __init__(self, persistence_manager: PersistenceManager, ensuring: str) -> None:
        self.persistence_manager = persistence_manager
        self.ensuring = ensuring
        self.database = persistence_manager.database
        self.type = persistence_manager.type
        self.provider = getattr(persistence_manager, "provider", None)

    def query(self, spec: QuerySpecification[T]) -> list[T]:
        ensure_store(self.persistence_manager, self.ensuring)
        return self.persistence_manager.query(spec)

    def open_cursor(self, spec: CursorSpecification[T]) -> Cursor[T]:
        ensure_store(self.persistence_manager, self.ensuring)
        return self.persistence_manager.open_cursor(spec)


def ensure_store(persistence_manager: PersistenceManager, ensuring: str) -> None:
    """Makes the store with `ensuring`, once for the persistence manager's database, however many ask."""
    key: Any = getattr(persistence_manager, "provider", None) or persistence_manager
    made = _made.setdefault(key, set())
    if ensuring in made:
        return
    lock = _making.setdefault(key, concurrency.Lock())
    with lock:
        if ensuring in made:
            return
        provider = getattr(persistence_manager, "provider", None)
        if provider is not None:
            SchemaExecutor(provider).run(ensuring)
        else:
            persistence_manager.execute(QuerySpecification[Any](ensuring))
        made.add(ensuring)
