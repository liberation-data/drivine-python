# Written by scripts/unasync.py from tests/integration/sqlite/test_sqlite_store.py:
# change that, and run the script again.
"""
drivine4j's store in a SQLite file, as Drivine keeps it: its layout, which the other libraries read as it is; what the
write function keeps of a node; moments to the nanosecond; search; writers on many connections; and the schema.
drivine4j's SqliteObjectManagerTest, SqliteSchemaManagementTest and SqliteGraphIntegrationTest, as far as the suites
over every store don't cover them already.
"""

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated, Any

import pytest

from drivine.cypher.shared_compiler import compile_cypher
from drivine.query.statement import cypher_statement
from drivine.sync import (
    DatabaseRegistry,
    DatabaseType,
    FullTextIndex,
    NodeFragment,
    NodeId,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    StatelessGraphObjectManager,
    VectorIndex,
    full_text_index,
    range_index,
    refs,
    uniqueness_constraint,
    vector_index,
)
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


def rows(pm: PersistenceManager, statement: str, **bindings: Any) -> list[Any]:
    return pm.query(QuerySpecification[Any](statement).bind(bindings))


def test_the_file_holds_labels_and_properties_as_json_text_and_the_stamp_as_a_column(gom, pm):
    career = gom.save(
        PersonCareer(
            person=Person(uuid="p1", name="Zoë"),
            employment_history=[
                WorkHistory(start_date="1843", role="analyst", target=Organization(uuid="o1", name="É"))
            ],
        )
    )
    nodes = rows(pm, "SELECT labels, props, stamp, typeof(labels) AS kind FROM drivine_node ORDER BY nid")
    assert [(it["labels"], it["props"], it["kind"]) for it in nodes] == [
        ('["Person"]', '{"uuid":"p1","name":"Zoë"}', "text"),
        ('["Organization"]', '{"uuid":"o1","name":"É"}', "text"),
    ]
    assert nodes[0]["stamp"] == career.person.stamp
    [edge] = rows(pm, "SELECT type, props, typeof(props) AS kind FROM drivine_edge")
    assert edge == {"type": "WORKS_FOR", "props": '{"startDate":"1843","role":"analyst"}', "kind": "text"}


def test_labels_are_written_sorted_and_unescaped_and_one_with_a_quote_or_a_dot_is_one_label(gom, pm):
    gom.save(Odd(id="o1"))
    [row] = rows(pm, "SELECT labels FROM drivine_node")
    assert json.loads(row["labels"]) == sorted(['Say "hi"', "dotted.label"])
    assert row["labels"] == json.dumps(sorted(['Say "hi"', "dotted.label"]), ensure_ascii=False, separators=(",", ":"))
    assert [it.id for it in gom.load_all(Odd)] == ["o1"]
    assert gom.load_all(Person) == []


def test_a_property_no_save_writes_keeps_the_text_it_was_stored_with(gom, pm):
    rows(
        pm,
        "INSERT INTO drivine_node (labels, props) VALUES ('[\"Reading\"]', $props)",
        props='{"id":"r1","value":1.50,"extra":1e3,"nested":{"x":2.0}}',
    )
    loaded = gom.load(Reading, "r1")
    assert loaded is not None and loaded.value == 1.5
    # The same value saved again changes nothing; a new one leaves the properties the object doesn't hold as they
    # were written, and writes those it holds as it holds them.
    gom.save(loaded)
    loaded.at = datetime(2024, 1, 1, tzinfo=UTC)
    gom.save(loaded)
    [row] = rows(pm, "SELECT props FROM drivine_node")
    assert row["props"] == '{"id":"r1","value":1.5,"extra":1e3,"nested":{"x":2.0},"at":"2024-01-01T00:00:00.000Z"}'


def test_moments_a_microsecond_apart_order_compare_and_seek_as_the_moments_they_are(gom, pm):
    base = datetime(2024, 5, 6, 7, 8, 9, 123456, tzinfo=UTC)
    # As drivine4j writes a moment: to the microsecond, at one offset or another.
    for id, text in (
        ("a", "2024-05-06T07:08:09.123456Z"),
        ("b", "2024-05-06T09:08:09.123457+02:00"),
        ("c", "2024-05-06T07:08:09.123458Z"),
    ):
        rows(
            pm,
            "INSERT INTO drivine_node (labels, props) VALUES ('[\"Reading\"]', $props)",
            props=json.dumps({"id": id, "at": text}),
        )
    q = refs(Reading)
    assert [it.id for it in gom.load_all(Reading, q.at.gt(base))] == ["b", "c"]
    assert [it.id for it in gom.load_all(Reading, q.at.eq(base + timedelta(microseconds=1)))] == ["b"]
    assert [it.id for it in gom.load_all(Reading, order_by=q.at.desc())] == ["c", "b", "a"]
    later = base.astimezone(timezone(timedelta(hours=-5)))
    assert [it.id for it in gom.load_all(Reading, q.at.gte(later))] == ["a", "b", "c"]


