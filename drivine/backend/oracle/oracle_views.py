from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from drivine.annotation.direction import Direction
from drivine.backend.oracle.oracle_graph import (
    OracleGraph,
    has_labels,
    json_key,
    node_document,
    related_subquery,
    sql_text,
)
from drivine.backend.sql.fragments import same_key
from drivine.backend.sql.nodes_of import NodesOf
from drivine.backend.sql.sql_views import SqlViews, by_id


class OracleViews(SqlViews):
    """
    Writes a view on Oracle (see SqlViews): each relationship's nodes aggregated with JSON_ARRAYAGG, and a relationship
    to a depth followed by a join for each relationship along the way.
    """

    empty_object = "JSON('{}')"

    def __init__(self, graph: OracleGraph, depth: Mapping[str, int] | None = None) -> None:
        super().__init__(graph, depth, "ORACLE")

    def object(self, entries: Sequence[tuple[str, str]]) -> str:
        return f"JSON_OBJECT({', '.join(f'{sql_text(key)} VALUE {value}' for key, value in entries)} RETURNING JSON)"

    def node_document(self, alias: str) -> str:
        return node_document(alias)

    def has_labels(self, alias: str, labels: Sequence[str]) -> str:
        return has_labels(alias, labels)

    def related_subquery(self, *hints: str) -> str:
        return related_subquery(*hints)

    def reached(self, type: str, direction: Direction, from_: str, depth: int) -> str:
        """
        Each way from the node row `from_`, as far as `depth` relationships, taking no relationship twice in a walk, as
        Cypher takes a relationship once in a path: a node reached by several ways is a row for each. CONNECT BY can't
        carry the relationships taken, and a recursive WITH can't see the outer row on Oracle (ORA-00904), so the walk
        is unrolled to its depth, a branch for each length with each pair of its relationships told apart, as drivine4j
        does (#394).
        """
        edges = self._steps(type, direction)
        branches: list[str] = []
        for k in range(1, depth + 1):
            branch = f"SELECT h{k}.b AS nid FROM {edges} h1"
            for i in range(2, k + 1):
                branch += f" JOIN {edges} h{i} ON h{i}.a = h{i - 1}.b"
            branch += f" WHERE h1.a = {from_}.nid"
            for i in range(1, k):
                for j in range(i + 1, k + 1):
                    branch += f" AND h{i}.eid <> h{j}.eid"
            branches.append(branch)
        return " UNION ALL ".join(branches)

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

    def _first_of_ways(self, ways: str, terms: Callable[[str], str]) -> str:
        """
        The first hop of each node reached in the order `terms`, over the properties of the hop's node each way
        carries: by a window over the ways, `FIRST_VALUE` and not `KEEP (DENSE_RANK FIRST …)`, which ends Oracle's
        (23.26) server process ordered by a value of JSON (drivine4j's `OracleSelect.pathOrder`).
        """
        w = f"{self.next_alias('w')}_"
        r = self.next_alias("r")
        return (
            f"SELECT {r}.nid, MIN({r}.hop) AS hop FROM (SELECT {w}.nid, FIRST_VALUE({w}.hop) OVER "
            f"(PARTITION BY {w}.nid ORDER BY {terms(w)}) AS hop FROM ({ways}) {w}) {r} GROUP BY {r}.nid"
        )

    def _same_node(self, a: str, b: str) -> str:
        """By the keys Oracle keeps, as the module's compiler joins a node back (see fragments.same_key)."""
        return same_key(a, b)
