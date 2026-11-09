# Written by scripts/unasync.py from tests/integration/sqlite/test_sqlite_ids_not_reused.py:
# change that, and run the script again.
"""
A SQLite graph gives a deleted node's or relationship's id to nothing after it, as Oracle and Postgres give none again
(drivine4j#713): its keys are `AUTOINCREMENT`. A file whose tables an earlier Drivine made, without it, is refused.
drivine4j's SqliteIdsNotReusedTest, ported.
"""

import sqlite3
from typing import Any

import pytest

from drivine.backend.sqlite.sqlite_graph import DATABASE_FILE, SqliteGraph
from drivine.query.statement import cypher_statement
from drivine.sync import (
    DatabaseRegistry,
    DatabaseType,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    ReusedIdsError,
    SchemaCatalog,
    SchemaManager,
    SchemaOptions,
    range_index,
    transaction,
    transactional,
)
from drivine.sync.connection.sqlite.sqlite_connection_provider import SqliteConnectionProvider
from drivine.sync.manager.graph_object_manager_factory import GraphObjectManagerFactory


def cypher(pm: PersistenceManager, statement: str) -> list[Any]:
    return pm.query(QuerySpecification[Any](cypher_statement(statement)))


def one(pm: PersistenceManager, statement: str) -> Any:
    [found] = cypher(pm, statement)
    return found


def refusal(graph: str, path: str) -> str:
    return (
        f"The SQLite graph '{graph}' in {path} was made by an earlier Drivine, whose node and relationship ids could "
        "be reused after a delete. Make a new file and save the graph into it again."
    )


def test_the_tables_are_made_with_autoincrement_keys(pm: PersistenceManager):
    made = pm.query(
        QuerySpecification[Any](
            "SELECT sql FROM sqlite_master WHERE name IN ('drivine_node', 'drivine_edge') ORDER BY name"
        )
    )
    assert len(made) == 2
    assert "eid   INTEGER PRIMARY KEY AUTOINCREMENT" in made[0]["sql"]
    assert "nid    INTEGER PRIMARY KEY AUTOINCREMENT" in made[1]["sql"]


def test_a_node_deleted_the_one_with_the_greatest_id_gives_its_id_to_no_node_made_after_it(
    pm: PersistenceManager,
):
    cypher(pm, "CREATE (:Kept)")
    deleted = one(pm, "CREATE (n:Gone) RETURN elementId(n)")
    cypher(pm, "MATCH (n:Gone) DELETE n")
    made = one(pm, "CREATE (n:New) RETURN elementId(n)")
    assert deleted != made
    assert one(pm, f"MATCH (n) WHERE elementId(n) = '{deleted}' RETURN count(n)") == 0


def test_a_relationship_deleted_gives_its_id_to_no_relationship_made_after_it(pm: PersistenceManager):
    cypher(pm, "CREATE (:A)-[:Kept]->(:B)")
    deleted = one(pm, "MATCH (a:A), (b:B) CREATE (a)-[r:Gone]->(b) RETURN elementId(r)")
    cypher(pm, "MATCH ()-[r:Gone]->() DELETE r")
    made = one(pm, "MATCH (a:A), (b:B) CREATE (a)-[r:New]->(b) RETURN elementId(r)")
    assert deleted != made


def test_within_one_statement_a_node_deleted_gives_its_id_to_none_the_statement_makes_after(
    pm: PersistenceManager,
):
    cypher(pm, "CREATE (:Kept)")
    row = one(
        pm, "CREATE (n:Gone) WITH elementId(n) AS deleted, n DELETE n CREATE (m:New) RETURN [deleted, elementId(m)]"
    )
    assert row[0] != row[1]
    # TCK Merge1 #14: a MERGE after a DELETE in the same statement makes a node of its own.
    cypher(pm, "MATCH (n) DETACH DELETE n")
    cypher(pm, "CREATE (:Kept), (:Gone)")
    merged = one(
        pm, "MATCH (n:Gone) WITH n, elementId(n) AS deleted DELETE n MERGE (m:Gone) RETURN [deleted, elementId(m)]"
    )
    assert merged[0] != merged[1]


def test_inside_one_transaction_a_node_deleted_gives_its_id_to_none_made_after_it(path: str):
    provider = SqliteConnectionProvider("sqlite", path)
    try:
        connection = provider.connect()
        try:
            connection.query(QuerySpecification[Any](cypher_statement("CREATE (:Kept)")))
            connection.start_transaction()
            try:
                [deleted] = connection.query(
                    QuerySpecification[Any](cypher_statement("CREATE (n:Gone) RETURN elementId(n)"))
                )
                connection.query(QuerySpecification[Any](cypher_statement("MATCH (n:Gone) DELETE n")))
                [made] = connection.query(
                    QuerySpecification[Any](cypher_statement("CREATE (n:New) RETURN elementId(n)"))
                )
                assert deleted != made
            finally:
                connection.rollback_transaction()
        finally:
            connection.release()
    finally:
        provider.end()


