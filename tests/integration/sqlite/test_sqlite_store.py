"""
drivine4j's store in a SQLite file, as Drivine keeps it: its layout, which the other libraries read as it is; what the
write function keeps of a node; moments to the nanosecond; search; writers on many connections; and the schema.
drivine4j's SqliteObjectManagerTest, SqliteSchemaManagementTest and SqliteGraphIntegrationTest, as far as the suites
over every store don't cover them already.
"""

import asyncio
import json
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Annotated, Any

import pytest

from drivine import (
    CascadeType,
    DatabaseRegistry,
    DatabaseType,
    DrivineError,
    FullTextIndex,
    NodeFragment,
    NodeId,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    StatelessGraphObjectManager,
    VectorIndex,
    above,
    case_of,
    equal_to,
    full_text_index,
    range_index,
    refs,
    uniqueness_constraint,
    vector_index,
)
from drivine.cypher.shared_compiler import compile_cypher
from drivine.query.statement import cypher_statement
from tests.integration.test_recursive_cascade import DeletableSessionTree, chain, seed
from tests.models import Organization, Person, PersonCareer, WorkHistory


class Reading(NodeFragment, labels=["Reading"]):
    id: Annotated[str, NodeId()]
    at: datetime | None = None
    value: float | None = None


class Note(NodeFragment, labels=["Note"]):
    id: Annotated[str, NodeId()]
    body: Annotated[str, FullTextIndex()] = ""


class Doc(NodeFragment, labels=["Doc"]):
    id: Annotated[str, NodeId()]
    embedding: Annotated[list[float], VectorIndex(dimensions=2)] = []


class Odd(NodeFragment, labels=['Say "hi"', "dotted.label"]):
    id: Annotated[str, NodeId()]


async def rows(pm: PersistenceManager, statement: str, **bindings: Any) -> list[Any]:
    return await pm.query(QuerySpecification[Any](statement).bind(bindings))


async def test_the_file_holds_labels_and_properties_as_json_text_and_the_stamp_as_a_column(gom, pm):
    career = await gom.save(
        PersonCareer(
            person=Person(uuid="p1", name="Zoë"),
            employment_history=[
                WorkHistory(start_date="1843", role="analyst", target=Organization(uuid="o1", name="É"))
            ],
        )
    )
    nodes = await rows(pm, "SELECT labels, props, stamp, typeof(labels) AS kind FROM drivine_node ORDER BY nid")
    assert [(it["labels"], it["props"], it["kind"]) for it in nodes] == [
        ('["Person"]', '{"uuid":"p1","name":"Zoë"}', "text"),
        ('["Organization"]', '{"uuid":"o1","name":"É"}', "text"),
    ]
    assert nodes[0]["stamp"] == career.person.stamp
    [edge] = await rows(pm, "SELECT type, props, typeof(props) AS kind FROM drivine_edge")
    assert edge == {"type": "WORKS_FOR", "props": '{"startDate":"1843","role":"analyst"}', "kind": "text"}


async def test_labels_are_written_sorted_and_unescaped_and_one_with_a_quote_or_a_dot_is_one_label(gom, pm):
    await gom.save(Odd(id="o1"))
    [row] = await rows(pm, "SELECT labels FROM drivine_node")
    assert json.loads(row["labels"]) == sorted(['Say "hi"', "dotted.label"])
    assert row["labels"] == json.dumps(sorted(['Say "hi"', "dotted.label"]), ensure_ascii=False, separators=(",", ":"))
    assert [it.id for it in await gom.load_all(Odd)] == ["o1"]
    assert await gom.load_all(Person) == []


async def test_a_property_no_save_writes_keeps_the_text_it_was_stored_with(gom, pm):
    await rows(
        pm,
        "INSERT INTO drivine_node (labels, props) VALUES ('[\"Reading\"]', $props)",
        props='{"id":"r1","value":1.50,"extra":1e3,"nested":{"x":2.0}}',
    )
    loaded = await gom.load(Reading, "r1")
    assert loaded is not None and loaded.value == 1.5
    # The same value saved again changes nothing; a new one leaves the properties the object doesn't hold as they
    # were written, and writes those it holds as it holds them.
    await gom.save(loaded)
    loaded.at = datetime(2024, 1, 1, tzinfo=UTC)
    await gom.save(loaded)
    [row] = await rows(pm, "SELECT props FROM drivine_node")
    # The moment as store layout 2 holds it, its tag (host.yaml's "Values a save writes").
    moment = '{"$t":"datetime","v":"2024-01-01T00:00:00Z","s":"10000020240101000000000000000064800"}'
    assert row["props"] == f'{{"id":"r1","value":1.5,"extra":1e3,"nested":{{"x":2.0}},"at":{moment}}}'


