# Written by scripts/unasync.py from tests/integration/sql/test_property_key_index.py:
# change that, and run the script again.
"""
#429, liberation-data/drivine4j#617: a node matched by a property its index is over, the value an unwound row's, was
compared as JSON inside a `CASE` alone, which no index answers, so a batch of relationships read every node of the label
for each row. Beside it now stands a test the index answers on Postgres, for a value of any kind, that leaves out no row
the comparison keeps: a text, a number, a float equal to a whole one, a truth value, a list, null and a missing key each
give the rows they gave. On Oracle the same test is written for the properties `OracleCypherCompiler`'s
`indexed_properties` names, which a statement the connection runs names none of yet (liberation-data/drivine4j#638): the
rows here are found as before on it, and its plan is checked with a compiler told of the indexes. drivine4j's
PropertyKeyIndexTest is the reference.
"""

import re
from collections.abc import Iterator
from typing import Any

import pytest

from drivine.cypher.sql.compiled_cypher import CompiledCypher
from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler
from drivine.sync import (
    DatabaseType,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    StatelessGraphObjectManager,
    uniqueness_constraint,
)
from tests.sync_integration.sql.test_id_key_plans import GRAPH, analyze, clear, plan, spec

NODES = 5000
IMPORT_LABEL = "UNIQUE IMPORT LABEL"
IMPORT_ID = "UNIQUE IMPORT ID"
IMPORTED = 2000
BIG = 9007199254740993
LONG = "x" * 5000


def query(pm: PersistenceManager, statement: str, parameters: dict[str, Any] | None = None) -> list[Any]:
    return pm.query(spec(statement, parameters))


@pytest.fixture(scope="module")
def seeded(factory: PersistenceManagerFactory, store: str) -> Iterator[None]:
    pm = factory.get(store)
    gom = StatelessGraphObjectManager(pm)
    constraints = [uniqueness_constraint("Person", "id"), uniqueness_constraint(IMPORT_LABEL, IMPORT_ID)]
    clear(pm)
    for it in constraints:
        gom.constraints.ensure(it)
    query(pm, f"UNWIND range(0, {IMPORTED - 1}) AS k CREATE (:`{IMPORT_LABEL}` {{`{IMPORT_ID}`: k}})")
    query(pm, f"UNWIND range(0, {NODES - 1}) AS k CREATE (:Person {{id: 'p' + toString(k), k: k}})")
    query(
        pm,
        "CREATE (:Person {id: 7, k: -1}), (:Person {id: 8.0, k: -2}), (:Person {id: true, k: -3}), "
        "(:Person {id: [1, 2], k: -4})",
    )
    query(
        pm,
        "CREATE (:Person {id: ['a', 'b'], k: -5}), (:Person {id: [], k: -6}), (:Person {id: [1], k: -7}), "
        "(:Person {id: [[1, 2]], k: -8})",
    )
    query(
        pm,
        "CREATE (:Person {id: 0.1, k: -9}), (:Person {id: 1.0e20, k: -10}), (:Person {id: -7, k: -11}), "
        "(:Person {id: 0, k: -12})",
    )
    query(
        pm,
        "CREATE (:Person {id: '', k: -13}), (:Person {id: $big, k: -14}), (:Person {id: $long, k: -15})",
        {"big": BIG, "long": LONG},
    )
    analyze(pm)
    yield
    clear(pm)
    for it in constraints:
        gom.constraints.drop(it)


def compiled(pm: PersistenceManager, statement: str, parameters: dict[str, Any]) -> CompiledCypher:
    """
    The statement as the store compiles it. On Oracle, compiled knowing the indexes the graph has, which a statement
    the connection runs is not yet: its rows are found as before, without the index, which the tests of rows here check
    stays so.
    """
    if pm.type is DatabaseType.ORACLE:
        indexed = {("Person", "id"), (IMPORT_LABEL, IMPORT_ID)}
        return OracleCypherCompiler(GRAPH, indexed_properties=indexed).compile(statement, parameters)
    return PostgresCypherCompiler(GRAPH).compile(statement, parameters)


