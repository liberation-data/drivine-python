"""
#393, drivine4j#575: an id compared by `=` or `IN`, where the row must pass the comparison, is read by the node's or the
relationship's primary key. The id is JSON, compared as JSON, so each such read was a scan of the table, and an
`UNWIND` of ids a scan for each. The rows are those the comparison alone keeps, under `NOT` and `OR` too, where no test
of the key stands. drivine4j's IdKeyPlanTest checks the same.
"""

import re
import time
from collections.abc import AsyncIterator
from typing import Any, Literal

import pytest

from drivine import DatabaseType, PersistenceManager, PersistenceManagerFactory, QuerySpecification, transaction
from drivine.backend.sql.sql_graph import sql_text
from drivine.cypher.cypher_specification import cypher_compiled
from drivine.cypher.sql.compiled_cypher import CompiledCypher
from drivine.query.statement import cypher_statement

GRAPH = "drivine"
NODES = 5000
EDGES = NODES - 1


def spec(cypher: str, parameters: dict[str, Any] | None = None) -> QuerySpecification[Any]:
    return QuerySpecification[Any](cypher_statement(cypher)).bind(parameters)


def store_of(pm: PersistenceManager) -> Literal["POSTGRES", "ORACLE", "SQLITE"]:
    if pm.type is DatabaseType.POSTGRES:
        return "POSTGRES"
    if pm.type is DatabaseType.ORACLE:
        return "ORACLE"
    return "SQLITE"


def lines(rows: list[Any]) -> str:
    """The rows of a plan, of one column, as lines."""
    return "\n".join(str(it if not isinstance(it, dict) else next(iter(it.values()))) for it in rows)


class Ids:
    """Each node's id, by its `k`, and each relationship's, by the `k` of the node it leaves."""

    def __init__(self, nodes: dict[int, int], edges: dict[int, int]) -> None:
        self.nodes = nodes
        self.edges = edges

    def __call__(self, k: int) -> int:
        return self.nodes[k]


async def clear(pm: PersistenceManager) -> None:
    await pm.execute(QuerySpecification("DELETE FROM drivine_edge"))
    await pm.execute(QuerySpecification("DELETE FROM drivine_node"))


async def analyze(pm: PersistenceManager) -> None:
    """Gathers the store's statistics of the graph's tables, which it plans by."""
    if pm.type is DatabaseType.ORACLE:
        await pm.execute(
            QuerySpecification(
                f"BEGIN DBMS_STATS.GATHER_TABLE_STATS(USER, '{GRAPH.upper()}_NODE'); "
                f"DBMS_STATS.GATHER_TABLE_STATS(USER, '{GRAPH.upper()}_EDGE'); END;"
            )
        )
    else:
        await pm.execute(QuerySpecification(f"ANALYZE {GRAPH}_node"))
        await pm.execute(QuerySpecification(f"ANALYZE {GRAPH}_edge"))


@pytest.fixture(scope="module")
async def ids(factory: PersistenceManagerFactory, store: str) -> AsyncIterator[Ids]:
    pm = factory.get(store)
    await clear(pm)
    await pm.query(spec(f"UNWIND range(0, {NODES - 1}) AS k CREATE (:Item {{k: k}})"))
    rows = await pm.query(spec("MATCH (n:Item) RETURN [n.k, id(n)]"))
    nodes = {int(k): int(i) for k, i in rows}
    # A relationship from each of the first nodes to the next, each found by its id.
    pairs = [[k, nodes[k], nodes[k + 1]] for k in range(EDGES)]
    await pm.query(
        spec(
            "UNWIND $pairs AS p MATCH (a), (b) WHERE id(a) = p[1] AND id(b) = p[2] CREATE (a)-[:NEXT {k: p[0]}]->(b)",
            {"pairs": pairs},
        )
    )
    edges = {int(k): int(i) for k, i in await pm.query(spec("MATCH (a)-[r:NEXT]->() RETURN [a.k, id(r)]"))}
    await analyze(pm)
    yield Ids(nodes, edges)
    await clear(pm)


