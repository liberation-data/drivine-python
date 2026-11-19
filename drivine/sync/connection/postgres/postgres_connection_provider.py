# Written by scripts/unasync.py from drivine/connection/postgres/postgres_connection_provider.py:
# change that, and run the script again.
from __future__ import annotations

from typing import Any

from drivine.backend.postgres.postgres_graph import PostgresGraph
from drivine.backend.sql.sql_graph import DEFAULT_GRAPH
from drivine.backend.sql.store_layout import stamping
from drivine.connection.database_type import DatabaseType
from drivine.sync.connection.connection import Connection
from drivine.sync.connection.connection_provider import ConnectionProvider
from drivine.sync.connection.graph_tables import GraphTables, judge
from drivine.sync.connection.postgres.postgres_connection import PostgresConnection
from drivine.sync.manager.contention import ddl_deadlocked, when_not_contended


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
        # Kept private (#503): not part of the provider's API, nor of anything that prints its attributes by name.
        self._password = password
        self.pool_max = pool_max
        self.connection_timeout = connection_timeout
        self.idle_timeout = idle_timeout
        # Refuses a name that can't name the graph's tables now, rather than at the first statement.
        self.graph = PostgresGraph(graph or DEFAULT_GRAPH)
        self.graph_name = self.graph.name
        self._pool: Any = None
        self._tables = GraphTables(self.graph.ensuring)

    def connect(self) -> Connection:
        """
        A connection, the graph's tables made first if this is the pool's first. The block is one statement, so it is
        applied whole or not at all. One turned away by a deadlock with other sessions' writes (40P01), as a session
        that waited for another's making may be when it then runs the block's statements over tables in use, is run
        again after a pause, and finds the store made (see ddl_deadlocked; #287, liberation-data/drivine#454).
        """
        pool = self._pool_of()
        # Opening an open pool does nothing: two first connections open it once.
        pool.open()
        connection = pool.getconn()
        try:
            self._tables.ensure(lambda statement: self._ensure(connection, pool, statement))
        except BaseException:
            pool.putconn(connection)
            raise
        return PostgresConnection(connection, pool.putconn, f"postgres.{connection.info.backend_pid}", self.graph.name)

    def connect_unjudged(self) -> Connection:
        """
        A connection that neither makes the graph's tables nor judges its store layout, as `connect` does first: what a
        store layout migration runs on, as the graph it migrates is one `connect` refuses (see StoreLayoutMigration).
        """
        pool = self._pool_of()
        pool.open()
        connection = pool.getconn()
        return PostgresConnection(connection, pool.putconn, f"postgres.{connection.info.backend_pid}", self.graph.name)

    def prepare_unjudged(self, connection: Connection) -> None:
        """
        Makes the graph's tables on `connection`, one of `connect_unjudged`'s, as a first connection makes them,
        without the layout's verdict: a store layout migration's `prepare`.
        """
        assert isinstance(connection, PostgresConnection)
        statement = self.graph.ensuring
        when_not_contended(lambda: connection.connection.execute(statement), ddl_deadlocked)

    def stamp_statement(self) -> tuple[str, dict[str, Any]]:
        """
        The stamp's `assign_if_absent` of this layout, and what it binds: a new id and this moment, as a first
        connection stamps a graph (see store_layout.stamping). A store layout migration's `assignStamp`.
        """
        stamp = stamping("postgres", self.graph.name)
        return stamp.statement, dict(stamp.parameters)

    def _ensure(self, connection: Any, pool: Any, statement: str) -> None:
        """
        Judges the graph's store layout (see graph_tables.judge), then runs `statement`, the graph's `ensuring`: a graph
        this version doesn't read is refused with StoreLayoutError before anything is made in it.
        """
        graph = self.graph
        judge("postgres", graph.name, graph.tables_made, PostgresConnection(connection, pool.putconn, "", graph.name))
        when_not_contended(lambda: connection.execute(statement), ddl_deadlocked)

    def end(self) -> None:
        # Cleared before waiting, so a second call made meanwhile doesn't close the pool again.
        pool, self._pool = self._pool, None
        self._tables.forget()
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
                password=self._password,
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