def probes_index(pm: PersistenceManager, explained: str) -> bool:
    """Whether the plan finds the nodes by the index over their `id`, not by reading every node of the label."""
    if pm.type is DatabaseType.POSTGRES:
        cond = rf"Index Cond: \(\(props ->> '(id|{IMPORT_ID})'::text\) = "
        return re.search(cond, explained) is not None and f"Seq Scan on {GRAPH}_node" not in explained
    # Oracle: the uniqueness constraint's index read by its key, and neither the index over labels, which reads every
    # node of the label, nor a full scan of the nodes.
    upper = GRAPH.upper()
    return (
        re.search(rf"INDEX (UNIQUE|RANGE) SCAN\s*\|\s*{upper}_UQ_", explained) is not None
        and f"{upper}_NODE_LABELS" not in explained
        and re.search(rf"TABLE ACCESS (STORAGE )?FULL\s*\|\s*{upper}_NODE\b", explained) is None
    )


def read_through_index(pm: PersistenceManager) -> None:
    """
    SQLite compares a row's value as JSON alone, as drivine4j's SQLite compiler does: no index finds a row's node there,
    and a batch of relationships over this graph reads every node for each end of each row, for longer than a test runs.
    """
    if pm.type is DatabaseType.SQLITE:
        pytest.skip("SQLite reads a row's node by no index: a batch over 7,000 nodes takes minutes, as before.")


def plans_checked(pm: PersistenceManager) -> None:
    if pm.type is DatabaseType.SQLITE:
        pytest.skip("SQLite compares a row's value as JSON alone, as before: its plan is not the one drivine4j checks.")


def test_a_rows_value_of_each_kind_finds_the_nodes_it_is_equal_to_as_json_compares_them(
    pm: PersistenceManager, seeded: None
):
    # 7.0 is 7, and 8 is 8.0, as in Cypher; a list, empty, of one item or of lists, is equal to the same list only, not
    # to its item or to its items in another order; null and a missing key are equal to nothing.
    rows = [
        *({"s": it} for it in ["p5", 7, 7.0, 8, True, [1, 2], None]),
        {},
        *({"s": it} for it in ["nobody", "7", ["a", "b"], [], [1], [[1, 2]], [2, 1], 1]),
    ]
    expected = [-8, -7, -6, -5, -4, -3, -2, -1, -1, 5]
    by_pattern = "UNWIND $rows AS row MATCH (a:Person {id: row.s}) RETURN a.k ORDER BY a.k"
    assert query(pm, by_pattern, {"rows": rows}) == expected
    # The same, compared in a WHERE.
    by_where = "UNWIND $rows AS row MATCH (a:Person) WHERE a.id = row.s RETURN a.k ORDER BY a.k"
    assert query(pm, by_where, {"rows": rows}) == expected
    # A list on its own: beside a null row, Oracle has been seen to plan the read otherwise and find lists it finds no
    # other way.
    assert query(pm, by_pattern, {"rows": [{"s": [1, 2]}, {"s": []}]}) == [-6, -4]


def test_a_rows_number_empty_text_or_long_text_finds_the_node_it_is_equal_to(pm: PersistenceManager, seeded: None):
    # -0.0 is 0, as in Cypher. Oracle's index holds no text for an empty or a long one, which JSON_VALUE reads as null.
    rows = [{"s": it} for it in [0.1, 1.0e20, -7, -0.0, BIG, "", LONG]]
    statement = "UNWIND $rows AS row MATCH (a:Person {id: row.s}) RETURN a.k"
    assert query(pm, f"{statement} ORDER BY a.k", {"rows": rows}) == [-15, -14, -13, -12, -11, -10, -9]
    # Each on its own, as rows that hold no list.
    for row, k in zip(rows, [-9, -10, -11, -12, -14, -13, -15], strict=True):
        assert query(pm, statement, {"rows": [row]}) == [k], row
    # A parameter's or a written empty or long text.
    assert query(pm, "MATCH (a:Person {id: $s}) RETURN a.k", {"s": ""}) == [-13]
    assert query(pm, "MATCH (a:Person {id: ''}) RETURN a.k") == [-13]
    assert query(pm, "MATCH (a:Person {id: $s}) RETURN a.k", {"s": LONG}) == [-15]


