# Written by scripts/unasync.py from drivine/connection/connection.py:
# change that, and run the script again.
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from drivine.cursor.cursor_specification import CursorSpecification
    from drivine.query.query_specification import QuerySpecification
    from drivine.sync.cursor.cursor import Cursor


T = TypeVar("T")


class Connection(ABC):
    """
    One session with a database. Outside a transaction each statement commits as it runs.
    """

    session_id: str

    @abstractmethod
    def query(self, spec: QuerySpecification[T]) -> list[T]: ...

    @abstractmethod
    def open_cursor(self, spec: CursorSpecification[T]) -> Cursor[T]: ...

    @abstractmethod
    def start_transaction(self) -> None: ...

    @abstractmethod
    def commit_transaction(self) -> None: ...

    @abstractmethod
    def rollback_transaction(self) -> None: ...

    @abstractmethod
    def release(self, error: BaseException | None = None) -> None: ...
