# Written by scripts/unasync.py from tests/integration/postgres/test_postgres_store.py:
# change that, and run the script again.
"""
drivine4j's store on Postgres, as Drivine makes and keeps it: made at the provider's first connection, cascades, its
catalog of indexes, and search, with an index and without. TypeScript's PostgresStore.spec, ported.
"""

import dataclasses
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any

import pytest

from drivine.backend.postgres.postgres_backend import PostgresBackend
from drivine.backend.postgres.postgres_graph import PostgresGraph
from drivine.dsl.query import Query
from drivine.sync import (
    CascadeType,
    ConnectionProperties,
    DatabaseRegistry,
    DrivineError,
    FullTextIndex,
    GraphView,
    NodeFragment,
    NodeId,
    PatchResult,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    Relationship,
    Root,
    StatelessGraphObjectManager,
    VectorIndex,
    case_of,
    cypher_statement,
    equal_to,
    full_text_index,
    range_index,
    refs,
    transactional,
    uniqueness_constraint,
    vector_index,
)
from tests.sync_integration.postgres.conftest import ON_POSTGRES
from tests.sync_integration.sql.sql_seed import SqlSeed

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
def seed(pm: PersistenceManager) -> Iterator[SqlSeed]:
    seed = SqlSeed(pm, "PgStore")
    seed.clear()
    pm.execute(QuerySpecification[Any](f"DELETE FROM {GRAPH.catalog} WHERE label LIKE 'PgStore%'"))
    yield seed
    seed.clear()


def exists(pm: PersistenceManager, table: str) -> bool:
    row = pm.get_one(QuerySpecification[Any](f"SELECT to_regclass('{table}') IS NOT NULL AS made"))
    return row["made"]


def test_makes_a_graphs_tables_at_its_providers_first_connection_whatever_is_sent_first(
    pm: PersistenceManager, registry: DatabaseRegistry, factory: PersistenceManagerFactory
):
    name = "pgstore_lazy"
    # CASCADE: the store's functions return a row of its node table.
    pm.execute(QuerySpecification[Any](f"DROP TABLE IF EXISTS {name}_edge, {name}_node, {name}_schema CASCADE"))
    if registry.registered("postgres_lazy") is None:
        properties = dataclasses.replace(ConnectionProperties.from_env("postgres"), default_graph_path=name)
        registry.builder().with_properties(properties).register("postgres_lazy")
    lazy_pm = factory.get("postgres_lazy")
    assert not exists(pm, f"{name}_node")
    # Plain SQL, through a persistence manager alone.
    assert lazy_pm.query(QuerySpecification[Any]("SELECT 1 AS one")) == [{"one": 1}]
    for table in ("node", "edge", "schema"):
        assert exists(pm, f"{name}_{table}")
    assert StatelessGraphObjectManager(lazy_pm).count(Ticket) == 0
    # drivine4j's store, as its node table's comment says: a manager on the JVM finds it made.
    comment = pm.get_one(
        QuerySpecification[Any](f"SELECT obj_description(to_regclass('{name}_node'), 'pg_class') AS version")
    )
    assert comment["version"] == PostgresGraph.VERSION
    pm.execute(QuerySpecification[Any](f"DROP TABLE {name}_edge, {name}_node, {name}_schema CASCADE"))


def test_makes_a_graphs_tables_when_its_managers_first_statement_is_a_batch_in_a_transaction_of_its_own(
    pm: PersistenceManager, registry: DatabaseRegistry, factory: PersistenceManagerFactory
):
    name = "pgstore_batch"
    pm.execute(QuerySpecification[Any](f"DROP TABLE IF EXISTS {name}_edge, {name}_node, {name}_schema CASCADE"))
    if registry.registered("postgres_batch") is None:
        properties = dataclasses.replace(ConnectionProperties.from_env("postgres"), default_graph_path=name)
        registry.builder().with_properties(properties).register("postgres_batch")
    batch = StatelessGraphObjectManager(factory.get("postgres_batch"))
    # A fragment and a view: two statements, which run in a transaction the batch opens.
    batch.save_all([Ticket(id="t1"), Holding(owner=Owner(id="o1"), items=[Item(id="i1")])])
    assert batch.count(Ticket) == 1
    holding = batch.load(Holding, "o1")
    assert holding is not None and [it.id for it in holding.items] == ["i1"]
    pm.execute(QuerySpecification[Any](f"DROP TABLE {name}_edge, {name}_node, {name}_schema CASCADE"))