async def test_a_moment_held_as_text_is_a_text_to_the_query_dsl_as_cypher_has_it(gom, pm):
    base = datetime(2024, 5, 6, 7, 8, 9, 123456, tzinfo=UTC)
    # A moment held as its text, as no save writes one since store layout 2, which holds a moment as its tag.
    for id, text in (
        ("a", "2024-05-06T07:08:09.123456Z"),
        ("b", "2024-05-06T09:08:09.123457+02:00"),
        ("c", "2024-05-06T07:08:09.123458Z"),
    ):
        await rows(
            pm,
            "INSERT INTO drivine_node (labels, props) VALUES ('[\"Reading\"]', $props)",
            props=json.dumps({"id": id, "at": text}),
        )
    q = refs(Reading)
    # A text is no moment, so it meets no comparison with one, as in Cypher (the shared module's fragments).
    assert [it.id for it in await gom.load_all(Reading, q.at.gt(base))] == []
    assert [it.id for it in await gom.load_all(Reading, q.at.eq(base + timedelta(microseconds=1)))] == []
    assert [it.id for it in await gom.load_all(Reading, q.at.gte(base.astimezone(timezone(timedelta(hours=-5)))))] == []
    # And it is ordered as the text it is.
    assert [it.id for it in await gom.load_all(Reading, order_by=q.at.desc())] == ["b", "c", "a"]


class Dated(NodeFragment, labels=["Dated"]):
    id: Annotated[str, NodeId()]
    on: date | None = None
    starts: time | None = None


async def test_saved_moments_and_dates_held_as_their_tags_compare_order_and_are_among_values_as_they_are(gom):
    """Store layout 2 holds a saved temporal value as its tag: a query reads the tag's text, as it reads a text one."""
    base = datetime(2024, 5, 6, 7, 8, 9, 123456, tzinfo=UTC)
    await gom.save_all(
        [
            Reading(id="a", at=base),
            Reading(id="b", at=(base + timedelta(microseconds=1)).astimezone(timezone(timedelta(hours=2)))),
            Reading(id="c", at=base + timedelta(microseconds=2)),
        ]
    )
    q = refs(Reading)
    assert [it.id for it in await gom.load_all(Reading, q.at.gt(base))] == ["b", "c"]
    # Equal only at its own offset: the same instant at another is another value, as Neo4j has it (the spec's "Zoned
    # values at one instant", drivine-cypher-spec#161).
    later = base + timedelta(microseconds=1)
    assert [it.id for it in await gom.load_all(Reading, q.at.eq(later.astimezone(timezone(timedelta(hours=2)))))] == [
        "b"
    ]
    assert [it.id for it in await gom.load_all(Reading, q.at.eq(later))] == []
    assert [it.id for it in await gom.load_all(Reading, order_by=q.at.desc())] == ["c", "b", "a"]
    among = [base, base + timedelta(microseconds=2)]
    assert sorted(it.id for it in await gom.load_all(Reading, q.at.in_(among))) == ["a", "c"]
    await gom.save_all([Dated(id="d1", on=date(2024, 1, 1), starts=time(9)), Dated(id="d2", on=date(2024, 1, 2))])
    d = refs(Dated)
    # A time, held as its tag too, compared and listed by its text, as it was saved.
    assert [it.id for it in await gom.load_all(Dated, d.starts.eq(time(9)))] == ["d1"]
    assert [it.id for it in await gom.load_all(Dated, d.starts.in_([time(9), time(10)]))] == ["d1"]
    assert [it.id for it in await gom.load_all(Dated, d.on.eq(date(2024, 1, 2)))] == ["d2"]
    assert [it.id for it in await gom.load_all(Dated, d.on.lt(date(2024, 1, 2)))] == ["d1"]
    assert [it.id for it in await gom.load_all(Dated, d.on.in_([date(2024, 1, 1)]))] == ["d1"]
    assert [it.id for it in await gom.load_all(Dated, d.on.in_([date(2024, 1, 1), None]))] == ["d1"]
    assert [it.id for it in await gom.load_all(Dated, order_by=d.on.desc())] == ["d2", "d1"]
    # And a seek past a time reads the tag's text, as a comparison does.
    await gom.save(Dated(id="d3", starts=time(10)))
    order, seek = [d.starts.asc(), d.id.asc()], [d.starts.after(time(9)), d.id.after("d1")]
    assert [it.id for it in await gom.load_all(Dated, order_by=order, seek=seek)] == ["d3"]


