from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self

from drivine.connection.connection_properties import ConnectionProperties
from drivine.connection.connection_provider import ConnectionProvider
from drivine.connection.database_type import DatabaseType
from drivine.connection.transaction_mode import TransactionMode
from drivine.drivine_error import DrivineError

if TYPE_CHECKING:
    from drivine.connection.database_registry import DatabaseRegistry

logger = logging.getLogger("drivine.connection")

_AUTO_COMMIT_ENGINES = (DatabaseType.FALKORDB,)
"""Engines without multi-statement transactions, which honour TransactionMode."""


class ConnectionProviderBuilder:
    def __init__(self, registry: DatabaseRegistry) -> None:
        self.registry = registry
        self._type: DatabaseType | None = None
        self._host: str | None = None
        self._port: int | None = None
        self._user_name: str | None = None
        self._password: str | None = None
        self._protocol: str | None = None
        self._pool_max: int = 40
        self._connection_timeout: int = 5000
        self._idle_timeout: int | None = None
        self._database_name: str | None = None
        self._transaction_mode: TransactionMode | None = None
        self._default_graph_path: str | None = None

    def with_properties(self, properties: ConnectionProperties) -> Self:
        self._type = properties.database_type
        self._host = properties.host
        self._port = properties.port
        self._user_name = properties.user_name
        self._password = properties.password
        self._protocol = properties.protocol
        self._pool_max = properties.pool_max if properties.pool_max is not None else 40
        self._connection_timeout = properties.connection_timeout if properties.connection_timeout is not None else 5000
        self._idle_timeout = properties.idle_timeout
        self._database_name = properties.database_name
        self._transaction_mode = properties.transaction_mode
        self._default_graph_path = properties.default_graph_path
        return self

    def database_type(self, type: DatabaseType) -> Self:
        self._type = type
        return self

    def host(self, host: str) -> Self:
        self._host = host
        return self

    def port(self, port: int) -> Self:
        self._port = port
        return self

    def user_name(self, user_name: str) -> Self:
        self._user_name = user_name
        return self

    def password(self, password: str) -> Self:
        self._password = password
        return self

    def protocol(self, protocol: str) -> Self:
        self._protocol = protocol
        return self

    def pool_max(self, pool_max: int) -> Self:
        self._pool_max = pool_max
        return self

    def connection_timeout(self, milliseconds: int) -> Self:
        self._connection_timeout = milliseconds
        return self

    def idle_timeout(self, milliseconds: int) -> Self:
        self._idle_timeout = milliseconds
        return self

    def database_name(self, name: str) -> Self:
        self._database_name = name
        return self

    def transaction_mode(self, mode: TransactionMode) -> Self:
        self._transaction_mode = mode
        return self

    def default_graph_path(self, path: str) -> Self:
        self._default_graph_path = path
        return self

    def register(self, name: str = "default") -> ConnectionProvider:
        """
        Registers the database under the name, and returns its provider. A database already registered under the
        name is returned as it is.
        """
        retained = self.registry.registered(name)
        if retained is not None:
            return retained
        if self._type is None:
            raise DrivineError("A database type is required.")
        if not self._host:
            raise DrivineError("host config is required")
        if self._default_graph_path and self._type is not DatabaseType.ORACLE:
            logger.warning("default_graph_path is ignored by %s, which names no graph", self._type)
        if self._transaction_mode and self._type not in _AUTO_COMMIT_ENGINES:
            logger.warning("transaction_mode is ignored by %s, which supports transactions", self._type)
        if self._type in (DatabaseType.NEO4J, DatabaseType.MEMGRAPH):
            provider = self._bolt_provider(name)
        elif self._type is DatabaseType.NEPTUNE:
            provider = self._neptune_provider(name)
        elif self._type is DatabaseType.FALKORDB:
            provider = self._falkordb_provider(name)
        else:
            raise DrivineError(f"{self._type} is not supported by drivine-python yet.")
        self.registry.register(provider)
        return provider

    def _bolt_provider(self, name: str) -> ConnectionProvider:
        from drivine.connection.neo4j.neo4j_connection_provider import neo4j_provider

        engine = "Neo4j" if self._type is DatabaseType.NEO4J else "Memgraph"
        if self._type is DatabaseType.NEO4J and not self._user_name:
            raise DrivineError("Neo4j requires a user name")
        if self._idle_timeout:
            logger.warning("idle_timeout is not supported by %s", engine)
        port = self._port or 7687
        if port != 7687:
            logger.warning("%s is a non-standard port for %s", port, engine)
        assert self._type is not None and self._host is not None
        return neo4j_provider(
            name,
            self._type,
            self._host,
            port,
            self._user_name,
            self._password,
            self._database_name,
            self._protocol or "bolt",
            self._pool_max,
            self._connection_timeout,
        )

    def _neptune_provider(self, name: str) -> ConnectionProvider:
        from drivine.connection.neo4j.neo4j_connection_provider import neptune_provider

        assert self._host is not None
        return neptune_provider(
            name, self._host, self._port or 8182, self._protocol or "bolt", self._connection_timeout
        )

    def _falkordb_provider(self, name: str) -> ConnectionProvider:
        """
        FalkorDB serves several graphs from one server: `database_name` names the one to work on, `graph` by default.
        """
        from drivine.connection.falkordb.falkordb_connection_provider import FalkorDbConnectionProvider

        assert self._host is not None
        if self._idle_timeout:
            logger.warning("idle_timeout is not supported by FalkorDB")
        return FalkorDbConnectionProvider(
            name,
            self._database_name or "graph",
            self._host,
            self._port or 6379,
            self._user_name,
            self._password,
            self._connection_timeout,
            self._transaction_mode or TransactionMode.WARN,
        )