def test_a_text_the_statement_works_out_or_an_item_of_a_list_it_writes_finds_its_node_though_empty_or_long(
    pm: PersistenceManager, seeded: None
):
    # Not an empty text worked out: on Oracle, a text function gives null for one, as SQL holds no empty text.
    rows = {"rows": [{"s": it} for it in [LONG, "p5"]]}
    worked_out = "UNWIND $rows AS row MATCH (a:Person {id: toString(row.s)}) RETURN a.k ORDER BY a.k"
    assert query(pm, worked_out, rows) == [-15, 5]
    assert query(pm, "UNWIND ['', 'p5'] AS s MATCH (a:Person {id: s}) RETURN a.k ORDER BY a.k") == [-13, 5]


def test_a_list_the_statement_writes_finds_what_each_of_its_rows_is_equal_to(pm: PersistenceManager, seeded: None):
    # A list, a parameter's list or a function's among them.
    statement = (
        "UNWIND [{s: 'p5'}, {s: [1, 2]}, {s: $l}, {s: range(7, 8)}] AS row MATCH (a:Person {id: row.s}) "
        "RETURN a.k ORDER BY a.k"
    )
    assert query(pm, statement, {"l": []}) == [-6, -4, 5]
    assert query(
        pm, "UNWIND [{s: 'p5'}, {s: $l}] AS row MATCH (a:Person {id: row.s}) RETURN a.k ORDER BY a.k", {"l": [1, 2]}
    ) == [-4, 5]
    assert query(pm, "UNWIND [[1, 2], 'p5'] AS s MATCH (a:Person {id: s}) RETURN a.k ORDER BY a.k") == [-4, 5]
    assert query(
        pm,
        "UNWIND [{s: datetime('2024-03-15T10:00:00Z')}, {s: range(1, 2)}] AS row MATCH (a:Person {id: row.s}) "
        "RETURN a.k",
    ) == [-4]
    assert query(
        pm, "UNWIND [{s: 'p5'}, {s: null}, {t: [1, 2]}, {s: 7.5}] AS row MATCH (a:Person {id: row.s}) RETURN a.k"
    ) == [5]
    # The items themselves, of a parameter's list or of one written, and parameters that hold no list among literals.
    scalars = {"ids": ["p5", 7.0, 8, True, BIG, None, 0.1], "a": 7.0, "b": 8, "c": True, "d": BIG}
    found = [-14, -9, -3, -2, -1, 5]
    without = [it for it in found if it != -9]
    assert query(pm, "UNWIND $ids AS id MATCH (a:Person {id: id}) RETURN a.k ORDER BY a.k", scalars) == found
    assert (
        query(pm, "UNWIND [$a, $b, $c, $d, 'p5'] AS s MATCH (a:Person {id: s}) RETURN a.k ORDER BY a.k", scalars)
        == without
    )
    assert (
        query(
            pm,
            "UNWIND [{s: $a}, {s: $b}, {s: $c}, {s: $d}, {s: 'p5'}] AS row MATCH (a:Person {id: row.s}) "
            "RETURN a.k ORDER BY a.k",
            scalars,
        )
        == without
    )
    assert query(
        pm, "UNWIND $ids AS id MATCH (a:Person {id: id}) RETURN a.k ORDER BY a.k", {"ids": ["p5", [1, 2], []]}
    ) == [-6, -4, 5]


def test_a_key_of_a_map_a_row_holds_finds_what_it_is_equal_to_where_the_map_is_missing_null_or_holds_a_list(
    pm: PersistenceManager, seeded: None
):
    statement = "UNWIND $rows AS row MATCH (a:Person {id: row.a.b}) RETURN a.k ORDER BY a.k"
    lists = [{"a": {"b": "p5"}}, {"a": {"b": [1, 2]}}, {"a": None}, {}, {"a": {}}]
    assert query(pm, statement, {"rows": lists}) == [-4, 5]
    scalars = [{"a": {"b": "p5"}}, {"a": None}, {}, {"a": {"c": [1]}}]
    assert query(pm, statement, {"rows": scalars}) == [5]
    # A key read of a list or a text, which Neo4j refuses: what the comparison alone gives, as a WHERE compares it.
    where = "UNWIND $rows AS row MATCH (a:Person) WHERE a.id = row.a.b RETURN a.k ORDER BY a.k"
    odd = {"rows": [{"a": [{"b": [1, 2]}]}, {"a": "text"}, {"a": {"b": "p5"}}]}
    assert query(pm, statement, odd) == query(pm, where, odd)


