# Written by scripts/unasync.py from tests/integration/sql/test_id_key_plans.py:
# change that, and run the script again.
"""
#393, drivine4j#575: an id compared by `=` or `IN`, where the row must pass the comparison, is read by the node's or the
relationship's primary key. The id is JSON, compared as JSON, so each such read was a scan of the table, and an
`UNWIND` of ids a scan for each. The rows are those the comparison alone keeps, under `NOT` and `OR` too, where no test
of the key stands. drivine4j's IdKeyPlanTest checks the same.
"""

import re
import time
from collections.abc import Iterator
from typing import Any, Literal

import pytest

from drivine.cypher.cypher_specification import cypher_compiled
from drivine.cypher.sql.compiled_cypher import CompiledCypher
from drivine.query.statement import cypher_statement
from drivine.sync import DatabaseType, PersistenceManager, PersistenceManagerFactory, QuerySpecification, transaction
from tests.sync_integration.oracle.oracle_seed import oracle_plan

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


def clear(pm: PersistenceManager) -> None:
    pm.execute(QuerySpecification("DELETE FROM drivine_edge"))
    pm.execute(QuerySpecification("DELETE FROM drivine_node"))


def analyze(pm: PersistenceManager) -> None:
    """Gathers the store's statistics of the graph's tables, which it plans by."""
    if pm.type is DatabaseType.ORACLE:
        pm.execute(
            QuerySpecification(
                f"BEGIN DBMS_STATS.GATHER_TABLE_STATS(USER, '{GRAPH.upper()}_NODE'); "
                f"DBMS_STATS.GATHER_TABLE_STATS(USER, '{GRAPH.upper()}_EDGE'); END;"
            )
        )
    else:
        pm.execute(QuerySpecification(f"ANALYZE {GRAPH}_node"))
        pm.execute(QuerySpecification(f"ANALYZE {GRAPH}_edge"))


@pytest.fixture(scope="module")
def ids(factory: PersistenceManagerFactory, store: str) -> Iterator[Ids]:
    pm = factory.get(store)
    clear(pm)
    pm.query(spec(f"UNWIND range(0, {NODES - 1}) AS k CREATE (:Item {{k: k}})"))
    rows = pm.query(spec("MATCH (n:Item) RETURN [n.k, id(n)]"))
    nodes = {int(k): int(i) for k, i in rows}
    # A relationship from each of the first nodes to the next, each found by its id.
    pairs = [[k, nodes[k], nodes[k + 1]] for k in range(EDGES)]
    pm.query(
        spec(
            "UNWIND $pairs AS p MATCH (a), (b) WHERE id(a) = p[1] AND id(b) = p[2] CREATE (a)-[:NEXT {k: p[0]}]->(b)",
            {"pairs": pairs},
        )
    )
    edges = {int(k): int(i) for k, i in pm.query(spec("MATCH (a)-[r:NEXT]->() RETURN [a.k, id(r)]"))}
    analyze(pm)
    yield Ids(nodes, edges)
    clear(pm)


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


