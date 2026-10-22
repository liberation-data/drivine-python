"""The store an object manager makes before its first statement: made once, and made again after a failure."""

import asyncio
from typing import Any, TypeVar, cast

import pytest

from drivine import DatabaseType, QuerySpecification
from drivine.cursor.cursor import Cursor
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.manager.ensured_store import EnsuredStore
from drivine.manager.persistence_manager import PersistenceManager

T = TypeVar("T")


class Recording(PersistenceManager):
    """Records the statements it runs, and fails the first `failures` of them."""

    def __init__(self, failures: int = 0) -> None:
        self.database = "recording"
        self.type = DatabaseType.POSTGRES
        self.failures = failures
        self.statements: list[str] = []

    async def query(self, spec: QuerySpecification[T]) -> list[T]:
        assert spec.statement is not None
        self.statements.append(spec.statement.text)
        # Gives way while it runs, as a statement sent to a database does, so another may ask meanwhile.
        await asyncio.sleep(0)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("the store could not be made")
        return []

    async def open_cursor(self, spec: CursorSpecification[T]) -> Cursor[T]:
        assert spec.statement is not None
        self.statements.append(f"CURSOR {spec.statement.text}")
        return cast(Cursor[T], None)


async def test_makes_the_store_once_before_the_first_statement():
    recording = Recording()
    store = EnsuredStore(recording, "MAKE STORE")
    await store.execute(QuerySpecification[Any]("SELECT 1"))
    await store.execute(QuerySpecification[Any]("SELECT 2"))
    assert recording.statements == ["MAKE STORE", "SELECT 1", "SELECT 2"]


async def test_makes_the_store_again_at_the_next_statement_when_making_it_failed():
    recording = Recording(failures=1)
    store = EnsuredStore(recording, "MAKE STORE")
    with pytest.raises(RuntimeError, match="could not be made"):
        await store.execute(QuerySpecification[Any]("SELECT 1"))
    await store.execute(QuerySpecification[Any]("SELECT 2"))
    assert recording.statements == ["MAKE STORE", "MAKE STORE", "SELECT 2"]


async def test_makes_the_store_once_for_statements_that_ask_at_once():
    recording = Recording()
    store = EnsuredStore(recording, "MAKE STORE")
    await asyncio.gather(*(store.execute(QuerySpecification[Any](f"SELECT {i}")) for i in range(3)))
    assert recording.statements.count("MAKE STORE") == 1
    assert recording.statements[0] == "MAKE STORE"


async def test_makes_the_store_before_a_cursor_is_opened():
    recording = Recording()
    store = EnsuredStore(recording, "MAKE STORE")
    await store.open_cursor(CursorSpecification[Any]("SELECT 1"))
    assert recording.statements == ["MAKE STORE", "CURSOR SELECT 1"]
