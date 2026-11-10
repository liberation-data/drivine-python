"""
A Postgres index build leaves no pooled session holding shared memory (#435, liberation-data/drivine4j#626).

A backend that has run anything in parallel, as a `CREATE INDEX` over a node table past `min_parallel_table_scan_size`
(8 MB) does, keeps a dynamic shared memory segment, a `/dev/shm/PostgreSQL.<n>` file, for as long as it lives, and the
pool keeps it alive. An HNSW build over such a table asks for nearly all of `maintenance_work_mem` (64 MB) as one
segment, so in the test container's 64 MB `/dev/shm`, Docker's default, it failed once pooled sessions held theirs. Each
build now runs with no parallel workers (see PostgresConnection.building_index). drivine4j's
PostgresIndexBuildSharedMemoryTest, ported, but for its check that the HNSW build ran in parallel: here it doesn't.

The graph's node table is filled past 8 MB once, with rows small enough to stay out of TOAST, where a row's size would
not count toward the table's. A session holds a segment where `/proc/<pid>/maps` of its backend maps one that
Postgres's own are not, which every backend maps: the container's user is a superuser, which may read it with
`pg_read_file`. The pool's sessions are known by their application name, and watched from a connection of no pool,
which runs nothing in parallel. A server whose segments are not files in `/dev/shm`, or whose user may not read them or
`/proc`, skips the tests.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest

from drivine import (
    ConnectionProperties,
    DatabaseRegistry,
    PersistenceManagerFactory,
    StatelessGraphObjectManager,
    range_index,
    vector_index,
)
from drivine.concurrency import sleep
from drivine.connection.postgres.postgres_connection import PostgresConnection
from tests.integration.postgres.conftest import ON_POSTGRES
from tests.integration.sql.new_graph import drop_graph, registry_on

pytestmark = ON_POSTGRES

GRAPH = "shm435"
LABEL = "Chunk"
DIMENSIONS = 32

RANGE_PROPERTIES = ["a", "b", "c", "f", "g", "h", "i", "j", "k", "l", "m", "n"]
"""The properties the HNSW test builds a range index over, one session each."""

POOL = "drivine-435"
"""The application name of the pool's sessions, by which `pooled_sessions` finds them."""


def mapped(pid: str) -> str:
    """SQL for the names of the segments the backend whose pid is `pid`, an SQL expression, maps, joined by commas."""
    return (
        "array_to_string(ARRAY(SELECT DISTINCT m[1] FROM regexp_matches(pg_read_file('/proc/' || "
        rf"{pid} || '/maps'), 'PostgreSQL\.[0-9]+', 'g') m), ',')"
    )


SEGMENTS = "SELECT f FROM pg_ls_dir('/dev/shm') f WHERE f LIKE 'PostgreSQL.%'"
"""The names of the dynamic shared memory segments in `/dev/shm`."""


@dataclass
class Watched:
    """The graph's managers, the pool they run on, and a connection of no pool that watches the pool's sessions."""

    gom: StatelessGraphObjectManager
    registry: DatabaseRegistry
    watcher: Any
    """A psycopg connection, typed Any: psycopg types a statement as a literal, and these are built here."""
    common: set[str]
    """The segments in `/dev/shm` before the tests: Postgres's own, which every backend maps."""

    async def rows(self, sql: str) -> list[tuple[Any, ...]]:
        cursor = await self.watcher.execute(sql)
        return await cursor.fetchall()

    async def segments(self) -> set[str]:
        return {it[0] for it in await self.rows(SEGMENTS)}

    def own(self, names: str) -> set[str]:
        """The segments named, as `mapped` gives them, that are not Postgres's own."""
        return {it for it in names.split(",") if it} - self.common

    async def pooled_sessions(self) -> dict[int, set[str]]:
        """The segments of its own that each backend of the pool's sessions maps, by its pid."""
        found = await self.rows(
            f"SELECT pid, {mapped('pid')} FROM pg_stat_activity WHERE application_name = '{POOL}' "
            "AND backend_type = 'client backend'"
        )
        return {pid: self.own(names) for pid, names in found}

    async def segments_of(self, pid: int) -> set[str]:
        """The segments of its own that the backend `pid` maps."""
        [(names,)] = await self.rows(f"SELECT {mapped(str(pid))}")
        return self.own(names)