async def test_a_full_text_search_scores_whole_words_ignores_case_as_unicode_does_and_leaves_out_what_it_must_not(gom):
    await gom.save_all(
        [
            Note(id="n1", body="L'ÉCOLE est là, école"),
            Note(id="n2", body="une école"),
            Note(id="n3", body="écoles"),
            Note(id="n4", body="école fermée"),
        ]
    )
    hits = await gom.load_matching(Note, "école -fermée", top_k=10)
    # Scored by BM25 (drivine4j#604), k1 1.2 and b 0.75, against the best: n1 holds the word twice in 21 characters, n2
    # once in 9, the average 48 / 4. Both hold it, so its rarity cancels: 2.2 / (1 + 1.2 * (0.25 + 0.75 * 9 / 12)) over
    # 4.4 / (2 + 1.2 * (0.25 + 0.75 * 21 / 12)).
    assert [it.value.id for it in hits] == ["n1", "n2"]
    assert hits[0].score == pytest.approx(1.0, abs=1e-9)
    assert hits[1].score == pytest.approx(0.981012658227848, abs=1e-9)


async def test_a_vector_search_measures_every_node_of_the_label_and_holds_to_its_threshold(gom):
    await gom.save_all([Doc(id="d1", embedding=[1, 0]), Doc(id="d2", embedding=[0, 1]), Doc(id="d3", embedding=[1, 1])])
    hits = await gom.load_nearest(Doc, [1, 0], top_k=3)
    assert [it.value.id for it in hits] == ["d1", "d3", "d2"]
    assert hits[0].score == pytest.approx(1.0) and hits[2].score == pytest.approx(0.5)
    assert [it.value.id for it in await gom.load_nearest(Doc, [1, 0], top_k=3, threshold=0.8)] == ["d1", "d3"]


@pytest.mark.async_only
async def test_writers_on_many_connections_save_at_once_each_waiting_its_turn_and_every_save_is_written(path: str):
    registries = [DatabaseRegistry() for _ in range(4)]
    for it in registries:
        it.builder().database_type(DatabaseType.SQLITE).database_name(path).register("sqlite")
    managers = [StatelessGraphObjectManager(PersistenceManagerFactory(it).get("sqlite")) for it in registries]
    try:
        await asyncio.gather(
            *(manager.save(Person(uuid=f"p{i}-{j}", name="n")) for i, manager in enumerate(managers) for j in range(10))
        )
        assert len(await managers[0].load_all(Person)) == 40
        deleted = await asyncio.gather(
            *(manager.delete(Person, f"p{i}-{j}") for i, manager in enumerate(managers) for j in range(10))
        )
        assert sum(deleted) == 40
    finally:
        for it in registries:
            await it.close()


async def test_the_store_is_made_once_and_made_again_keeps_what_it_holds(path: str, gom, pm):
    await gom.save(Person(uuid="p1", name="Ada"))
    other = DatabaseRegistry()
    other.builder().database_type(DatabaseType.SQLITE).database_name(path).register("sqlite")
    try:
        again = StatelessGraphObjectManager(PersistenceManagerFactory(other).get("sqlite"))
        assert [it.uuid for it in await again.load_all(Person)] == ["p1"]
    finally:
        await other.close()
    tables = await rows(pm, "SELECT name FROM sqlite_master WHERE type IN ('table', 'index') ORDER BY name")
    # Store layout 2's count store and lock table among them (store.yaml).
    assert [it["name"] for it in tables if not it["name"].startswith("sqlite_")] == [
        "drivine_count2",
        "drivine_count2_delta",
        "drivine_count2_dk",
        "drivine_edge",
        "drivine_edge_dst",
        "drivine_edge_src",
        "drivine_lock2",
        "drivine_node",
        "drivine_schema",
    ]


async def test_a_nodes_relationships_go_with_it(gom, pm):
    await gom.save(
        PersonCareer(
            person=Person(uuid="p1", name="Ada"),
            employment_history=[WorkHistory(start_date="1843", role="x", target=Organization(uuid="o1", name="E"))],
        )
    )
    assert await gom.delete(Organization, "o1") == 1
    assert await rows(pm, "SELECT COUNT(*) AS n FROM drivine_edge") == [{"n": 0}]