def test_a_truth_value_or_a_number_finds_no_text_that_looks_like_it_nor_a_text_the_truth_value_or_number(
    pm: PersistenceManager, seeded: None
):
    # By =, by its reverse, or by not-equal.
    query(pm, "CREATE (:Odd {id: 'true', k: 1}), (:Odd {id: true, k: 2}), (:Odd {id: '7', k: 3}), (:Odd {id: 7, k: 4})")
    rows = {"rows": [{"i": i, "s": s} for i, s in enumerate([True, "true", 7, "7", None, 7.0])]}
    equal = [2, 11, 24, 33, 54]
    projected = "RETURN row.i * 10 + a.k AS p ORDER BY p"
    assert query(pm, f"UNWIND $rows AS row MATCH (a:Odd {{id: row.s}}) {projected}", rows) == equal
    assert query(pm, f"UNWIND $rows AS row MATCH (a:Odd) WHERE a.id = row.s {projected}", rows) == equal
    assert query(pm, f"UNWIND $rows AS row MATCH (a:Odd) WHERE row.s = a.id {projected}", rows) == equal
    # Not equal: a null row is equal to nothing and unequal to nothing.
    unequal = [1, 3, 4, 12, 13, 14, 21, 22, 23, 31, 32, 34, 51, 52, 53]
    assert query(pm, f"UNWIND $rows AS row MATCH (a:Odd) WHERE a.id <> row.s {projected}", rows) == unequal
    assert query(pm, f"UNWIND $rows AS row MATCH (a:Odd) WHERE NOT a.id = row.s {projected}", rows) == unequal
    if pm.type is not DatabaseType.ORACLE:
        # Postgres's JSON, and SQLite's, hold no infinity.
        return
    # An infinity finds the one stored, and NaN is equal to nothing, itself included.
    infinite = {"i": float("inf"), "n": float("-inf"), "nan": float("nan")}
    query(pm, "CREATE (:Odd {id: $i, k: 5}), (:Odd {id: $n, k: 6}), (:Odd {id: $nan, k: 7})", infinite)
    infinities = {"rows": [{"s": it} for it in [float("inf"), float("-inf")]]}
    by_pattern = "UNWIND $rows AS row MATCH (a:Odd {id: row.s}) RETURN a.k ORDER BY a.k"
    assert query(pm, by_pattern, infinities) == [5, 6]
    by_where = "UNWIND $rows AS row MATCH (a:Odd) WHERE a.id = row.s RETURN a.k ORDER BY a.k"
    assert query(pm, by_where, infinities) == [5, 6]
    nan = {"rows": [{"s": float("nan")}]}
    if query(pm, by_pattern, nan) != []:
        # As Oracle compared NaN already, with no index named: NaN is bound as the text "NaN" (#164), and is equal to
        # itself (#327, liberation-data/drivine4j#409).
        pytest.xfail("On Oracle a NaN given is equal to a NaN stored (#164, #327).")


def test_a_merge_of_each_row_finds_the_node_it_is_equal_to_and_makes_none(pm: PersistenceManager, seeded: None):
    rows = [{"s": it} for it in ["p3", 7.0, 8, [1, 2], True, 0.1, ""]]
    statement = "UNWIND $rows AS row MERGE (a:Person {id: row.s}) RETURN a.k ORDER BY a.k"
    assert query(pm, statement, {"rows": rows}) == [-13, -9, -4, -3, -2, -1, 3]
    assert query(pm, "MATCH (a:Person) RETURN count(a)") == [NODES + 15]


def test_a_node_at_a_relationships_end_is_found_by_a_rows_or_a_parameters_value(pm: PersistenceManager, seeded: None):
    query(pm, "MATCH (a:Person {id: 'p10'}), (b:Person {id: 'p11'}) CREATE (a)-[:LINKS]->(b)")
    assert query(pm, "MATCH (a:Person)-[:LINKS]->(b:Person {id: $s}) RETURN a.k", {"s": "p11"}) == [10]
    assert query(pm, "MATCH (a:Person)-[:LINKS]->(b:Person {id: 'p11'}) RETURN a.k") == [10]
    assert query(
        pm,
        "UNWIND $rows AS row MATCH (a:Person)-[:LINKS]->(b:Person {id: row.s}) RETURN a.k",
        {"rows": [{"s": "p11"}, {"s": "p12"}]},
    ) == [10]
    assert query(pm, "MATCH (a:Person)-[:LINKS]->(b:Person {id: $s}) RETURN count(a)", {"s": "p12"}) == [0]


