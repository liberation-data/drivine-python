# Written by scripts/unasync.py from tests/integration/test_cypher_set_based_delete.py:
# change that, and run the script again.
"""
A `DELETE` deletes the nodes and relationships of all its rows at once, a statement for each table, and one that ends
the statement reads its ids from the match itself (#410, liberation-data/drivine4j#587): it deletes what Neo4j does, on
Neo4j and on the SQL stores Cypher is compiled for. The rows are Neo4j's. drivine4j's `CypherWriteCrossEngineTest`, its
tests for #587, and Drivine for TypeScript's `CypherSetBasedDelete.spec.ts`. The shapes the compiler gives are pinned by
tests/unit/test_cypher_set_based_delete.py.
"""

from typing import Any

import pytest

from drivine.query.statement import cypher_statement
from drivine.sync import DatabaseType, PersistenceManager, QuerySpecification, transaction


def query(pm: PersistenceManager, cypher: str) -> list[Any]:
    return pm.query(QuerySpecification[Any](cypher_statement(cypher)))


def ring(pm: PersistenceManager) -> None:
    """Four nodes, `k` 1 to 4: 1, 2 and 3 in a ring of relationships `i` 1 to 3, 1 with a loop `i` 4 too, and 4 with
    no relationship."""
    query(
        pm,
        "CREATE (a:CyDelete {k: 1}), (b:CyDelete {k: 2}), (c:CyDelete {k: 3}), (:CyDelete {k: 4}), "
        "(a)-[:R {i: 1}]->(b), (b)-[:R {i: 2}]->(c), (c)-[:R {i: 3}]->(a), (a)-[:R {i: 4}]->(a)",
    )


def ks(pm: PersistenceManager) -> list[Any]:
    return query(pm, "MATCH (n:CyDelete) RETURN n.k ORDER BY n.k")


def rs(pm: PersistenceManager) -> list[Any]:
    return query(pm, "MATCH ()-[r]->() RETURN r.i ORDER BY r.i")


@pytest.fixture
def graph(cypher_pm: PersistenceManager) -> PersistenceManager:
    """The store's graph, cleared: a statement here reads the whole of it, as `MATCH (n) DETACH DELETE n` does."""
    if cypher_pm.type is DatabaseType.NEO4J:
        cypher_pm.execute(QuerySpecification("MATCH (n) DETACH DELETE n"))
    else:
        # A node's relationships go with it: the relationship table cascades.
        cypher_pm.execute(QuerySpecification("DELETE FROM drivine_node"))
    return cypher_pm


def test_a_detach_delete_deletes_each_node_once_however_many_rows_match_it_with_its_relationships(
    graph: PersistenceManager,
):
    ring(graph)
    query(graph, "MATCH (n:CyDelete)-[]-() DETACH DELETE n, n")
    assert ks(graph) == [4]
    assert rs(graph) == []
    # A node read by its label alone: on Oracle the delete's subquery over the node table found none.
    query(graph, "MATCH (n:CyDelete) DETACH DELETE n")
    assert ks(graph) == []
    ring(graph)
    query(graph, "MATCH (n) DETACH DELETE n")
    assert [
        query(graph, "MATCH (n) RETURN count(n)"),
        query(graph, "MATCH ()-[r]->() RETURN count(r)"),
    ] == [
        [0],
        [0],
    ]


def test_the_rows_after_a_detach_delete_are_those_it_matched_with_what_was_read_before_it(
    graph: PersistenceManager,
):
    ring(graph)
    assert query(graph, "MATCH (n:CyDelete)-[r]->() WITH n, n.k AS k DETACH DELETE n RETURN k ORDER BY k") == [
        1,
        1,
        2,
        3,
    ]
    assert ks(graph) == [4]
    assert query(graph, "MATCH (n:CyDelete) DETACH DELETE n RETURN count(*)") == [1]
    assert ks(graph) == []


def test_a_detach_delete_after_a_with_that_orders_and_limits_deletes_what_it_keeps_and_a_null_is_no_node(
    graph: PersistenceManager,
):
    ring(graph)
    query(graph, "MATCH (n:CyDelete) WITH n ORDER BY n.k DESC LIMIT 2 DETACH DELETE n")
    assert ks(graph) == [1, 2]
    assert rs(graph) == [1, 4]
    query(graph, "MATCH (n:CyDelete {k: 2}) OPTIONAL MATCH (n)-[:NONE]->(m) DETACH DELETE m")
    assert ks(graph) == [1, 2]
    query(graph, "OPTIONAL MATCH ()-[r:NONE]->() DELETE r")
    assert rs(graph) == [1, 4]


