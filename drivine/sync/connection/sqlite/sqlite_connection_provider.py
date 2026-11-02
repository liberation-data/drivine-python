# Written by scripts/unasync.py from drivine/connection/sqlite/sqlite_connection_provider.py:
# change that, and run the script again.
from __future__ import annotations

import itertools

from drivine.backend.sql.sql_graph import DEFAULT_GRAPH
from drivine.backend.sqlite.sqlite_graph import SqliteGraph
from drivine.connection.database_type import DatabaseType
from drivine.connection.sqlite.sqlite_session import DEFAULT_BUSY_TIMEOUT, SqliteSession
from drivine.sync import concurrency
from drivine.sync.connection.connection import Connection
from drivine.sync.connection.connection_provider import ConnectionProvider
from drivine.sync.connection.graph_tables import GraphTables
from drivine.sync.connection.sqlite.sqlite_connection import SqliteConnection

_SESSIONS = itertools.count(1)
"""Numbers the connections a provider opens, which name them in the log."""


class SqliteConnectionProvider(ConnectionProvider):
    """
    Connections to the SQLite file at `path`, kept in a pool of their own, each opened as Drivine's statements want it
    (see SqliteSession). The file is made if it is not there, and the graph's tables on the first connection where
    they aren't (see GraphTables), so SQL sent through a persistence manager alone finds them, as an object manager's
    statements do.

    A file has no server, user or password. It is drivine4j's store, so a file one library wrote, another reads.
    """

    def __init__(
        self,
        name: str,
        path: str,
        pool_max: int = 10,
        busy_timeout: int | None = None,
        graph: str | None = None,
    ) -> None:
        """
        `busy_timeout` is how long, in ms, a connection waits for the file another holds locked: 30 s by default.
        `graph` is the graph this database's managers work on, the tables they read and write: `drivine` unless named.
        """
        self.name = name
        self.type = DatabaseType.SQLITE
        self.path = path
        self.pool_max = pool_max
        self.busy_timeout = busy_timeout if busy_timeout is not None else DEFAULT_BUSY_TIMEOUT
        # Refuses a name that can't name the graph's tables now, rather than at the first statement.
        self.graph = SqliteGraph(graph or DEFAULT_GRAPH)
        self.graph_name = self.graph.name
        self._idle: list[SqliteSession] = []
        self._pooling = concurrency.Lock()
        self._ended = False
        self._tables = GraphTables(self.graph.made)

    def connect(self) -> Connection:
        """A connection, from the pool or newly opened; the graph's tables made first if this is the pool's first."""
        session = self._session()
        try:
            self._tables.ensure(lambda _: concurrency.blocking(session.ensure, self.graph, on=session.thread))
        except BaseException:
            self._give_back(session)
            raise
        return SqliteConnection(session, self._give_back, f"sqlite.{next(_SESSIONS)}", self.graph.name or DEFAULT_GRAPH)

    def end(self) -> None:
        with self._pooling:
            idle, self._idle = self._idle, []
            self._ended = True
        self._tables.forget()
        for session in idle:
            concurrency.blocking(session.close, on=session.thread)

    def _session(self) -> SqliteSession:
        with self._pooling:
            self._ended = False
            if self._idle:
                return self._idle.pop()
        return concurrency.blocking(SqliteSession, self.path, self.busy_timeout)

    def _give_back(self, session: SqliteSession) -> None:
        """Keeps the connection for the next, unless the pool holds as many as it keeps or has ended."""
        with self._pooling:
            kept = not self._ended and len(self._idle) < self.pool_max
            if kept:
                self._idle.append(session)
        if not kept:
            concurrency.blocking(session.close, on=session.thread)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r}, type={self.type.value}, path={self.path!r})"
