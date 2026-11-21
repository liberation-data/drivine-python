"""
Managers booting at once on a fresh Oracle graph, each from a pool of its own, as replicas booting together do: each
ensures the store's identity constraint. Two sessions building one index at once on an Oracle table deadlock in the
library cache (ORA-04020), so since compiler 0.52.2 an ensure builds an item in its turn, as on Postgres
(liberation-data/drivine-cypher-spec#418, drivine4j#777): one session builds, and the rest find the item made.
drivine4j's StatelessStoreIdentityOracleTest's concurrent first boots, ported.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from drivine import DatabaseRegistry, PersistenceManagerFactory, QuerySpecification, StatelessGraphObjectManager
from drivine.connection.oracle import oracle_connection_provider
from drivine.schema import schema_executor
from tests.integration.oracle.oracle_seed import ON_ORACLE
from tests.integration.sql.new_graph import drop_graph, registry_on

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

        async def seen_run(executor: Any, statement: Any, bindings: Any = None, once: bool = False) -> list[Any]:
            take = isinstance(statement, str) and "'CLAIM:" in statement and "INSERT INTO" in statement.upper()
            self.takes += take
            try:
                return await run(executor, statement, bindings, once=once)
            except Exception as error:
                self.refused += take and "ORA-00001" in str(error)
                raise

        monkeypatch.setattr(schema_executor, "_contended", seen_contended)
        monkeypatch.setattr(oracle_connection_provider, "ddl_deadlocked", seen_deadlocked)
        monkeypatch.setattr(schema_executor.SchemaExecutor, "run", seen_run)

    def library_cache_deadlocks(self) -> int:
        return sum("ORA-04020" in it for it in self.contended)


@pytest.fixture
async def registries() -> AsyncIterator[list[DatabaseRegistry]]:
    """The test's registries, each closed after it; and its graph, a full node table's among them, dropped then."""
    made: list[DatabaseRegistry] = []
    try:
        yield made
    finally:
        for registry in made:
            await registry.close()
        cleaner = registry_on("oracle", GRAPH)
        try:
            await drop_graph(PersistenceManagerFactory(cleaner).get("oracle"), GRAPH)
        finally:
            await cleaner.close()


@pytest.mark.async_only
@pytest.mark.parametrize("nodes", [0, 300_000])
async def test_managers_booting_at_once_on_a_fresh_graph_take_turns_and_agree_on_one_identity(
    registries: list[DatabaseRegistry], monkeypatch: pytest.MonkeyPatch, nodes: int
):
    watched = Watched(monkeypatch)
    rounds: list[tuple[float, int, int, int]] = []
    for _ in range(ROUNDS):
        cleaner = registry_on("oracle", GRAPH)
        registries.append(cleaner)
        cleaning = PersistenceManagerFactory(cleaner).get("oracle")
        await drop_graph(cleaning, GRAPH)
        if nodes:
            # A node table the identity constraint's index takes a while to build over, so the boots' builds overlap.
            # A registry of its own: the cleaner's pool made the tables once already, and dropped them.
            seeder = registry_on("oracle", GRAPH)
            registries.append(seeder)
            cleaning = PersistenceManagerFactory(seeder).get("oracle")
            provider = seeder.connection_provider("oracle")
            assert provider is not None
            await (await provider.connect()).release()  # makes the graph's tables
            await cleaning.execute(
                QuerySpecification[Any](
                    f"INSERT INTO {GRAPH}_node (labels, props) SELECT JSON('[\"Filler\"]'), "
                    f"JSON_OBJECT('id' VALUE LEVEL) FROM dual CONNECT BY LEVEL <= {nodes}"
                )
            )
            await cleaning.execute(
                QuerySpecification[Any](
                    f"DELETE FROM {GRAPH}_node WHERE JSON_EXISTS(labels, '$[*]?(@ == \"DrivineStore\")')"
                )
            )
        managers: list[StatelessGraphObjectManager] = []
        for _ in range(BOOTS):
            registry = registry_on("oracle", GRAPH)
            registries.append(registry)
            managers.append(StatelessGraphObjectManager(PersistenceManagerFactory(registry).get("oracle")))
        deadlocks, takes, refused = watched.library_cache_deadlocks(), watched.takes, watched.refused
        started = time.monotonic()
        identities = await asyncio.wait_for(asyncio.gather(*(it.store_identity() for it in managers)), 300)
        rounds.append(
            (
                time.monotonic() - started,
                watched.library_cache_deadlocks() - deadlocks,
                watched.takes - takes,
                watched.refused - refused,
            )
        )
        assert len({it.id for it in identities}) == 1
        _, met, taken, turned_away = rounds[-1]
        # The boots took turns: a take ran, and another session's take was refused while it held the turn (ORA-00001),
        # where without turns no take runs. And none met the library cache deadlock that turns keep away.
        assert met == 0, f"{nodes} nodes: the boots met ORA-04020: {rounds}"
        assert taken > turned_away > 0, f"{nodes} nodes: the boots took no turns: {rounds}"
    # On compiler 0.52.1, without turns, a round of a full table's boots took 12 to 20 s, where Oracle broke the library
    # cache's cycle after about 9 s rather than refuse a statement; on 0.52.2, 3 to 7 s. Shown, not asserted.
    print(f"{nodes} nodes: rounds (seconds, ORA-04020 met, takes, takes refused): {rounds}")