def test_a_full_text_search_scores_whole_words_ignores_case_as_unicode_does_and_leaves_out_what_it_must_not(gom):
    gom.save_all(
        [
            Note(id="n1", body="L'ÉCOLE est là, école"),
            Note(id="n2", body="une école"),
            Note(id="n3", body="écoles"),
            Note(id="n4", body="école fermée"),
        ]
    )
    hits = gom.load_matching(Note, "école -fermée", top_k=10)
    # Scored by BM25 (drivine4j#604), k1 1.2 and b 0.75, against the best: n1 holds the word twice in 21 characters, n2
    # once in 9, the average 48 / 4. Both hold it, so its rarity cancels: 2.2 / (1 + 1.2 * (0.25 + 0.75 * 9 / 12)) over
    # 4.4 / (2 + 1.2 * (0.25 + 0.75 * 21 / 12)).
    assert [it.value.id for it in hits] == ["n1", "n2"]
    assert hits[0].score == pytest.approx(1.0, abs=1e-9)
    assert hits[1].score == pytest.approx(0.981012658227848, abs=1e-9)


def test_a_vector_search_measures_every_node_of_the_label_and_holds_to_its_threshold(gom):
    gom.save_all([Doc(id="d1", embedding=[1, 0]), Doc(id="d2", embedding=[0, 1]), Doc(id="d3", embedding=[1, 1])])
    hits = gom.load_nearest(Doc, [1, 0], top_k=3)
    assert [it.value.id for it in hits] == ["d1", "d3", "d2"]
    assert hits[0].score == pytest.approx(1.0) and hits[2].score == pytest.approx(0.5)
    assert [it.value.id for it in gom.load_nearest(Doc, [1, 0], top_k=3, threshold=0.8)] == ["d1", "d3"]


def test_the_store_is_made_once_and_made_again_keeps_what_it_holds(path: str, gom, pm):
    gom.save(Person(uuid="p1", name="Ada"))
    other = DatabaseRegistry()
    other.builder().database_type(DatabaseType.SQLITE).database_name(path).register("sqlite")
    try:
        again = StatelessGraphObjectManager(PersistenceManagerFactory(other).get("sqlite"))
        assert [it.uuid for it in again.load_all(Person)] == ["p1"]
    finally:
        other.close()
    tables = rows(pm, "SELECT name FROM sqlite_master WHERE type IN ('table', 'index') ORDER BY name")
    assert [it["name"] for it in tables if not it["name"].startswith("sqlite_")] == [
        "drivine_edge",
        "drivine_edge_dst",
        "drivine_edge_src",
        "drivine_node",
        "drivine_schema",
    ]


def test_a_nodes_relationships_go_with_it(gom, pm):
    gom.save(
        PersonCareer(
            person=Person(uuid="p1", name="Ada"),
            employment_history=[WorkHistory(start_date="1843", role="x", target=Organization(uuid="o1", name="E"))],
        )
    )
    assert gom.delete(Organization, "o1") == 1
    assert rows(pm, "SELECT COUNT(*) AS n FROM drivine_edge") == [{"n": 0}]


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
def test_sources_sqlite_may_reorder_still_give_cyphers_rows(pm, cypher: str, expected: list[Any]):
    # Joined ON TRUE rather than by CROSS JOIN (#391): after a LEFT JOIN, by a json_each over the node it may not find,
    # and in a lateral query, SQLite's planner must still give each row Cypher does.
    assert "CROSS JOIN" not in compile_cypher(cypher, {}, "SQLITE", "drivine").sql
    pm.execute(
        QuerySpecification(
            cypher_statement(
                "CREATE (:Linked {id: 'a'})-[:NEXT]->(:Linked {id: 'b', tags: ['x', 'y']}), (:Linked {id: 'c'})"
            )
        )
    )
    found = pm.query(QuerySpecification[Any](cypher_statement(cypher)))
    assert sorted(found, key=lambda it: [str(v) for v in it]) == expected


# ----- The schema -----


def plan(pm: PersistenceManager, statement: str) -> str:
    return " ".join(str(it["detail"]) for it in rows(pm, f"EXPLAIN QUERY PLAN {statement}"))


def test_a_range_index_is_made_recorded_matched_again_and_read_by_a_lookup(gom, pm):
    spec = range_index("Person", "name")
    assert (gom.indexes.ensure(spec)).status == "CREATED"
    assert (gom.indexes.ensure(spec)).status == "ALREADY_MATCHING"
    [listed] = gom.indexes.list("RANGE")
    assert (listed.label, listed.properties) == ("Person", ("name",))
    statement = gom.backend.load_all_fragments(gom.registry.fragment(Person), gom.backend.written("n.name = 'Ada'"))
    assert "USING INDEX drivine_ix_Personname_" in plan(pm, statement.text)


