# Written by scripts/unasync.py from tests/integration/sqlite/test_sqlite_groups.py:
# change that, and run the script again.
"""
A grouped read run on SQLite, which needs no container: a property key ordered as Cypher orders it, read through a node
of the group that has it (drivine4j's `SqliteSelect.groups`), and a least or greatest value of its own kind, read back
as the type its field declares. A port of Drivine for TypeScript's SqliteGroupsRun.spec, with drivine-python#537.
"""

import json
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

import pytest

from drivine.query.statement import cypher_statement
from drivine.sync import (
    Hop,
    NodeFragment,
    NodeId,
    PersistenceManager,
    QuerySpecification,
    StatelessGraphObjectManager,
    count,
    key,
    max_,
    min_,
    path_key,
    refs,
)


class Item(NodeFragment, labels=["GroupRun", "Item"]):
    id: Annotated[str, NodeId()]
    value: Any = None
    name: str | None = None
    at: datetime | None = None
    on: date | None = None
    rank: int | None = None
    shelf: str | None = None


class Shelf(NodeFragment, labels=["GroupRun", "Shelf"]):
    id: Annotated[Any, NodeId()]


def insert(pm: PersistenceManager, values: list[Any]) -> None:
    """A node of each value, as its JSON, `None` for none: a moment as the tag a save writes, its text first."""
    for i, value in enumerate(values):
        props = {"id": f"i{i}", **({"value": value} if value is not None else {})}
        pm.execute(
            QuerySpecification[Any](
                'INSERT INTO drivine_node (labels, props) VALUES (\'["GroupRun","Item"]\', $props)'
            ).bind({"props": json.dumps(props)})
        )


def moment(v: str, s: str) -> dict[str, str]:
    """A moment's tag as a save writes it: its text, then its sort key."""
    return {"$t": "datetime", "v": v, "s": s}


MOMENTS = [
    moment("2024-01-01T00:00:00.5Z", "10000020240101000000500000000064800"),
    moment("2024-01-01T00:00:00Z", "10000020240101000000000000000064800"),
    moment("2024-01-01T01:00:00+02:00", "10000020231231230000000000000072000"),
]
"""Three moments whose texts sort otherwise than they do: `.5Z` before `Z` by its text, and the offset out of place."""


def firsts(gom: StatelessGraphObjectManager, direction: Literal["asc", "desc"]) -> list[Any]:
    """The groups' keys of `value`, ordered by it `direction`, as the least id of each."""
    groups = gom.groups.load(
        Item,
        keys={"value": key("value")},
        aggregates={"first": min_("id"), "all": count()},
        order_by=[("value", direction)],
    )
    return [it["first"] for it in groups]


def test_orders_a_key_of_moments_as_cypher_orders_them_by_the_moment_an_offsets_included(gom, pm):
    insert(pm, MOMENTS)
    assert firsts(gom, "asc") == ["i2", "i1", "i0"]
    assert firsts(gom, "desc") == ["i0", "i1", "i2"]


def test_orders_a_key_of_mixed_kinds_as_cypher_orders_them_a_text_a_boolean_a_number_and_no_value_last(gom, pm):
    insert(pm, ["b", True, 10, 9, None, False, "a"])
    assert firsts(gom, "asc") == ["i6", "i0", "i5", "i1", "i3", "i2", "i4"]
    assert firsts(gom, "desc") == ["i4", "i2", "i3", "i1", "i5", "i0", "i6"]


def test_orders_the_groups_by_a_key_it_was_not_asked_to_order_by_a_tie_broken_so_too(gom, pm):
    insert(pm, MOMENTS[:2])
    groups = gom.groups.load(Item, keys={"value": key("value")}, aggregates={"first": min_("id")})
    assert [it["first"] for it in groups] == ["i1", "i0"]


def test_gives_a_least_and_greatest_text_and_moment_of_its_own_kind_in_cyphers_order_not_as_a_number(gom):
    gom.save_all(
        [
            Item(id="a", shelf="one", name="pear", at=datetime(2026, 3, 1, tzinfo=UTC)),
            Item(id="b", shelf="one", name="apple", at=datetime(2026, 1, 1, tzinfo=UTC)),
            Item(id="c", shelf="one", name="quince", at=datetime(2026, 2, 1, tzinfo=UTC)),
            Item(id="d", shelf="one"),
        ]
    )
    [group] = gom.groups.load(
        Item,
        keys={"shelf": key("shelf")},
        aggregates={"first": min_("name"), "last": max_("name"), "earliest": min_("at"), "latest": max_("at")},
    )
    # A moment is given as its text, Cypher's, as the store gives it.
    assert group == {
        "shelf": "one",
        "first": "apple",
        "last": "quince",
        "earliest": "2026-01-01T00:00:00Z",
        "latest": "2026-03-01T00:00:00Z",
    }


def shelves(gom: StatelessGraphObjectManager, direction: Literal["asc", "desc"]) -> list[Any]:
    """The groups of each item's shelf, ordered by it `direction`, as the shelves' ids."""
    groups = gom.groups.load(
        Item, keys={"shelf": path_key(Shelf, [Hop("ON")])}, aggregates={"all": count()}, order_by=[("shelf", direction)]
    )
    return [it["shelf"].id for it in groups]