def test_keeps_a_graphs_tables_made_at_a_transactions_first_statement_though_the_transaction_rolls_back(
    pm: PersistenceManager, registry: DatabaseRegistry, factory: PersistenceManagerFactory
):
    name = "pgstore_rolled"
    drop = f"DROP TABLE IF EXISTS {name}_edge, {name}_node, {name}_schema CASCADE"
    pm.execute(QuerySpecification[Any](drop))
    if registry.registered("postgres_rolled") is None:
        properties = dataclasses.replace(ConnectionProperties.from_env("postgres"), default_graph_path=name)
        registry.builder().with_properties(properties).register("postgres_rolled")
    rolled = StatelessGraphObjectManager(factory.get("postgres_rolled"))

    @transactional()
    def open_ticket() -> None:
        rolled.save(Ticket(id="rolled-1"))
        raise ValueError("Changed our minds")

    with pytest.raises(ValueError, match="Changed our minds"):
        open_ticket()
    assert exists(pm, f"{name}_node")
    assert rolled.count(Ticket) == 0
    pm.execute(QuerySpecification[Any](drop))


def test_makes_a_graphs_tables_when_its_managers_first_statement_is_a_schema_statement(
    pm: PersistenceManager, registry: DatabaseRegistry, factory: PersistenceManagerFactory
):
    name = "pgstore_schema_first"
    drop = f"DROP TABLE IF EXISTS {name}_edge, {name}_node, {name}_schema CASCADE"
    pm.execute(QuerySpecification[Any](drop))
    if registry.registered("postgres_schema_first") is None:
        properties = dataclasses.replace(ConnectionProperties.from_env("postgres"), default_graph_path=name)
        registry.builder().with_properties(properties).register("postgres_schema_first")
    schema_first = StatelessGraphObjectManager(factory.get("postgres_schema_first"))
    assert (schema_first.indexes.ensure(range_index("Ticket", "title"))).status == "CREATED"
    assert exists(pm, f"{name}_schema")
    pm.execute(QuerySpecification[Any](drop))


def test_refuses_a_graph_name_that_cant_name_its_tables_within_63_characters():
    assert len(PostgresGraph(f"g{'x' * 48}").name) == 49
    with pytest.raises(DrivineError, match="can't name a graph on Postgres"):
        PostgresGraph(f"g{'x' * 49}")
    assert len(PostgresGraph(f"g{'x' * 48}").vectors("Label", "embedding")) + 2 <= 63


def test_deletes_a_views_root_with_every_node_it_reaches_or_only_those_left_with_nothing_else(
    gom: StatelessGraphObjectManager, seed: SqlSeed
):
    def holding(owner: str) -> None:
        seed.node(["Owner"], {"id": owner})
        seed.node(["Item"], {"id": f"{owner}-item"})
        seed.node(["Tag"], {"id": f"{owner}-tag"})
        seed.edge(owner, "HOLDS", f"{owner}-item")
        seed.edge(owner, "TAGGED", f"{owner}-tag")

    holding("all")
    seed.edge("all-item", "TAGGED", "all-tag")
    holding("orphans")
    # Another owner holds the orphans owner's tag, so a delete of orphans only leaves it: a node is an orphan when it
    # has no relationship but to the roots deleted.
    seed.node(["Owner"], {"id": "other"})
    seed.edge("other", "TAGGED", "orphans-tag")
    assert gom.delete(Holding, "all", cascade=CascadeType.DELETE_ALL) == 1
    assert gom.load(Item, "all-item") is None
    assert gom.load(Tag, "all-tag") is None
    assert gom.delete(Holding, "orphans", cascade=CascadeType.DELETE_ORPHAN) == 1
    assert gom.load(Item, "orphans-item") is None
    assert gom.load(Tag, "orphans-tag") is not None
    assert gom.load(Owner, "other") is not None


