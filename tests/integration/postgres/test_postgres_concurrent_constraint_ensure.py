"""
A uniqueness constraint's index built online on Postgres (liberation-data/drivine-cypher-spec#382, #414), and two
ensures of it at once, each from its own manager and connection pool, as two processes would run them. Whatever each
reports, a constraint reported made is enforced, and none is reported made where nothing enforces it. drivine4j's
PostgresConcurrentConstraintEnsureTest, ported. Async only: scripts/unasync.py writes no sync copy of it.

The statements over the big tables are run on a connection of no pool, ended after the suite: a session that ran
anything in parallel over them (a sample of the keys that repeat, the build the invalid-index test has refused) keeps
a segment of /dev/shm until it disconnects, which a pool would keep, and leave a later HNSW build in the container's
64 MB too little (see test_postgres_index_build_shared_memory).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable
from typing import Any

import psycopg
import pytest

from drivine import (
    ConnectionProperties,
    DatabaseRegistry,
    EnsureResult,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    StatelessGraphObjectManager,
    cypher_statement,
    range_index,
    uniqueness_constraint,
)
from tests.integration.postgres.conftest import ON_POSTGRES
from tests.integration.sql.new_graph import TABLES, registry_on

pytestmark = ON_POSTGRES

SPEC = uniqueness_constraint("Big", "id")

NODES = 300_000
"""Enough nodes that a build takes about a second, so two ensures overlap."""

ROUNDS = 3


@pytest.fixture(scope="module")
async def watcher() -> AsyncIterator[Any]:
    """A connection of no pool, committing as it runs, that seeds the graphs and reads Postgres's catalog."""
    properties = ConnectionProperties.from_env("postgres")
    connection: Any = await psycopg.AsyncConnection.connect(
        host=properties.host,
        port=properties.port,
        dbname=properties.database_name,
        user=properties.user_name,
        password=properties.password,
        autocommit=True,
    )
    try:
        yield connection
    finally:
        await connection.close()


class Graphs:
    """The managers of the graphs a test makes, each of a registry, so a pool, of its own, as a process has."""

    def __init__(self, watcher: Any) -> None:
        self.watcher = watcher
        self.registries: list[DatabaseRegistry] = []
        self.made: set[str] = set()

    async def run(self, *statements: str) -> None:
        for statement in statements:
            await self.watcher.execute(statement)

    async def rows(self, sql: str) -> list[tuple[Any, ...]]:
        return await (await self.watcher.execute(sql)).fetchall()

    async def drop(self, graph: str) -> None:
        # CASCADE: the store's functions return a row of its node table.
        await self.run(f"DROP TABLE IF EXISTS {', '.join(f'{graph}_{it}' for it in TABLES)} CASCADE")

    async def manager(self, graph: str) -> StatelessGraphObjectManager:
        """A manager for `graph`, with its tables made, over a pool of its own."""
        registry = registry_on("postgres", graph)
        self.registries.append(registry)
        provider = registry.connection_provider("postgres")
        assert provider is not None
        await (await provider.connect()).release()  # makes the graph's tables
        self.made.add(graph)
        return StatelessGraphObjectManager(PersistenceManagerFactory(registry).get("postgres"))

    async def seeded(self, graph: str, nodes: int = NODES, repeats: int = 0) -> StatelessGraphObjectManager:
        """A fresh graph of `nodes` nodes labelled Big, each with its own id, and `repeats` more repeating ids 1 on."""
        await self.drop(graph)
        gom = await self.manager(graph)
        insert = (
            f"INSERT INTO {graph}_node (labels, props) SELECT '[\"Big\"]'::jsonb, jsonb_build_object('id', g) "
            "FROM generate_series(1, {}) g"
        )
        await self.run(insert.format(nodes))
        if repeats > 0:
            await self.run(insert.format(repeats))
        # Without parallel workers: a session that ran a vacuum in parallel keeps a segment of /dev/shm until it ends.
        await self.run(f"VACUUM (PARALLEL 0, ANALYZE) {graph}_edge", f"VACUUM (PARALLEL 0, ANALYZE) {graph}_node")
        return gom

    async def unique_indexes(self, graph: str) -> dict[str, bool]:
        """Each unique index Drivine names a constraint's on the graph's node table (not its key), and its validity."""
        found = await self.rows(
            "SELECT c.relname, i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            f"WHERE i.indrelid = '{graph}_node'::regclass AND i.indisunique AND c.relname LIKE '{graph}\\_uq%'"
        )
        return {name: valid for name, valid in found}

    async def close(self) -> None:
        try:
            for graph in self.made:
                await self.drop(graph)
        finally:
            for registry in self.registries:
                await registry.close()


