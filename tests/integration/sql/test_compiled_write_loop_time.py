"""
A compiled write that loops over its rows takes time in proportion to them (#419, liberation-data/drivine4j#588). Each
row the loop kept was appended to a JSON list of the rows it leaves, which copied the list, so ten times the rows took
about a hundred times as long: on drivine4j, a `CALL (n) { SET … RETURN … }` over 20,000 nodes took 183 s on
Postgres where it took 1.8 s over 2,000. Each write is timed over SMALL rows and LARGE, ten times as many, and the
large run must take less than RATIO times the small one, or FLOOR_MS where that is more: a ratio, so a slower runner,
which is slower at both, passes alike. drivine4j's `CompiledWriteLoopTimeTest`, and Drivine for TypeScript's
`CypherWriteLoopTime.spec.ts`. The tests run one at a time, so no other test's load is in the timings.

On Postgres the MERGE is timed with the edge table vacuumed while empty, as autovacuum leaves it late in a full run:
its per-row read was then planned as a Seq Scan, kept as the loop filled the table, so it was quadratic: 27 s over
20,000 rows (#420, liberation-data/drivine4j#609). The CALL…RETURN joins its one-row list to the node table for each
row, and Postgres, which takes the list for 100 rows, scanned every node for it instead: 36.8 s over 20,000 rows in one
CI run. Each loop is now planned with seq scans and hash and merge joins off (see
PostgresCypherCompiler._planned_as_nested_loops), and both pass as the other writes do.

On Postgres every write is also timed with the node table analyzed just after it was emptied, as an autoanalyze then
leaves it: its pages of dead rows taken to hold one row. The CALL…RETURN's per-row read was then planned to materialize
a scan of the whole primary key index and filter it for the row's node, so it was quadratic again, as seen three times
in CI: 72 s to 142 s over 20,000 rows (#430). Materialization is now off in the loop too.
"""

import time
from dataclasses import dataclass
from typing import Any

import pytest

from drivine import DatabaseType, PersistenceManager, QuerySpecification
from drivine.query.statement import cypher_statement

pytestmark = pytest.mark.nightly

SMALL = 2_000
LARGE = 20_000

RATIO = 30
"""Linear is about 10, and was 50 to 100 when each row copied the list."""

FLOOR_MS = 3_000
"""Where the small run is short, its fixed costs, and a runner's jitter, are much of it: the large one gets this at
least."""


@dataclass(frozen=True)
class Write:
    """A write timed, what it gives for `n` rows where it gives any, and the read that counts what it left."""

    name: str
    cypher: str
    check: str
    left: int | None
    """What the check counts, or with None the rows the write was timed over."""
    gives: bool = False
    """Whether the write gives the count of its rows."""


WRITES = [
    Write("CREATE", "UNWIND range(1, $n) AS i CREATE (:CyLoop {i: i})", "MATCH (n:CyLoop) RETURN count(n)", None),
    Write("SET", "MATCH (n:CyLoop) SET n.x = n.i", "MATCH (n:CyLoop) WHERE n.x IS NOT NULL RETURN count(n)", None),
    Write("REMOVE", "MATCH (n:CyLoop) REMOVE n.x", "MATCH (n:CyLoop) WHERE n.x IS NOT NULL RETURN count(n)", 0),
    # The check reads each self-loop from its one node. Read as `(:CyLoop)-[r:CY_SELF]->(:CyLoop)`, it joined the node
    # table twice, which on Postgres, its statistics left saying the table is empty (#430), was planned as a nested
    # loop over every node for each node: 8 minutes over 20,000 rows, untimed, in CI (liberation-data/drivine#680).
    Write(
        "MERGE",
        "MATCH (n:CyLoop) MERGE (n)-[:CY_SELF]->(n)",
        "MATCH (n:CyLoop)-[r:CY_SELF]->(n) RETURN count(r)",
        None,
    ),
    Write(
        "CALL…RETURN",
        "MATCH (n:CyLoop) CALL (n) { SET n.y = n.i RETURN n.i AS j } RETURN count(j)",
        "MATCH (n:CyLoop) WHERE n.y = n.i RETURN count(n)",
        None,
        gives=True,
    ),
]