def test_clearing_the_graph_keeps_its_ids_counting_from_where_they_were_as_on_postgres(pm: PersistenceManager):
    before = int(str(one(pm, "CREATE (n:Gone) RETURN elementId(n)")))
    # As the tests' cleanup clears it: the rows go, and SQLite's counter of the table stays.
    pm.execute(QuerySpecification("DELETE FROM drivine_node"))
    after = int(str(one(pm, "CREATE (n:New) RETURN elementId(n)")))
    assert after > before, f"{after} after {before}"


# ----- A file an earlier Drivine made -----


def earlier_file(path: str, graph: str, keep_edges: bool = False) -> str:
    """A file with the graph's tables made as an earlier Drivine made them: keys without AUTOINCREMENT."""
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            f"CREATE TABLE {graph}_node (nid INTEGER PRIMARY KEY, labels TEXT NOT NULL, props TEXT NOT NULL, "
            "stamp TEXT)"
        )
        key = "INTEGER PRIMARY KEY AUTOINCREMENT" if keep_edges else "INTEGER PRIMARY KEY"
        connection.execute(
            f"CREATE TABLE {graph}_edge (eid {key}, src INTEGER NOT NULL, dst INTEGER NOT NULL, type TEXT NOT NULL, "
            "props TEXT NOT NULL DEFAULT '{}')"
        )
        connection.execute(f'INSERT INTO {graph}_node (labels, props) VALUES (\'["Person"]\', \'{{"name": "Ada"}}\')')
        connection.commit()
    finally:
        connection.close()
    return path


def count(path: str, statement: str) -> int:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(statement).fetchone()[0]
    finally:
        connection.close()


def test_a_file_whose_graph_an_earlier_drivine_made_is_refused_on_every_connection_and_nothing_is_made(
    path: str,
):
    earlier_file(path, "drivine")
    provider = SqliteConnectionProvider("earlier", path)
    try:
        for _ in range(2):
            with pytest.raises(ReusedIdsError) as refused:
                provider.connect()
            assert refused.value.graph == "drivine"
            assert refused.value.path == path
            assert str(refused.value) == refusal("drivine", path)
    finally:
        provider.end()
    made = "SELECT count(*) FROM sqlite_master WHERE name IN ('drivine_schema', 'drivine_edge_src', 'sqlite_sequence')"
    assert count(path, made) == 0


def test_a_file_an_earlier_drivine_made_is_refused_through_a_registrys_persistence_manager(path: str):
    earlier_file(path, "drivine")
    registry = DatabaseRegistry()
    registry.builder().database_type(DatabaseType.SQLITE).database_name(path).register("earlier")
    try:
        pm = PersistenceManagerFactory(registry).get("earlier")
        for _ in range(2):
            with pytest.raises(ReusedIdsError, match="earlier Drivine"):
                pm.query(QuerySpecification[Any]("SELECT 1"))
    finally:
        registry.close()


def test_schema_applied_to_a_file_an_earlier_drivine_made_raises_the_refusal_itself(path: str):
    # Not wrapped in a failure of the owners' schema, so code that looks for the class finds it (drivine4j#713).
    earlier_file(path, "drivine")
    registry = DatabaseRegistry()
    registry.builder().database_type(DatabaseType.SQLITE).database_name(path).register("earlier")
    try:
        catalogs = [
            SchemaCatalog.of(range_index("Person", "name")).named("one").with_version("1").for_database("earlier"),
            SchemaCatalog.of(range_index("Person", "age")).named("two").for_database("earlier"),
        ]
        factory = GraphObjectManagerFactory(PersistenceManagerFactory(registry))
        manager = SchemaManager(registry, factory, SchemaOptions(catalogs))
        for apply in (manager.enforce, manager.recreate_all, lambda: manager.ensure("one", range_index("Person", "x"))):
            with pytest.raises(ReusedIdsError) as refused:
                apply()
            assert type(refused.value) is ReusedIdsError
    finally:
        registry.close()