# ----- Compiled Cypher -----


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        (
            "MATCH (a:Linked) OPTIONAL MATCH (a)-[:NEXT]->(b) MATCH (c:Linked) RETURN [a.id, b.id, c.id]",
            [["a", "b", "a"], ["a", "b", "b"], ["a", "b", "c"]]
            + [["b", None, "a"], ["b", None, "b"], ["b", None, "c"]]
            + [["c", None, "a"], ["c", None, "b"], ["c", None, "c"]],
        ),
        (
            "MATCH (a:Linked) OPTIONAL MATCH (a)-[:NEXT]->(b) UNWIND b.tags AS t RETURN [a.id, t]",
            [["a", "x"], ["a", "y"]],
        ),
        (
            "MATCH (a:Linked) CALL { WITH a MATCH (a)-[:NEXT]->(b) RETURN b } RETURN [a.id, b.id]",
            [["a", "b"]],
        ),
    ],
    ids=["an OPTIONAL MATCH then a MATCH", "an UNWIND of an OPTIONAL MATCH's node", "a CALL that imports its row"],
)
async def test_sources_sqlite_may_reorder_still_give_cyphers_rows(pm, cypher: str, expected: list[Any]):
    # Joined ON TRUE rather than by CROSS JOIN (#391): after a LEFT JOIN, by a json_each over the node it may not find,
    # and in a lateral query, SQLite's planner must still give each row Cypher does.
    assert "CROSS JOIN" not in compile_cypher(cypher, {}, "SQLITE", "drivine").sql
    await pm.execute(
        QuerySpecification(
            cypher_statement(
                "CREATE (:Linked {id: 'a'})-[:NEXT]->(:Linked {id: 'b', tags: ['x', 'y']}), (:Linked {id: 'c'})"
            )
        )
    )
    found = await pm.query(QuerySpecification[Any](cypher_statement(cypher)))
    assert sorted(found, key=lambda it: [str(v) for v in it]) == expected


# ----- The schema -----


async def plan(pm: PersistenceManager, statement: str) -> str:
    return " ".join(str(it["detail"]) for it in await rows(pm, f"EXPLAIN QUERY PLAN {statement}"))


async def test_a_range_index_is_made_recorded_matched_again_and_read_by_a_lookup(gom, pm):
    spec = range_index("Person", "name")
    assert (await gom.indexes.ensure(spec)).status == "CREATED"
    assert (await gom.indexes.ensure(spec)).status == "ALREADY_MATCHING"
    [listed] = await gom.indexes.list("RANGE")
    assert (listed.label, listed.properties) == ("Person", ("name",))
    statement = gom.backend.load_all_fragments(gom.registry.fragment(Person), gom.backend.written("n.name = 'Ada'"))
    assert "USING INDEX drivine_ix2_Personname_" in await plan(pm, statement.text)


async def test_an_index_dropped_by_hand_is_not_listed_and_is_made_again_when_asked_for(gom, pm):
    spec = range_index("Person", "name")
    await gom.indexes.ensure(spec)
    [index] = await rows(pm, "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'drivine_ix_%'")
    await rows(pm, f"DROP INDEX {index['name']}")
    assert await gom.indexes.list("RANGE") == []
    assert (await gom.indexes.ensure(spec)).status == "CREATED"


async def test_a_uniqueness_constraint_is_enforced_for_its_label_alone_and_one_over_repeats_is_a_violation(gom):
    await gom.save_all([Person(uuid="p1", name="Ada"), Person(uuid="p2", name="Ada")])
    result = await gom.constraints.ensure(uniqueness_constraint("Person", "name"))
    assert result.status == "VIOLATION" and result.sample is not None
    [held] = result.sample
    assert held["properties"] == {"name": "Ada"} and held["count"] == 2 and len(held["nodes"]) == 2
    await gom.delete(Person, "p2")
    assert (await gom.constraints.ensure(uniqueness_constraint("Person", "name"))).status == "CREATED"
    with pytest.raises(Exception, match="UNIQUE constraint failed"):
        await gom.save(Person(uuid="p3", name="Ada"))
    await gom.save(Organization(uuid="o1", name="Ada"))
    await gom.constraints.drop(uniqueness_constraint("Person", "name"))
    await gom.save(Person(uuid="p3", name="Ada"))