def test_each_row_of_an_unwind_finds_its_node_through_the_index_not_by_reading_every_node_of_the_label(
    pm: PersistenceManager, seeded: None
):
    plans_checked(pm)
    rows = {
        "rows": [{"s": f"p{it * 7}", "d": f"p{it * 7 + 1}"} for it in range(100)],
        "ids": [f"p{it * 7}" for it in range(100)],
    }
    statements = [
        "UNWIND $rows AS row MATCH (a:Person {id: row.s}) RETURN a.k",
        "UNWIND $ids AS id MATCH (a:Person {id: id}) RETURN a.k",
        "UNWIND $rows AS row MATCH (a:Person {id: row.s}) MATCH (b:Person {id: row.d}) RETURN a.k, b.k",
    ]
    # On Oracle a WHERE names no label to write the labelled index's expression for.
    if pm.type is DatabaseType.POSTGRES:
        statements.append("UNWIND $rows AS row MATCH (a:Person) WHERE a.id = row.s RETURN a.k")
    for statement in statements:
        explained = plan(pm, compiled(pm, statement, rows))
        print(f"{statement}\n{explained}\n")
        assert probes_index(pm, explained), f"{statement} reads every node of the label for each row:\n{explained}"
    assert query(pm, "UNWIND $rows AS row MATCH (a:Person {id: row.s}) RETURN count(a)", rows) == [100]


def test_a_rows_number_finds_a_node_whose_number_the_store_keeps_with_more_zeros_after_its_point(
    pm: PersistenceManager, seeded: None
):
    # Postgres's numeric keeps the scale a product makes: 0.25 * 10 is 2.50, 2.0 * 2.0 is 4.00, and 1.0E-5 is 0.000010.
    query(
        pm,
        "CREATE (:Scaled {id: 0.25 * 10, k: 1}), (:Scaled {id: 0.25 * 4, k: 2}), (:Scaled {id: 2.0 * 2.0, k: 3}), "
        "(:Scaled {id: 1.0E-5, k: 4}), (:Scaled {id: [1.0, 2.50], k: 5})",
    )
    for given, expected in [([2.5, 1, 4, 0.00001], [1, 2, 3, 4]), ([1.0, 4.0], [2, 3]), ([[1, 2.5]], [5])]:
        bound = {"rows": [{"s": it} for it in given]}
        statement = "UNWIND $rows AS row MATCH (a:Scaled {id: row.s}) RETURN a.k ORDER BY a.k"
        assert query(pm, statement, bound) == expected, given
        where = "UNWIND $rows AS row MATCH (a:Scaled) WHERE a.id = row.s RETURN a.k ORDER BY a.k"
        assert query(pm, where, bound) == expected, f"{given} in a WHERE"


def test_the_same_statement_given_rows_with_no_list_then_rows_with_one_finds_the_lists_node_the_second_time(
    pm: PersistenceManager, seeded: None
):
    # A statement is compiled for the rows it is given: one whose rows hold no list at the key leaves out the test that
    # lets a list through, and is no statement compiled for rows that do.
    statement = "UNWIND $rows AS row MATCH (a:Person {id: row.s}) RETURN a.k ORDER BY a.k"
    assert query(pm, statement, {"rows": [{"s": "p5"}]}) == [5]
    assert query(pm, statement, {"rows": [{"s": "p5"}, {"s": [1, 2]}]}) == [-4, 5]
    assert query(pm, statement, {"rows": [{"s": "p5"}]}) == [5]


def test_a_relationship_for_each_row_is_made_between_the_nodes_the_row_names(pm: PersistenceManager, seeded: None):
    read_through_index(pm)
    rows = {"rows": [{"s": f"p{it}", "d": f"p{it + 1}"} for it in range(50)]}
    query(
        pm,
        "UNWIND $rows AS row MATCH (a:Person {id: row.s}) MATCH (b:Person {id: row.d}) CREATE (a)-[:FOLLOWS]->(b)",
        rows,
    )
    assert query(pm, "MATCH (:Person)-[r:FOLLOWS]->(:Person) RETURN count(r)") == [50]
    assert query(pm, "MATCH (a:Person {id: 'p0'})-[:FOLLOWS]->(b) RETURN [a.k, b.k]") == [[0, 1]]


