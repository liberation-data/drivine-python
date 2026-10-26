from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from drivine import concurrency


class GraphTables:
    """
    The tables of the graph a SQL store's connection provider works on (Oracle, Postgres), made on the provider's first
    connection: by `ensuring`, the statement that makes them when they aren't there and does nothing when they are, the
    one an object manager on the store would need. So whatever is sent first, a persistence manager's Cypher or plain
    SQL, an object manager's statement or a schema change, finds the tables.

    It runs once per pool: connections that ask at once, from tasks or threads, wait for the first, which runs it; once
    it has run, a connection asks nothing more. One that fails is run again by the next connection. The statement
    checks before it writes, so a user who may read the tables but not make them works once they are there.
    """

    def __init__(self, ensuring: str) -> None:
        self.ensuring = ensuring
        self._making = concurrency.Lock()
        self._made = False

    async def ensure(self, run: Callable[[str], Awaitable[Any]]) -> None:
        """Runs `ensuring` with `run`, on the connection asking, unless it has run on this pool."""
        if self._made:
            return
        async with self._making:
            if not self._made:
                await run(self.ensuring)
                self._made = True

    def forget(self) -> None:
        """Has the next connection run it again, as a pool that was closed is a new one."""
        self._made = False