@pytest.fixture
def item(pm: PersistenceManager) -> str:
    """
    The label the statements match their nodes by, which every node carries. Oracle reads a label from the index over
    labels before any key, `nid = 6` written included, so its statements match no label, and show the key read.
    """
    return "" if pm.type is DatabaseType.ORACLE else ":Item"


def compiled(pm: PersistenceManager, statement: str, parameters: dict[str, Any]) -> CompiledCypher:
    """The statement as the store compiles it."""
    found = cypher_compiled(spec(statement, parameters), store_of(pm), GRAPH)[1]
    assert found is not None
    return found


async def plan(pm: PersistenceManager, compiled: CompiledCypher) -> str:
    """The plan the store makes for the compiled statement, as it writes it."""
    if pm.type is DatabaseType.SQLITE:
        # A line for each table or rows the plan reads, each led by the table it reads.
        tables = {it[2]: it[1] for it in re.findall(rf"\b({GRAPH}_(node|edge)) (\w+)", compiled.sql)}
        rows = await pm.query(QuerySpecification[Any](f"EXPLAIN QUERY PLAN {compiled.sql}").bind(compiled.parameters))
        details = [str(it["detail"]) for it in rows]
        read = [it for it in details if it.startswith(("SCAN ", "SEARCH "))]
        return "\n".join(f"{tables.get(it.split(' ')[1], 'rows')}: {it}" for it in read)
    if pm.type is DatabaseType.POSTGRES:
        # With no hash or merge join. Over a graph this small, Postgres reads every node once and joins them to a
        # hundred ids by a hash of the key, where it reads each node by the key over a larger one: without either join,
        # the plan reads the key where it can.
        async with transaction(rollback=True):
            await pm.execute(QuerySpecification("SET LOCAL enable_hashjoin = off"))
            await pm.execute(QuerySpecification("SET LOCAL enable_mergejoin = off"))
            return lines(await pm.query(QuerySpecification[Any](f"EXPLAIN {compiled.sql}").bind(compiled.parameters)))
    # Oracle: its values written in as literals, explained and read in one block, so on one session.
    sql = compiled.sql
    for name, value in sorted(compiled.parameters.items(), key=lambda it: -len(it[0])):
        sql = sql.replace(f"${name}", "NULL" if value is None else sql_text(str(value)))
    block = (
        "DECLARE\n    v_plan SYS_REFCURSOR;\nBEGIN\n"
        f"    EXECUTE IMMEDIATE {sql_text(f'EXPLAIN PLAN FOR {sql}')};\n"
        "    OPEN v_plan FOR SELECT plan_table_output FROM TABLE(DBMS_XPLAN.DISPLAY(format => 'BASIC +PREDICATE'));\n"
        "    DBMS_SQL.RETURN_RESULT(v_plan);\nEND;"
    )
    return lines(await pm.query(QuerySpecification[Any](block)))


def reads_by_key(pm: PersistenceManager, plan: str, table: str) -> bool:
    """Whether the plan reads a row of the table, `node` or `edge`, by its primary key."""
    if pm.type is DatabaseType.SQLITE:
        return any(
            it.startswith(f"{table}: SEARCH ") and "INTEGER PRIMARY KEY (rowid=?)" in it for it in plan.splitlines()
        )
    key = "nid" if table == "node" else "eid"
    if pm.type is DatabaseType.POSTGRES:
        # Reading the row by the key, or reading none where the key is known to be no id, as for `5.5`.
        by_key = (
            rf"Index (Only )?Scan using {GRAPH}_{table}_pkey on {GRAPH}_{table} \w+[^\n]*\n\s+Index Cond: \({key} = "
        )
        return "One-Time Filter: false" in plan or re.search(by_key, plan) is not None
    # Oracle: the key read, by its index or as the key of a join. A node read by the key a relationship's end gives is
    # no key the statement compares. Where the statement reads no more of a node than its key, Oracle drops it, and
    # reads the relationship's end by the key compared, through its index. Within a half of a relationship read both
    # ways round (#390), the key is named with no table's.
    upper = key.upper()
    return any(
        (re.search(rf'access\(.*"{upper}"', it) and not re.search(r'"\."(SRC|DST)"', it))
        or (table == "node" and re.search(r'access\("\w+"\."(SRC|DST)"=JSON_VALUE', it))
        for it in plan.splitlines()
    )


