"""
drivine4j's store on Postgres, as an object manager makes and keeps it: made at its first statement, cascades, its
catalog of indexes, and search, with an index and without. TypeScript's PostgresStore.spec, ported.
"""

import dataclasses
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

import pytest

from drivine import (
    CascadeType,
    ConnectionProperties,
    DatabaseRegistry,
    DrivineError,
    FullTextIndex,
    GraphView,
    NodeFragment,
    NodeId,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    Relationship,
    Root,
    StatelessGraphObjectManager,
    VectorIndex,
    full_text_index,
    range_index,
    refs,
    uniqueness_constraint,
    vector_index,
)
from drivine.backend.postgres.postgres_backend import PostgresBackend
from drivine.backend.postgres.postgres_graph import PostgresGraph
from drivine.dsl.query import Query
from tests.integration.postgres.conftest import ON_POSTGRES
from tests.integration.sql.sql_seed import SqlSeed

pytestmark = ON_POSTGRES

GRAPH = PostgresGraph()


class Ticket(NodeFragment, labels=["PgStore", "Ticket"]):
    id: Annotated[str, NodeId()]
    title: str | None = None


class Owner(NodeFragment, labels=["PgStore", "Owner"]):
    id: Annotated[str, NodeId()]


class Item(NodeFragment, labels=["PgStore", "Item"]):
    id: Annotated[str, NodeId()]


class Tag(NodeFragment, labels=["PgStore", "Tag"]):
    id: Annotated[str, NodeId()]


class Holding(GraphView):
    owner: Annotated[Owner, Root()]
    items: Annotated[list[Item], Relationship("HOLDS")] = []
    tags: Annotated[list[Tag], Relationship("TAGGED")] = []


class Doc(NodeFragment, labels=["PgStore", "Doc"]):
    id: Annotated[str, NodeId()]
    body: Annotated[str | None, FullTextIndex()] = None
    embedding: Annotated[list[float] | None, VectorIndex(dimensions=3)] = None


@pytest.fixture
async def seed(pm: PersistenceManager) -> AsyncIterator[SqlSeed]:
    seed = SqlSeed(pm, "PgStore")
    await seed.prepare()
    await seed.clear()
    await pm.execute(QuerySpecification[Any](f"DELETE FROM {GRAPH.catalog} WHERE label LIKE 'PgStore%'"))
    yield seed
    await seed.clear()


async def exists(pm: PersistenceManager, table: str) -> bool:
    row = await pm.get_one(QuerySpecification[Any](f"SELECT to_regclass('{table}') IS NOT NULL AS made"))
    return row["made"]


async def test_makes_a_graphs_tables_at_an_object_managers_first_statement_and_not_at_a_connection(
    pm: PersistenceManager, registry: DatabaseRegistry, factory: PersistenceManagerFactory
):
    name = "pgstore_lazy"
    # CASCADE: the store's functions return a row of its node table.
    await pm.execute(QuerySpecification[Any](f"DROP TABLE IF EXISTS {name}_edge, {name}_node, {name}_schema CASCADE"))
    if registry.registered("postgres_lazy") is None:
        properties = dataclasses.replace(ConnectionProperties.from_env("postgres"), default_graph_path=name)
        registry.builder().with_properties(properties).register("postgres_lazy")
    lazy_pm = factory.get("postgres_lazy")
    assert await lazy_pm.query(QuerySpecification[Any]("SELECT 1 AS one")) == [{"one": 1}]
    assert not await exists(pm, f"{name}_node")
    lazy = StatelessGraphObjectManager(lazy_pm)
    assert await lazy.count(Ticket) == 0
    for table in ("node", "edge", "schema"):
        assert await exists(pm, f"{name}_{table}")
    # drivine4j's store, as its node table's comment says: a manager on the JVM finds it made.
    comment = await pm.get_one(
        QuerySpecification[Any](f"SELECT obj_description(to_regclass('{name}_node'), 'pg_class') AS version")
    )
    assert comment["version"] == PostgresGraph.VERSION
    await pm.execute(QuerySpecification[Any](f"DROP TABLE {name}_edge, {name}_node, {name}_schema CASCADE"))


def test_refuses_a_graph_name_that_cant_name_its_tables_within_63_characters():
    assert len(PostgresGraph(f"g{'x' * 48}").name) == 49
    with pytest.raises(DrivineError, match="can't name a graph on Postgres"):
        PostgresGraph(f"g{'x' * 49}")
    assert len(PostgresGraph(f"g{'x' * 48}").vectors("Label", "embedding")) + 2 <= 63


async def test_deletes_a_views_root_with_every_node_it_reaches_or_only_those_left_with_nothing_else(
    gom: StatelessGraphObjectManager, seed: SqlSeed
):
    async def holding(owner: str) -> None:
        await seed.node(["Owner"], {"id": owner})
        await seed.node(["Item"], {"id": f"{owner}-item"})
        await seed.node(["Tag"], {"id": f"{owner}-tag"})
        await seed.edge(owner, "HOLDS", f"{owner}-item")
        await seed.edge(owner, "TAGGED", f"{owner}-tag")

    await holding("all")
    await seed.edge("all-item", "TAGGED", "all-tag")
    await holding("orphans")
    # Another owner holds the orphans owner's tag, so a delete of orphans only leaves it: a node is an orphan when it
    # has no relationship but to the roots deleted.
    await seed.node(["Owner"], {"id": "other"})
    await seed.edge("other", "TAGGED", "orphans-tag")
    assert await gom.delete(Holding, "all", cascade=CascadeType.DELETE_ALL) == 1
    assert await gom.load(Item, "all-item") is None
    assert await gom.load(Tag, "all-tag") is None
    assert await gom.delete(Holding, "orphans", cascade=CascadeType.DELETE_ORPHAN) == 1
    assert await gom.load(Item, "orphans-item") is None
    assert await gom.load(Tag, "orphans-tag") is not None
    assert await gom.load(Owner, "other") is not None