def test_an_index_dropped_by_hand_is_not_listed_and_is_made_again_when_asked_for(gom, pm):
    spec = range_index("Person", "name")
    gom.indexes.ensure(spec)
    [index] = rows(pm, "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'drivine_ix_%'")
    rows(pm, f"DROP INDEX {index['name']}")
    assert gom.indexes.list("RANGE") == []
    assert (gom.indexes.ensure(spec)).status == "CREATED"


def test_a_uniqueness_constraint_is_enforced_for_its_label_alone_and_one_over_repeats_is_a_violation(gom):
    gom.save_all([Person(uuid="p1", name="Ada"), Person(uuid="p2", name="Ada")])
    result = gom.constraints.ensure(uniqueness_constraint("Person", "name"))
    assert result.status == "VIOLATION" and result.sample is not None
    [held] = result.sample
    assert held["properties"] == {"name": "Ada"} and held["count"] == 2 and len(held["nodes"]) == 2
    gom.delete(Person, "p2")
    assert (gom.constraints.ensure(uniqueness_constraint("Person", "name"))).status == "CREATED"
    with pytest.raises(Exception, match="UNIQUE constraint failed"):
        gom.save(Person(uuid="p3", name="Ada"))
    gom.save(Organization(uuid="o1", name="Ada"))
    gom.constraints.drop(uniqueness_constraint("Person", "name"))
    gom.save(Person(uuid="p3", name="Ada"))


def test_a_vector_and_a_full_text_index_are_recorded_and_found_and_a_change_of_size_is_drift(gom, pm):
    assert (gom.indexes.ensure(vector_index("Doc", "embedding", 2))).status == "CREATED"
    assert (gom.indexes.ensure(full_text_index("Note", ["body"]))).status == "CREATED"
    assert (gom.indexes.ensure(vector_index("Doc", "embedding", 2))).status == "ALREADY_MATCHING"
    drift = gom.indexes.ensure(vector_index("Doc", "embedding", 3))
    assert drift.status == "DRIFT" and drift.found is not None and drift.found.dimensions == 2
    # Nothing is made for either: SQLite has no vector index, and a full-text search reads every node.
    made = rows(pm, "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'")
    assert sorted(it["name"] for it in made) == ["drivine_edge_dst", "drivine_edge_src"]


class Tagged(NodeFragment, labels=["Tagged"]):
    id: Annotated[str, NodeId()]
    code: Any = None
    size: int | None = None


def test_a_grouped_read_orders_a_text_before_a_number_before_a_boolean_and_nulls_last_ascending(gom, pm):
    # As Postgres's jsonb, the graph engines and drivine4j's SQLite (SqliteSelect.groupOrder) order a group.
    for id, props in (
        ("t1", {"code": 2, "size": 5}),
        ("t2", {"code": "b"}),
        ("t3", {"code": True, "size": 1}),
        ("t4", {"code": 10, "size": 3}),
        ("t5", {"code": "a", "size": 2}),
        ("t6", {}),
    ):
        rows(
            pm,
            "INSERT INTO drivine_node (labels, props) VALUES ('[\"Tagged\"]', $props)",
            props=json.dumps({"id": id, **props}),
        )
    from drivine.sync import count, key, max_

    def codes(found: list[dict[str, Any]]) -> list[Any]:
        return [it["code"] for it in found]

    keyed = {"code": key("code")}
    assert codes(gom.groups.load(Tagged, keys=keyed, aggregates={"n": count()})) == ["a", "b", 2, 10, True, None]
    descending = gom.groups.load(Tagged, keys=keyed, aggregates={"n": count()}, order_by=[("code", "desc")])
    assert codes(descending) == [None, True, 10, 2, "b", "a"]
    biggest = {"n": count(), "biggest": max_("size")}
    by_size = gom.groups.load(Tagged, keys=keyed, aggregates=biggest, order_by=[("biggest", "asc")])
    assert [it["biggest"] for it in by_size] == [1, 2, 3, 5, None, None]
    by_size = gom.groups.load(Tagged, keys=keyed, aggregates=biggest, order_by=[("biggest", "desc")])
    assert [it["biggest"] for it in by_size] == [None, None, 5, 3, 2, 1]


class Flagged(NodeFragment, labels=["Flagged"]):
    id: Annotated[str, NodeId()]
    flag: bool = False
    note: str = ""


def test_a_patch_where_a_boolean_is_bound_meets_the_node_a_load_with_the_same_where_finds(gom):
    gom.save(Flagged(id="f1", flag=True))
    where = "(n.props ->> '$.\"flag\"') = $flag"
    assert [it.id for it in gom.load_all(Flagged, where, {"flag": True})] == ["f1"]
    patched = gom.patch(Flagged, "f1", set={"note": "met"}, where=where, bindings={"flag": True})
    assert patched.applied
    loaded = gom.load(Flagged, "f1")
    assert loaded is not None and loaded.note == "met"