def scans(pm: PersistenceManager, plan: str, table: str) -> bool:
    """Whether the plan reads every row of the table."""
    if pm.type is DatabaseType.SQLITE:
        return any(it.startswith(f"{table}: SCAN ") for it in plan.splitlines())
    if pm.type is DatabaseType.POSTGRES:
        # A scan of the table, of the index over labels (which every node here carries), or of the key's index with no
        # condition on the key, each of which reads each row as a scan does.
        key = "nid" if table == "node" else "eid"
        lines_ = plan.splitlines()
        following = [*lines_[1:], ""]
        return any(
            f"Seq Scan on {GRAPH}_{table} " in line
            or (f"Bitmap Heap Scan on {GRAPH}_{table} " in line and "Recheck Cond: (labels" in after)
            or (
                f"Scan using {GRAPH}_{table}_pkey on {GRAPH}_{table} " in line and f"Index Cond: ({key} = " not in after
            )
            for line, after in zip(lines_, following, strict=True)
        )
    # Oracle: a scan of the table, or of an index, that reads no row by its key: each row is compared with each id.
    full = rf"TABLE ACCESS (STORAGE )?FULL\s*\|\s*{GRAPH.upper()}_{table.upper()}\b"
    scanned = re.search(full, plan) is not None or re.search(r"INDEX (FULL|FAST FULL|RANGE) SCAN", plan) is not None
    return scanned and not reads_by_key(pm, plan, table)


async def assert_keyed(
    pm: PersistenceManager, statement: str, parameters: dict[str, Any], expected: list[Any], table: str = "node"
) -> None:
    """That the statement gives `expected`, and that the store's plan reads the table by its key and scans no row of
    it."""
    found = await pm.query(spec(statement, parameters))
    assert found == expected, statement
    explained = await plan(pm, compiled(pm, statement, parameters))
    assert reads_by_key(pm, explained, table) and not scans(pm, explained, table), (
        f"{statement} does not read the {table} by its key:\n{explained}"
    )


async def assert_unkeyed(pm: PersistenceManager, statement: str, parameters: dict[str, Any], expected: list[Any]):
    """That the statement gives `expected`: where no test of the key may stand, the store reads every node, as
    before."""
    assert await pm.query(spec(statement, parameters)) == expected, statement
    explained = await plan(pm, compiled(pm, statement, parameters))
    assert scans(pm, explained, "node"), (
        f"{statement} reads a node by its key, where the comparison may be false and keep the row:\n{explained}"
    )


async def test_an_id_equal_to_a_parameter_either_way_round_reads_the_node_by_its_key(
    pm: PersistenceManager, ids: Ids, item: str
):
    five = ids(5)
    await assert_keyed(pm, f"MATCH (n{item}) WHERE id(n) = $id RETURN n.k", {"id": five}, [5])
    await assert_keyed(pm, f"MATCH (n{item}) WHERE $id = id(n) RETURN n.k", {"id": five}, [5])
    await assert_keyed(pm, "MATCH (n) WHERE elementId(n) = $id RETURN n.k", {"id": five}, [5])
    await assert_keyed(
        pm, f"MATCH (n{item}) WHERE n.k >= 0 AND id(n) = $id AND n.k < 100 RETURN n.k", {"id": five}, [5]
    )
    # 5.0 is equal to 5, as in Cypher.
    await assert_keyed(pm, f"MATCH (n{item}) WHERE id(n) = $id RETURN n.k", {"id": float(five)}, [5])
    # Written in the statement, with an exponent too.
    await assert_keyed(pm, f"MATCH (n{item}) WHERE id(n) = {five} RETURN n.k", {}, [5])
    await assert_keyed(pm, f"MATCH (n{item}) WHERE id(n) = {five}e0 RETURN n.k", {}, [5])
    await assert_keyed(pm, f"MATCH (n{item}) WHERE id(n) IN [{ids(3)}, {ids(4)}e0] RETURN n.k ORDER BY n.k", {}, [3, 4])


