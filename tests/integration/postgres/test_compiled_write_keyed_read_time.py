"""
A compiled write whose loop reads a node or a relationship by its key, from a table the loop adds to, or joins a table
to the rows of a `CALL { … }` for one row, takes time in proportion to its rows on Postgres (#420,
liberation-data/drivine4j#609): a `MERGE` that reads its pattern, a `SET` of what the loop made, in a `MERGE`'s
`ON CREATE`, a `CALL { … }` or a `FOREACH`, with the tables vacuumed while empty; and a `CALL { … }` that returns a
value or deletes what it made, over nodes just made, which Postgres planned to hash a scan of every node for each row
(17 s over 20,000 rows). Each is timed as tests/integration/sql/test_compiled_write_loop_time.py times a write, from an
empty graph vacuumed, that NODES makes the nodes of where it reads them. drivine4j's
CompiledWriteKeyedReadTimePostgresTest, ported.

The writes run on a graph of their own, GRAPH, as drivine4j's and Drivine for TypeScript's do, through a pool that is
closed after each test. A VACUUM of 20,000 dead relationships vacuums their indexes in parallel, and the session that
ran it keeps some 1.2 MB of /dev/shm until it ends; with that held, a later parallel build of a vector index, which asks
for nearly all of a container's 64 MB, failed for want of space.
"""

import dataclasses
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from drivine import (
    ConnectionProperties,
    DatabaseRegistry,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
)
from drivine.query.statement import cypher_statement
from tests.integration.postgres.conftest import ON_POSTGRES
from tests.integration.sql.test_compiled_write_loop_time import LARGE, SMALL, slow

pytestmark = ON_POSTGRES

GRAPH = "cy_keyed"
"""The graph the writes are timed on: tables of their own, apart from those the other tests read."""

NAME = f"postgres_{GRAPH}"


@dataclass(frozen=True)
class Write:
    """A write timed, and the read that counts what it left: n each time, over n rows."""

    name: str
    cypher: str
    check: str


WRITES = [
    Write(
        "MERGE ON CREATE SET",
        "MATCH (n:CyLoop) MERGE (n)-[r:CY_SELF]->(n) ON CREATE SET r.w = n.i",
        "MATCH (:CyLoop)-[r:CY_SELF]->(:CyLoop) WHERE r.w IS NOT NULL RETURN count(r)",
    ),
    Write(
        "CALL MERGE",
        "MATCH (n:CyLoop) CALL (n) { MERGE (n)-[:CY_SELF]->(n) } RETURN count(n)",
        "MATCH (:CyLoop)-[r:CY_SELF]->(:CyLoop) RETURN count(r)",
    ),
    Write(
        "CALL CREATE SET",
        "MATCH (n:CyLoop) CALL (n) { CREATE (n)-[r:CY_SELF]->(n) SET r.w = n.i } RETURN count(n)",
        "MATCH (:CyLoop)-[r:CY_SELF]->(:CyLoop) WHERE r.w IS NOT NULL RETURN count(r)",
    ),
    Write(
        "FOREACH CREATE SET",
        "MATCH (n:CyLoop) FOREACH (x IN [1] | CREATE (n)-[r:CY_SELF]->(n) SET r.w = x)",
        "MATCH (:CyLoop)-[r:CY_SELF]->(:CyLoop) WHERE r.w = 1 RETURN count(r)",
    ),
    Write(
        "CALL RETURN",
        "MATCH (n:CyLoop) CALL (n) { SET n.y = n.i RETURN n.i AS j } RETURN count(j)",
        "MATCH (n:CyLoop) WHERE n.y = n.i RETURN count(n)",
    ),
    Write(
        "CALL DETACH DELETE",
        "MATCH (n:CyLoop) CALL (n) { CREATE (m:CyMade) DETACH DELETE m } RETURN count(n)",
        "MATCH (n:CyLoop) RETURN count(n)",
    ),
    Write(
        "CALL CREATE node SET",
        "UNWIND range(1, $n) AS i CALL (i) { CREATE (a:CyLoop {i: i}) SET a.y = i } RETURN count(i)",
        "MATCH (n:CyLoop) WHERE n.y = n.i RETURN count(n)",
    ),
]

NODES = "UNWIND range(1, $n) AS i CREATE (:CyLoop {i: i})"
"""The nodes a write that reads them is given."""


@pytest.fixture
async def keyed(registry: DatabaseRegistry, factory: PersistenceManagerFactory) -> AsyncIterator[PersistenceManager]:
    """A manager of GRAPH, its rows deleted and its pool closed after the test, however it ends."""
    if registry.registered(NAME) is None:
        properties = dataclasses.replace(ConnectionProperties.from_env("postgres"), default_graph_path=GRAPH)
        registry.builder().with_properties(properties).register(NAME)
    pm = factory.get(NAME)
    try:
        yield pm
    finally:
        try:
            await pm.execute(QuerySpecification(f"DELETE FROM {GRAPH}_node"))
        finally:
            provider = registry.registered(NAME)
            assert provider is not None
            await provider.end()


async def query(pm: PersistenceManager, cypher: str, n: int) -> list[Any]:
    return await pm.query(QuerySpecification[Any](cypher_statement(cypher)).bind({"n": n}))


async def timed(pm: PersistenceManager, write: Write, n: int) -> int:
    """The milliseconds the write took over `n` rows, from an empty graph vacuumed."""
    # A node's relationships go with it: the relationship table cascades.
    await pm.execute(QuerySpecification(f"DELETE FROM {GRAPH}_node"))
    # Statistics that say the tables are empty: autocommitted, as VACUUM runs outside a transaction.
    await pm.execute(QuerySpecification(f"VACUUM ANALYZE {GRAPH}_edge"))
    await pm.execute(QuerySpecification(f"VACUUM ANALYZE {GRAPH}_node"))
    if write.cypher.startswith("MATCH"):
        await query(pm, NODES, n)
    start = time.perf_counter()
    await query(pm, write.cypher, n)
    ms = round((time.perf_counter() - start) * 1000)
    assert (write.cypher, n, await query(pm, write.check, n)) == (write.cypher, n, [n])
    return ms


@pytest.mark.parametrize("write", WRITES, ids=[it.name for it in WRITES])
async def test_a_write_whose_loop_reads_a_table_for_each_row_takes_about_ten_times_as_long_over_ten_times_the_rows(
    keyed: PersistenceManager, write: Write
):
    small, large = await timed(keyed, write, SMALL), await timed(keyed, write, LARGE)
    report = f"{write.name} {small} ms over {SMALL} rows, {large} ms over {LARGE}"
    print(f"CompiledWriteKeyedReadTime POSTGRES: {report}")
    assert not slow(small, large), report
