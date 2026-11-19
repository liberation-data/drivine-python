# Written by scripts/unasync.py from tests/integration/test_ordered_path_kinds.py:
# change that, and run the script again.
"""
A view's path ordered by a hop's property gives Neo4j's order on every store whose Cypher orders the property's kinds
(#540): a date by its moment, an offset's included, and kinds in Cypher's order, a temporal, a text, a boolean, a
number, no value last. On the SQL stores the path is ordered by the shared module's order fragments over the hop's
node, as drivine4j orders it; the library's own SQL before ordered a date by its tag's text (`.5Z` before `Z`) and kinds
out of Cypher's order. On Neo4j and Memgraph the rows are sorted by the engine's own ORDER BY before they are collected
(#553), which on both orders the kinds as Cypher does; Memgraph's datetime() refuses the dates the date cases write, so
those two are skipped there. FalkorDB, which orders temporals and mixed kinds by its own rules, is left out. Drivine for
TypeScript's StatelessGraphObjectManager.orderedPathKinds.spec.ts.
"""

from typing import Annotated, Any, Literal

import pytest

from drivine.sync import (
    Direction,
    GraphPath,
    GraphView,
    NodeFragment,
    NodeId,
    PersistenceManager,
    QuerySpecification,
    Root,
    StatelessGraphObjectManager,
    hop,
)
from tests.sync_integration.conftest import ENGINES

# Seeded in Cypher, which Postgres, Oracle and SQLite compile to their SQL.
pytestmark = pytest.mark.written_in_cypher


class OrderedPathKindActor(NodeFragment, labels=["OrderedPathKind", "Actor"]):
    id: Annotated[str, NodeId()]


class OrderedPathKindDirector(NodeFragment, labels=["OrderedPathKind", "Director"]):
    id: Annotated[str, NodeId()]


class ActorDirectors(GraphView):
    actor: Annotated[OrderedPathKindActor, Root()]
    lowest_first: Annotated[
        list[OrderedPathKindDirector],
        GraphPath([hop("ACTED_IN", Direction.OUTGOING, "Movie", "value"), hop("DIRECTED_BY")]),
    ] = []
    highest_first: Annotated[
        list[OrderedPathKindDirector],
        GraphPath([hop("ACTED_IN", Direction.OUTGOING, "Movie", "value", False), hop("DIRECTED_BY")]),
    ] = []
    lowest: Annotated[
        OrderedPathKindDirector | None,
        GraphPath([hop("ACTED_IN", Direction.OUTGOING, "Movie", "value"), hop("DIRECTED_BY")]),
    ] = None
    highest: Annotated[
        OrderedPathKindDirector | None,
        GraphPath([hop("ACTED_IN", Direction.OUTGOING, "Movie", "value", False), hop("DIRECTED_BY")]),
    ] = None


DATED_ON_MEMGRAPH = pytest.mark.skip(
    reason=(
        "Memgraph's datetime() reads neither a fraction of a second before Z nor an offset (.5Z, +02:00), so the dates "
        "this case writes can't be made there; its ORDER BY of other kinds is held to Neo4j's"
    )
)


def stores(**marks: list[pytest.MarkDecorator]) -> list[Any]:
    """The stores a case runs on, FalkorDB left out, each with the marks given for it by its registered name."""
    return [pytest.param(it, marks=marks.get(it, [])) for it in ENGINES if it != "falkordb"]


@pytest.fixture
def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    return StatelessGraphObjectManager(pm)


def create(pm: PersistenceManager, values: list[str]) -> None:
    """Actor `a`, who acted in a movie of each value, Cypher's, directed by `d<i>`; `null` for a movie without one."""
    movies = [
        f"(a)-[:ACTED_IN]->(:OrderedPathKind:Movie {{id: 'm{i}'{'' if value == 'null' else f', value: {value}'}}})"
        f"-[:DIRECTED_BY]->(:OrderedPathKind:Director {{id: 'd{i}'}})"
        for i, value in enumerate(values)
    ]
    pm.execute(QuerySpecification(f"CREATE (a:OrderedPathKind:Actor {{id: 'a'}}), {', '.join(movies)}"))