def plan(pm: PersistenceManager, compiled: CompiledCypher) -> str:
    """The plan the store makes for the compiled statement, as it writes it."""
    if pm.type is DatabaseType.SQLITE:
        # A line for each table or rows the plan reads, each led by the table it reads.
        tables = {it[2]: it[1] for it in re.findall(rf"\b({GRAPH}_(node|edge)) (\w+)", compiled.sql)}
        rows = pm.query(QuerySpecification[Any](f"EXPLAIN QUERY PLAN {compiled.sql}").bind(compiled.parameters))
        details = [str(it["detail"]) for it in rows]
        read = [it for it in details if it.startswith(("SCAN ", "SEARCH "))]
        return "\n".join(f"{tables.get(it.split(' ')[1], 'rows')}: {it}" for it in read)
    if pm.type is DatabaseType.POSTGRES:
        # With no hash or merge join. Over a graph this small, Postgres reads every node once and joins them to a
        # hundred ids by a hash of the key, where it reads each node by the key over a larger one: without either join,
        # the plan reads the key where it can.
        with transaction(rollback=True):
            pm.execute(QuerySpecification("SET LOCAL enable_hashjoin = off"))
            pm.execute(QuerySpecification("SET LOCAL enable_mergejoin = off"))
            return lines(pm.query(QuerySpecification[Any](f"EXPLAIN {compiled.sql}").bind(compiled.parameters)))
    # Oracle: its values written in as literals, explained and read in one block, so on one session.
    return oracle_plan(pm, compiled.sql, compiled.parameters, "BASIC +PREDICATE")


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
    scanned = scans_any(pm, plan, table) or re.search(r"INDEX (FULL|FAST FULL|RANGE) SCAN", plan) is not None
    return scanned and not reads_by_key(pm, plan, table)


def scans_any(pm: PersistenceManager, plan: str, table: str) -> bool:
    """
    Whether the plan reads every row of the table for some node, though it reads another by its key. On SQLite and
    Postgres, `scans` says so of each row source; on Oracle, a scan of the table beside a read by the key or not.
    """
    if pm.type is not DatabaseType.ORACLE:
        return scans(pm, plan, table)
    return re.search(rf"TABLE ACCESS (STORAGE )?FULL\s*\|\s*{GRAPH.upper()}_{table.upper()}\b", plan) is not None


def assert_keyed(
    pm: PersistenceManager, statement: str, parameters: dict[str, Any], expected: list[Any], table: str = "node"
) -> None:
    """That the statement gives `expected`, and that the store's plan reads the table by its key and scans no row of
    it."""
    found = pm.query(spec(statement, parameters))
    assert found == expected, statement
    explained = plan(pm, compiled(pm, statement, parameters))
    assert reads_by_key(pm, explained, table) and not scans(pm, explained, table), (
        f"{statement} does not read the {table} by its key:\n{explained}"
    )


def assert_unkeyed(pm: PersistenceManager, statement: str, parameters: dict[str, Any], expected: list[Any]):
    """That the statement gives `expected`: where no test of the key may stand, the store reads every node, as
    before."""
    assert pm.query(spec(statement, parameters)) == expected, statement
    explained = plan(pm, compiled(pm, statement, parameters))
    assert scans(pm, explained, "node"), (
        f"{statement} reads a node by its key, where the comparison may be false and keep the row:\n{explained}"
    )


def test_an_id_equal_to_a_parameter_either_way_round_reads_the_node_by_its_key(
    pm: PersistenceManager, ids: Ids, item: str
):
    five = ids(5)
    assert_keyed(pm, f"MATCH (n{item}) WHERE id(n) = $id RETURN n.k", {"id": five}, [5])
    assert_keyed(pm, f"MATCH (n{item}) WHERE $id = id(n) RETURN n.k", {"id": five}, [5])
    assert_keyed(pm, "MATCH (n) WHERE elementId(n) = $id RETURN n.k", {"id": five}, [5])
    assert_keyed(pm, f"MATCH (n{item}) WHERE n.k >= 0 AND id(n) = $id AND n.k < 100 RETURN n.k", {"id": five}, [5])
    # 5.0 is equal to 5, as in Cypher.
    assert_keyed(pm, f"MATCH (n{item}) WHERE id(n) = $id RETURN n.k", {"id": float(five)}, [5])
    # Written in the statement, with an exponent too.
    assert_keyed(pm, f"MATCH (n{item}) WHERE id(n) = {five} RETURN n.k", {}, [5])
    assert_keyed(pm, f"MATCH (n{item}) WHERE id(n) = {five}e0 RETURN n.k", {}, [5])
    assert_keyed(pm, f"MATCH (n{item}) WHERE id(n) IN [{ids(3)}, {ids(4)}e0] RETURN n.k ORDER BY n.k", {}, [3, 4])


