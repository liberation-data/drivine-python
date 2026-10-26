# Written by scripts/unasync.py from drivine/connection/oracle/oracle_connection_provider.py:
# change that, and run the script again.
from __future__ import annotations

from typing import Any

from drivine.backend.oracle.oracle_graph import DEFAULT_GRAPH, OracleGraph
from drivine.connection.database_type import DatabaseType
from drivine.sync.connection.connection import Connection
from drivine.sync.connection.connection_provider import ConnectionProvider
from drivine.sync.connection.graph_tables import GraphTables
from drivine.sync.connection.oracle.oracle_connection import OracleConnection


class OracleConnectionProvider(ConnectionProvider):
    """
    Connections to Oracle, from a pool of its own, through python-oracledb in thin mode (no Instant Client). The
    graph's tables are made on the first connection when they aren't there (see GraphTables), so Cypher or SQL sent
    through a persistence manager alone finds them, as an object manager's statements do.
    """

    def __init__(
        self,
        name: str,
        connect_string: str,
        user_name: str | None = None,
        password: str | None = None,
        pool_max: int = 10,
        connection_timeout: int | None = None,
        graph: str | None = None,
    ) -> None:
        """`connect_string` is `host:port/service`, say `localhost:1521/FREEPDB1`; `connection_timeout` is in ms."""
        self.name = name
        self.type = DatabaseType.ORACLE
        self.connect_string = connect_string
        self.user_name = user_name
        self.password = password
        self.pool_max = pool_max
        self.connection_timeout = connection_timeout
        # Refuses a name that can't name the graph's tables now, rather than at the first connection.
        self.graph = OracleGraph(graph or DEFAULT_GRAPH)
        self.graph_name = self.graph.name
        self._pool: Any = None
        self._tables = GraphTables(self.graph.ensuring)

    def connect(self) -> Connection:
        """
        A connection, the graph's tables made first if this is the pool's first (schema statements commit on their own).
        """
        pool = self._pool_of()
        connection = pool.acquire()
        try:
            self._tables.ensure(lambda statement: self._run(connection, statement))
            cursor = connection.cursor()
            cursor.execute("SELECT SYS_CONTEXT('USERENV', 'SID') FROM dual")
            row = cursor.fetchone()
            cursor.close()
        except BaseException:
            pool.release(connection)
            raise
        return OracleConnection(connection, pool.release, f"oracle.{row[0] if row else '?'}", self.graph.name)

    def end(self) -> None:
        # Cleared before waiting, so a second call made meanwhile doesn't close the pool again.
        pool, self._pool = self._pool, None
        self._tables.forget()
        if pool is not None:
            pool.close(force=True)

    def _run(self, connection: Any, statement: str) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute(statement)
        finally:
            cursor.close()

    def _pool_of(self) -> Any:
        if self._pool is None:
            import oracledb

            self._pool = oracledb.create_pool(
                user=self.user_name,
                password=self.password,
                dsn=self.connect_string,
                min=0,
                max=self.pool_max,
                tcp_connect_timeout=self.connection_timeout / 1000 if self.connection_timeout else None,
            )
        return self._pool
