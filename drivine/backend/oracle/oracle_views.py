from __future__ import annotations

from collections.abc import Mapping, Sequence

from drivine.annotation.direction import Direction
from drivine.backend.oracle.oracle_graph import (
    OracleGraph,
    has_labels,
    holding,
    json_key,
    node_document,
    related_subquery,
    sql_text,
)
from drivine.backend.sql.nodes_of import NodesOf
from drivine.backend.sql.order_reads import OrderReads
from drivine.backend.sql.sql_views import SqlViews, by_id
from drivine.model.graph_view_model import GraphViewModel


class OracleViews(SqlViews):
    """
    Writes a view on Oracle (see SqlViews): each relationship's nodes aggregated with JSON_ARRAYAGG, and a relationship
    to a depth followed with CONNECT BY.
    """

    empty_object = "JSON('{}')"

    def __init__(self, graph: OracleGraph, depth: Mapping[str, int] | None = None) -> None:
        super().__init__(graph, depth)

    def cascaded(self, model: GraphViewModel, roots: str) -> list[str]:
        """
        Queries of the ids of the nodes a load of the view from the roots in the PL/SQL collection `roots` reaches.
        """
        return self._reached_from_roots(model, f"n.nid IN (SELECT column_value FROM TABLE({roots}))")

    def object(self, entries: Sequence[tuple[str, str]]) -> str:
        return f"JSON_OBJECT({', '.join(f'{sql_text(key)} VALUE {value}' for key, value in entries)} RETURNING JSON)"

    def node_document(self, alias: str) -> str:
        return node_document(alias)

    def has_labels(self, alias: str, labels: Sequence[str]) -> str:
        return has_labels(alias, labels)

    def related_subquery(self, *hints: str) -> str:
        return related_subquery(*hints)

    def reached(self, type: str, direction: Direction, from_: str, depth: int) -> str:
        edges = self.graph.edges
        typed = sql_text(type)
        if direction is Direction.UNDIRECTED:
            pairs = (
                f"(SELECT src AS a, dst AS b FROM {edges} WHERE type = {typed} UNION ALL "
                f"SELECT dst, src FROM {edges} WHERE type = {typed})"
            )
        elif direction is Direction.INCOMING:
            pairs = f"(SELECT dst AS a, src AS b FROM {edges} WHERE type = {typed})"
        else:
            pairs = f"(SELECT src AS a, dst AS b FROM {edges} WHERE type = {typed})"
        return (
            f"SELECT b AS nid FROM {pairs} START WITH a = {from_}.nid "
            f"CONNECT BY NOCYCLE PRIOR b = a AND LEVEL <= {depth}"
        )

    def nodes_of(self, ids: str, x: str) -> NodesOf:
        """
        They're joined from the ids, so a query correlated to another row is read from where it starts; tested with
        `IN`, Oracle would test every node against it. The hint has each read by its id: Oracle takes a test of a
        node's labels for a lookup of one row, and would read every node of the label for each id.
        """
        r = self.next_alias("r")
        return NodesOf(
            f"FROM (SELECT DISTINCT nid FROM ({ids})) {r} JOIN {self.graph.nodes} {x} ON {x}.nid = {r}.nid", by_id(x)
        )

    def number(self, node: str, property: str) -> str:
        return f"JSON_VALUE({node}.props, '${json_key(property)}' RETURNING NUMBER)"

    def zero_if_null(self, value: str) -> str:
        return f"NVL({value}, 0)"

    def _array_of(self, item: str, order: str | None = None) -> str:
        return f"JSON_ARRAYAGG({item}{f' ORDER BY {order}' if order else ''} RETURNING JSON)"

    def _select(self, hint: str) -> str:
        return f"SELECT /*+ {hint} */ "

    def _order_reads(self, node: str, property: str) -> OrderReads:
        """`JSON_VALUE` reads a stored `""` as null, so whether the node holds one is read on its own."""
        key = f"${json_key(property)}"
        read = holding(node, property)
        return OrderReads(
            f"JSON_VALUE({read}, '{key}.numberOnly()' RETURNING NUMBER)",
            f"JSON_VALUE({read}, '{key}')",
            f"CASE WHEN JSON_EXISTS({read}, '{key}?(@ == \"\")') THEN 1 END",
        )
