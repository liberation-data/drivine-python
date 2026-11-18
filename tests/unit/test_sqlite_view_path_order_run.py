"""
A view's path ordered by a hop's property, run on SQLite in a file of the test's own, so no container: ordered as
Cypher, and Neo4j, order the hop's property (#540), through the shared module's order fragments over the hop's node, as
drivine4j's `SqliteSelect.pathOrder` orders it. The library's own SQL before ordered a date by its tag's text, so `.5Z`
came before `Z` and an offset's moment was not read, and ordered a boolean as no value. Drivine for TypeScript's
SqliteViewPathOrderRun.spec.ts.
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

import pytest

from drivine import (
    DatabaseRegistry,
    DatabaseType,
    Direction,
    GraphPath,
    GraphView,
    NodeFragment,
    NodeId,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    Root,
    StatelessGraphObjectManager,
    hop,
)


class PathOrderRunActor(NodeFragment, labels=["PathOrderRun", "Actor"]):
    id: Annotated[str, NodeId()]


class PathOrderRunDirector(NodeFragment, labels=["PathOrderRun", "Director"]):
    id: Annotated[str, NodeId()]


class ActorDirectors(GraphView):
    actor: Annotated[PathOrderRunActor, Root()]
    lowest_first: Annotated[
        list[PathOrderRunDirector],
        GraphPath([hop("ACTED_IN", Direction.OUTGOING, "Movie", "value"), hop("DIRECTED_BY")]),
    ] = []
    highest_first: Annotated[
        list[PathOrderRunDirector],
        GraphPath([hop("ACTED_IN", Direction.OUTGOING, "Movie", "value", False), hop("DIRECTED_BY")]),
    ] = []
    lowest: Annotated[
        PathOrderRunDirector | None,
        GraphPath([hop("ACTED_IN", Direction.OUTGOING, "Movie", "value"), hop("DIRECTED_BY")]),
    ] = None
    highest: Annotated[
        PathOrderRunDirector | None,
        GraphPath([hop("ACTED_IN", Direction.OUTGOING, "Movie", "value", False), hop("DIRECTED_BY")]),
    ] = None


ABSENT = object()
"""A movie without a value."""


def datetime_tag(v: str, s: str) -> dict[str, str]:
    """A datetime's tag as a save writes it: its text, then its sort key."""
    return {"$t": "datetime", "v": v, "s": s}


def cypher(text: str) -> QuerySpecification[Any]:
    return QuerySpecification[Any](text).finalized_copy("CYPHER")


@pytest.fixture
async def pm(tmp_path: Path) -> AsyncIterator[PersistenceManager]:
    registry = DatabaseRegistry()
    registry.builder().database_type(DatabaseType.SQLITE).database_name(str(tmp_path / "g.db")).register("pathOrder")
    try:
        yield PersistenceManagerFactory(registry).get("pathOrder")
    finally:
        await registry.close()


async def create(pm: PersistenceManager, values: list[Any]) -> None:
    """
    Actor `a` acted in a movie of each value, directed by director `d<i>`, a movie without a value for ABSENT. Each
    movie's properties are written as their JSON, a date as the tag a save writes, its text first.
    """
    await pm.execute(cypher("CREATE (:PathOrderRun:Actor {id: 'a'})"))
    for i, value in enumerate(values):
        await pm.execute(
            cypher(
                "MATCH (a:PathOrderRun:Actor {id: 'a'}) "
                f"CREATE (a)-[:ACTED_IN]->(:PathOrderRun:Movie {{id: 'm{i}'}})"
                f"-[:DIRECTED_BY]->(:PathOrderRun:Director {{id: 'd{i}'}})"
            )
        )
        props = {"id": f"m{i}", **({} if value is ABSENT else {"value": value})}
        await pm.execute(
            QuerySpecification[Any]("UPDATE drivine_node SET props = $props WHERE props ->> '$.id' = $was").bind(
                {"props": json.dumps(props), "was": f"m{i}"}
            )
        )