def test_records_an_index_as_drivine4j_does_reads_a_filter_through_it_and_drops_what_it_recorded(
    pm: PersistenceManager, gom: StatelessGraphObjectManager, seed: SqlSeed
):
    pm.execute(
        QuerySpecification[Any](
            f'INSERT INTO {GRAPH.nodes} (labels, props) SELECT \'["PgStore", "Ticket"]\', '
            "jsonb_build_object('id', 't' || i, 'title', 'title ' || i) FROM generate_series(1, 3000) i"
        )
    )
    pm.execute(QuerySpecification[Any](f"ANALYZE {GRAPH.nodes}"))
    spec = range_index("Ticket", "title")
    assert (gom.indexes.ensure(spec)).status == "CREATED"
    assert (gom.indexes.ensure(spec)).status == "ALREADY_MATCHING"
    name = GRAPH.object_name("ix", ["Ticket", "title"])
    recorded = pm.query(
        QuerySpecification[Any](
            f"SELECT kind, properties, object_name FROM {GRAPH.catalog} WHERE label = 'Ticket' AND kind = 'RANGE_INDEX'"
        )
    )
    assert recorded == [{"kind": "RANGE_INDEX", "properties": '["title"]', "object_name": name}]
    backend = PostgresBackend(GRAPH)
    model = gom.registry.fragment(Ticket)
    query = backend.render_query(Query(where=refs(Ticket).title.eq("title 7")), model, lambda it: [])
    statement = backend.load_all_fragments(model, query.where)
    plan = pm.query(QuerySpecification[Any](f"EXPLAIN {statement.text}").bind({**statement.bindings, **query.bindings}))
    assert name.lower() in json.dumps(plan)
    assert [it.id for it in gom.load_all(Ticket, refs(Ticket).title.eq("title 7"))] == ["t7"]
    gom.indexes.drop(spec)
    assert not exists(pm, name)
    assert not any(it.label == "Ticket" for it in gom.indexes.list("RANGE"))
    gom.delete_all(Ticket)


def test_reports_the_values_a_uniqueness_constraint_would_refuse_and_refuses_a_second_node_once_made(
    gom: StatelessGraphObjectManager, seed: SqlSeed
):
    seed.node(["Ticket"], {"id": "u1", "title": "same"})
    seed.node(["Ticket"], {"id": "u2", "title": "same"})
    spec = uniqueness_constraint("Ticket", "title")
    refused = gom.constraints.ensure(spec)
    assert refused.status == "VIOLATION"
    assert refused.sample is not None
    [held] = refused.sample
    assert held["properties"] == {"title": "same"} and held["count"] == 2 and len(held["nodes"]) == 2
    gom.delete(Ticket, "u2")
    assert (gom.constraints.ensure(spec)).status == "CREATED"
    with pytest.raises(DrivineError):
        seed.node(["Ticket"], {"id": "u3", "title": "same"})
    gom.constraints.drop(spec)
    gom.delete_all(Ticket)


def test_searches_by_vector_and_by_text_alike_with_an_index_and_without_one(
    gom: StatelessGraphObjectManager, seed: SqlSeed
):
    docs = [
        ("d1", "graph databases store nodes", [1, 0, 0]),
        ("d2", "a relational database stores rows", [0.9, 0.1, 0]),
        ("d3", "graphs of graphs", [0, 1, 0]),
        ("d4", "nothing at all", [0, 0, 1]),
    ]
    for id_, body, embedding in docs:
        seed.node(["Doc"], {"id": id_, "body": body, "embedding": embedding})
    # A node with an embedding of another size is not measured, as pgvector measures vectors of one size.
    seed.node(["Doc"], {"id": "d5", "body": "graph", "embedding": [1, 0]})

    def nearest() -> list[str]:
        return [it.value.id for it in gom.load_nearest(Doc, [1, 0, 0], top_k=2)]

    def matching(text: str) -> list[tuple[str, float]]:
        return [(it.value.id, it.score) for it in gom.load_matching(Doc, text, top_k=5)]

    exact = nearest()
    assert exact == ["d1", "d2"]
    scored = matching("graph -relational")
    assert [id_ for id_, _ in scored] == ["d1", "d5"]
    assert scored[0][1] == 1
    assert sorted(id_ for id_, _ in matching("graph*")) == ["d1", "d3", "d5"]
    gom.indexes.ensure(vector_index("Doc", "embedding", 3))
    gom.indexes.ensure(full_text_index("Doc", "body"))
    assert nearest() == exact
    assert matching("graph -relational") == scored
    gom.indexes.drop(vector_index("Doc", "embedding", 3))
    gom.indexes.drop(full_text_index("Doc", "body"))


