# Written by scripts/unasync.py from tests/integration/oracle/test_oracle_concurrent_first_boots.py:
# change that, and run the script again.
"""
Managers booting at once on a fresh Oracle graph, each from a pool of its own, as replicas booting together do: each
ensures the store's identity constraint. Two sessions building one index at once on an Oracle table deadlock in the
library cache (ORA-04020), so since compiler 0.52.2 an ensure builds an item in its turn, as on Postgres
(liberation-data/drivine-cypher-spec#418, drivine4j#777): one session builds, and the rest find the item made.
drivine4j's StatelessStoreIdentityOracleTest's concurrent first boots, ported.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from drivine.connection.oracle import oracle_connection_provider
from drivine.schema import schema_executor
from drivine.sync import DatabaseRegistry
from tests.sync_integration.oracle.oracle_seed import ON_ORACLE

pytestmark = ON_ORACLE

GRAPH = "boot777"
BOOTS = 8
ROUNDS = 3


class Watched:
    """What the boots' schema statements met: each contended failure, and each take of a turn and its outcome."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.contended: list[str] = []
        self.takes = 0
        self.refused = 0
        contended = schema_executor._contended  # pyright: ignore[reportPrivateUsage]
        deadlocked = oracle_connection_provider.ddl_deadlocked
        run = schema_executor.SchemaExecutor.run

        def seen_contended(failure: BaseException, waited_out_busy: bool) -> bool:
            self.contended.append(str(failure))
            return contended(failure, waited_out_busy)

        def seen_deadlocked(failure: BaseException | None) -> bool:
            self.contended.append(str(failure))
            return deadlocked(failure)

        def seen_run(executor: Any, statement: Any, bindings: Any = None, once: bool = False) -> list[Any]:
            take = isinstance(statement, str) and "'CLAIM:" in statement and "INSERT INTO" in statement.upper()
            self.takes += take
            try:
                return run(executor, statement, bindings, once=once)
            except Exception as error:
                self.refused += take and "ORA-00001" in str(error)
                raise

        monkeypatch.setattr(schema_executor, "_contended", seen_contended)
        monkeypatch.setattr(oracle_connection_provider, "ddl_deadlocked", seen_deadlocked)
        monkeypatch.setattr(schema_executor.SchemaExecutor, "run", seen_run)

    def library_cache_deadlocks(self) -> int:
        return sum("ORA-04020" in it for it in self.contended)


@pytest.fixture
def registries() -> Iterator[list[DatabaseRegistry]]:
    made: list[DatabaseRegistry] = []
    try:
        yield made
    finally:
        for registry in made:
            registry.close()
