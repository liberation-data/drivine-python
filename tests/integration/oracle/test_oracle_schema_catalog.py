"""
A store on Oracle that drivine4j and this library share: the catalog of what was made of indexes and constraints is
one, so each library sees, uses and drops what the other made.
"""

import dataclasses
import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest

from drivine import (
    ConnectionProperties,
    DatabaseRegistry,
    DrivineError,
    NodeFragment,
    NodeId,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    StatelessGraphObjectManager,
    VectorIndex,
    full_text_index,
    range_index,
    uniqueness_constraint,
    vector_index,
)
from drivine.backend.oracle.oracle_backend import OracleBackend
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.manager.graph_object_manager_factory import GraphObjectManagerFactory
from tests.integration.oracle.oracle_seed import ON_ORACLE

pytestmark = ON_ORACLE

DRIVINE4J = json.loads((Path(__file__).parents[2] / "golden" / "drivine4j-oracle-schema.json").read_text())
"""What drivine4j wrote of a store's schema on Oracle, graph `jvm`: each item, with the statements that make it."""

GRAPH = "jvm"
EARLIER_GRAPH = "earlier"


class Chunk(NodeFragment, labels=["Chunk"]):
    id: Annotated[str, NodeId()]
    embedding: Annotated[list[float] | None, VectorIndex(dimensions=2)] = None


RANGES = [range_index("Session", ["lastActivityAt", "sessionId"]), range_index("Person", "name")]
VECTORS = vector_index("Chunk", "embedding", 2, "COSINE")
TEXTS = full_text_index("Article", ["title", "body"])
UNIQUES = [uniqueness_constraint("Person", "uuid"), uniqueness_constraint("Person", ["email", "tenant"])]


@pytest.fixture(scope="module")
def managers(registry: DatabaseRegistry, factory: PersistenceManagerFactory) -> GraphObjectManagerFactory:
    for graph in (GRAPH, EARLIER_GRAPH):
        if registry.registered(f"oracle_{graph}") is None:
            properties = dataclasses.replace(ConnectionProperties.from_env("oracle"), default_graph_path=graph)
            registry.builder().with_properties(properties).register(f"oracle_{graph}")
    return GraphObjectManagerFactory(factory)


async def run(pm: PersistenceManager, statement: str) -> None:
    await pm.execute(QuerySpecification[Any](statement))


async def rows(pm: PersistenceManager, graph: str) -> list[dict[str, Any]]:
    found = await pm.query(
        QuerySpecification[Any](
            "SELECT JSON_OBJECT('kind' VALUE kind, 'label' VALUE label, 'properties' VALUE properties, "
            "'name' VALUE name, 'object_name' VALUE object_name, 'details' VALUE details RETURNING JSON) "
            f"FROM {graph}_schema ORDER BY kind, label, properties"
        )
    )
    return [json.loads(it) if isinstance(it, str) else it for it in found]


async def indexes(pm: PersistenceManager, graph: str) -> list[str]:
    """The indexes on the graph's tables, by name, as Oracle holds them."""
    return await pm.query(
        QuerySpecification[str](
            f"SELECT index_name FROM user_indexes WHERE table_name LIKE '{graph.upper()}\\_%' ESCAPE '\\' "
            "AND index_type <> 'LOB' ORDER BY index_name"
        )
    )


async def drop_graph(pm: PersistenceManager, graph: str) -> None:
    """Drops the graph's property graph and every table of its own: nodes, relationships, catalog, side tables."""
    await run(
        pm,
        f"""BEGIN
    EXECUTE IMMEDIATE 'DROP PROPERTY GRAPH IF EXISTS {graph}';
    FOR it IN (SELECT table_name FROM user_tables WHERE table_name LIKE '{graph.upper()}\\_%' ESCAPE '\\') LOOP
        EXECUTE IMMEDIATE 'DROP TABLE ' || it.table_name || ' CASCADE CONSTRAINTS PURGE';
    END LOOP;
END;""",
    )