def test_an_id_that_is_no_whole_number_a_text_or_null_is_equal_to_no_node(pm: PersistenceManager, ids: Ids, item: str):
    statement = f"MATCH (n{item}) WHERE id(n) = $id RETURN n.k"
    assert_keyed(pm, statement, {"id": ids(5) + 0.5}, [])
    assert_keyed(pm, statement, {"id": str(ids(5))}, [])
    assert_keyed(pm, statement, {"id": [ids(5)]}, [])
    assert pm.query(spec(statement, {"id": None})) == []


def test_each_id_an_unwind_gives_reads_its_node_by_its_key(pm: PersistenceManager, ids: Ids, item: str):
    given = [float(ids(5)), "x", None, ids(6), [ids(7)], {"id": ids(8)}, ids(9) + 0.5]
    assert_keyed(
        pm, f"UNWIND $ids AS i MATCH (n{item}) WHERE id(n) = i RETURN n.k ORDER BY n.k", {"ids": given}, [5, 6]
    )
    assert_keyed(
        pm,
        f"UNWIND $ids AS i MATCH (n{item}) WHERE i = id(n) RETURN n.k ORDER BY n.k",
        {"ids": [ids(1), ids(2)]},
        [1, 2],
    )


def test_an_id_in_a_list_given_or_written_reads_each_node_by_its_key(pm: PersistenceManager, ids: Ids, item: str):
    given = [float(ids(5)), "x", None, ids(6), [ids(7)], {"id": ids(8)}, ids(9) + 0.5]
    listed = f"MATCH (n{item}) WHERE id(n) IN $ids RETURN n.k ORDER BY n.k"
    assert_keyed(pm, listed, {"ids": given}, [5, 6])
    assert_keyed(pm, listed, {"ids": [ids(3), ids(4)]}, [3, 4])
    assert_keyed(
        pm,
        f"MATCH (n{item}) WHERE id(n) IN [$a, $b] RETURN n.k ORDER BY n.k",
        {"a": ids(3), "b": float(ids(4))},
        [3, 4],
    )
    # A null among the items written: compared item by item, as Cypher compares them.
    assert_keyed(pm, f"MATCH (n{item}) WHERE id(n) IN [$a, null] RETURN n.k", {"a": ids(3)}, [3])
    assert_keyed(pm, f"MATCH (n{item}) WHERE id(n) IN $ids RETURN n.k", {"ids": []}, [])


def test_a_relationships_id_and_a_walk_from_a_nodes_read_by_their_keys(pm: PersistenceManager, ids: Ids, item: str):
    edges = ids.edges
    assert_keyed(pm, "MATCH ()-[r]->() WHERE id(r) = $id RETURN r.k", {"id": edges[3]}, [3], table="edge")
    assert_keyed(
        pm,
        "MATCH ()-[r]->() WHERE elementId(r) IN $ids RETURN r.k ORDER BY r.k",
        {"ids": [edges[3], edges[4]]},
        [3, 4],
        table="edge",
    )
    assert_keyed(pm, f"MATCH (a{item})-[r]->(b) WHERE id(a) = $id RETURN b.k", {"id": ids(5)}, [6])
    assert_keyed(pm, f"MATCH (a{item}) WHERE id(a) = $id MATCH (b) WHERE id(b) = id(a) RETURN b.k", {"id": ids(5)}, [5])


def test_a_node_equal_to_a_node_read_by_its_key_is_read_by_its_key_too_whichever_side_of_the_equals_it_is_on(
    pm: PersistenceManager, ids: Ids, item: str
):
    # #396, drivine4j#576: Oracle's comparison of the two keys, `+ 0` on one side alone, read every node for that side.
    for test in ["b = a", "a = b", "id(b) = id(a)", "id(a) = id(b)"]:
        statement = f"MATCH (a{item}) WHERE id(a) = $id MATCH (b{item}) WHERE {test} RETURN b.k"
        parameters = {"id": ids(5)}
        assert_keyed(pm, statement, parameters, [5])
        explained = plan(pm, compiled(pm, statement, parameters))
        assert not scans_any(pm, explained, "node"), f"{statement} reads every node for b:\n{explained}"


