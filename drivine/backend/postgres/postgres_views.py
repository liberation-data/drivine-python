from __future__ import annotations

from collections.abc import Mapping, Sequence

from drivine.annotation.direction import Direction
from drivine.backend.postgres.postgres_graph import (
    PostgresGraph,
    has_labels,
    json_value,
    node_document,
    number_of,
    text_of,
)
from drivine.backend.sql.nodes_of import NodesOf
from drivine.backend.sql.order_reads import OrderReads
from drivine.backend.sql.sql_graph import sql_text
from drivine.backend.sql.sql_views import SqlViews
from drivine.model import stamps


class PostgresViews(SqlViews):
    """
    Writes a view on Postgres (see SqlViews): each relationship's nodes aggregated with `jsonb_agg`, and a relationship
    to a depth followed by a recursive query.
    """

    empty_object = "'{}'::jsonb"

    def __init__(self, graph: PostgresGraph, depth: Mapping[str, int] | None = None) -> None:
        super().__init__(graph, depth)

    def object(self, entries: Sequence[tuple[str, str]]) -> str:
        return f"jsonb_build_object({', '.join(f'{sql_text(key)}, {value}' for key, value in entries)})"

    def node_document(self, alias: str) -> str:
        return node_document(alias)

    def has_labels(self, alias: str, labels: Sequence[str]) -> str:
        return has_labels(alias, labels)

    def related_subquery(self, *hints: str) -> str:
        return "SELECT 1"

    def reached(self, type: str, direction: Direction, from_: str, depth: int) -> str:
        """
        Each way from the node row `from_`, a relationship at a time, with the relationships it has taken so far, none
        of which it takes again, as Cypher walks a path: a node reached by several ways is a row for each.
        """
        walk = self.next_alias("w")
        edges = self.graph.edges
        typed = sql_text(type)
        if direction is Direction.UNDIRECTED:
            pairs = (
                f"(SELECT eid, src AS a, dst AS b FROM {edges} WHERE type = {typed} UNION ALL "
                f"SELECT eid, dst, src FROM {edges} WHERE type = {typed})"
            )
        elif direction is Direction.INCOMING:
            pairs = f"(SELECT eid, dst AS a, src AS b FROM {edges} WHERE type = {typed})"
        else:
            pairs = f"(SELECT eid, src AS a, dst AS b FROM {edges} WHERE type = {typed})"
        return (
            f"WITH RECURSIVE {walk} (nid, depth, eids) AS ("
            f"SELECT e.b, 1, ARRAY[e.eid] FROM {pairs} e WHERE e.a = {from_}.nid "
            f"UNION ALL SELECT e.b, w.depth + 1, w.eids || e.eid FROM {walk} w JOIN {pairs} e ON e.a = w.nid "
            f"WHERE w.depth < {depth} AND e.eid <> ALL (w.eids)) SELECT nid FROM {walk}"
        )

    def nodes_of(self, ids: str, x: str) -> NodesOf:
        r = self.next_alias("r")
        return NodesOf(
            f"FROM (SELECT DISTINCT nid FROM ({ids}) {r}_ids) {r} JOIN {self.graph.nodes} {x} ON {x}.nid = {r}.nid", ""
        )

    def number(self, node: str, property: str) -> str:
        return number_of(node, property)

    def zero_if_null(self, value: str) -> str:
        return f"COALESCE({value}, 0)"

    def _array_of(self, item: str, order: str | None = None) -> str:
        return f"jsonb_agg({item}{f' ORDER BY {order}' if order else ''})"

    def _select(self, hint: str) -> str:
        return "SELECT "

    def _order_reads(self, node: str, property: str) -> OrderReads:
        """`->>` reads a stored `""` as `""`, so it is a text and needs no reading of its own."""
        if property == stamps.PROPERTY:
            return OrderReads("CAST(NULL AS numeric)", text_of(node, property), "CAST(NULL AS integer)")
        return OrderReads(
            number_of(node, property),
            f"CASE WHEN jsonb_typeof({json_value(node, property)}) = 'string' THEN {text_of(node, property)} END",
            "CAST(NULL AS integer)",
        )
