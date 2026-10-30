# Written by scripts/unasync.py from drivine/connection/oracle/oracle_connection_provider.py:
# change that, and run the script again.
from __future__ import annotations

import contextlib
import logging
import re
from typing import Any

from drivine.backend.oracle.oracle_graph import DEFAULT_GRAPH, OracleGraph
from drivine.connection.database_type import DatabaseType
from drivine.sync.connection.connection import Connection
from drivine.sync.connection.connection_provider import ConnectionProvider
from drivine.sync.connection.graph_tables import GraphTables
from drivine.sync.connection.oracle.oracle_connection import OracleConnection
from drivine.sync.manager.contention import ddl_deadlocked, when_not_contended

logger = logging.getLogger("drivine.connection")

ORACLE_MAJOR = 23
ORACLE_MINOR = 9
"""The oldest Oracle Drivine runs Cypher on, 23.9: see oracle_refusal."""


def oracle_refusal(version: Any) -> str | None:
    """
    Why Drivine runs no Cypher on the Oracle of `version`, as python-oracledb gives it (`connection.version`,
    "23.26.3.0.0"): one older than 23.9, which has no `UUID()`, the random version 4 UUID compiled Cypher's
    `randomUUID()` is (#252, as liberation-data/drivine4j#401). None for 23.9 or later, and for a version that can't be
    read: the database says what is missing when a statement needs it.
    """
    found = re.match(r"(\d+)\.(\d+)", version) if isinstance(version, str) else None
    if found is None or (int(found[1]), int(found[2])) >= (ORACLE_MAJOR, ORACLE_MINOR):
        return None
    return (
        f"Drivine needs Oracle {ORACLE_MAJOR}.{ORACLE_MINOR} or later (it uses UUID()); this database is {version}. "
        f"Upgrade, or use an Oracle Free image {ORACLE_MAJOR}.{ORACLE_MINOR} or newer. Plain SQL over this provider "
        "still runs; Cypher does not."
    )


NO_ADAPTIVE_PLANS = "ALTER SESSION SET OPTIMIZER_ADAPTIVE_PLANS = FALSE"
"""
Run in each session the pool opens: Oracle makes no adaptive plan in it. An adaptive plan picks its joins as it runs,
and once the graph's tables had grown and shrunk without statistics, one gave no rows for a join that filters a node by
a label and a property, where the same plan, fixed, gave them all (#146, liberation-data/drivine#331). A walk's step
already asks for no adaptive plan; this covers every statement. One statement can ask for an adaptive plan again:
`/*+ OPT_PARAM('optimizer_adaptive_plans' 'true') */`.
"""