def test_an_id_compared_across_a_relationship_of_either_direction_read_both_ways_round_reads_its_key(
    pm: PersistenceManager, ids: Ids, item: str
):
    # Postgres and Oracle read such a relationship from both halves of a UNION ALL (#390): its eid is the halves', each
    # read by its key.
    edges = ids.edges
    assert_keyed(pm, f"MATCH (a{item})-[r]-(b) WHERE id(a) = $id RETURN b.k ORDER BY b.k", {"id": ids(5)}, [4, 6])
    assert_keyed(pm, "MATCH ()-[r]-() WHERE id(r) = $id RETURN r.k", {"id": edges[3]}, [3, 3], table="edge")
    assert_keyed(
        pm,
        "MATCH ()-[r]-() WHERE id(r) IN $ids RETURN r.k ORDER BY r.k",
        {"ids": [edges[3], edges[4]]},
        [3, 3, 4, 4],
        table="edge",
    )
    assert_keyed(
        pm,
        f"MATCH (a{item}) WHERE id(a) = $id MATCH (a)-[]-(b) MATCH (c) WHERE id(c) = id(b) RETURN c.k ORDER BY c.k",
        {"id": ids(5)},
        [4, 6],
    )


def test_under_not_beside_an_or_or_as_a_value_the_comparison_stands_alone_and_gives_what_it_gave(
    pm: PersistenceManager, ids: Ids, item: str
):
    five = ids(5)
    counted = f"MATCH (n{item}) WHERE NOT id(n) IN $ids RETURN count(n)"
    assert_unkeyed(pm, f"MATCH (n{item}) WHERE NOT id(n) = $id RETURN count(n)", {"id": five}, [NODES - 1])
    assert_unkeyed(pm, counted, {"ids": [five, None]}, [0])
    assert_unkeyed(pm, counted, {"ids": [five, "x"]}, [NODES - 1])
    assert_unkeyed(pm, f"MATCH (n{item}) WHERE id(n) = $id OR n.k = 7 RETURN n.k ORDER BY n.k", {"id": five}, [5, 7])
    assert_unkeyed(pm, f"MATCH (n{item}) WHERE (id(n) = $id) = false RETURN count(n)", {"id": five}, [NODES - 1])
    assert_unkeyed(pm, f"MATCH (n{item}) RETURN count(CASE WHEN id(n) = $id THEN 1 END)", {"id": five}, [1])
    assert pm.query(spec(f"MATCH (n{item}) WHERE n.k = 5 RETURN [n.k, id(n) = $id]", {"id": float(five)})) == [
        [5, True]
    ]
    assert pm.query(spec(f"MATCH (n{item}) WHERE n.k = 5 RETURN [n.k, id(n) IN $ids]", {"ids": [None, "x"]})) == [
        [5, None]
    ]


def test_an_unwind_of_a_hundred_ids_reads_a_hundred_nodes_not_every_node_for_each(
    pm: PersistenceManager, ids: Ids, item: str
):
    given = {"ids": [ids(it * 7) for it in range(100)]}
    statement = f"UNWIND $ids AS i MATCH (n{item}) WHERE id(n) = i RETURN count(n)"
    assert pm.query(spec(statement, given)) == [100]
    runs: list[float] = []
    for _ in range(5):
        start = time.perf_counter()
        pm.query(spec(statement, given))
        runs.append((time.perf_counter() - start) * 1000)
    print(f"{store_of(pm)}: UNWIND of 100 ids over {NODES} nodes, mean of 5 runs: {sum(runs) / len(runs):.2f} ms")
    assert_keyed(pm, statement, given, [100])
