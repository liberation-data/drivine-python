"""
A SQLite graph gives a deleted node's or relationship's id to nothing after it, as Oracle and Postgres give none again
(drivine4j#713): its keys are `AUTOINCREMENT`. A file whose tables an earlier Drivine made, without it, is refused.
drivine4j's SqliteIdsNotReusedTest, ported.
"""

import asyncio
from typing import Any

import apsw
import pytest

from drivine import (
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
from drivine.backend.sqlite.sqlite_graph import DATABASE_FILE, SqliteGraph
from drivine.connection.sqlite.sqlite_connection_provider import SqliteConnectionProvider
from drivine.manager.graph_object_manager_factory import GraphObjectManagerFactory
from drivine.query.statement import cypher_statement


async def cypher(pm: PersistenceManager, statement: str) -> list[Any]:
    return await pm.query(QuerySpecification[Any](cypher_statement(statement)))


async def one(pm: PersistenceManager, statement: str) -> Any:
    [found] = await cypher(pm, statement)
    return found


def refusal(graph: str, path: str) -> str:
    return (
        f"The SQLite graph '{graph}' in {path} was made by an earlier Drivine, whose node and relationship ids could "
        "be reused after a delete. Make a new file and save the graph into it again."
    )


async def test_the_tables_are_made_with_autoincrement_keys(pm: PersistenceManager):
    made = await pm.query(
        QuerySpecification[Any](
            "SELECT sql FROM sqlite_master WHERE name IN ('drivine_node', 'drivine_edge') ORDER BY name"
        )
    )
    assert len(made) == 2
    assert "eid   INTEGER PRIMARY KEY AUTOINCREMENT" in made[0]["sql"]
    assert "nid    INTEGER PRIMARY KEY AUTOINCREMENT" in made[1]["sql"]


async def test_a_node_deleted_the_one_with_the_greatest_id_gives_its_id_to_no_node_made_after_it(
    pm: PersistenceManager,
):
    await cypher(pm, "CREATE (:Kept)")
    deleted = await one(pm, "CREATE (n:Gone) RETURN elementId(n)")
    await cypher(pm, "MATCH (n:Gone) DELETE n")
    made = await one(pm, "CREATE (n:New) RETURN elementId(n)")
    assert deleted != made
    assert await one(pm, f"MATCH (n) WHERE elementId(n) = '{deleted}' RETURN count(n)") == 0


async def test_a_relationship_deleted_gives_its_id_to_no_relationship_made_after_it(pm: PersistenceManager):
    await cypher(pm, "CREATE (:A)-[:Kept]->(:B)")
    deleted = await one(pm, "MATCH (a:A), (b:B) CREATE (a)-[r:Gone]->(b) RETURN elementId(r)")
    await cypher(pm, "MATCH ()-[r:Gone]->() DELETE r")
    made = await one(pm, "MATCH (a:A), (b:B) CREATE (a)-[r:New]->(b) RETURN elementId(r)")
    assert deleted != made


async def test_within_one_statement_a_node_deleted_gives_its_id_to_none_the_statement_makes_after(
    pm: PersistenceManager,
):
    await cypher(pm, "CREATE (:Kept)")
    row = await one(
        pm, "CREATE (n:Gone) WITH elementId(n) AS deleted, n DELETE n CREATE (m:New) RETURN [deleted, elementId(m)]"
    )
    assert row[0] != row[1]
    # TCK Merge1 #14: a MERGE after a DELETE in the same statement makes a node of its own.
    await cypher(pm, "MATCH (n) DETACH DELETE n")
    await cypher(pm, "CREATE (:Kept), (:Gone)")
    merged = await one(
        pm, "MATCH (n:Gone) WITH n, elementId(n) AS deleted DELETE n MERGE (m:Gone) RETURN [deleted, elementId(m)]"
    )
    assert merged[0] != merged[1]


async def test_inside_one_transaction_a_node_deleted_gives_its_id_to_none_made_after_it(path: str):
    provider = SqliteConnectionProvider("sqlite", path)
    try:
        connection = await provider.connect()
        try:
            await connection.query(QuerySpecification[Any](cypher_statement("CREATE (:Kept)")))
            await connection.start_transaction()
            try:
                [deleted] = await connection.query(
                    QuerySpecification[Any](cypher_statement("CREATE (n:Gone) RETURN elementId(n)"))
                )
                await connection.query(QuerySpecification[Any](cypher_statement("MATCH (n:Gone) DELETE n")))
                [made] = await connection.query(
                    QuerySpecification[Any](cypher_statement("CREATE (n:New) RETURN elementId(n)"))
                )
                assert deleted != made
            finally:
                await connection.rollback_transaction()
        finally:
            await connection.release()
    finally:
        await provider.end()


async def test_clearing_the_graph_keeps_its_ids_counting_from_where_they_were_as_on_postgres(pm: PersistenceManager):
    before = int(str(await one(pm, "CREATE (n:Gone) RETURN elementId(n)")))
    # As the tests' cleanup clears it: the rows go, and SQLite's counter of the table stays.
    await pm.execute(QuerySpecification("DELETE FROM drivine_node"))
    after = int(str(await one(pm, "CREATE (n:New) RETURN elementId(n)")))
    assert after > before, f"{after} after {before}"


# ----- A file an earlier Drivine made -----


def earlier_file(path: str, graph: str, keep_edges: bool = False) -> str:
    """A file with the graph's tables made as an earlier Drivine made them: keys without AUTOINCREMENT."""
    connection = apsw.Connection(path)
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
    finally:
        connection.close()
    return path


def count(path: str, statement: str) -> int:
    connection = apsw.Connection(path)
    try:
        return connection.execute(statement).fetchone()[0]
    finally:
        connection.close()


async def test_a_file_whose_graph_an_earlier_drivine_made_is_refused_on_every_connection_and_nothing_is_made(
    path: str,
):
    earlier_file(path, "drivine")
    provider = SqliteConnectionProvider("earlier", path)
    try:
        for _ in range(2):
            with pytest.raises(ReusedIdsError) as refused:
                await provider.connect()
            assert refused.value.graph == "drivine"
            assert refused.value.path == path
            assert str(refused.value) == refusal("drivine", path)
    finally:
        await provider.end()
    made = "SELECT count(*) FROM sqlite_master WHERE name IN ('drivine_schema', 'drivine_edge_src', 'sqlite_sequence')"
    assert count(path, made) == 0


async def test_a_file_an_earlier_drivine_made_is_refused_through_a_registrys_persistence_manager(path: str):
    earlier_file(path, "drivine")
    registry = DatabaseRegistry()
    registry.builder().database_type(DatabaseType.SQLITE).database_name(path).register("earlier")
    try:
        pm = PersistenceManagerFactory(registry).get("earlier")
        for _ in range(2):
            with pytest.raises(ReusedIdsError, match="earlier Drivine") as refused:
                await pm.query(QuerySpecification[Any]("SELECT 1"))
            assert type(refused.value) is ReusedIdsError
    finally:
        await registry.close()


async def test_schema_applied_to_a_file_an_earlier_drivine_made_raises_the_refusal_itself(path: str):
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
                await apply()
            assert type(refused.value) is ReusedIdsError
    finally:
        await registry.close()


async def test_a_first_connection_taken_inside_a_transaction_raises_the_refusal_itself(path: str):
    # Not wrapped as the transaction's own failure, in a block or a transactional call alike.
    earlier_file(path, "drivine")
    registry = DatabaseRegistry()
    registry.builder().database_type(DatabaseType.SQLITE).database_name(path).register("earlier")
    try:
        pm = PersistenceManagerFactory(registry).get("earlier")

        @transactional()
        async def read() -> list[Any]:
            return await pm.query(QuerySpecification[Any]("SELECT 1"))

        with pytest.raises(ReusedIdsError) as refused:
            async with transaction():
                await pm.query(QuerySpecification[Any]("SELECT 1"))
        assert type(refused.value) is ReusedIdsError
        with pytest.raises(ReusedIdsError) as refused:
            await read()
        assert type(refused.value) is ReusedIdsError
    finally:
        await registry.close()


async def test_a_refused_connection_goes_back_to_its_pool_and_is_refused_again_on_it(path: str):
    earlier_file(path, "drivine")
    provider = SqliteConnectionProvider("earlier", path)
    try:
        with pytest.raises(ReusedIdsError):
            await provider.connect()
        [kept] = provider._idle
        with pytest.raises(ReusedIdsError):
            await provider.connect()
        assert provider._idle == [kept]
    finally:
        await provider.end()


@pytest.mark.async_only
async def test_connections_asking_at_once_are_each_refused_naming_the_graph_as_the_provider_named_it(path: str):
    # The tables keep the case that made them; the graph is found by its name in any case.
    earlier_file(path, "Contracts")
    provider = SqliteConnectionProvider("earlier", path, graph="contracts")
    try:
        found = await asyncio.gather(*(provider.connect() for _ in range(5)), return_exceptions=True)
        assert all(isinstance(it, ReusedIdsError) for it in found), found
        assert {str(it) for it in found} == {refusal("contracts", path)}
        assert len(provider._idle) == 5
    finally:
        await provider.end()


async def test_a_file_whose_table_of_nodes_alone_is_old_is_refused_and_another_graph_in_it_is_not(path: str):
    earlier_file(path, "drivine", keep_edges=True)
    refused = SqliteConnectionProvider("earlier", path)
    try:
        with pytest.raises(ReusedIdsError):
            await refused.connect()
    finally:
        await refused.end()
    # Another graph in the same file is made, and works.
    other = SqliteConnectionProvider("earlier", path, graph="fresh")
    try:
        connection = await other.connect()
        try:
            await connection.query(QuerySpecification[Any](cypher_statement("CREATE (:Person {name: 'Ada'})")))
        finally:
            await connection.release()
    finally:
        await other.end()


def test_the_graphs_look_refuses_a_store_an_earlier_drivine_made_before_anything_is_made(path: str):
    earlier_file(path, "G")
    graph = SqliteGraph("G")
    sent: list[str] = []
    connection = apsw.Connection(path)

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
    connection = apsw.Connection(path)

    def run(statement: str) -> Any:
        row = connection.execute(statement).fetchone()
        return None if row is None else row[0]

    try:
        with pytest.raises(ReusedIdsError):
            SqliteGraph("autoincrement").ensure(run)
    finally:
        connection.close()


def test_a_store_in_memory_an_earlier_drivine_made_is_refused_naming_no_file():
    connection = apsw.Connection(":memory:")
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


async def test_a_fresh_file_is_made_with_autoincrement_keys_and_works(path: str):
    provider = SqliteConnectionProvider("fresh", path)
    try:
        connection = await provider.connect()
        try:
            await connection.query(
                QuerySpecification[Any](
                    cypher_statement("CREATE (:Person {name: 'Ada'})-[:KNOWS]->(:Person {name: 'Bob'})")
                )
            )
            assert await connection.query(
                QuerySpecification[Any](cypher_statement("MATCH (n:Person) RETURN count(n)"))
            ) == [2]
        finally:
            await connection.release()
        # Connected again: looked for, found, not refused.
        await (await provider.connect()).release()
    finally:
        await provider.end()
    keyed = (
        "SELECT count(*) FROM sqlite_master WHERE name IN ('drivine_node', 'drivine_edge') "
        "AND sql LIKE '%AUTOINCREMENT%'"
    )
    assert count(path, keyed) == 2
    connection = apsw.Connection(path)
    try:
        assert [it[0] for it in connection.execute("SELECT name FROM sqlite_sequence ORDER BY name")] == [
            "drivine_edge",
            "drivine_node",
        ]
    finally:
        connection.close()
