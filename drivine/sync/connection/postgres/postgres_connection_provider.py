# Written by scripts/unasync.py from drivine/connection/postgres/postgres_connection_provider.py:
# change that, and run the script again.
from __future__ import annotations

from typing import Any

from drivine.backend.postgres.postgres_graph import PostgresGraph
from drivine.backend.sql.sql_graph import DEFAULT_GRAPH
from drivine.connection.database_type import DatabaseType
from drivine.sync.connection.connection import Connection
from drivine.sync.connection.connection_provider import ConnectionProvider
from drivine.sync.connection.postgres.postgres_connection import PostgresConnection


class PostgresConnectionProvider(ConnectionProvider):
    """
    Connections to Postgres, from a pool of its own, through psycopg. Each connection commits a statement as it runs,
    outside a transaction.
    """

    def __init__(
        self,
        name: str,
        host: str,
        port: int,
        database: str,
        user_name: str | None = None,
        password: str | None = None,
        pool_max: int = 10,
        connection_timeout: int | None = None,
        idle_timeout: int | None = None,
        graph: str | None = None,
    ) -> None:
        """
        `connection_timeout` and `idle_timeout` are in ms. `graph` is the graph an object manager on this database
        works on: `drivine` unless named. Its tables are made by the object manager, before its first statement; a
        persistence manager's plain SQL never makes them.
        """
        self.name = name
        self.type = DatabaseType.POSTGRES
        self.host = host
        self.port = port
        self.database = database
        self.user_name = user_name
        self.password = password
        self.pool_max = pool_max
        self.connection_timeout = connection_timeout
        self.idle_timeout = idle_timeout
        # Refuses a name that can't name the graph's tables now, rather than at the first statement.
        self.graph_name = PostgresGraph(graph or DEFAULT_GRAPH).name
        self._pool: Any = None

    def connect(self) -> Connection:
        pool = self._pool_of()
        # Opening an open pool does nothing: two first connections open it once.
        pool.open()
        connection = pool.getconn()
        return PostgresConnection(connection, pool.putconn, f"postgres.{connection.info.backend_pid}")

    def end(self) -> None:
        # Cleared before waiting, so a second call made meanwhile doesn't close the pool again.
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()

    def _pool_of(self) -> Any:
        if self._pool is None:
            import psycopg_pool
            from psycopg.conninfo import make_conninfo
            from psycopg.rows import dict_row

            conninfo = make_conninfo(
                host=self.host,
                port=self.port,
                dbname=self.database,
                user=self.user_name,
                password=self.password,
                connect_timeout=max(1, self.connection_timeout // 1000) if self.connection_timeout else None,
            )
            self._pool = psycopg_pool.ConnectionPool(
                conninfo,
                min_size=0,
                max_size=self.pool_max,
                max_idle=self.idle_timeout / 1000 if self.idle_timeout else 600.0,
                kwargs={"autocommit": True, "row_factory": dict_row},
                open=False,
            )
        return self._pool