def apoc_batch(
    start: int, end: int, write: str = "CREATE (start)-[r:NEXT_CHUNK]->(finish) SET r += row.properties"
) -> str:
    """A batch of relationships as `apoc.export.cypher` writes it: its rows written out, its key a name of spaces."""
    listed = ", ".join(
        f"{{start: {{_id: {it}}}, end: {{_id: {it + 1}}}, properties: {{}}}}" for it in range(start, end)
    )
    return (
        f"UNWIND [{listed}] AS row MATCH (start:`{IMPORT_LABEL}` {{`{IMPORT_ID}`: row.start._id}}) "
        f"MATCH (finish:`{IMPORT_LABEL}` {{`{IMPORT_ID}`: row.end._id}}) {write}"
    )


def test_a_batch_of_relationships_as_apoc_exports_it_finds_each_rows_nodes_through_the_index_on_a_key_of_spaces(
    pm: PersistenceManager, seeded: None
):
    read_through_index(pm)
    # Its reads, planned as a query: a write compiles to a procedure, which no EXPLAIN takes.
    explained = plan(pm, compiled(pm, apoc_batch(0, 20, "RETURN start, finish"), {}))
    print(f"apoc\n{explained}\n")
    assert probes_index(pm, explained), f"an APOC batch reads every node of the label for each row:\n{explained}"
    # Not written on Oracle, as in Drivine for TypeScript: a statement the connection runs there names no index yet
    # (liberation-data/drivine4j#638), and reads every node of the label for each end of each row: minutes for 10.
    if pm.type is DatabaseType.ORACLE:
        return
    query(pm, apoc_batch(0, 5))
    query(pm, apoc_batch(5, 10))
    assert query(pm, f"MATCH (:`{IMPORT_LABEL}`)-[r:NEXT_CHUNK]->(:`{IMPORT_LABEL}`) RETURN count(r)") == [10]
    assert query(
        pm, f"MATCH (a)-[:NEXT_CHUNK]->(b {{`{IMPORT_ID}`: 10}}) RETURN [a.`{IMPORT_ID}`, b.`{IMPORT_ID}`]"
    ) == [[9, 10]]


