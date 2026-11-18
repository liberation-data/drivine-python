from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from drivine.annotation.direction import Direction
from drivine.backend.sql.nodes_of import NodesOf
from drivine.backend.sql.sql_graph import sql_text
from drivine.backend.sql.sql_views import SqlViews
from drivine.backend.sqlite.sqlite_graph import (
    SqliteGraph,
    has_labels,
    node_document,
    number_of,
)


class SqliteViews(SqlViews):
    """
    Writes a view on SQLite (see SqlViews): each relationship's nodes aggregated with `json_group_array`, and a
    relationship to a depth followed by a recursive query. A value a subquery gives is text, so each is read as JSON
    with `json(…)` where an object holds it, or it would be put in as a string.
    """

    empty_object = "json_object()"
    first_row = "LIMIT 1"

    def __init__(self, graph: SqliteGraph, depth: Mapping[str, int] | None = None) -> None:
        super().__init__(graph, depth, "SQLITE")

    def object(self, entries: Sequence[tuple[str, str]]) -> str:
        return f"json_object({', '.join(f'{sql_text(key)}, json({value})' for key, value in entries)})"

    def node_document(self, alias: str) -> str:
        return node_document(alias)

    def has_labels(self, alias: str, labels: Sequence[str]) -> str:
        return has_labels(alias, labels)

    def related_subquery(self, *hints: str) -> str:
        return "SELECT 1"

    def reached(self, type: str, direction: Direction, from_: str, depth: int) -> str:
        """
        Each way from the node row `from_`, a relationship at a time, with the relationships it has taken so far, none
        of which it takes again, as Cypher walks a path: a node reached by several ways is a row for each. SQLite has no
        arrays, so the relationships taken are the text of their ids, each between commas, which a step looks in.
        """
        walk = self.next_alias("w")
        pairs = self._steps(type, direction)
        return (
            f"WITH RECURSIVE {walk} (nid, depth, eids) AS ("
            f"SELECT e.b, 1, ',' || e.eid || ',' FROM {pairs} e WHERE e.a = {from_}.nid "
            f"UNION ALL SELECT e.b, w.depth + 1, w.eids || e.eid || ',' FROM {walk} w JOIN {pairs} e ON e.a = w.nid "
            f"WHERE w.depth < {depth} AND instr(w.eids, ',' || e.eid || ',') = 0) SELECT nid FROM {walk}"
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
        return f"json_group_array(json({item}){f' ORDER BY {order}' if order else ''})"

    def _select(self, hint: str) -> str:
        return "SELECT "

    def _first_of_ways(self, ways: str, terms: Callable[[str], str]) -> str:
        """
        The first hop of each node reached in the order `terms`, over the node of the hop joined back by its id: the
        first of the ids gathered in that order (drivine4j's `SqliteSelect.pathOrder`).
        """
        w = self.next_alias("w")
        s = f"{self.next_alias('s')}_"
        return (
            f"SELECT {w}.nid, json_group_array({s}.nid ORDER BY {terms(s)}) ->> '$[0]' AS hop "
            f"FROM ({ways}) {w} JOIN {self.graph.nodes} {s} ON {s}.nid = {w}.hop GROUP BY {w}.nid"
        )