async def test_a_vector_and_a_full_text_index_are_recorded_and_found_and_a_change_of_size_is_drift(gom, pm):
    assert (await gom.indexes.ensure(vector_index("Doc", "embedding", 2))).status == "CREATED"
    assert (await gom.indexes.ensure(full_text_index("Note", ["body"]))).status == "CREATED"
    assert (await gom.indexes.ensure(vector_index("Doc", "embedding", 2))).status == "ALREADY_MATCHING"
    drift = await gom.indexes.ensure(vector_index("Doc", "embedding", 3))
    assert drift.status == "DRIFT" and drift.found is not None and drift.found.dimensions == 2
    # Nothing is made for either: SQLite has no vector index, and a full-text search reads every node.
    made = await rows(pm, "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'")
    assert sorted(it["name"] for it in made) == ["drivine_count2_dk", "drivine_edge_dst", "drivine_edge_src"]


class Tagged(NodeFragment, labels=["Tagged"]):
    id: Annotated[str, NodeId()]
    code: Any = None
    size: int | None = None


async def test_a_grouped_read_orders_a_text_before_a_boolean_before_a_number_and_nulls_last_ascending(gom, pm):
    # As Cypher orders a key, as drivine4j's SQLite does (SqliteSelect.groups): through a node of the group.
    for id, props in (
        ("t1", {"code": 2, "size": 5}),
        ("t2", {"code": "b"}),
        ("t3", {"code": True, "size": 1}),
        ("t4", {"code": 10, "size": 3}),
        ("t5", {"code": "a", "size": 2}),
        ("t6", {}),
    ):
        await rows(
            pm,
            "INSERT INTO drivine_node (labels, props) VALUES ('[\"Tagged\"]', $props)",
            props=json.dumps({"id": id, **props}),
        )
    from drivine import count, key, max_

    def codes(found: list[dict[str, Any]]) -> list[Any]:
        return [it["code"] for it in found]

    keyed = {"code": key("code")}
    assert codes(await gom.groups.load(Tagged, keys=keyed, aggregates={"n": count()})) == ["a", "b", True, 2, 10, None]
    descending = await gom.groups.load(Tagged, keys=keyed, aggregates={"n": count()}, order_by=[("code", "desc")])
    assert codes(descending) == [None, 10, 2, True, "b", "a"]
    biggest = {"n": count(), "biggest": max_("size")}
    by_size = await gom.groups.load(Tagged, keys=keyed, aggregates=biggest, order_by=[("biggest", "asc")])
    assert [it["biggest"] for it in by_size] == [1, 2, 3, 5, None, None]
    by_size = await gom.groups.load(Tagged, keys=keyed, aggregates=biggest, order_by=[("biggest", "desc")])
    assert [it["biggest"] for it in by_size] == [None, None, 5, 3, 2, 1]


class Flagged(NodeFragment, labels=["Flagged"]):
    id: Annotated[str, NodeId()]
    flag: bool = False
    note: str = ""


async def test_a_patch_where_a_boolean_is_bound_meets_the_node_a_load_with_the_same_where_finds(gom):
    await gom.save(Flagged(id="f1", flag=True))
    where = "(n.props ->> '$.\"flag\"') = $flag"
    assert [it.id for it in await gom.load_all(Flagged, where, {"flag": True})] == ["f1"]
    patched = await gom.patch(Flagged, "f1", set={"note": "met"}, where=where, bindings={"flag": True})
    assert patched.applied
    loaded = await gom.load(Flagged, "f1")
    assert loaded is not None and loaded.note == "met"


async def test_a_property_compared_in_the_shorthand_with_a_bound_boolean_meets_the_nodes_that_hold_it(gom):
    # drivine-python#542: SQLite binds True as 1, and the shorthand reads `n.flag` as the text "true", so a load, a
    # count, a delete and a patch found nothing; the shorthand compares it with the boolean's text, as `= true`.
    from drivine import count, key

    await gom.save_all([Flagged(id="b1", flag=True), Flagged(id="b2", flag=False)])
    where = "n.flag = $flag"
    assert [it.id for it in await gom.load_all(Flagged, where, {"flag": True})] == ["b1"]
    assert [it.id for it in await gom.load_all(Flagged, "n.flag <> $flag", {"flag": True})] == ["b2"]
    assert [it.id for it in await gom.load_all(Flagged, "n.flag != $flag", {"flag": False})] == ["b1"]
    flagged = {"flagged": count(where)}
    keyed = {"id": key("id")}
    counted = await gom.groups.load(
        Flagged, keys=keyed, aggregates=flagged, bindings={"flag": True}, order_by=[("id", "asc")]
    )
    assert counted == [{"id": "b1", "flagged": 1}, {"id": "b2", "flagged": 0}]
    assert await gom.count(Flagged, where, {"flag": False}) == 1
    patched = await gom.patch(Flagged, "b1", set={"note": "met"}, where=where, bindings={"flag": True})
    assert patched.applied
    assert not (await gom.patch(Flagged, "b1", set={"note": "missed"}, where=where, bindings={"flag": False})).applied
    groups = await gom.groups.load(Flagged, where=where, bindings={"flag": True}, keys=keyed)
    assert groups == [{"id": "b1"}]
    assert await gom.delete_all(Flagged, where, {"flag": False}) == 1
    assert [it.id for it in await gom.load_all(Flagged)] == ["b1"]