async def fresh_graph(
    pm: PersistenceManager, managers: GraphObjectManagerFactory, graph: str
) -> StatelessGraphObjectManager:
    """Makes the graph afresh, with none of its tables beside the nodes and nothing recorded of its schema."""
    await drop_graph(pm, graph)
    await run(pm, OracleGraph(graph).ensuring)
    return managers.get(f"oracle_{graph}")


async def made_by_drivine4j(pm: PersistenceManager) -> None:
    for item in DRIVINE4J["items"]:
        for statement in item["statements"]:
            await run(pm, statement)


@pytest.fixture(scope="module", autouse=True)
async def tidy(factory: PersistenceManagerFactory):
    yield
    pm = factory.get("oracle")
    await drop_graph(pm, GRAPH)
    await drop_graph(pm, EARLIER_GRAPH)


async def test_lists_uses_and_drops_what_drivine4j_made_and_makes_none_beside_it(
    pm: PersistenceManager, managers: GraphObjectManagerFactory
):
    gom = await fresh_graph(pm, managers, GRAPH)
    await made_by_drivine4j(pm)
    written = await rows(pm, GRAPH)
    made = await indexes(pm, GRAPH)

    listed = await gom.indexes.list("RANGE")
    assert [(it.label, it.properties) for it in listed] == [
        ("Person", ("email", "tenant")),
        ("Person", ("name",)),
        ("Person", ("uuid",)),
        ("Session", ("lastActivityAt", "sessionId")),
    ]
    assert not any(it.incomplete for it in listed)
    assert [it.properties for it in await gom.constraints.list()] == [("email", "tenant"), ("uuid",)]
    [vector] = await gom.indexes.list("VECTOR")
    assert (vector.label, vector.properties, vector.dimensions, vector.similarity) == (
        "Chunk",
        ("embedding",),
        2,
        "COSINE",
    )
    assert [it.properties for it in await gom.indexes.list("FULLTEXT")] == [("title", "body")]

    for spec in [*RANGES, VECTORS, TEXTS]:
        assert (await gom.indexes.ensure(spec)).status == "ALREADY_MATCHING"
    for spec in UNIQUES:
        assert (await gom.constraints.ensure(spec)).status == "ALREADY_MATCHING"
    assert await rows(pm, GRAPH) == written
    assert await indexes(pm, GRAPH) == made

    # A search goes through the table drivine4j made, which its trigger keeps in step with what this library saves.
    graph = OracleGraph(GRAPH)
    backend = OracleBackend(graph)

    async def table_of(kind: Literal["VECTOR", "FULLTEXT"], label: str, properties: list[str]) -> str | None:
        return await pm.maybe_get_one(backend.search_index_table(kind, label, properties).spec())

    assert await table_of("VECTOR", "Chunk", ["embedding"]) == graph.vectors("Chunk", "embedding")
    assert await table_of("FULLTEXT", "Article", ["title", "body"]) == graph.texts("Article", ["title", "body"])
    await gom.save_all([Chunk(id="c1", embedding=[1, 0]), Chunk(id="c2", embedding=[0, 1])])
    side = graph.vectors("Chunk", "embedding")
    assert int(await pm.get_one(QuerySpecification[Any](f"SELECT COUNT(*) FROM {side}"))) == 2
    assert [it.value.id for it in await gom.load_nearest(Chunk, [0, 1], top_k=1)] == ["c2"]

    for spec in UNIQUES:
        await gom.constraints.drop(spec)
    for spec in [*RANGES, *(range_index(it.label, it.properties) for it in UNIQUES), VECTORS, TEXTS]:
        await gom.indexes.drop(spec)
    assert await rows(pm, GRAPH) == []
    assert [it for it in await indexes(pm, GRAPH) if re.match(r"^JVM_(IX|LX|UQ|V|T)_", it)] == []
    assert await table_of("VECTOR", "Chunk", ["embedding"]) is None