async def loaded(pm: PersistenceManager) -> dict[str, Any]:
    view = await StatelessGraphObjectManager(pm).load(ActorDirectors, "a")
    assert view is not None
    return {
        "lowest_first": [it.id for it in view.lowest_first],
        "highest_first": [it.id for it in view.highest_first],
        "lowest": view.lowest.id if view.lowest else None,
        "highest": view.highest.id if view.highest else None,
    }


async def test_orders_a_date_hop_property_by_its_moment_an_offsets_included_not_by_its_tags_text(pm):
    await create(
        pm,
        [
            datetime_tag("2024-01-01T00:00:00.5Z", "10000020240101000000500000000064800"),
            datetime_tag("2024-01-01T00:00:00Z", "10000020240101000000000000000064800"),
            datetime_tag("2024-01-01T01:00:00+02:00", "10000020231231230000000000000072000"),
            ABSENT,
        ],
    )
    assert await loaded(pm) == {
        "lowest_first": ["d2", "d1", "d0", "d3"],
        "highest_first": ["d0", "d1", "d2", "d3"],
        "lowest": "d2",
        "highest": "d0",
    }


async def test_orders_a_hop_property_of_mixed_kinds_as_cypher_does_a_text_a_boolean_a_number_and_no_value_last(pm):
    await create(pm, ["b", True, 10, 9, ABSENT, False, "a"])
    assert await loaded(pm) == {
        "lowest_first": ["d6", "d0", "d5", "d1", "d3", "d2", "d4"],
        "highest_first": ["d2", "d3", "d1", "d5", "d0", "d6", "d4"],
        "lowest": "d6",
        "highest": "d2",
    }


async def test_orders_a_map_a_list_a_date_and_a_text_hop_property_in_cyphers_order_of_kinds(pm):
    await create(pm, ["a", datetime_tag("2024-01-01T00:00:00Z", "10000020240101000000000000000064800"), [1], {"k": 1}])
    assert await loaded(pm) == {
        "lowest_first": ["d3", "d2", "d1", "d0"],
        "highest_first": ["d0", "d1", "d2", "d3"],
        "lowest": "d3",
        "highest": "d0",
    }


async def test_orders_an_empty_text_as_a_text_the_lowest_and_targets_of_equal_values_by_their_node(pm):
    await create(pm, ["", "a", ABSENT, ""])
    assert await loaded(pm) == {
        "lowest_first": ["d0", "d3", "d1", "d2"],
        "highest_first": ["d1", "d0", "d3", "d2"],
        "lowest": "d0",
        "highest": "d1",
    }


async def test_keeps_for_a_node_reached_by_several_hop_nodes_the_first_of_them_a_date_and_a_number_apart(pm):
    # d0 is reached through a date, a number and a movie without a value: lowest first it keeps the date, highest
    # first the number, and never the movie without one.
    await create(
        pm,
        [
            datetime_tag("2024-01-01T00:00:00.5Z", "10000020240101000000500000000064800"),
            "x",
            datetime_tag("2024-01-01T00:00:00Z", "10000020240101000000000000000064800"),
        ],
    )
    await pm.execute(
        cypher(
            "MATCH (a:PathOrderRun:Actor {id: 'a'}), (d:PathOrderRun:Director {id: 'd0'}) "
            "CREATE (a)-[:ACTED_IN]->(:PathOrderRun:Movie {id: 'm9', value: 7})-[:DIRECTED_BY]->(d), "
            "(a)-[:ACTED_IN]->(:PathOrderRun:Movie {id: 'm10'})-[:DIRECTED_BY]->(d)"
        )
    )
    assert await loaded(pm) == {
        "lowest_first": ["d2", "d0", "d1"],
        "highest_first": ["d0", "d1", "d2"],
        "lowest": "d2",
        "highest": "d0",
    }