def test_compiled_knowing_the_indexes_oracle_finds_the_rows_it_finds_compiled_as_the_connection_compiles(
    pm: PersistenceManager, seeded: None
):
    # On Postgres the connection compiles as `compiled` does, and the tests above find its rows. On Oracle the
    # connection names no index (liberation-data/drivine4j#638), so the labelled index's test is run here: no row it
    # leaves out may be one the JSON comparison keeps, as 1 and 1.0, or a double and a number, are. The labels of these
    # nodes are named as indexed, whether or not an index is over them: the rows are the test's, not the plan.
    if pm.type is not DatabaseType.ORACLE:
        pytest.skip("Only Oracle compiles a statement otherwise knowing the indexes.")
    query(
        pm,
        "CREATE (:KeyOdd {id: 'true', k: 1}), (:KeyOdd {id: true, k: 2}), (:KeyOdd {id: '7', k: 3}), "
        "(:KeyOdd {id: 7, k: 4})",
    )
    query(
        pm,
        "CREATE (:KeyScaled {id: 0.25 * 10, k: 1}), (:KeyScaled {id: 0.25 * 4, k: 2}), "
        "(:KeyScaled {id: 2.0 * 2.0, k: 3}), (:KeyScaled {id: 1.0E-5, k: 4}), (:KeyScaled {id: 8.0, k: 5}), "
        "(:KeyScaled {id: 1.0e20, k: 6})",
    )
    indexed = {("Person", "id"), ("KeyOdd", "id"), ("KeyScaled", "id")}

    def run(compiler: OracleCypherCompiler, cypher: str, parameters: dict[str, Any]) -> list[str]:
        it = compiler.compile(cypher, parameters)
        return [str(row) for row in pm.query(QuerySpecification[Any](it.sql).bind(dict(it.parameters)))]

    def rows(*values: Any) -> dict[str, Any]:
        return {"rows": [{"s": it} for it in values]}

    by_row = "UNWIND $rows AS row MATCH (a:Person {id: row.s}) RETURN a.k ORDER BY a.k"
    # Each with whether the labelled test is written. Not where a row holds a null, at the key or as the map a key is
    # read of, nor where a written list holds a negative number, `-7` a negation and no literal: compared as values that
    # may hold a null, as drivine4j compares them, with no test beside them. Each has a twin without, that writes it.
    cases: list[tuple[str, dict[str, Any], bool]] = [
        (by_row, rows("p5", 7, 7.0, 8, True, [1, 2], None, "nobody", "7", ["a", "b"], [], [1], [[1, 2]], 1), False),
        (by_row, rows("p5", 7, 7.0, 8, True, [1, 2], "nobody", "7", ["a", "b"], [], [1], [[1, 2]], 1), True),
        (by_row, rows(0.1, 1.0e20, -7, -0.0, BIG, "", LONG), True),
        (by_row, rows(0.1, 1.0e20, -7, -0.0, BIG, 8.0, 8, 7, 7.0), True),
        (by_row, rows(0.1, 1.0e20, -7.0, 0.0, 8.0), True),
        (
            "UNWIND $ids AS id MATCH (a:Person {id: id}) RETURN a.k ORDER BY a.k",
            {"ids": ["p5", 7.0, 8, True, BIG, 0.1]},
            True,
        ),
        (
            "UNWIND ['', 'p5', 8, 7.0, 0.1, 1.0e20, -7, -0.0, 0] AS s MATCH (a:Person {id: s}) RETURN a.k ORDER BY a.k",
            {},
            False,
        ),
        (
            "UNWIND ['', 'p5', 8, 7.0, 0.1, 1.0e20, 0] AS s MATCH (a:Person {id: s}) RETURN a.k ORDER BY a.k",
            {},
            True,
        ),
        (
            "UNWIND $rows AS row MATCH (a:Person {id: row.a.b}) RETURN a.k ORDER BY a.k",
            {"rows": [{"a": {"b": "p5"}}, {"a": {"b": 8}}, {"a": None}, {}, {"a": {"c": [1]}}]},
            False,
        ),
        (
            "UNWIND $rows AS row MATCH (a:Person {id: row.a.b}) RETURN a.k ORDER BY a.k",
            {"rows": [{"a": {"b": "p5"}}, {"a": {"b": 8}}, {}, {"a": {"c": [1]}}]},
            True,
        ),
        (
            "UNWIND $rows AS row MATCH (a:KeyOdd {id: row.s}) RETURN a.k ORDER BY a.k",
            rows(True, "true", 7, "7", 7.0),
            True,
        ),
        (
            "UNWIND $rows AS row MATCH (a:KeyScaled {id: row.s}) RETURN a.k ORDER BY a.k",
            rows(2.5, 1, 4, 0.00001, 1.0, 4.0, 8, 8.0, 1.0e20, 10**20),
            True,
        ),
        ("MATCH (a:Person {id: $s}) RETURN a.k", {"s": "p42"}, True),
    ]
    for cypher, parameters, writes in cases:
        plain, knowing = OracleCypherCompiler(GRAPH), OracleCypherCompiler(GRAPH, indexed_properties=indexed)
        differs = plain.compile(cypher, parameters).sql != knowing.compile(cypher, parameters).sql
        assert differs is writes, f"{cypher} {parameters}: the labelled test {'not ' if writes else ''}written"
        bare = run(OracleCypherCompiler(GRAPH), cypher, parameters)
        assert bare, cypher
        assert run(OracleCypherCompiler(GRAPH, indexed_properties=indexed), cypher, parameters) == bare, (
            f"{cypher} {parameters}"
        )


def test_a_parameters_text_finds_its_node_through_the_index(pm: PersistenceManager, seeded: None):
    plans_checked(pm)
    explained = plan(pm, compiled(pm, "MATCH (a:Person {id: $s}) RETURN a.k", {"s": "p42"}))
    print(f"parameter\n{explained}\n")
    assert probes_index(pm, explained), f"a parameter reads every node of the label:\n{explained}"
    assert query(pm, "MATCH (a:Person {id: $s}) RETURN a.k", {"s": "p42"}) == [42]
