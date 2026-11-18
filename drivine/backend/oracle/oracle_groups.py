from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from drivine.backend.graph_statement import GraphStatement
from drivine.backend.oracle.oracle_fragments import called, hinted
from drivine.backend.oracle.oracle_graph import OracleGraph, json_key
from drivine.backend.oracle.oracle_views import OracleViews
from drivine.backend.sql.fragment_groups import FragmentGroups
from drivine.backend.sql.fragments import same_key
from drivine.backend.sql.sql_views import SqlViews
from drivine.backend.sql.value_layout import held_key
from drivine.cypher.shared_compiler import SharedStore


class OracleGroups(FragmentGroups):
    """
    A grouped read on Oracle (see FragmentGroups; drivine4j's `OracleSelect.groups`): a property key is the JSON value
    the module gives; a sum or a mean adds a property where a number is stored, read by the name the layout holds it by.
    A least or greatest value is read on the group's node that has it, joined back, its column that node's `nid`: the
    first of the group by a window over each node's row before they are grouped, `FIRST_VALUE` and not `KEEP
    (DENSE_RANK FIRST …)`, which ends Oracle's (23.26) server process ordered by a value of JSON (`ORA-03113`). Each
    node is joined back by the keys Oracle keeps (`same_key`), and the statement carries the hints its fragments ask
    for after its first `SELECT`.
    """

    store: SharedStore = "ORACLE"

    def __init__(self, graph: OracleGraph) -> None:
        super().__init__(graph)

    def _views(self) -> OracleViews:
        return OracleViews(cast(OracleGraph, self.graph))

    def _aggregated(self, views: SqlViews, property: str) -> str:
        """The property where a number is stored, null otherwise, by the name the layout holds it by (`$$score`)."""
        return views.number(self.row, held_key(property))

    def _first(self, of: str, order: str, kept: str) -> str:
        """
        The first of the group's `of` in the order `order`, of the rows that meet `kept`, by a window over the rows
        before they are grouped: those that meet `kept` first, so the group's first is one of them where it has any,
        and null where it has none.
        """
        first = self._window(
            f"FIRST_VALUE({of}) OVER (PARTITION BY {self._grouping} "
            f"ORDER BY CASE WHEN {kept} THEN 0 ELSE 1 END, {order})"
        )
        return f"CASE WHEN COUNT(CASE WHEN {kept} THEN 1 END) > 0 THEN MIN({first}) END"

    def _id_value(self, property: str) -> str:
        return f"JSON_QUERY(p.props, '${json_key(held_key(property))}' RETURNING JSON)"

    def _same_node(self, a: str, b: str) -> str:
        return same_key(a, b)

    def _made(self, text: str, bindings: Mapping[str, Any]) -> GraphStatement:
        """With the hints the statement's fragments ask for, and the stored functions they call (oracle_fragments)."""
        made, calls = called(hinted(text, self._fragments.hints), self.graph.name, self._fragments)
        return GraphStatement(made, bindings, calls=calls)
