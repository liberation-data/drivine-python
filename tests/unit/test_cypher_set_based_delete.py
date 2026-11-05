"""
A `DELETE` compiles to one statement for each table it deletes from, over all its rows at once, and not to a loop of a
statement for each row (#410, liberation-data/drivine4j#587): `MATCH (n) DETACH DELETE n` over 1.1M nodes ran 1.1M
single-row deletes, 17 minutes on Postgres. One that ends the statement and deletes from one table reads its ids from
the match itself, so the match's rows are not gathered into a list first. The rows are pinned by
tests/integration/test_cypher_set_based_delete.py. drivine4j's `SetBasedDeleteTest`, Drivine for TypeScript's
`SetBasedDelete.spec.ts`.
"""

import re

import pytest

from drivine import DrivineError
from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler
from drivine.cypher.sql.sql_cypher_compiler import SqlCypherCompiler
from drivine.cypher.sql.sqlite_cypher_compiler import SqliteCypherCompiler

COMPILERS: dict[str, type[SqlCypherCompiler]] = {
    "postgres": PostgresCypherCompiler,
    "oracle": OracleCypherCompiler,
    "sqlite": SqliteCypherCompiler,
}

GATHERED = re.compile(r'INTO cy_rows\b|"variable":"cy_rows"')
"""The rows gathered into the procedure's list of them: the step that sets it, in PL/pgSQL and PL/SQL, or in SQLite's
steps."""

LOOP = re.compile(r'FOR cy_record IN|"op":"loop"')
"""A loop over the rows, in PL/pgSQL and PL/SQL, or in SQLite's steps."""


def compiled(store: str, cypher: str) -> str:
    """The statement as the store's compiler makes it: on Postgres, the function made before it too."""
    it = COMPILERS[store]("g").compile(cypher)
    return "\n".join([*([it.prelude] if it.prelude else []), it.sql])


def shape(store: str, cypher: str, table: str) -> tuple[int, bool, bool]:
    """How often the table's rows are deleted, whether the statement loops over rows, and whether it gathers them."""
    sql = compiled(store, cypher)
    return (
        len(re.findall(rf"DELETE FROM {table}\b", sql)),
        LOOP.search(sql) is not None,
        GATHERED.search(sql) is not None,
    )


@pytest.mark.parametrize("store", COMPILERS)
@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (n) DETACH DELETE n",
        "MATCH (n:X)-[:R]->() WHERE n.k > 1 DETACH DELETE n",
        "MATCH (n:X) WITH n ORDER BY n.k LIMIT 2 DETACH DELETE n",
    ],
)
def test_a_detach_delete_that_ends_the_statement_deletes_its_matchs_nodes_in_one_statement_gathering_no_rows(
    store: str, cypher: str
) -> None:
    assert shape(store, cypher, "g_node") == (1, False, False)


@pytest.mark.parametrize("store", COMPILERS)
@pytest.mark.parametrize("cypher", ["MATCH ()-[r:R]->() DELETE r", "MATCH ()-[r:R]->() DETACH DELETE r"])
def test_a_delete_of_relationships_that_ends_the_statement_deletes_them_in_one_statement_gathering_no_rows(
    store: str, cypher: str
) -> None:
    assert shape(store, cypher, "g_edge") == (1, False, False)


@pytest.mark.parametrize("store", COMPILERS)
def test_a_delete_after_a_with_that_binds_a_value_gathers_the_rows_and_deletes_them_in_one_statement(
    store: str,
) -> None:
    assert shape(store, "MATCH (n) WITH n, n.k AS k DETACH DELETE n", "g_node") == (1, False, True)


@pytest.mark.parametrize("store", COMPILERS)
def test_a_delete_of_nodes_without_detach_gathers_its_rows_once_so_its_test_reads_the_nodes_it_deletes(
    store: str,
) -> None:
    # Read from the match twice, a match that makes a value anew for each row would test some nodes and delete others.
    sql = compiled(store, "MATCH (n) WHERE randomUUID() < '8' DELETE n")
    assert (len(GATHERED.findall(sql)), len(re.findall(r"DELETE FROM g_node\b", sql)), LOOP.search(sql)) == (1, 1, None)


def test_on_oracle_a_delete_that_reads_its_match_reads_the_key_with_plus_0_and_plans_no_adaptive_plan() -> None:
    assert re.search(
        r"DELETE FROM g_node WHERE nid IN \(SELECT /\*\+ NO_ADAPTIVE_PLAN \*/ n\d+\.nid \+ 0 AS id",
        compiled("oracle", "MATCH (g:Gone) DETACH DELETE g"),
    )
    assert re.search(
        r"DELETE FROM g_edge WHERE eid IN "
        r"\(SELECT /\*\+ NO_JSON_TABLE_TRANSFORM NO_ADAPTIVE_PLAN \*/ e\d+\.eid \+ 0 AS id",
        compiled("oracle", "MATCH ()-[r:R]->() DELETE r"),
    )
    for store in ("postgres", "sqlite"):
        assert re.search(
            r"DELETE FROM g_node WHERE nid IN \(SELECT n\d+\.nid AS id",
            compiled(store, "MATCH (g:Gone) DETACH DELETE g"),
        )


@pytest.mark.parametrize("store", COMPILERS)
def test_a_delete_that_ends_the_statement_refuses_what_a_delete_over_the_rows_refuses(store: str) -> None:
    with pytest.raises(
        DrivineError,
        match=r"^m is not defined: a variable is bound by a pattern, a WITH or an UNWIND before it is used\.$",
    ):
        compiled(store, "MATCH (n) DETACH DELETE m")
    with pytest.raises(
        DrivineError, match=r"^A DELETE of anything but a variable is Cypher, and is not compiled to SQL"
    ):
        compiled(store, "MATCH (n) DETACH DELETE n.x")


@pytest.mark.parametrize("store", COMPILERS)
@pytest.mark.parametrize(
    ("cypher", "tables"),
    [
        ("MATCH (n) DETACH DELETE n RETURN count(*)", (0, 1)),
        ("MATCH (n) DELETE n", (0, 1)),
        ("MATCH (a)-[r]->(b) DELETE r, a", (1, 1)),
        ("MATCH (a)-[r]->(b) DETACH DELETE r, a, b", (1, 1)),
        ("MATCH (a)-[r]->(b) DETACH DELETE a DELETE r", (1, 1)),
        ("MATCH (n) WITH n, n.k AS k DETACH DELETE n RETURN sum(k)", (0, 1)),
    ],
)
def test_a_delete_with_rows_after_it_or_of_nodes_and_relationships_deletes_each_table_in_one_statement(
    store: str, cypher: str, tables: tuple[int, int]
) -> None:
    assert tuple(shape(store, cypher, table)[0] for table in ("g_edge", "g_node")) == tables
    assert not shape(store, cypher, "g_node")[1]


@pytest.mark.parametrize("store", COMPILERS)
@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (n) WITH collect(n) AS ns FOREACH (n IN ns | DETACH DELETE n)",
        "MATCH (n) CALL (n) { DETACH DELETE n } RETURN count(*)",
    ],
)
def test_a_delete_in_a_foreach_or_a_writing_call_deletes_in_one_statement_for_each_pass_each_pass_a_row(
    store: str, cypher: str
) -> None:
    assert shape(store, cypher, "g_node")[0] == 1