def test_a_first_connection_taken_inside_a_transaction_raises_the_refusal_itself(path: str):
    # Not wrapped as the transaction's own failure, in a block or a transactional call alike.
    earlier_file(path, "drivine")
    registry = DatabaseRegistry()
    registry.builder().database_type(DatabaseType.SQLITE).database_name(path).register("earlier")
    try:
        pm = PersistenceManagerFactory(registry).get("earlier")

        @transactional()
        def read() -> list[Any]:
            return pm.query(QuerySpecification[Any]("SELECT 1"))

        with pytest.raises(ReusedIdsError) as refused:
            with transaction():
                pm.query(QuerySpecification[Any]("SELECT 1"))
        assert type(refused.value) is ReusedIdsError
        with pytest.raises(ReusedIdsError) as refused:
            read()
        assert type(refused.value) is ReusedIdsError
    finally:
        registry.close()


def test_a_refused_connection_goes_back_to_its_pool_and_is_refused_again_on_it(path: str):
    earlier_file(path, "drivine")
    provider = SqliteConnectionProvider("earlier", path)
    try:
        with pytest.raises(ReusedIdsError):
            provider.connect()
        [kept] = provider._idle
        with pytest.raises(ReusedIdsError):
            provider.connect()
        assert provider._idle == [kept]
    finally:
        provider.end()


def test_a_file_whose_table_of_nodes_alone_is_old_is_refused_and_another_graph_in_it_is_not(path: str):
    earlier_file(path, "drivine", keep_edges=True)
    refused = SqliteConnectionProvider("earlier", path)
    try:
        with pytest.raises(ReusedIdsError):
            refused.connect()
    finally:
        refused.end()
    # Another graph in the same file is made, and works.
    other = SqliteConnectionProvider("earlier", path, graph="fresh")
    try:
        connection = other.connect()
        try:
            connection.query(QuerySpecification[Any](cypher_statement("CREATE (:Person {name: 'Ada'})")))
        finally:
            connection.release()
    finally:
        other.end()


def test_the_graphs_look_refuses_a_store_an_earlier_drivine_made_before_anything_is_made(path: str):
    earlier_file(path, "G")
    graph = SqliteGraph("G")
    sent: list[str] = []
    connection = sqlite3.connect(path)

    def run(statement: str) -> Any:
        sent.append(statement)
        row = connection.execute(statement).fetchone()
        return None if row is None else row[0]

    try:
        with pytest.raises(ReusedIdsError) as refused:
            graph.ensure(run)
    finally:
        connection.close()
    # The look, and the file named, as the graph was named: nothing else.
    assert sent == [graph.made, DATABASE_FILE]
    assert str(refused.value) == refusal("G", path)


def test_a_graph_whose_name_holds_autoincrement_an_earlier_drivine_made_is_refused(path: str):
    # Its table's name is in the statement that made it, and is not the key's AUTOINCREMENT.
    earlier_file(path, "autoincrement")
    connection = sqlite3.connect(path)

    def run(statement: str) -> Any:
        row = connection.execute(statement).fetchone()
        return None if row is None else row[0]

    try:
        with pytest.raises(ReusedIdsError):
            SqliteGraph("autoincrement").ensure(run)
    finally:
        connection.close()


def test_a_store_in_memory_an_earlier_drivine_made_is_refused_naming_no_file():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE drivine_node (nid INTEGER PRIMARY KEY, labels TEXT, props TEXT, stamp TEXT)")

        def run(statement: str) -> Any:
            row = connection.execute(statement).fetchone()
            return None if row is None else row[0]

        with pytest.raises(ReusedIdsError) as refused:
            SqliteGraph().ensure(run)
    finally:
        connection.close()
    assert refused.value.path is None
    assert str(refused.value) == refusal("drivine", "an in-memory database")


def test_a_fresh_file_is_made_with_autoincrement_keys_and_works(path: str):
    provider = SqliteConnectionProvider("fresh", path)
    try:
        connection = provider.connect()
        try:
            connection.query(
                QuerySpecification[Any](
                    cypher_statement("CREATE (:Person {name: 'Ada'})-[:KNOWS]->(:Person {name: 'Bob'})")
                )
            )
            assert connection.query(QuerySpecification[Any](cypher_statement("MATCH (n:Person) RETURN count(n)"))) == [
                2
            ]
        finally:
            connection.release()
        # Connected again: looked for, found, not refused.
        (provider.connect()).release()
    finally:
        provider.end()
    keyed = (
        "SELECT count(*) FROM sqlite_master WHERE name IN ('drivine_node', 'drivine_edge') "
        "AND sql LIKE '%AUTOINCREMENT%'"
    )
    assert count(path, keyed) == 2
    connection = sqlite3.connect(path)
    try:
        assert [it[0] for it in connection.execute("SELECT name FROM sqlite_sequence ORDER BY name")] == [
            "drivine_edge",
            "drivine_node",
        ]
    finally:
        connection.close()
