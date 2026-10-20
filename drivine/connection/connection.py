from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from drivine.cursor.cursor import Cursor
    from drivine.cursor.cursor_specification import CursorSpecification
    from drivine.query.query_specification import QuerySpecification


T = TypeVar("T")


class Connection(ABC):
    """
    One session with a database. Outside a transaction each statement commits as it runs.
    """

    session_id: str

    @abstractmethod
    async def query(self, spec: QuerySpecification[T]) -> list[T]: ...

    @abstractmethod
    async def open_cursor(self, spec: CursorSpecification[T]) -> Cursor[T]: ...

    @abstractmethod
    async def start_transaction(self) -> None: ...

    @abstractmethod
    async def commit_transaction(self) -> None: ...

    @abstractmethod
    async def rollback_transaction(self) -> None: ...

    @abstractmethod
    async def release(self, error: BaseException | None = None) -> None: ...