def test_mints_the_store_identity_once_as_a_node_of_the_graph(pm: PersistenceManager):
    identity = StatelessGraphObjectManager(pm).store_identity()
    again = StatelessGraphObjectManager(pm).store_identity()
    assert again.id == identity.id


class Status(str, Enum):  # noqa: UP042 - not a StrEnum: str() of this one is its name, "Status.OPEN".
    OPEN = "Open"


class Level(Enum):
    LOW = 1
    HIGH = 2


class Job(NodeFragment, labels=["PgStore", "Job"]):
    id: Annotated[str, NodeId()]
    title: str
    level: Level


def test_tests_text_against_a_str_enum_as_the_value_it_is_stored_as(gom: StatelessGraphObjectManager, seed):
    gom.save_all([Job(id="a", title="Open", level=Level.HIGH), Job(id="b", title="Shut", level=Level.LOW)])
    q = refs(Job)
    assert [it.id for it in gom.load_all(Job, q.title.eq_ignore_case(Status.OPEN))] == ["a"]
    assert [it.id for it in gom.load_all(Job, q.title.starts_with(Status.OPEN))] == ["a"]


def test_orders_by_a_case_comparing_with_an_enum_of_numbers_as_the_number_it_is_stored_as(
    gom: StatelessGraphObjectManager, seed
):
    gom.save_all([Job(id="a", title="Open", level=Level.HIGH), Job(id="b", title="Shut", level=Level.LOW)])
    q = refs(Job)
    ranking = case_of([equal_to(q.level, Level.LOW).then(0)], 1)
    assert [it.id for it in gom.load_all(Job, order_by=[ranking.asc(), q.id.asc()])] == ["b", "a"]


def test_patches_where_a_raw_condition_holds_with_each_kind_of_value_written_into_it(
    gom: StatelessGraphObjectManager, seed
):
    gom.save(Ticket(id="t1", title="Open"))
    where = (
        "(n.props ->> 'title') = $title AND CAST($flag AS boolean) AND $gone IS NULL "
        "AND CAST($at AS timestamptz) < now() AND CAST($ids AS jsonb) @> (n.props -> 'id')"
    )
    values = {"title": "Open", "flag": True, "gone": None, "at": datetime(2024, 1, 2, tzinfo=UTC), "ids": ["t1"]}
    unmet = gom.patch(Ticket, "t1", set={"title": "Shut"}, where=where, bindings={**values, "flag": False})
    assert unmet == PatchResult(True, False, False)
    assert gom.patch(Ticket, "t1", set={"title": "Shut"}, where=where, bindings=values) == PatchResult(True, True, True)
    assert (gom.load(Ticket, "t1")).title == "Shut"


def test_a_user_who_may_only_read_the_graphs_tables_reads_them_with_cypher_once_they_are_made(
    pm: PersistenceManager, factory: PersistenceManagerFactory
):
    name, role = "pgstore_read_only", f"drivine_reader_{uuid.uuid4().hex[:8]}"
    drop = f"DROP TABLE IF EXISTS {name}_edge, {name}_node, {name}_schema CASCADE"
    pm.execute(QuerySpecification[Any](drop))
    owner = DatabaseRegistry()
    properties = dataclasses.replace(ConnectionProperties.from_env("postgres"), default_graph_path=name)
    owner.builder().with_properties(properties).register("owner")
    reader = DatabaseRegistry()
    reader.builder().with_properties(dataclasses.replace(properties, user_name=role, password="reader")).register(
        "reader"
    )
    try:
        (
            PersistenceManagerFactory(owner)
            .get("owner")
            .execute(QuerySpecification[Any](cypher_statement("CREATE (:PgStore:Reader {id: 'r1'})")))
        )
        pm.execute(QuerySpecification[Any](f"CREATE ROLE {role} LOGIN PASSWORD 'reader'"))
        pm.execute(QuerySpecification[Any](f"GRANT SELECT ON {name}_node, {name}_edge, {name}_schema TO {role}"))
        found = (
            PersistenceManagerFactory(reader)
            .get("reader")
            .query(QuerySpecification[Any](cypher_statement("MATCH (n:Reader) RETURN n.id")))
        )
        assert found == ["r1"]
    finally:
        owner.close()
        reader.close()
        pm.execute(QuerySpecification[Any](drop))
        pm.execute(QuerySpecification[Any](f"DROP ROLE IF EXISTS {role}"))
