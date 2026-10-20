from __future__ import annotations

from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase, Auth, TrustSystemCAs

from drivine.connection.database_type import DatabaseType
from drivine.connection.neo4j.connection import Neo4jConnection
from drivine.connection.provider import Connection, ConnectionProvider, short_id


class Neo4jConnectionProvider(ConnectionProvider):
    """
    Connections to a Bolt engine, on the async Neo4j driver: Neo4j, Memgraph and Neptune. The driver is made on the
    first connection, inside the event loop that will use it.
    """

    def __init__(
        self,
        name: str,
        type: DatabaseType,
        uri: str,
        auth: Auth | tuple[str, str] | None,
        database: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.type = type
        self.uri = uri
        self.auth = auth
        self.database = database
        self.config = config or {}
        self._driver: AsyncDriver | None = None

    @property
    def driver(self) -> AsyncDriver:
        if self._driver is None:
            self._driver = AsyncGraphDatabase.driver(self.uri, auth=self.auth, **self.config)  # pyright: ignore[reportUnknownMemberType]
        return self._driver

    async def connect(self) -> Connection:
        return Neo4jConnection(self.driver.session(database=self.database), short_id())  # pyright: ignore[reportUnknownMemberType]

    async def end(self) -> None:
        if self._driver is not None:
            driver, self._driver = self._driver, None
            await driver.close()


def neo4j_provider(
    name: str,
    type: DatabaseType,
    host: str,
    port: int,
    user: str | None,
    password: str | None,
    database: str | None,
    protocol: str,
    pool_max: int | None,
    connection_timeout: int | None,
) -> Neo4jConnectionProvider:
    """
    Neo4j, or Memgraph, whose default install has auth off, so its credentials are optional.
    """
    auth = (user, password or "") if user else None
    return Neo4jConnectionProvider(
        name, type, f"{protocol}://{host}:{port}", auth, database, _config(pool_max, connection_timeout)
    )


def neptune_provider(
    name: str, host: str, port: int, protocol: str, connection_timeout: int | None
) -> Neo4jConnectionProvider:
    """
    Neptune speaks Bolt over TLS. Without IAM auth it takes any basic token, and one connection at a time.
    """
    auth = Auth("basic", "username", "", realm="realm")
    config = _config(1, connection_timeout)
    if protocol == "bolt":
        config.update(encrypted=True, trusted_certificates=TrustSystemCAs())
    return Neo4jConnectionProvider(name, DatabaseType.NEPTUNE, f"{protocol}://{host}:{port}", auth, None, config)


def _config(pool_max: int | None, connection_timeout: int | None) -> dict[str, Any]:
    config: dict[str, Any] = {}
    if pool_max is not None:
        config["max_connection_pool_size"] = pool_max
    if connection_timeout is not None:
        config["connection_timeout"] = connection_timeout / 1000
    return config