class Opened(NodeFragment, labels=["Opened"]):
    id: Annotated[str, NodeId()]
    at: datetime | None = None
    open: bool | None = None
    flags: list[bool] | None = None
    days: list[datetime] | None = None
    count: int | None = None


async def test_the_query_dsl_compares_seeks_orders_and_patches_a_boolean_and_a_moment_as_a_save_stores_them(gom):
    # The DSL is the shared module's fragments, a patch's condition written into the statement the write function
    # runs, each value as a literal. A port of Drivine for TypeScript's SqliteStore.spec.
    first, second = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
    await gom.save_all(
        [
            Opened(id="m1", at=first, open=True, flags=[True], days=[first], count=1),
            Opened(id="m2", at=second, open=False, flags=[False], days=[second], count=2),
        ]
    )
    q = refs(Opened)

    async def found(where: Any) -> list[str]:
        return sorted(it.id for it in await gom.load_all(Opened, where))

    assert await found(q.at.eq(first)) == ["m1"]
    assert await found(q.at.gt(first)) == ["m2"]
    assert await found(q.at.in_([second])) == ["m2"]
    assert await found(q.open.eq(True)) == ["m1"]
    assert await found(q.open.in_([False])) == ["m2"]
    assert await found(q.open.not_in([False])) == ["m1"]
    assert await found(q.flags.has_item(True)) == ["m1"]
    assert await found(q.days.has_item(second)) == ["m2"]
    assert [it.id for it in await gom.load_all(Opened, order_by=q.at.asc(), seek=q.at.after(first))] == ["m2"]
    assert [it.id for it in await gom.load_all(Opened, order_by=q.open.asc(), seek=q.open.after(False))] == ["m1"]
    ordered = case_of([equal_to(q.open, False).then(0), above(q.at, first).then(1)], 2).asc()
    assert [it.id for it in await gom.load_all(Opened, order_by=ordered)] == ["m2", "m1"]

    async def patched(where: Any) -> bool:
        return (await gom.patch(Opened, "m1", set={"count": 3}, where=where)).applied

    assert await patched(q.at.eq(first))
    assert await patched(q.open.eq(True))
    assert await patched(q.open.in_([True]))
    assert await patched(q.days.has_item(first))
    assert not await patched(q.open.eq(False))


async def test_a_cascade_and_its_depth_check_read_the_dsls_where_over_the_roots_row_n_(gom, pm):
    # The roots are the row n_, which the DSL's fragments read; what the cascade reaches from them is SQL over n.
    await seed(pm, chain(4))
    q = refs(DeletableSessionTree)
    for cascade in (CascadeType.DELETE_ALL, CascadeType.DELETE_ORPHAN):
        with pytest.raises(DrivineError, match="reaches 3 levels deep"):
            await gom.delete(DeletableSessionTree, "rc-s0", where=q.session.id.eq("rc-s0"), cascade=cascade)
        assert await gom.delete(DeletableSessionTree, "rc-s0", where=q.session.id.eq("other"), cascade=cascade) == 0
    assert (
        await gom.delete(DeletableSessionTree, "rc-s1", where=q.session.id.eq("rc-s1"), cascade=CascadeType.DELETE_ALL)
        == 8
    )


async def test_a_raw_wheres_binding_named_as_the_dsls_own_keeps_its_value_beside_a_seek(gom):
    # The DSL's values are bound under the shared module's names (`_cy__q0`), so a caller's `_q0` is the caller's.
    # Were the seek's value bound as `_q0` too, the where would keep every node but "a", and find "b" and "c".
    await gom.save_all([Reading(id=it) for it in ("a", "b", "c")])
    q = refs(Reading)
    found = await gom.load_all(
        Reading, "n.id <> $_q0", bindings={"_q0": "b"}, order_by=q.id.asc(), seek=q.id.after("a")
    )
    assert [it.id for it in found] == ["c"]
