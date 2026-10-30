from __future__ import annotations

from typing import Any

from drivine.backend.postgres.postgres_graph import PostgresGraph
from drivine.backend.sql.sql_graph import DEFAULT_GRAPH
from drivine.connection.connection import Connection
from drivine.connection.connection_provider import ConnectionProvider
from drivine.connection.database_type import DatabaseType
from drivine.connection.graph_tables import GraphTables
from drivine.connection.postgres.postgres_connection import PostgresConnection
from drivine.manager.contention import ddl_deadlocked, when_not_contended


class PostgresConnectionProvider(ConnectionProvider):
    """
    Connections to Postgres, from a pool of its own, through psycopg. Each connection commits a statement as it runs,
    outside a transaction. The graph's tables are made on the first connection when they aren't there (see
    GraphTables), so Cypher or SQL sent through a persistence manager alone finds them, as an object manager's
    statements do.
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
        `connection_timeout` and `idle_timeout` are in ms. `graph` is the graph this database's managers work on, the
        tables Cypher is compiled to SQL over: `drivine` unless named.
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
        self.graph = PostgresGraph(graph or DEFAULT_GRAPH)
        self.graph_name = self.graph.name
        self._pool: Any = None
        self._tables = GraphTables(self.graph.ensuring)

    async def connect(self) -> Connection:
        """
        A connection, the graph's tables made first if this is the pool's first. The block is one statement, so it is
        applied whole or not at all. One turned away by a deadlock with other sessions' writes (40P01), as a session
        that waited for another's making may be when it then runs the block's statements over tables in use, is run
        again after a pause, and finds the store made (see ddl_deadlocked; #287, liberation-data/drivine#454).
        """
        pool = self._pool_of()
        # Opening an open pool does nothing: two first connections open it once.
        await pool.open()
        connection = await pool.getconn()
        try:
            await self._tables.ensure(
                lambda statement: when_not_contended(lambda: connection.execute(statement), ddl_deadlocked)
            )
        except BaseException:
            await pool.putconn(connection)
            raise
        return PostgresConnection(connection, pool.putconn, f"postgres.{connection.info.backend_pid}", self.graph.name)

    async def end(self) -> None:
        # Cleared before waiting, so a second call made meanwhile doesn't close the pool again.
        pool, self._pool = self._pool, None
        self._tables.forget()
        if pool is not None:
            await pool.close()

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
            self._pool = psycopg_pool.AsyncConnectionPool(
                conninfo,
                min_size=0,
                max_size=self.pool_max,
                max_idle=self.idle_timeout / 1000 if self.idle_timeout else 600.0,
                kwargs={"autocommit": True, "row_factory": dict_row},
                open=False,
            )
        return self._pool
