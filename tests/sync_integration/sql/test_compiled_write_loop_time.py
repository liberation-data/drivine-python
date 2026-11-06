# Written by scripts/unasync.py from tests/integration/sql/test_compiled_write_loop_time.py:
# change that, and run the script again.
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
"""

import time
from dataclasses import dataclass
from typing import Any

import pytest

from drivine.query.statement import cypher_statement
from drivine.sync import DatabaseType, PersistenceManager, QuerySpecification

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
    Write(
        "MERGE",
        "MATCH (n:CyLoop) MERGE (n)-[:CY_SELF]->(n)",
        "MATCH (:CyLoop)-[r:CY_SELF]->(:CyLoop) RETURN count(r)",
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


def query(pm: PersistenceManager, cypher: str, parameters: dict[str, Any] | None = None) -> list[Any]:
    return pm.query(QuerySpecification[Any](cypher_statement(cypher)).bind(parameters))


def timed(pm: PersistenceManager, n: int) -> list[int]:
    """The milliseconds each write took over `n` rows, each checked by what it gave and what it left."""
    # A node's relationships go with it: the relationship table cascades.
    pm.execute(QuerySpecification("DELETE FROM drivine_node"))
    times: list[int] = []
    for write in WRITES:
        if vacuumed_empty(pm, write):
            # Statistics that say the table is empty: autocommitted, as VACUUM runs outside a transaction. Not in
            # parallel: a VACUUM of many dead relationships otherwise vacuums the edge indexes in parallel, and the
            # pooled session that ran it keeps some 1.2 MB of /dev/shm until it ends, which a later parallel build of
            # a vector index, asking for nearly all of a container's 64 MB, then fails for want of.
            pm.execute(QuerySpecification("VACUUM (PARALLEL 0, ANALYZE) drivine_edge"))
        start = time.perf_counter()
        rows = query(pm, write.cypher, {"n": n})
        times.append(round((time.perf_counter() - start) * 1000))
        if write.gives:
            assert (write.name, n, rows) == (write.name, n, [n])
        left = n if write.left is None else write.left
        assert (write.name, n, query(pm, write.check)) == (write.name, n, [left])
    return times


def slow(small: int, large: int) -> bool:
    """Whether a write that took `small` ms over SMALL rows and `large` over LARGE grew faster than its rows."""
    return large > max(RATIO * small, FLOOR_MS)


@pytest.mark.parametrize("write", WRITES, ids=[it.name for it in WRITES])
def test_a_write_over_ten_times_the_rows_takes_about_ten_times_as_long_not_a_hundred(
    pm: PersistenceManager, store: str, write: Write
):
    if store not in TIMES:
        try:
            TIMES[store] = (timed(pm, SMALL), timed(pm, LARGE))
        finally:
            pm.execute(QuerySpecification("DELETE FROM drivine_node"))
            if pm.type == DatabaseType.POSTGRES:
                # The tables are left as small as they were found, which a VACUUM of the empty tables truncates them
                # to. The node table, left at some 8 MB of dead rows, was over min_parallel_table_scan_size, so a later
                # index on it was built in parallel: a vector index asks for nearly all of a container's 64 MB of
                # /dev/shm, and a pooled session that built any index in parallel keeps some of it until it ends.
                pm.execute(QuerySpecification("VACUUM (PARALLEL 0) drivine_node, drivine_edge"))
    small, large = TIMES[store]
    report = "; ".join(
        f"{it.name} {small[i]} ms over {SMALL} rows, {large[i]} ms over {LARGE}" for i, it in enumerate(WRITES)
    )
    print(f"CompiledWriteLoopTime {pm.type.name}: {report}")
    i = WRITES.index(write)
    assert not slow(small[i], large[i]), report