async def test_records_an_index_as_drivine4j_does_reads_a_filter_through_it_and_drops_what_it_recorded(
    pm: PersistenceManager, gom: StatelessGraphObjectManager, seed: SqlSeed
):
    await pm.execute(
        QuerySpecification[Any](
            f'INSERT INTO {GRAPH.nodes} (labels, props) SELECT \'["PgStore", "Ticket"]\', '
            "jsonb_build_object('id', 't' || i, 'title', 'title ' || i) FROM generate_series(1, 3000) i"
        )
    )
    await pm.execute(QuerySpecification[Any](f"ANALYZE {GRAPH.nodes}"))
    spec = range_index("Ticket", "title")
    assert (await gom.indexes.ensure(spec)).status == "CREATED"
    assert (await gom.indexes.ensure(spec)).status == "ALREADY_MATCHING"
    name = GRAPH.object_name("ix", ["Ticket", "title"])
    recorded = await pm.query(
        QuerySpecification[Any](
            f"SELECT kind, properties, object_name FROM {GRAPH.catalog} WHERE label = 'Ticket' AND kind = 'RANGE_INDEX'"
        )
    )
    assert recorded == [{"kind": "RANGE_INDEX", "properties": '["title"]', "object_name": name}]
    backend = PostgresBackend(GRAPH)
    model = gom.registry.fragment(Ticket)
    query = backend.render_query(Query(where=refs(Ticket).title.eq("title 7")), model, lambda it: [])
    statement = backend.load_all_fragments(model, query.where)
    plan = await pm.query(
        QuerySpecification[Any](f"EXPLAIN {statement.text}").bind({**statement.bindings, **query.bindings})
    )
    assert name.lower() in json.dumps(plan)
    assert [it.id for it in await gom.load_all(Ticket, refs(Ticket).title.eq("title 7"))] == ["t7"]
    await gom.indexes.drop(spec)
    assert not await exists(pm, name)
    assert not any(it.label == "Ticket" for it in await gom.indexes.list("RANGE"))
    await gom.delete_all(Ticket)


async def test_reports_the_values_a_uniqueness_constraint_would_refuse_and_refuses_a_second_node_once_made(
    gom: StatelessGraphObjectManager, seed: SqlSeed
):
    await seed.node(["Ticket"], {"id": "u1", "title": "same"})
    await seed.node(["Ticket"], {"id": "u2", "title": "same"})
    spec = uniqueness_constraint("Ticket", "title")
    refused = await gom.constraints.ensure(spec)
    assert refused.status == "VIOLATION"
    assert refused.sample == ("same",)
    await gom.delete(Ticket, "u2")
    assert (await gom.constraints.ensure(spec)).status == "CREATED"
    with pytest.raises(DrivineError):
        await seed.node(["Ticket"], {"id": "u3", "title": "same"})
    await gom.constraints.drop(spec)
    await gom.delete_all(Ticket)


async def test_searches_by_vector_and_by_text_alike_with_an_index_and_without_one(
    gom: StatelessGraphObjectManager, seed: SqlSeed
):
    docs = [
        ("d1", "graph databases store nodes", [1, 0, 0]),
        ("d2", "a relational database stores rows", [0.9, 0.1, 0]),
        ("d3", "graphs of graphs", [0, 1, 0]),
        ("d4", "nothing at all", [0, 0, 1]),
    ]
    for id_, body, embedding in docs:
        await seed.node(["Doc"], {"id": id_, "body": body, "embedding": embedding})
    # A node with an embedding of another size is not measured, as pgvector measures vectors of one size.
    await seed.node(["Doc"], {"id": "d5", "body": "graph", "embedding": [1, 0]})

    async def nearest() -> list[str]:
        return [it.value.id for it in await gom.load_nearest(Doc, [1, 0, 0], top_k=2)]

    async def matching(text: str) -> list[tuple[str, float]]:
        return [(it.value.id, it.score) for it in await gom.load_matching(Doc, text, top_k=5)]

    exact = await nearest()
    assert exact == ["d1", "d2"]
    scored = await matching("graph -relational")
    assert [id_ for id_, _ in scored] == ["d1", "d5"]
    assert scored[0][1] == 1
    assert sorted(id_ for id_, _ in await matching("graph*")) == ["d1", "d3", "d5"]
    await gom.indexes.ensure(vector_index("Doc", "embedding", 3))
    await gom.indexes.ensure(full_text_index("Doc", "body"))
    assert await nearest() == exact
    assert await matching("graph -relational") == scored
    await gom.indexes.drop(vector_index("Doc", "embedding", 3))
    await gom.indexes.drop(full_text_index("Doc", "body"))


async def test_mints_the_store_identity_once_as_a_node_of_the_graph(pm: PersistenceManager):
    identity = await StatelessGraphObjectManager(pm).store_identity()
    again = await StatelessGraphObjectManager(pm).store_identity()
    assert again.id == identity.id