def test_a_detach_delete_that_reads_its_ids_from_a_match_of_two_nodes_deletes_only_the_node_it_names(
    graph: PersistenceManager,
):
    # The delete's subquery reads the node table twice, a property of each two ways (liberation-data/drivine4j#415).
    query(graph, "CREATE (:CyDelete:N1 {k: 1, n: 1}), (:CyDelete:N2 {k: 2, n: 2})")
    query(graph, "MATCH (a:N1), (b:N2) WHERE a.n + b.n > 0 AND a.n - b.n < 5 DETACH DELETE a")
    assert ks(graph) == [2]
    query(graph, "CREATE (:CyDelete:N1 {k: 3, n: 1})-[:R {i: 1}]->(:CyDelete:N2 {k: 4, n: 2})")
    query(graph, "MATCH (a:N1)-[r]->(b:N2) WHERE a.n + b.n > 0 AND a.n - b.n < 5 DELETE r")
    assert rs(graph) == []
    assert ks(graph) == [2, 3, 4]


def test_a_delete_of_relationships_deletes_each_once_one_found_both_ways_round_included(
    graph: PersistenceManager,
):
    ring(graph)
    assert query(graph, "MATCH (:CyDelete)-[r]-(:CyDelete) WHERE r.i <> 2 DELETE r RETURN count(*)") == [5]
    assert rs(graph) == [2]
    query(graph, "MATCH ()-[r]->() DELETE r")
    assert rs(graph) == []
    assert ks(graph) == [1, 2, 3, 4]


def test_a_delete_of_a_node_that_keeps_a_relationship_fails_deleting_nothing_and_deletes_where_rows_delete_all(
    graph: PersistenceManager,
):
    ring(graph)
    # Node 1 keeps the relationship from 3.
    with pytest.raises(Exception, match="still has relationships"):  # noqa: PT011 - each store says it in its own words
        query(graph, "MATCH (a:CyDelete {k: 1})-[r]->() DELETE r, a")
    assert ks(graph) == [1, 2, 3, 4]
    assert rs(graph) == [1, 2, 3, 4]
    # Each row deletes one of node 1's relationships, and node 1 is deleted once.
    query(graph, "MATCH (a:CyDelete {k: 1})-[r]-() DELETE r, a")
    assert ks(graph) == [2, 3, 4]
    assert rs(graph) == [2]
    # A node on each row of a cross product, deleted once.
    query(graph, "MATCH (a:CyDelete {k: 4}), (b:CyDelete) DELETE a")
    assert ks(graph) == [2, 3]


def test_a_plain_delete_of_a_node_with_relationships_fails_with_what_to_do_on_a_sql_store(
    graph: PersistenceManager,
):
    ring(graph)
    if graph.type is DatabaseType.NEO4J:
        expected = r"still has relationships"
    else:
        # Oracle's error opens with its code, ORA-20000, as a raised error does there.
        expected = r"A node is deleted that still has relationships: delete them first, or DETACH DELETE it\."
    with pytest.raises(Exception, match=expected):  # noqa: PT011 - Neo4j's error is its driver's
        query(graph, "MATCH (n:CyDelete {k: 2}) DELETE n")
    assert ks(graph) == [1, 2, 3, 4]


def test_a_detach_delete_beside_a_delete_in_a_call_and_in_a_foreach_deletes_what_neo4j_does(
    graph: PersistenceManager,
):
    ring(graph)
    # The relationship from 1 to 2 goes with node 1, and the DELETE after it finds it gone.
    query(graph, "MATCH (a:CyDelete {k: 1})-[r]->(:CyDelete {k: 2}) DETACH DELETE a DELETE r")
    assert ks(graph) == [2, 3, 4]
    assert rs(graph) == [2]
    # In a CALL { … } and a FOREACH, each row is written alone.
    assert query(graph, "MATCH (n:CyDelete) WHERE n.k > 2 CALL (n) { DETACH DELETE n } RETURN count(*)") == [2]
    assert ks(graph) == [2]
    query(graph, "CREATE (:CyDelete {k: 5})-[:R {i: 5}]->(:CyDelete {k: 6})")
    query(graph, "MATCH (n:CyDelete) WITH collect(n) AS ns FOREACH (n IN ns | DETACH DELETE n)")
    assert ks(graph) == []
    assert rs(graph) == []


