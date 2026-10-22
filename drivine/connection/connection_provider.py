from __future__ import annotations

from abc import ABC, abstractmethod

from drivine.connection.connection import Connection
from drivine.connection.database_type import DatabaseType


class ConnectionProvider(ABC):
    """
    Provides connections to one database.
    """

    name: str
    """The name the database is registered under."""
    type: DatabaseType
    graph_name: str | None = None
    """
    The graph the store names, where it names one: on Oracle, the property graph and its tables; on Postgres, the
    tables an object manager works on.
    """

    @abstractmethod
    async def connect(self) -> Connection: ...

    @abstractmethod
    async def end(self) -> None:
        """Closes the provider's driver or pool. Call it once, when the application stops."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r}, type={self.type.value})"