async def eventually(done: Callable[[], Awaitable[bool]], state: Callable[[], Awaitable[str]]) -> None:
    """
    Waits, up to ten seconds, for `done`: a parallel worker's backend ends a moment after its query. Fails with `state`
    where it is not done by then.
    """
    until = time.monotonic() + 10
    while not await done():
        if time.monotonic() > until:
            pytest.fail(await state())
        await sleep(0.1)


@pytest.fixture(scope="module")
async def watched() -> AsyncIterator[Watched]:
    properties = ConnectionProperties.from_env("postgres")
    watcher: Any = await psycopg.AsyncConnection.connect(
        host=properties.host,
        port=properties.port,
        dbname=properties.database_name,
        user=properties.user_name,
        password=properties.password,
        application_name=f"{POOL}-watcher",
        autocommit=True,
    )
    try:
        try:
            cursor = await watcher.execute(
                f"SELECT current_setting('dynamic_shared_memory_type') = 'posix' AND "
                f"{mapped('pg_backend_pid()')} IS NOT NULL"
            )
            row = await cursor.fetchone()
            readable = row is not None and row[0] is True and bool(await (await watcher.execute(SEGMENTS)).fetchall())
        except psycopg.Error:
            readable = False
        if not readable:
            pytest.skip(
                "the server keeps no segments in /dev/shm, or its user may not read them and /proc/<pid>/maps, as a "
                "superuser on Linux may"
            )
        await watcher.execute(f"DROP TABLE IF EXISTS {GRAPH}_edge, {GRAPH}_node, {GRAPH}_schema CASCADE")
        common = {it[0] for it in await (await watcher.execute(SEGMENTS)).fetchall()}
        # libpq names each session the pool opens after its environment: the watcher names its own.
        previous = os.environ.get("PGAPPNAME")
        os.environ["PGAPPNAME"] = POOL
        registry = registry_on("postgres", GRAPH, pool_max=16)
        pm = PersistenceManagerFactory(registry).get("postgres")
        try:
            provider = registry.connection_provider("postgres")
            assert provider is not None
            await (await provider.connect()).release()  # makes the graph's tables
            # Some 700 bytes a row, under the 2 KB past which a row's props would go to TOAST: 20,000 of them are past
            # 10 MB.
            await watcher.execute(
                f"""
                INSERT INTO {GRAPH}_node (labels, props)
                SELECT '["{LABEL}"]'::jsonb, jsonb_build_object(
                    'a', i, 'b', 'b' || i, 'c', i % 100, 'd', i % 7, 'e', 'e' || i, 'f', i % 3, 'g', i % 5,
                    'h', i % 11, 'i', i % 13, 'j', i % 17, 'k', i % 19, 'l', i % 23, 'm', i % 29, 'n', i % 31,
                    'pad', repeat('p', 100),
                    'embedding', (SELECT jsonb_agg(round(random()::numeric, 6))
                                  FROM generate_series(1, {DIMENSIONS}) WHERE i > 0))
                FROM generate_series(1, 20000) i
                """
            )
            [(size,)] = await (await watcher.execute(f"SELECT pg_relation_size('{GRAPH}_node')")).fetchall()
            assert size > 10 * 1024 * 1024, f"the node table is {size} bytes: a build over it would not be parallel"
            yield Watched(StatelessGraphObjectManager(pm), registry, watcher, common)
        finally:
            try:
                await drop_graph(pm, GRAPH)
            finally:
                await registry.close()
                if previous is None:
                    os.environ.pop("PGAPPNAME", None)
                else:
                    os.environ["PGAPPNAME"] = previous
    finally:
        await watcher.close()


