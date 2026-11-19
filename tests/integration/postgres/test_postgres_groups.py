"""
A grouped read run on Postgres, as test_sqlite_groups runs one on SQLite (drivine-cypher-spec#235, step 3): a property
key ordered as Cypher orders it, read through a node of the group that has it, and a least or greatest value of its own
kind, given as the group's column and compared and ordered at its node (drivine4j's `PostgresSelect.groups`), read back
as the type its field declares. test_postgres_query_dsl pins the SQL. A port of Drivine for TypeScript's
PostgresGroupsRun.spec.
"""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

import pytest

from drivine import (
    Hop,
    NodeFragment,
    NodeId,
    PersistenceManager,
    StatelessGraphObjectManager,
    count,
    key,
    max_,
    min_,
    path_key,
    refs,
)
from tests.integration.postgres.conftest import ON_POSTGRES
from tests.integration.sql.sql_seed import SqlSeed

pytestmark = ON_POSTGRES


class Item(NodeFragment, labels=["PgGroupRun", "Item"]):
    id: Annotated[str, NodeId()]
    value: Any = None
    name: str | None = None
    at: datetime | None = None
    on: date | None = None
    rank: int | None = None
    shelf: str | None = None


class Shelf(NodeFragment, labels=["PgGroupRun", "Shelf"]):
    id: Annotated[Any, NodeId()]


@pytest.fixture
async def seed(pm: PersistenceManager) -> AsyncIterator[SqlSeed]:
    seed = SqlSeed(pm, "PgGroupRun")
    await seed.clear()
    yield seed
    await seed.clear()


async def create(seed: SqlSeed, values: list[Any]) -> None:
    """A node of each value, as its JSON, `None` for none: a moment as the tag a save writes, its text first."""
    for i, value in enumerate(values):
        await seed.node(["Item"], {"id": f"i{i}", **({"value": value} if value is not None else {})})


def moment(v: str, s: str) -> dict[str, str]:
    """A moment's tag as a save writes it: its text, then its sort key."""
    return {"$t": "datetime", "v": v, "s": s}


MOMENTS = [
    moment("2024-01-01T00:00:00.5Z", "10000020240101000000500000000064800"),
    moment("2024-01-01T00:00:00Z", "10000020240101000000000000000064800"),
    moment("2024-01-01T01:00:00+02:00", "10000020231231230000000000000072000"),
]
"""Three moments whose texts sort otherwise than they do: `.5Z` before `Z` by its text, and the offset out of place."""


async def firsts(gom: StatelessGraphObjectManager, direction: Literal["asc", "desc"]) -> list[Any]:
    """The groups' keys of `value`, ordered by it `direction`, as the least id of each."""
    groups = await gom.groups.load(
        Item,
        keys={"value": key("value")},
        aggregates={"first": min_("id"), "all": count()},
        order_by=[("value", direction)],
    )
    return [it["first"] for it in groups]


async def test_orders_a_key_of_moments_as_cypher_orders_them_by_the_moment_an_offsets_included(gom, seed):
    await create(seed, MOMENTS)
    assert await firsts(gom, "asc") == ["i2", "i1", "i0"]
    assert await firsts(gom, "desc") == ["i0", "i1", "i2"]


async def test_orders_a_key_of_mixed_kinds_as_cypher_orders_them_a_text_a_boolean_a_number_and_no_value_last(gom, seed):
    await create(seed, ["b", True, 10, 9, None, False, "a"])
    assert await firsts(gom, "asc") == ["i6", "i0", "i5", "i1", "i3", "i2", "i4"]
    assert await firsts(gom, "desc") == ["i4", "i2", "i3", "i1", "i5", "i0", "i6"]


async def test_orders_the_groups_by_a_key_it_was_not_asked_to_order_by_a_tie_broken_so_too(gom, seed):
    await create(seed, MOMENTS[:2])
    groups = await gom.groups.load(Item, keys={"value": key("value")}, aggregates={"first": min_("id")})
    assert [it["first"] for it in groups] == ["i1", "i0"]


async def test_gives_a_least_and_greatest_text_date_and_moment_as_the_type_its_field_declares(gom, seed):
    at = datetime(2026, 1, 2, 3, 4, 5, 123000, tzinfo=UTC)
    await gom.save_all(
        [
            Item(id="a", shelf="one", name="pear", at=datetime(2026, 1, 1, tzinfo=UTC), on=date(2026, 1, 1), rank=1),
            Item(id="b", shelf="one", name="apple", at=at, on=date(2026, 1, 2), rank=5),
            Item(id="c", shelf="one", name="quince"),
        ]
    )
    q = refs(Item)
    [group] = await gom.groups.load(
        Item,
        keys={"shelf": key(q.shelf)},
        aggregates={
            "first": min_(q.name),
            "last": max_(q.name),
            "earliest": min_(q.at),
            "latest": max_(q.at),
            "day": max_(q.on),
            "rank": max_(q.rank),
        },
    )
    assert group == {
        "shelf": "one",
        "first": "apple",
        "last": "quince",
        "earliest": datetime(2026, 1, 1, tzinfo=UTC),
        "latest": at,
        "day": date(2026, 1, 2),
        "rank": 5,
    }


