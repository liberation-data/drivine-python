"""
What the Postgres backend reads of the store's answers: contention, and a schema marker's inventory. And how it reads
the nodes related either way round.
"""

from typing import Annotated

from drivine import Direction, ModelRegistry, NodeFragment, NodeId
from drivine.backend.matched_node import MatchedNode
from drivine.backend.postgres.postgres_backend import PostgresBackend
from drivine.backend.postgres.postgres_graph import PostgresGraph


class SqlError(Exception):
    """An error as psycopg raises one: with the SQLSTATE Postgres gave."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def caused_by(cause: BaseException) -> Exception:
    try:
        raise RuntimeError("wrapped") from cause
    except RuntimeError as error:
        return error


def test_a_serialization_failure_or_a_deadlock_is_contention_however_deep_it_is_wrapped():
    backend = PostgresBackend()
    assert backend.contended(SqlError("40001"))
    assert backend.contended(caused_by(caused_by(SqlError("40P01"))))


def test_any_other_failure_is_not_contention():
    backend = PostgresBackend()
    assert not backend.contended(SqlError("23505"))
    assert not backend.contended(caused_by(ValueError("40001")))


def test_keeps_an_empty_inventory_apart_from_none():
    assert PostgresBackend.marker_items(None) is None
    assert PostgresBackend.marker_items("") == []
    assert PostgresBackend.marker_items("a\nb") == ["a", "b"]


class Doc(NodeFragment, labels=["Doc"]):
    id: Annotated[str, NodeId()]


def test_reads_the_nodes_related_either_way_round_from_the_both_ways_rows_joined_on_an_equality():
    # With an OR across the two directions, Postgres read every relationship of the type (#390). The rows are pinned by
    # tests/integration/sql/test_sql_writes.py; this pins the shape.
    graph = PostgresGraph()
    doc = ModelRegistry().fragment(Doc)
    either = PostgresBackend(graph).related(MatchedNode(doc, "d1"), "KNOWS", Direction.UNDIRECTED, doc).text
    assert f"JOIN {graph.both_ways()} e ON e.type = " in either
    assert "x ON e.a = a.nid AND (e.back = 0 OR e.src <> e.dst) AND x.nid = e.b WHERE" in either
    assert " OR (e.dst = a.nid" not in either
    for direction in (Direction.OUTGOING, Direction.INCOMING):
        assert "AS back" not in PostgresBackend(graph).related(MatchedNode(doc, "d1"), "KNOWS", direction, doc).text