def test_a_delete_in_a_transaction_that_rolls_back_is_undone_and_read_within_it_as_it_left_the_graph(
    graph: PersistenceManager,
):
    # Drivine Studio runs Cypher in a transaction of its own: the one statement a DELETE is goes with the rest of it.
    ring(graph)
    with transaction(rollback=True):
        query(graph, "MATCH (n:CyDelete) WHERE n.k > 2 DETACH DELETE n")
        query(graph, "MATCH (:CyDelete)-[r {i: 1}]->() DELETE r")
        seen = [ks(graph), query(graph, "MATCH (:CyDelete)-[r]->() RETURN r.i ORDER BY r.i")]
    assert seen == [[1, 2], [4]]
    assert ks(graph) == [1, 2, 3, 4]
    assert rs(graph) == [1, 2, 3, 4]
    # Planned again after the rollback, Oracle crashed the session on these deletes where their subquery read the index
    # over labels (liberation-data/drivine#612, liberation-data/drivine4j#595).
    query(graph, "MATCH (a:CyDelete) OPTIONAL MATCH (a)-[r:R]->(:CyDelete {k: 2}) DELETE r")
    assert rs(graph) == [2, 3, 4]
    query(graph, "MATCH (a:CyDelete) OPTIONAL MATCH (a)-[:R]->(m:CyDelete {k: 3}) DETACH DELETE m")
    assert ks(graph) == [1, 2, 4]
    assert rs(graph) == [4]


def test_a_delete_of_an_optional_matchs_variable_deletes_where_it_is_not_null_and_a_match_of_no_rows_nothing(
    graph: PersistenceManager,
):
    ring(graph)
    query(graph, "MATCH (a:CyDelete) OPTIONAL MATCH (a)-[r:R]->(:CyDelete {k: 2}) DELETE r")
    assert rs(graph) == [2, 3, 4]
    query(graph, "MATCH (a:CyDelete) OPTIONAL MATCH (a)-[:R]->(m:CyDelete {k: 3}) DETACH DELETE m")
    assert ks(graph) == [1, 2, 4]
    assert rs(graph) == [4]
    # Node 4 has no relationship, so its row's is null.
    query(graph, "MATCH (a:CyDelete) WHERE a.k IN [1, 4] OPTIONAL MATCH (a)-[r]-() DELETE r, a")
    assert ks(graph) == [2]
    assert rs(graph) == []
    query(graph, "MATCH (n:CyDeleteNone) DELETE n")
    assert query(graph, "MATCH (n:CyDeleteNone) DETACH DELETE n RETURN count(*)") == [0]
    assert ks(graph) == [2]


def test_the_rows_a_delete_leaves_are_written_by_the_clauses_after_it_and_a_foreach_deletes_each_item_alone(
    graph: PersistenceManager,
):
    ring(graph)
    query(graph, "MATCH (n:CyDelete) WHERE n.k > 2 DETACH DELETE n WITH count(*) AS c CREATE (:CyDelete {k: 10 + c})")
    assert ks(graph) == [1, 2, 12]
    assert rs(graph) == [1, 4]
    query(
        graph, "MATCH (a:CyDelete {k: 1})-[r]-() WITH a, collect(r) AS rs FOREACH (r IN rs | DELETE r) WITH a DELETE a"
    )
    assert ks(graph) == [2, 12]
    assert rs(graph) == []


def test_a_delete_that_ends_the_statement_after_a_write_deletes_what_the_rows_the_write_left_hold(
    graph: PersistenceManager,
):
    # The delete reads its ids from the rows the write put back, each node or relationship joined from its table.
    ring(graph)
    query(graph, "MATCH (n:CyDelete) WHERE n.k > 2 SET n.gone = true DETACH DELETE n")
    assert ks(graph) == [1, 2]
    assert rs(graph) == [1, 4]
    query(graph, "MATCH (a:CyDelete {k: 1}) CREATE (a)-[:R {i: 9}]->(c:CyDelete {k: 9}) DETACH DELETE c")
    assert ks(graph) == [1, 2]
    assert rs(graph) == [1, 4]
    query(graph, "MATCH ()-[r]->() SET r.x = 1 DELETE r")
    assert rs(graph) == []
    assert ks(graph) == [1, 2]
