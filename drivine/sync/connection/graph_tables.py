# Written by scripts/unasync.py from drivine/connection/graph_tables.py:
# change that, and run the script again.
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from drivine.sync import concurrency

logger = logging.getLogger("drivine.connection")

_ORACLE_WILL_NOT_MAKE = re.compile(r"ORA-(01031|16000)\b")
_POSTGRES_WILL_NOT_MAKE = ("42501", "25006")


def will_not_make(failure: BaseException) -> bool:
    """
    Whether the store refused the block for good, so that running it again on the next connection would fail the
    same way: a user who may not make tables (ORA-01031 on Oracle, 42501 on Postgres), or a database open read-only
    (ORA-16000; 25006, as on a hot standby). The failure and each cause under it are looked at.
    """
    cause: BaseException | None = failure
    for _ in range(10):
        if cause is None:
            return False
        if _ORACLE_WILL_NOT_MAKE.search(str(cause)) or getattr(cause, "sqlstate", None) in _POSTGRES_WILL_NOT_MAKE:
            return True
        cause = cause.__cause__
    return False


class GraphTables:
    """
    The tables of the graph a SQL store's connection provider works on (Oracle, Postgres), made on the provider's first
    connection: by `ensuring`, the statement that makes them when they aren't there and does nothing when they are, the
    one an object manager on the store would need. So whatever is sent first, a persistence manager's Cypher or plain
    SQL, an object manager's statement or a schema change, finds the tables.

    It runs once per pool: connections that ask at once, from tasks or threads, wait for the first, which runs it; once
    it has run, a connection asks nothing more. One that fails is run again by the next connection. The statement
    checks before it writes, so a user who may read the tables but not make them works once they are there. Pools or
    processes that first connect at once each run it, and the statement makes the tables once among them: Postgres's
    takes a lock, Oracle's waits on the other session's locks (see OracleGraph.ensuring). One turned away by a deadlock
    with another session is run again by the provider's `run` itself (#287): the connections waiting wait through it,
    and only a failure it gives up on is one here.

    A store that will not make the tables (see will_not_make), as a user who may not make them or a database open
    read-only, is warned of once and not asked again on this pool: the connection is handed out, plain SQL works, and a
    statement over tables that are not there says so. Asked again, the block would fail every connection.
    """

    def __init__(self, ensuring: str) -> None:
        self.ensuring = ensuring
        self._making = concurrency.Lock()
        self._made = False

    def ensure(self, run: Callable[[str], Any]) -> None:
        """Runs `ensuring` with `run`, on the connection asking, unless it has run on this pool."""
        if self._made:
            return
        with self._making:
            if not self._made:
                try:
                    run(self.ensuring)
                except Exception as failure:
                    if not will_not_make(failure):
                        raise
                    logger.warning(
                        "Could not make the graph's tables, and not tried again on this pool: the user may not make "
                        "them, or the database is read-only. Plain SQL works; statements over tables that are not "
                        "there fail. (%s)",
                        failure,
                    )
                self._made = True

    def forget(self) -> None:
        """Has the next connection run it again, as a pool that was closed is a new one."""
        self._made = False