async def shelves(gom: StatelessGraphObjectManager, direction: Literal["asc", "desc"]) -> list[Any]:
    """The groups of each item's shelf, ordered by it `direction`, as the shelves' ids."""
    groups = await gom.groups.load(
        Item, keys={"shelf": path_key(Shelf, [Hop("ON")])}, aggregates={"all": count()}, order_by=[("shelf", direction)]
    )
    return [it["shelf"].id for it in groups]


async def test_orders_a_path_key_by_its_nodes_id_as_cypher_orders_it_a_text_before_a_number(gom, pm, seed):
    by_ref = SqlSeed(pm, "PgGroupRun", "ref")
    for item, shelf in (("i0", 10), ("i1", "b"), ("i2", 9)):
        await seed.node(["Item"], {"id": item, "ref": item})
        await seed.node(["Shelf"], {"id": shelf, "ref": f"s-{item}"})
        await by_ref.edge(item, "ON", f"s-{item}")
    assert await shelves(gom, "asc") == ["b", 9, 10]
    assert await shelves(gom, "desc") == [10, 9, "b"]


async def test_orders_a_path_key_whose_nodes_id_is_a_moment_by_the_moment_not_by_its_tags_text(gom, pm, seed):
    by_ref = SqlSeed(pm, "PgGroupRun", "ref")
    for i, id in enumerate(MOMENTS):
        await seed.node(["Item"], {"id": f"i{i}", "ref": f"i{i}"})
        await seed.node(["Shelf"], {"id": id, "ref": f"s{i}"})
        await by_ref.edge(f"i{i}", "ON", f"s{i}")

    async def instants(direction: Literal["asc", "desc"]) -> list[datetime]:
        return [
            (it if isinstance(it, datetime) else datetime.fromisoformat(str(it))).astimezone(UTC)
            for it in await shelves(gom, direction)
        ]

    expected = [
        datetime(2023, 12, 31, 23, tzinfo=UTC),
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, 0, 0, 0, 500000, tzinfo=UTC),
    ]
    assert await instants("asc") == expected
    assert await instants("desc") == expected[::-1]


async def test_compares_orders_and_filters_a_least_or_greatest_value_at_its_node_before_the_page_is_taken(gom, seed):
    await gom.save_all(
        [
            Item(id="a", shelf="one", rank=3, name="x"),
            Item(id="b", shelf="one", rank=7, name="y"),
            Item(id="c", shelf="two", rank=5, name="x"),
            Item(id="d", shelf="three"),
            Item(id="e", shelf="four", rank=1, name="y"),
        ]
    )

    async def top(**spec: Any) -> list[dict[str, Any]]:
        return await gom.groups.load(Item, keys={"shelf": key("shelf")}, aggregates={"top": max_("rank")}, **spec)

    # A group with no value is last ascending and first descending, as Cypher orders a null.
    assert await top(order_by=[("top", "asc")]) == [
        {"shelf": "four", "top": 1},
        {"shelf": "two", "top": 5},
        {"shelf": "one", "top": 7},
        {"shelf": "three", "top": None},
    ]
    assert [it["shelf"] for it in await top(order_by=[("top", "desc")])] == ["three", "one", "two", "four"]
    # The having is a condition over the joined node, outside the grouping, and the page is taken after it.
    kept: dict[str, Any] = {"having": [("top", "gte", 5)], "order_by": [("shelf", "asc")]}
    assert await top(**kept) == [{"shelf": "one", "top": 7}, {"shelf": "two", "top": 5}]
    assert await top(**kept, skip=1, limit=1) == [{"shelf": "two", "top": 5}]
    # Not ordered or filtered by it, the value is the group's column alone.
    assert await top(order_by=[("shelf", "desc")]) == [
        {"shelf": "two", "top": 5},
        {"shelf": "three", "top": None},
        {"shelf": "one", "top": 7},
        {"shelf": "four", "top": 1},
    ]
    # A filtered least value is of the nodes that meet the filter: none in a group is no value.
    q = refs(Item)
    filtered = await gom.groups.load(
        Item, keys={"shelf": key(q.shelf)}, aggregates={"low": min_(q.rank, q.name.eq("y"))}, order_by=[("low", "asc")]
    )
    assert filtered == [
        {"shelf": "four", "low": 1},
        {"shelf": "one", "low": 7},
        {"shelf": "three", "low": None},
        {"shelf": "two", "low": None},
    ]