class OracleConnectionProvider(ConnectionProvider):
    """
    Connections to Oracle, from a pool of its own, through python-oracledb in thin mode (no Instant Client). The
    graph's tables are made on the first connection when they aren't there (see GraphTables), so Cypher or SQL sent
    through a persistence manager alone finds them, as an object manager's statements do.

    Drivine needs Oracle 23.9 or later. The first connection's version, read once for the pool as the tables are made,
    says whether the store runs Cypher: on an older Oracle a connection is still handed out and plain SQL runs, and
    Cypher, which needs Oracle's `UUID()`, is refused with why (see oracle_refusal).
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
        sets_no_adaptive_plans: bool = True,
    ) -> None:
        """
        `connect_string` is `host:port/service`, say `localhost:1521/FREEPDB1`; `connection_timeout` is in ms.
        `sets_no_adaptive_plans` is whether each session the pool opens is set to make no adaptive plan (see
        NO_ADAPTIVE_PLANS). On by default: an adaptive plan can give a read of the graph no rows, and no error (#146).
        False leaves the sessions as Oracle opens them; run the statement in each yourself then, or tune the database.
        """
        self.name = name
        self.type = DatabaseType.ORACLE
        self.connect_string = connect_string
        self.user_name = user_name
        self.password = password
        self.pool_max = pool_max
        self.connection_timeout = connection_timeout
        self.sets_no_adaptive_plans = sets_no_adaptive_plans
        # Refuses a name that can't name the graph's tables now, rather than at the first connection.
        self.graph = OracleGraph(graph or DEFAULT_GRAPH)
        self.graph_name = self.graph.name
        self._pool: Any = None
        self._tables = GraphTables(self.graph.ensuring)
        self._cypher_refusal: str | None = None
        """Why this store runs no Cypher, found on the first connection: see oracle_refusal. None where it runs it."""

    def connect(self) -> Connection:
        """
        A connection, the graph's tables made first if this is the pool's first (schema statements commit on their own).
        """
        pool = self._pool_of()
        connection = pool.acquire()
        try:
            self._tables.ensure(lambda statement: self._ensure(connection, statement))
            cursor = connection.cursor()
            cursor.execute("SELECT SYS_CONTEXT('USERENV', 'SID') FROM dual")
            row = cursor.fetchone()
            cursor.close()
        except BaseException:
            pool.release(connection)
            raise
        return OracleConnection(
            connection,
            pool.release,
            f"oracle.{row[0] if row else '?'}",
            self.graph.name,
            f"{self.connect_string}|{self.user_name or ''}",
            self._cypher_refusal,
        )

    def end(self) -> None:
        # Cleared before waiting, so a second call made meanwhile doesn't close the pool again.
        pool, self._pool = self._pool, None
        self._tables.forget()
        if pool is not None:
            pool.close(force=True)

    def _ensure(self, connection: Any, statement: str) -> None:
        """
        Runs `statement`, the graph's `ensuring`, and has every session asked again whether it may call the graph's
        functions (see OracleStoredFunctions), as the block makes them where they were missing. First it reads the
        database's version, which the driver found as it connected and which needs no grant: see oracle_refusal.

        A block turned away by a deadlock with another session, as when the DDL of one pool's first connection and other
        pools' first writes each wait on the other's lock on the node table (ORA-04020), is run again after a pause (see
        ddl_deadlocked): what the block made before it was turned away stays, and the block makes the rest, or finds the
        store another session finished meanwhile and does nothing (#287, liberation-data/drivine#454). One whose lock
        stayed busy (ORA-00054) is not: it has waited OracleGraph.DDL_WAIT_SECONDS inside the block already.
        """
        self._check_version(connection)
        when_not_contended(lambda: self._run(connection, statement), ddl_deadlocked)
        self.graph.ensured()

    def _check_version(self, connection: Any) -> None:
        """Finds whether the Oracle behind `connection` is too old for Cypher, and warns if so."""
        try:
            self._cypher_refusal = oracle_refusal(connection.version)
        except Exception:
            logger.debug("Could not read the version of Oracle database '%s'.", self.name, exc_info=True)
            self._cypher_refusal = None
        if self._cypher_refusal:
            logger.warning("ORACLE database '%s': %s", self.name, self._cypher_refusal)

    def _run(self, connection: Any, statement: str) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute(statement)
        finally:
            cursor.close()

    def _pool_of(self) -> Any:
        if self._pool is None:
            import oracledb

            pool: Any = None

            def without_adaptive_plans(connection: Any, _requested_tag: str | None) -> None:
                """
                Sets a session the pool opens to make no adaptive plan. One that can't be set is dropped from the pool
                and its error raised from `acquire`, rather than given out unset: python-oracledb keeps a session whose
                callback failed counted as out, and an async pool at its size then waits for it for ever
                (oracle/python-oracledb#626).
                """
                try:
                    self._run(connection, NO_ADAPTIVE_PLANS)
                except BaseException:
                    with contextlib.suppress(Exception):
                        pool.drop(connection)
                    raise

            pool = oracledb.create_pool(
                user=self.user_name,
                password=self.password,
                dsn=self.connect_string,
                min=0,
                max=self.pool_max,
                tcp_connect_timeout=self.connection_timeout / 1000 if self.connection_timeout else None,
                session_callback=without_adaptive_plans if self.sets_no_adaptive_plans else None,
            )
            self._pool = pool
        return self._pool