@pytest.fixture
async def graphs(watcher: Any) -> AsyncIterator[Graphs]:
    made = Graphs(watcher)
    try:
        yield made
    finally:
        await made.close()


async def enforced(pm: PersistenceManager) -> bool:
    """Whether a second node of id 1 is refused."""
    try:
        await pm.execute(QuerySpecification[Any](cypher_statement("CREATE (:Big {id: 1})")))
    except Exception:
        return True
    return False


async def together(*ensures: Awaitable[Any]) -> list[Any]:
    """The ensures run at once, started together: what each gave, or raised."""
    return list(await asyncio.wait_for(asyncio.gather(*ensures, return_exceptions=True), 300))


async def twice(graphs: Graphs, graph: str) -> list[Any]:
    """Two ensures of SPEC at once, each of its own manager over `graph`."""
    first, second = await graphs.manager(graph), await graphs.manager(graph)
    return await together(first.constraints.ensure(SPEC), second.constraints.ensure(SPEC))


def reports_made(result: Any) -> bool:
    """What enforces nothing is never reported made: each result is made, matching, a violation, or a failure."""
    return isinstance(result, EnsureResult) and result.status in ("CREATED", "ALREADY_MATCHING")


def status(result: Any) -> str | None:
    return result.status if isinstance(result, EnsureResult) else None


async def test_a_write_to_the_label_commits_while_the_constraints_index_is_being_built_and_it_then_enforces(
    graphs: Graphs,
):
    graph = "cc382_online"
    # As many nodes as the range index's test, so the build outlasts the first write's compile.
    gom = await graphs.seeded(graph, nodes=1_000_000)

    async def building() -> int:
        [(count,)] = await graphs.rows(
            f"SELECT count(*) FROM pg_stat_progress_create_index WHERE relid = '{graph}_node'::regclass"
        )
        return count

    ensure = asyncio.create_task(gom.constraints.ensure(SPEC))
    try:
        deadline = time.monotonic() + 60
        while await building() == 0:
            assert not ensure.done(), f"the build ended before it was seen under way: {ensure.result()}"
            assert time.monotonic() < deadline, "no build was seen under way"
            await asyncio.sleep(0.005)
        await gom.persistence_manager.execute(QuerySpecification[Any](cypher_statement("CREATE (:Big {id: -1})")))
        still_building = await building() > 0
        assert status(await asyncio.wait_for(ensure, 300)) == "CREATED"
        assert still_building, "the write committed only once the build had ended"
        assert list((await graphs.unique_indexes(graph)).values()) == [True]
        assert await enforced(gom.persistence_manager)
    finally:
        if not ensure.done():
            ensure.cancel()


async def test_two_ensures_at_once_over_data_that_holds_each_id_once_end_with_one_valid_index_that_enforces(
    graphs: Graphs,
):
    for round_ in range(ROUNDS):
        graph = f"cc382_both{round_}"
        gom = await graphs.seeded(graph)
        results = await twice(graphs, graph)
        assert all(reports_made(it) for it in results), f"round {round_}: {results}"
        assert any(status(it) == "CREATED" for it in results), f"round {round_}: {results}"
        indexes = await graphs.unique_indexes(graph)
        assert list(indexes.values()) == [True], f"round {round_}: {indexes}"
        assert await enforced(gom.persistence_manager), f"round {round_}"


async def test_two_ensures_at_once_over_data_that_repeats_an_id_neither_report_it_made_and_leave_no_index(
    graphs: Graphs,
):
    for round_ in range(ROUNDS):
        graph = f"cc382_dup{round_}"
        gom = await graphs.seeded(graph, repeats=3)
        results = await twice(graphs, graph)
        assert not any(reports_made(it) for it in results), f"round {round_}: {results}"
        assert any(status(it) == "VIOLATION" for it in results), f"round {round_}: {results}"
        assert await graphs.unique_indexes(graph) == {}, f"round {round_}"
        refused = await enforced(gom.persistence_manager)
        assert not refused, f"round {round_}: a second id 1 was refused, with no constraint made"