async def test_records_what_drivine4j_records_to_the_object_names(
    pm: PersistenceManager, managers: GraphObjectManagerFactory
):
    await fresh_graph(pm, managers, GRAPH)
    await made_by_drivine4j(pm)
    theirs = await rows(pm, GRAPH)

    gom = await fresh_graph(pm, managers, GRAPH)
    for spec in [*RANGES, VECTORS, TEXTS]:
        assert (await gom.indexes.ensure(spec)).status == "CREATED"
    for spec in UNIQUES:
        assert (await gom.constraints.ensure(spec)).status == "CREATED"
    ours = await rows(pm, GRAPH)

    # drivine4j's unique index over several properties is of another expression than its labelled index, and it makes
    # both. This library's is of the labelled index's own, which Oracle would not make twice, so it stands for the
    # labelled index, as over one property.
    def pair(it: dict[str, Any]) -> bool:
        return it["kind"] == "RANGE_INDEX" and it["properties"] == '["email","tenant"]'

    assert [it for it in ours if not pair(it)] == [it for it in theirs if not pair(it)]
    unique = OracleGraph(GRAPH).object_name("uq", ["Person", "email", "tenant"])
    assert [it for it in ours if pair(it)] == [{**it, "details": {"unique": unique}} for it in theirs if pair(it)]


async def test_completes_a_range_index_that_lost_the_constraint_standing_for_its_labelled_index(
    pm: PersistenceManager, managers: GraphObjectManagerFactory
):
    gom = await fresh_graph(pm, managers, GRAPH)
    unique = UNIQUES[0]
    served = range_index(unique.label, unique.properties)

    async def incomplete() -> bool:
        return next(it for it in await gom.indexes.list("RANGE") if it.label == "Person").incomplete

    assert (await gom.indexes.ensure(served)).status == "CREATED"
    assert (await gom.constraints.ensure(unique)).status == "CREATED"
    assert not await incomplete()

    await gom.constraints.drop(unique)
    assert await incomplete()
    assert (await gom.indexes.ensure(served)).status == "CREATED"
    assert not await incomplete()
    assert OracleGraph(GRAPH).object_name("lx", ["Person", "uuid"]).upper() in await indexes(pm, GRAPH)


async def test_keeps_the_index_two_labels_share_over_a_property_until_the_last_range_index_over_it_is_dropped(
    pm: PersistenceManager, managers: GraphObjectManagerFactory
):
    gom = await fresh_graph(pm, managers, GRAPH)
    graph = OracleGraph(GRAPH)
    shared = graph.object_name("ix", ["name"]).upper()
    person, company = range_index("Person", "name"), range_index("Company", "name")
    assert (await gom.indexes.ensure(person)).status == "CREATED"
    assert (await gom.indexes.ensure(company)).status == "CREATED"
    assert [it for it in await indexes(pm, GRAPH) if it == shared] == [shared]

    await gom.indexes.drop(person)
    assert shared in await indexes(pm, GRAPH)
    assert graph.object_name("lx", ["Person", "name"]).upper() not in await indexes(pm, GRAPH)
    assert [(it.label, it.incomplete) for it in await gom.indexes.list("RANGE")] == [("Company", False)]

    await gom.indexes.drop(company)
    assert shared not in await indexes(pm, GRAPH)
    assert await rows(pm, GRAPH) == []


async def test_leaves_the_range_index_incomplete_where_the_data_refuses_its_constraint_for_an_ensure_to_complete(
    pm: PersistenceManager, managers: GraphObjectManagerFactory
):
    gom = await fresh_graph(pm, managers, GRAPH)
    graph = OracleGraph(GRAPH)
    unique = UNIQUES[0]
    served = range_index(unique.label, unique.properties)
    assert (await gom.indexes.ensure(served)).status == "CREATED"
    for _ in range(2):
        await run(
            pm, f'INSERT INTO {graph.nodes} (labels, props) VALUES (JSON(\'["Person"]\'), JSON(\'{{"uuid": "u1"}}\'))'
        )

    async def incomplete() -> bool:
        return next(it for it in await gom.indexes.list("RANGE") if it.label == "Person").incomplete

    # Unchecked, the unique index is refused (ORA-01452) after the labelled index it would stand for was dropped.
    with pytest.raises(DrivineError, match="ORA-01452"):
        await gom.constraints.ensure(unique, 0)
    assert await gom.constraints.list() == []
    assert await incomplete()

    assert (await gom.indexes.ensure(served)).status == "CREATED"
    assert not await incomplete()
    assert graph.object_name("lx", ["Person", "uuid"]).upper() in await indexes(pm, GRAPH)


