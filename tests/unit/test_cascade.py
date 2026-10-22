"""What a cascading delete through a view follows: every level of a view that contains itself, what a kept node holds,
and the reads that refuse a cascade that would stop short."""

from typing import Annotated, Any

import pytest

from drivine import (
    CascadeType,
    DatabaseType,
    Direction,
    DrivineError,
    GraphView,
    ModelRegistry,
    NodeFragment,
    NodeId,
    QuerySpecification,
    Relationship,
    Root,
    StatelessGraphObjectManager,
)
from drivine.backend.cascade_targets import CascadeTargets
from drivine.backend.cypher_backend import CypherBackend
from drivine.backend.cypher_grammar import NEO4J_GRAMMAR
from drivine.backend.graph_backend import GraphBackend
from drivine.backend.oracle.oracle_backend import OracleBackend
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.postgres.postgres_backend import PostgresBackend
from drivine.backend.postgres.postgres_graph import PostgresGraph


class Doc(NodeFragment, labels=["Doc"]):
    id: Annotated[str, NodeId()]


class Note(NodeFragment, labels=["Note"]):
    id: Annotated[str, NodeId()]


class Tree(GraphView):
    """A view that contains itself: parts point at the doc they are part of."""

    doc: Annotated[Doc, Root()]
    notes: Annotated[list[Note], Relationship("HAS")] = []
    parts: Annotated[list["Tree"], Relationship("PART_OF", Direction.INCOMING, max_depth=2)] = []


class Shelf(GraphView):
    doc: Annotated[Doc, Root()]
    trees: Annotated[list[Tree], Relationship("SHELVES")] = []


class Pals(GraphView):
    doc: Annotated[Doc, Root()]
    pals: Annotated[list[Doc], Relationship("KNOWS", Direction.UNDIRECTED)] = []


class Thread(GraphView):
    """A view that contains itself one level deep."""

    doc: Annotated[Doc, Root()]
    replies: Annotated[list["Thread"], Relationship("REPLIES_TO", Direction.INCOMING)] = []


class Plain(GraphView):
    doc: Annotated[Doc, Root()]
    notes: Annotated[list[Note], Relationship("HAS")] = []


registry = ModelRegistry()

BACKENDS: list[GraphBackend] = [
    CypherBackend(NEO4J_GRAMMAR),
    OracleBackend(OracleGraph()),
    PostgresBackend(PostgresGraph()),
]


def test_reads_each_level_of_a_view_that_contains_itself_holding_what_the_root_does():
    targets = CascadeTargets(NEO4J_GRAMMAR).of(registry.view(Tree), "n")
    assert "[(n)<-[:PART_OF]-(c2:Doc)-[:HAS]->(c3:Note) | c3]" in targets
    assert "[(n)<-[:PART_OF]-(c2:Doc)<-[:PART_OF]-(c4:Doc)-[:HAS]->(c5:Note) | c5]" in targets
    assert "*" not in targets


@pytest.mark.parametrize("backend", BACKENDS, ids=["cypher", "oracle", "postgres"])
def test_checks_a_recursive_field_one_level_below_its_depth_where_it_is_reached(backend: GraphBackend):
    assert backend.cascade_depth_checks(registry.view(Plain)) == []
    assert backend.cascade_depth_checks(registry.fragment(Doc)) == []
    [check] = backend.cascade_depth_checks(registry.view(Shelf))
    assert (check.view, check.field, check.max_depth) == ("Tree", "parts", 2)
    assert "beyond" in check.statement.text


def test_reads_the_nodes_past_the_depth_through_the_shelf_and_counts_those_the_cascade_does_not_reach():
    [check] = CypherBackend(NEO4J_GRAMMAR).cascade_depth_checks(registry.view(Shelf))
    beyond = "[(n)-[:SHELVES]->(c1:Doc)<-[:PART_OF]-(c3:Doc)<-[:PART_OF]-(c5:Doc)<-[:PART_OF]-(c7:Doc) | c7] AS beyond"
    assert beyond in check.statement.text
    assert "WHERE NOT b IN targets AND b <> n" in check.statement.text