async def test_two_ensures_at_once_over_an_index_left_invalid_end_with_one_valid_index_that_enforces(graphs: Graphs):
    # The window #382 closes: an earlier build of the constraint's index was refused, which left it invalid under its
    # name, recorded; the data is then put right, and two processes ensure the constraint at once.
    for round_ in range(ROUNDS):
        graph = f"cc382_invalid{round_}"
        gom = await graphs.seeded(graph)
        assert status(await gom.constraints.ensure(SPEC)) == "CREATED"
        [(name, valid)] = (await graphs.unique_indexes(graph)).items()
        assert valid
        [(definition,)] = await graphs.rows(f"SELECT pg_get_indexdef('{name}'::regclass)")
        await graphs.run(
            f"DROP INDEX {name}",
            f"INSERT INTO {graph}_node (labels, props) VALUES ('[\"Big\"]'::jsonb, '{{\"id\": 7}}'::jsonb)",
        )
        with pytest.raises(psycopg.Error):
            await graphs.run(definition.replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX CONCURRENTLY "))
        assert await graphs.unique_indexes(graph) == {name: False}, "the refused build left it invalid"
        await graphs.run(f"DELETE FROM {graph}_node WHERE nid = (SELECT max(nid) FROM {graph}_node)")

        results = await twice(graphs, graph)
        assert all(reports_made(it) for it in results), f"round {round_}: {results}"
        assert await graphs.unique_indexes(graph) == {name: True}, f"round {round_}"
        assert await enforced(gom.persistence_manager), f"round {round_}"


async def test_a_claim_of_the_turn_older_than_60_seconds_left_by_a_session_that_ended_is_taken_over_at_once(
    graphs: Graphs,
):
    # A session that claimed its turn to build the constraint and ended without giving it up: with no online build
    # under way, the next ensure takes the claim over, rather than wait 60 seconds for it.
    graph = "cc382_stale"
    gom = await graphs.seeded(graph, nodes=1_000)
    await graphs.run(
        f"INSERT INTO {graph}_schema (kind, label, properties, details) VALUES ('CLAIM:UNIQUENESS_CONSTRAINT', 'Big', "
        "'[\"id\"]', jsonb_build_object('at', (clock_timestamp() - interval '2 minutes')::text))"
    )
    started = time.monotonic()
    assert status(await gom.constraints.ensure(SPEC)) == "CREATED"
    seconds = time.monotonic() - started
    assert seconds < 30, f"the stale claim was waited out, not taken over: {seconds:.0f} s"
    assert list((await graphs.unique_indexes(graph)).values()) == [True]
    assert await enforced(gom.persistence_manager)
    [(claims,)] = await graphs.rows(f"SELECT count(*) FROM {graph}_schema WHERE kind LIKE 'CLAIM:%'")
    assert claims == 0, "the claim taken over was given up once done"


DEADLOCKED = "deadlocked with another session's online build on the same table"
"""What the planner says at INFO where Postgres cancelled one ensure's build as a deadlock with the other's."""


async def test_two_ensures_at_once_of_different_items_on_one_table_both_make_theirs(
    graphs: Graphs, caplog: pytest.LogCaptureFixture
):
    # A constraint and a range index on the same node table, built online at once: Postgres cancels one as a deadlock,
    # which waits for the other's build to end, and is made then. The planner must see that deadlock, run once
    # (drivine-python#572), in at least one round: a host that ran the build again itself would hide it, and pass.
    deadlocked: list[bool] = []
    for round_ in range(ROUNDS):
        graph = f"cc382_apart{round_}"
        gom = await graphs.seeded(graph)
        await graphs.run(f"UPDATE {graph}_node SET props = props || jsonb_build_object('n', props -> 'id')")
        first, second = await graphs.manager(graph), await graphs.manager(graph)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="drivine.schema"):
            results = await together(first.constraints.ensure(SPEC), second.indexes.ensure(range_index("Big", "n")))
        deadlocked.append(any(DEADLOCKED in it.getMessage() for it in caplog.records))
        assert all(status(it) == "CREATED" for it in results), f"round {round_}: {results}"
        assert list((await graphs.unique_indexes(graph)).values()) == [True], f"round {round_}"
        assert await enforced(gom.persistence_manager), f"round {round_}"
    assert any(deadlocked), (
        f"no round's builds deadlocked, so the planner's handling of one went untested: {deadlocked}"
    )
