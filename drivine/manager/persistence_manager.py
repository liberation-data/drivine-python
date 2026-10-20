from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, TypeVar

from drivine.connection.database_type import DatabaseType
from drivine.cursor.cursor import Cursor
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.drivine_error import DrivineError
from drivine.query.query_specification import QuerySpecification

T = TypeVar("T")


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