async def test_an_hnsw_build_after_range_index_builds_passes_and_no_session_of_the_pool_holds_shared_memory_after(
    watched: Watched,
):
    """
    Each session that builds a range index in parallel keeps a segment of about 192 KB, the area its parallel queries
    share, as long as it lives. The HNSW build needs 63,999,616 bytes and Postgres's own segments about 1 MB, which
    leaves room in 64 MB for ten: twelve sessions that each built one, kept by their pool, were enough that it failed,
    `could not resize shared memory segment ... No space left on device`.
    """
    before = await watched.segments()
    provider = watched.registry.connection_provider("postgres")
    assert provider is not None
    # A connection held out of the pool while each range index builds, as an application's other tasks hold theirs,
    # so each build is on a session of its own.
    held: list[PostgresConnection] = []
    try:
        for prop in RANGE_PROPERTIES:
            session = await provider.connect()
            assert isinstance(session, PostgresConnection)
            held.append(session)
            assert (await watched.gom.indexes.ensure(range_index(LABEL, prop))).status == "CREATED"
    finally:
        for it in held:
            await it.release()
    # Each build was on a session of its own: none of the twelve held while it ran, and the one the last build was on.
    assert len({it.session_id for it in held}) == len(RANGE_PROPERTIES)
    assert len(await watched.pooled_sessions()) > len(RANGE_PROPERTIES)

    vector = vector_index(LABEL, "embedding", DIMENSIONS)
    assert (await watched.gom.indexes.ensure(vector)).status == "CREATED"
    assert [it for it in await watched.gom.indexes.list("VECTOR") if it.label == LABEL]

    async def holding() -> dict[int, set[str]]:
        return {pid: own for pid, own in (await watched.pooled_sessions()).items() if own}

    async def none_holding() -> bool:
        return not await holding()

    async def no_new_segments() -> bool:
        return not (await watched.segments() - before)

    async def state() -> str:
        held = await holding()
        return f"pooled sessions holding a segment: {held}; segments {await watched.segments()}, before {before}"

    await eventually(none_holding, state)
    await eventually(no_new_segments, state)


async def test_a_build_leaves_the_sessions_own_setting_as_it_was_and_holds_no_shared_memory(watched: Watched):
    """
    A pool of one session, which the build runs on, as the session an application set
    `max_parallel_maintenance_workers` on: the setting is 3 again after, and the build left it no segment. Reset, the
    setting is the server's again after the next build.
    """
    registry = registry_on("postgres", GRAPH, pool_max=1)
    try:
        provider = registry.connection_provider("postgres")
        assert provider is not None
        session = await provider.connect()
        assert isinstance(session, PostgresConnection)
        pid = session.connection.info.backend_pid
        await session.connection.execute("SET max_parallel_maintenance_workers = 3")
        await session.release()

        gom = StatelessGraphObjectManager(PersistenceManagerFactory(registry).get("postgres"))
        assert (await gom.indexes.ensure(range_index(LABEL, "d"))).status == "CREATED"

        again = await provider.connect()
        assert isinstance(again, PostgresConnection)
        try:
            assert again.connection.info.backend_pid == pid, "the same session"
            cursor = await again.connection.execute(
                "SELECT current_setting('max_parallel_maintenance_workers') AS workers"
            )
            assert await cursor.fetchone() == {"workers": "3"}
            await again.connection.execute("RESET max_parallel_maintenance_workers")
        finally:
            await again.release()
        assert await watched.segments_of(pid) == set(), "the build ran in parallel"

        # A setting the session didn't set is the server's again after a build, not set to the value it had.
        assert (await gom.indexes.ensure(range_index(LABEL, "e"))).status == "CREATED"
        last = await provider.connect()
        assert isinstance(last, PostgresConnection)
        try:
            assert last.connection.info.backend_pid == pid, "the same session"
            cursor = await last.connection.execute(
                "SELECT source FROM pg_settings WHERE name = 'max_parallel_maintenance_workers'"
            )
            assert (await cursor.fetchone())["source"] != "session"
        finally:
            await last.release()
    finally:
        await registry.close()