async def test_understands_a_store_this_library_wrote_before_it_recorded_what_drivine4j_records(
    pm: PersistenceManager, managers: GraphObjectManagerFactory
):
    gom = await fresh_graph(pm, managers, EARLIER_GRAPH)
    graph = OracleGraph(EARLIER_GRAPH)
    catalog = graph.catalog

    # As this library made a range index and a uniqueness constraint: one labelled index each, named by a hash.
    def labelled(prop: str) -> str:
        return (
            f"CASE WHEN JSON_EXISTS(n.labels, '$?(@ == \"Person\")') THEN JSON_VALUE(n.props, '$.\"{prop}\"' "
            "RETURNING VARCHAR2(4000) NULL ON ERROR) END"
        )

    await run(pm, f"CREATE INDEX EARLIER_R_0123456789AB ON {graph.nodes} n ({labelled('name')})")
    await run(pm, f"CREATE UNIQUE INDEX EARLIER_U_0123456789AB ON {graph.nodes} n ({labelled('uuid')})")
    await run(
        pm,
        f"INSERT INTO {catalog} (kind, label, properties, name, object_name, details) VALUES "
        "('RANGE', 'Person', '[\"name\"]', 'Person_name_range', 'EARLIER_R_0123456789AB', JSON('{}'))",
    )
    await run(
        pm,
        f"INSERT INTO {catalog} (kind, label, properties, name, object_name, details) VALUES "
        "('UNIQUE', 'Person', '[\"uuid\"]', 'Person_uuid_unique', 'EARLIER_U_0123456789AB', JSON('{}'))",
    )
    # And a vector index: its table as it is named now, recorded under the kind it was recorded as then.
    await gom.indexes.ensure(VECTORS)
    await run(pm, f"UPDATE {catalog} SET kind = 'VECTOR' WHERE kind = 'VECTOR_INDEX'")

    assert [(it.properties, it.incomplete) for it in await gom.indexes.list("RANGE")] == [(("name",), False)]
    assert [it.properties for it in await gom.constraints.list()] == [("uuid",)]
    assert (await gom.indexes.ensure(range_index("Person", "name"))).status == "ALREADY_MATCHING"
    assert (await gom.constraints.ensure(UNIQUES[0])).status == "ALREADY_MATCHING"
    assert (await gom.indexes.ensure(VECTORS)).status == "ALREADY_MATCHING"
    backend = OracleBackend(graph)
    found = await pm.maybe_get_one(backend.search_index_table("VECTOR", "Chunk", ["embedding"]).spec())
    assert found == graph.vectors("Chunk", "embedding")

    # A range index over the unique property is served by the earlier constraint's index, as by one made now.
    assert (await gom.indexes.ensure(range_index("Person", "uuid"))).status == "CREATED"
    # A constraint over the earlier range index's property replaces that index, which Oracle won't hold twice.
    assert (await gom.constraints.ensure(uniqueness_constraint("Person", "name"))).status == "CREATED"
    assert "EARLIER_R_0123456789AB" not in await indexes(pm, EARLIER_GRAPH)
    assert [(it.properties, it.incomplete) for it in await gom.indexes.list("RANGE")] == [
        (("name",), False),
        (("uuid",), False),
    ]

    await gom.constraints.drop(UNIQUES[0])
    await gom.constraints.drop(uniqueness_constraint("Person", "name"))
    for spec in [range_index("Person", "name"), range_index("Person", "uuid"), VECTORS]:
        await gom.indexes.drop(spec)
    assert await rows(pm, EARLIER_GRAPH) == []
    assert [it for it in await indexes(pm, EARLIER_GRAPH) if re.match(r"^EARLIER_(R|U|IX|LX|UQ|V)_", it)] == []