async def test_an_id_that_is_no_whole_number_a_text_or_null_is_equal_to_no_node(
    pm: PersistenceManager, ids: Ids, item: str
):
    statement = f"MATCH (n{item}) WHERE id(n) = $id RETURN n.k"
    await assert_keyed(pm, statement, {"id": ids(5) + 0.5}, [])
    await assert_keyed(pm, statement, {"id": str(ids(5))}, [])
    await assert_keyed(pm, statement, {"id": [ids(5)]}, [])
    assert await pm.query(spec(statement, {"id": None})) == []


async def test_each_id_an_unwind_gives_reads_its_node_by_its_key(pm: PersistenceManager, ids: Ids, item: str):
    given = [float(ids(5)), "x", None, ids(6), [ids(7)], {"id": ids(8)}, ids(9) + 0.5]
    await assert_keyed(
        pm, f"UNWIND $ids AS i MATCH (n{item}) WHERE id(n) = i RETURN n.k ORDER BY n.k", {"ids": given}, [5, 6]
    )
    await assert_keyed(
        pm,
        f"UNWIND $ids AS i MATCH (n{item}) WHERE i = id(n) RETURN n.k ORDER BY n.k",
        {"ids": [ids(1), ids(2)]},
        [1, 2],
    )


async def test_an_id_in_a_list_given_or_written_reads_each_node_by_its_key(pm: PersistenceManager, ids: Ids, item: str):
    given = [float(ids(5)), "x", None, ids(6), [ids(7)], {"id": ids(8)}, ids(9) + 0.5]
    listed = f"MATCH (n{item}) WHERE id(n) IN $ids RETURN n.k ORDER BY n.k"
    await assert_keyed(pm, listed, {"ids": given}, [5, 6])
    await assert_keyed(pm, listed, {"ids": [ids(3), ids(4)]}, [3, 4])
    await assert_keyed(
        pm,
        f"MATCH (n{item}) WHERE id(n) IN [$a, $b] RETURN n.k ORDER BY n.k",
        {"a": ids(3), "b": float(ids(4))},
        [3, 4],
    )
    # A null among the items written: compared item by item, as Cypher compares them.
    await assert_keyed(pm, f"MATCH (n{item}) WHERE id(n) IN [$a, null] RETURN n.k", {"a": ids(3)}, [3])
    await assert_keyed(pm, f"MATCH (n{item}) WHERE id(n) IN $ids RETURN n.k", {"ids": []}, [])


async def test_a_relationships_id_and_a_walk_from_a_nodes_read_by_their_keys(
    pm: PersistenceManager, ids: Ids, item: str
):
    edges = ids.edges
    await assert_keyed(pm, "MATCH ()-[r]->() WHERE id(r) = $id RETURN r.k", {"id": edges[3]}, [3], table="edge")
    await assert_keyed(
        pm,
        "MATCH ()-[r]->() WHERE elementId(r) IN $ids RETURN r.k ORDER BY r.k",
        {"ids": [edges[3], edges[4]]},
        [3, 4],
        table="edge",
    )
    await assert_keyed(pm, f"MATCH (a{item})-[r]->(b) WHERE id(a) = $id RETURN b.k", {"id": ids(5)}, [6])
    await assert_keyed(
        pm, f"MATCH (a{item}) WHERE id(a) = $id MATCH (b) WHERE id(b) = id(a) RETURN b.k", {"id": ids(5)}, [5]
    )


