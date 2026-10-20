from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, TypeVar

from drivine.connection.database_type import DatabaseType

if TYPE_CHECKING:
    from drivine.cursor.cursor import Cursor
    from drivine.query.specification import CursorSpecification, QuerySpecification

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


class ConnectionProvider(ABC):
    """
    Provides connections to one database.
    """

    name: str
    """The name the database is registered under."""
    type: DatabaseType
    graph_name: str | None = None
    """The graph the store names, where it names one: on Oracle, the property graph and its tables."""

    @abstractmethod
    async def connect(self) -> Connection: ...

    @abstractmethod
    async def end(self) -> None:
        """Closes the provider's driver or pool. Call it once, when the application stops."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r}, type={self.type.value})"


def short_id(*_: Any) -> str:
    """A short id for a session or transaction, for the logs."""
    return secrets.token_hex(4)[:7]
