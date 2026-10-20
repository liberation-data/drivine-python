from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Self

from drivine.connection.database_type import DatabaseType, TransactionMode
from drivine.connection.properties import ConnectionProperties
from drivine.connection.provider import ConnectionProvider
from drivine.errors import DrivineError

logger = logging.getLogger("drivine.connection")

DEFAULT = "default"

_AUTO_COMMIT_ENGINES = (DatabaseType.FALKORDB,)
"""Engines without multi-statement transactions, which honour TransactionMode."""


class DatabaseRegistry:
    """
    The application's databases, by name. `"default"` is the first one registered.
    """

    def __init__(self) -> None:
        self._providers: dict[str, ConnectionProvider] = {}

    @property
    def providers(self) -> list[ConnectionProvider]:
        return list(self._providers.values())

    def builder(self) -> ConnectionProviderBuilder:
        return ConnectionProviderBuilder(self)

    def build_or_resolve_from_env(
        self, name: str | None = None, env: Mapping[str, str] | None = None
    ) -> ConnectionProvider:
        """
        Registers the database the environment describes, as `DATABASE_TYPE`, `DATABASE_HOST` and so on, or
        `<NAME>_DATABASE_TYPE`… for a named one. A database already registered under the name is returned as it is.
        """
        retained = self.registered(name or DEFAULT)
        if retained is not None:
            return retained
        return self.builder().with_properties(ConnectionProperties.from_env(name, env)).register(name or DEFAULT)

    def connection_provider(self, name: str = DEFAULT) -> ConnectionProvider | None:
        if name == DEFAULT and name not in self._providers:
            return next(iter(self._providers.values()), None)
        return self._providers.get(name)

    def registered(self, name: str) -> ConnectionProvider | None:
        """The provider registered under exactly this name."""
        return self._providers.get(name)

    def register(self, provider: ConnectionProvider) -> None:
        self._providers[provider.name] = provider

    async def close(self) -> None:
        """
        Ends every provider: their drivers and pools. Call it once, when the application stops.
        """
        providers = list(self._providers.values())
        self._providers.clear()
        results = await asyncio.gather(*(it.end() for it in providers), return_exceptions=True)
        for provider, result in zip(providers, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("Ending %s failed: %s", provider, result)


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

    def register(self, name: str = DEFAULT) -> ConnectionProvider:
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
        else:
            raise DrivineError(f"{self._type} is not supported by drivine-python yet.")
        self.registry.register(provider)
        return provider

    def _bolt_provider(self, name: str) -> ConnectionProvider:
        from drivine.connection.neo4j.provider import neo4j_provider

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
        from drivine.connection.neo4j.provider import neptune_provider

        assert self._host is not None
        return neptune_provider(
            name, self._host, self._port or 8182, self._protocol or "bolt", self._connection_timeout
        )