async def test_an_id_compared_across_a_relationship_of_either_direction_read_both_ways_round_reads_its_key(
    pm: PersistenceManager, ids: Ids, item: str
):
    # Postgres and Oracle read such a relationship from both halves of a UNION ALL (#390): its eid is the halves', each
    # read by its key.
    edges = ids.edges
    await assert_keyed(pm, f"MATCH (a{item})-[r]-(b) WHERE id(a) = $id RETURN b.k ORDER BY b.k", {"id": ids(5)}, [4, 6])
    await assert_keyed(pm, "MATCH ()-[r]-() WHERE id(r) = $id RETURN r.k", {"id": edges[3]}, [3, 3], table="edge")
    await assert_keyed(
        pm,
        "MATCH ()-[r]-() WHERE id(r) IN $ids RETURN r.k ORDER BY r.k",
        {"ids": [edges[3], edges[4]]},
        [3, 3, 4, 4],
        table="edge",
    )
    await assert_keyed(
        pm,
        f"MATCH (a{item}) WHERE id(a) = $id MATCH (a)-[]-(b) MATCH (c) WHERE id(c) = id(b) RETURN c.k ORDER BY c.k",
        {"id": ids(5)},
        [4, 6],
    )


async def test_under_not_beside_an_or_or_as_a_value_the_comparison_stands_alone_and_gives_what_it_gave(
    pm: PersistenceManager, ids: Ids, item: str
):
    five = ids(5)
    counted = f"MATCH (n{item}) WHERE NOT id(n) IN $ids RETURN count(n)"
    await assert_unkeyed(pm, f"MATCH (n{item}) WHERE NOT id(n) = $id RETURN count(n)", {"id": five}, [NODES - 1])
    await assert_unkeyed(pm, counted, {"ids": [five, None]}, [0])
    await assert_unkeyed(pm, counted, {"ids": [five, "x"]}, [NODES - 1])
    await assert_unkeyed(
        pm, f"MATCH (n{item}) WHERE id(n) = $id OR n.k = 7 RETURN n.k ORDER BY n.k", {"id": five}, [5, 7]
    )
    await assert_unkeyed(pm, f"MATCH (n{item}) WHERE (id(n) = $id) = false RETURN count(n)", {"id": five}, [NODES - 1])
    await assert_unkeyed(pm, f"MATCH (n{item}) RETURN count(CASE WHEN id(n) = $id THEN 1 END)", {"id": five}, [1])
    assert await pm.query(spec(f"MATCH (n{item}) WHERE n.k = 5 RETURN [n.k, id(n) = $id]", {"id": float(five)})) == [
        [5, True]
    ]
    assert await pm.query(spec(f"MATCH (n{item}) WHERE n.k = 5 RETURN [n.k, id(n) IN $ids]", {"ids": [None, "x"]})) == [
        [5, None]
    ]


async def test_an_unwind_of_a_hundred_ids_reads_a_hundred_nodes_not_every_node_for_each(
    pm: PersistenceManager, ids: Ids, item: str
):
    given = {"ids": [ids(it * 7) for it in range(100)]}
    statement = f"UNWIND $ids AS i MATCH (n{item}) WHERE id(n) = i RETURN count(n)"
    assert await pm.query(spec(statement, given)) == [100]
    runs: list[float] = []
    for _ in range(5):
        start = time.perf_counter()
        await pm.query(spec(statement, given))
        runs.append((time.perf_counter() - start) * 1000)
    print(f"{store_of(pm)}: UNWIND of 100 ids over {NODES} nodes, mean of 5 runs: {sum(runs) / len(runs):.2f} ms")
    await assert_keyed(pm, statement, given, [100])
