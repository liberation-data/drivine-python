from __future__ import annotations

from typing import Any

from drivine.connection.auto_commit_transactions import AutoCommitTransactions
from drivine.connection.connection import Connection
from drivine.connection.connection_provider import ConnectionProvider
from drivine.connection.database_type import DatabaseType
from drivine.connection.falkordb.falkordb_connection import FalkorDbConnection
from drivine.connection.transaction_mode import TransactionMode
from drivine.utils.short_id import short_id


class FalkorDbConnectionProvider(ConnectionProvider):
    """
    Connections to a graph on a FalkorDB server, on the async falkordb client, which is made on the first connection
    and shared.
    """

    def __init__(
        self,
        name: str,
        graph_name: str,
        host: str,
        port: int,
        user_name: str | None = None,
        password: str | None = None,
        connection_timeout: int | None = None,
        transaction_mode: TransactionMode = TransactionMode.WARN,
    ) -> None:
        self.name = name
        self.type = DatabaseType.FALKORDB
        self.graph_name = graph_name
        self.host = host
        self.port = port
        self.user_name = user_name
        self.password = password
        self.connection_timeout = connection_timeout
        self.transaction_mode = transaction_mode
        self.transactions = AutoCommitTransactions("FalkorDB", transaction_mode)
        self._client: Any = None

    @property
    def client(self) -> Any:
        if self._client is None:
            from falkordb.asyncio import FalkorDB

            timeout = self.connection_timeout / 1000 if self.connection_timeout is not None else None
            self._client = FalkorDB(
                host=self.host,
                port=self.port,
                username=self.user_name,
                password=self.password,
                socket_connect_timeout=timeout,
            )
        return self._client

    async def connect(self) -> Connection:
        return FalkorDbConnection(self.client.select_graph(self.graph_name), self.transactions, short_id())

    async def end(self) -> None:
        # Cleared before waiting, so a second call made meanwhile doesn't close the client again.
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