def loaded(gom: StatelessGraphObjectManager) -> dict[str, Any]:
    view = gom.load(ActorDirectors, "a")
    assert view is not None
    return {
        "lowest_first": [it.id for it in view.lowest_first],
        "highest_first": [it.id for it in view.highest_first],
        "lowest": view.lowest.id if view.lowest else None,
        "highest": view.highest.id if view.highest else None,
    }


def ordered(pm: PersistenceManager, values: str, way: Literal["ASC", "DESC"]) -> list[str]:
    return pm.get_one(
        QuerySpecification[list[str]](
            f"UNWIND {values} AS v WITH v ORDER BY v IS NULL, v {way} RETURN [x IN collect(v) | toString(x)]"
        )
    )


@pytest.mark.parametrize("pm", ["default", "memgraph"], indirect=True)
def test_orders_values_of_several_kinds_by_order_by_as_neo4j_does_its_own_engines(pm):
    # What the engine's own ORDER BY does with values of several kinds, apart from any view: Neo4j orders a temporal
    # value, a text, a boolean, a number, no value last, and so does Memgraph's: a view's path on either is sorted by
    # the engine's ORDER BY alone (#553).
    kinds = "['b', true, 10, 9, null, false, 'a']"
    assert ordered(pm, kinds, "ASC") == ["a", "b", "false", "true", "9", "10"]
    assert ordered(pm, kinds, "DESC") == ["10", "9", "true", "false", "b", "a"]
    dated = ordered(pm, "['b', datetime('2024-01-01T00:00:00Z'), 9]", "ASC")
    assert dated[0].startswith("2024-01-01")
    assert dated[1:] == ["b", "9"]


@pytest.mark.parametrize("pm", stores(memgraph=[DATED_ON_MEMGRAPH]), indirect=True)
def test_orders_a_date_hop_property_by_its_moment_an_offsets_included_not_by_its_text(pm, gom):
    create(
        pm,
        [
            "datetime('2024-01-01T00:00:00.5Z')",
            "datetime('2024-01-01T00:00:00Z')",
            "datetime('2024-01-01T01:00:00+02:00')",
            "null",
        ],
    )
    assert loaded(gom) == {
        "lowest_first": ["d2", "d1", "d0", "d3"],
        "highest_first": ["d0", "d1", "d2", "d3"],
        "lowest": "d2",
        "highest": "d0",
    }


@pytest.mark.parametrize("pm", stores(), indirect=True)
def test_orders_a_hop_property_of_mixed_kinds_as_cypher_does_a_text_a_boolean_a_number_no_value_last(pm, gom):
    create(pm, ["'b'", "true", "10", "9", "null", "false", "'a'"])
    assert loaded(gom) == {
        "lowest_first": ["d6", "d0", "d5", "d1", "d3", "d2", "d4"],
        "highest_first": ["d2", "d3", "d1", "d5", "d0", "d6", "d4"],
        "lowest": "d6",
        "highest": "d2",
    }


@pytest.mark.parametrize("pm", stores(memgraph=[DATED_ON_MEMGRAPH]), indirect=True)
def test_keeps_for_a_node_reached_by_a_date_and_a_number_the_date_lowest_first_the_number_highest(pm, gom):
    create(pm, ["datetime('2024-01-01T00:00:00.5Z')", "'x'", "datetime('2024-01-01T00:00:00Z')"])
    pm.execute(
        QuerySpecification(
            "MATCH (a:OrderedPathKind:Actor {id: 'a'}), (d:OrderedPathKind:Director {id: 'd0'}) "
            "CREATE (a)-[:ACTED_IN]->(:OrderedPathKind:Movie {id: 'm9', value: 7})-[:DIRECTED_BY]->(d)"
        )
    )
    assert loaded(gom) == {
        "lowest_first": ["d2", "d0", "d1"],
        "highest_first": ["d0", "d1", "d2"],
        "lowest": "d2",
        "highest": "d0",
    }