def test_keeps_below_a_kept_node_the_far_end_of_each_step_the_cascade_follows_in_its_direction():
    cypher = CypherBackend(NEO4J_GRAMMAR).delete(registry.view(Tree), None, CascadeType.DELETE_ORPHAN).text
    assert "any(h IN [(h)-[:HAS]->(t) | h] + [(h)<-[:PART_OF]-(t) | h] WHERE" in cypher
    tree = OracleBackend(OracleGraph()).delete(registry.view(Tree), None, CascadeType.DELETE_ORPHAN).text
    assert "ON (e.type = 'HAS' AND e.src = k.nid) OR (e.type = 'PART_OF' AND e.dst = k.nid)" in tree
    assert tree.count("e.type = 'PART_OF' AND") == 1
    assert ") CYCLE nid SET looped TO 'Y' DEFAULT 'N'" in tree
    pals = PostgresBackend(PostgresGraph()).delete(registry.view(Pals), None, CascadeType.DELETE_ORPHAN).text
    assert "ON (e.type = 'KNOWS' AND (e.src = k.nid OR e.dst = k.nid))" in pals
    assert pals.startswith("WITH RECURSIVE cascade_roots")


class _CountingStore:
    """Answers each statement with one number: `below` for a depth check, 1 for anything else. Records what it ran."""

    type = DatabaseType.NEO4J

    def __init__(self) -> None:
        self.run: list[str] = []
        self.specs: list[QuerySpecification[Any]] = []
        self.below = 2

    async def get_one(self, spec: QuerySpecification[Any]) -> int:
        assert spec.statement is not None
        self.run.append(spec.statement.text)
        self.specs.append(spec)
        return self.below if "AS beyond" in spec.statement.text else 1


async def test_refuses_a_cascade_before_deleting_when_its_depth_check_finds_nodes_below_and_deletes_when_none():
    store = _CountingStore()
    gom = StatelessGraphObjectManager(store, ModelRegistry())  # pyright: ignore[reportArgumentType]

    with pytest.raises(DrivineError) as refused:
        await gom.delete(Thread, "d1", cascade=CascadeType.DELETE_ALL)
    assert str(refused.value).startswith(
        "Cannot cascade-delete Thread 'd1': Thread.replies reaches 1 level deep (max_depth = 1), and 2 nodes below "
        "that would be left behind. Raise max_depth on replies to cover the deepest level. Nothing was deleted."
    )
    assert ["AS beyond" in it for it in store.run] == [True]

    store.run.clear()
    assert await gom.delete(Thread, "d1") == 1
    assert ["AS beyond" in it for it in store.run] == [False]

    store.run.clear()
    store.below = 0
    assert await gom.delete(Thread, "d1", cascade=CascadeType.DELETE_ORPHAN) == 1
    assert ["AS beyond" in it for it in store.run] == [True, False]


async def test_checks_a_nested_views_depth_with_the_deletes_predicate_and_bindings_naming_the_view_that_declares_it():
    store = _CountingStore()
    store.below = 1
    gom = StatelessGraphObjectManager(store, ModelRegistry())  # pyright: ignore[reportArgumentType]

    with pytest.raises(DrivineError) as refused:
        await gom.delete(
            Shelf, "s1", where="n.title = $title", bindings={"title": "Poems"}, cascade=CascadeType.DELETE_ALL
        )
    assert str(refused.value) == (
        "Cannot cascade-delete Shelf 's1': Tree.parts reaches 2 levels deep (max_depth = 2), and 1 node below "
        "that would be left behind. Raise max_depth on parts to cover the deepest level. Nothing was deleted."
    )
    assert len(store.run) == 1
    assert "(n.title = $title)" in store.run[0]
    assert store.specs[0].parameters == {"title": "Poems", "id": "s1"}