TIMES: dict[str, tuple[list[int], list[int]]] = {}
"""The milliseconds each write took over SMALL rows and LARGE, by store: timed once, for the cases of every write."""


def vacuumed_empty(pm: PersistenceManager, write: Write) -> bool:
    """
    Whether the write is timed with the edge table vacuumed while empty, in which state its per-row read was quadratic
    until #420.
    """
    return pm.type == DatabaseType.POSTGRES and write.name == "MERGE"


async def query(pm: PersistenceManager, cypher: str, parameters: dict[str, Any] | None = None) -> list[Any]:
    return await pm.query(QuerySpecification[Any](cypher_statement(cypher)).bind(parameters))


async def timed(pm: PersistenceManager, n: int) -> list[int]:
    """The milliseconds each write took over `n` rows, each checked by what it gave and what it left."""
    # A node's relationships go with it: the relationship table cascades.
    await pm.execute(QuerySpecification("DELETE FROM drivine_node"))
    if pm.type == DatabaseType.POSTGRES:
        # Statistics that say the node table, still at its pages of dead rows, is empty, as an autoanalyze that runs
        # just after the DELETE leaves them (#430).
        await pm.execute(QuerySpecification("ANALYZE drivine_node"))
    times: list[int] = []
    for write in WRITES:
        if vacuumed_empty(pm, write):
            # Statistics that say the table is empty: autocommitted, as VACUUM runs outside a transaction. Not in
            # parallel: a VACUUM of many dead relationships otherwise vacuums the edge indexes in parallel, and the
            # pooled session that ran it keeps some 1.2 MB of /dev/shm until it ends, which a later parallel build of
            # a vector index, asking for nearly all of a container's 64 MB, then fails for want of.
            await pm.execute(QuerySpecification("VACUUM (PARALLEL 0, ANALYZE) drivine_edge"))
        start = time.perf_counter()
        rows = await query(pm, write.cypher, {"n": n})
        times.append(round((time.perf_counter() - start) * 1000))
        if write.gives:
            assert (write.name, n, rows) == (write.name, n, [n])
        left = n if write.left is None else write.left
        assert (write.name, n, await query(pm, write.check)) == (write.name, n, [left])
    return times


def slow(small: int, large: int) -> bool:
    """Whether a write that took `small` ms over SMALL rows and `large` over LARGE grew faster than its rows."""
    return large > max(RATIO * small, FLOOR_MS)


@pytest.mark.parametrize("write", WRITES, ids=[it.name for it in WRITES])
async def test_a_write_over_ten_times_the_rows_takes_about_ten_times_as_long_not_a_hundred(
    pm: PersistenceManager, store: str, write: Write
):
    if store not in TIMES:
        if pm.type == DatabaseType.POSTGRES:
            # The node table keeps the statistics its ANALYZE gave it while the writes are timed: an autoanalyze that
            # lands after the CREATE, as one did in the first of a run's Postgres timings, takes the table to hold its
            # rows, and the CALL…RETURN passed without #430's fix.
            await pm.execute(QuerySpecification("ALTER TABLE drivine_node SET (autovacuum_enabled = off)"))
        try:
            TIMES[store] = (await timed(pm, SMALL), await timed(pm, LARGE))
        finally:
            await pm.execute(QuerySpecification("DELETE FROM drivine_node"))
            if pm.type == DatabaseType.POSTGRES:
                await pm.execute(QuerySpecification("ALTER TABLE drivine_node RESET (autovacuum_enabled)"))
                # The tables are left as small as they were found, which a VACUUM of the empty tables truncates them
                # to. The node table, left at some 8 MB of dead rows, was over min_parallel_table_scan_size, so a later
                # index on it was built in parallel: a vector index asks for nearly all of a container's 64 MB of
                # /dev/shm, and a pooled session that built any index in parallel keeps some of it until it ends.
                await pm.execute(QuerySpecification("VACUUM (PARALLEL 0) drivine_node, drivine_edge"))
    small, large = TIMES[store]
    report = "; ".join(
        f"{it.name} {small[i]} ms over {SMALL} rows, {large[i]} ms over {LARGE}" for i, it in enumerate(WRITES)
    )
    print(f"CompiledWriteLoopTime {pm.type.name}: {report}")
    i = WRITES.index(write)
    assert not slow(small[i], large[i]), report