def test_orders_a_path_key_by_its_nodes_id_as_cypher_orders_it_a_text_before_a_number(gom, pm):
    pm.execute(
        QuerySpecification[Any](
            cypher_statement(
                "CREATE (:GroupRun:Item {id: 'i0'})-[:ON]->(:GroupRun:Shelf {id: 10}), "
                "(:GroupRun:Item {id: 'i1'})-[:ON]->(:GroupRun:Shelf {id: 'b'}), "
                "(:GroupRun:Item {id: 'i2'})-[:ON]->(:GroupRun:Shelf {id: 9})"
            )
        )
    )
    assert shelves(gom, "asc") == ["b", 9, 10]
    assert shelves(gom, "desc") == [10, 9, "b"]


def test_orders_a_path_key_whose_nodes_id_is_a_moment_by_the_moment_not_by_its_tags_text(gom, pm):
    for i, id in enumerate(MOMENTS):
        pm.execute(
            QuerySpecification[Any](
                cypher_statement(f"CREATE (:GroupRun:Item {{id: 'i{i}'}})-[:ON]->(:GroupRun:Shelf {{id: 's{i}'}})")
            )
        )
        # The moment's tag as a save writes it, its text first.
        pm.execute(
            QuerySpecification[Any]("UPDATE drivine_node SET props = $props WHERE props ->> '$.id' = $was").bind(
                {"props": json.dumps({"id": id}), "was": f"s{i}"}
            )
        )

    def instants(direction: Literal["asc", "desc"]) -> list[datetime]:
        return [
            (it if isinstance(it, datetime) else datetime.fromisoformat(str(it))).astimezone(UTC)
            for it in shelves(gom, direction)
        ]

    expected = [
        datetime(2023, 12, 31, 23, tzinfo=UTC),
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, 0, 0, 0, 500000, tzinfo=UTC),
    ]
    assert instants("asc") == expected
    assert instants("desc") == expected[::-1]


def test_compares_orders_and_filters_a_least_or_greatest_value_at_its_node_before_the_page_is_taken(gom):
    gom.save_all(
        [
            Item(id="a", shelf="one", rank=3, name="x"),
            Item(id="b", shelf="one", rank=7, name="y"),
            Item(id="c", shelf="two", rank=5, name="x"),
            Item(id="d", shelf="three"),
            Item(id="e", shelf="four", rank=1, name="y"),
        ]
    )

    def top(**spec: Any) -> list[dict[str, Any]]:
        return gom.groups.load(Item, keys={"shelf": key("shelf")}, aggregates={"top": max_("rank")}, **spec)

    # A group with no value is last ascending and first descending, as Cypher orders a null.
    assert top(order_by=[("top", "asc")]) == [
        {"shelf": "four", "top": 1},
        {"shelf": "two", "top": 5},
        {"shelf": "one", "top": 7},
        {"shelf": "three", "top": None},
    ]
    assert [it["shelf"] for it in top(order_by=[("top", "desc")])] == ["three", "one", "two", "four"]
    # The having is a condition over the joined node, outside the grouping, and the page is taken after it.
    kept: dict[str, Any] = {"having": [("top", "gte", 5)], "order_by": [("shelf", "asc")]}
    assert top(**kept) == [{"shelf": "one", "top": 7}, {"shelf": "two", "top": 5}]
    assert top(**kept, skip=1, limit=1) == [{"shelf": "two", "top": 5}]
    # A filtered least value is of the nodes that meet the filter: none in a group is no value.
    q = refs(Item)
    filtered = gom.groups.load(
        Item, keys={"shelf": key(q.shelf)}, aggregates={"low": min_(q.rank, q.name.eq("y"))}, order_by=[("low", "asc")]
    )
    assert filtered == [
        {"shelf": "four", "low": 1},
        {"shelf": "one", "low": 7},
        {"shelf": "three", "low": None},
        {"shelf": "two", "low": None},
    ]


@pytest.mark.xfail(
    strict=True,
    reason="A raw where is SQL, which the module's fragments don't write: n.at reads the tag layout 2 holds a moment "
    "as, and the moment is written in as its ISO text, so the two are never equal (drivine#782).",
)
def test_a_raw_where_compares_a_moment_as_a_save_stores_it(gom):
    first = datetime(2026, 1, 1, tzinfo=UTC)
    gom.save(Item(id="m1", at=first))
    patched = gom.patch(Item, "m1", set={"rank": 3}, where="n.at = $when", bindings={"when": first})
    assert patched.applied


def test_not_in_holds_for_a_node_without_the_property_only_where_the_list_is_empty_as_in_cypher(gom):
    # The DSL is the shared module's on SQLite: `NOT (null IN [])` is true, and `NOT (null IN ['x'])` is null.
    gom.save_all([Item(id="a", name="x"), Item(id="b")])
    q = refs(Item)
    assert sorted(it.id for it in gom.load_all(Item, q.name.not_in([]))) == ["a", "b"]
    assert [it.id for it in gom.load_all(Item, q.name.not_in(["y"]))] == ["a"]
    assert [it.id for it in gom.load_all(Item, q.name.not_in(["x"]))] == []
