from __future__ import annotations

from collections.abc import Callable
from typing import cast

from drivine.backend.oracle.oracle_graph import OracleGraph, json_key
from drivine.backend.oracle.oracle_query_renderer import OracleQueryRenderer
from drivine.backend.oracle.oracle_views import OracleViews
from drivine.backend.sql.sql_groups import SqlGroups
from drivine.backend.sql.sql_query_renderer import SqlQueryRenderer
from drivine.backend.sql.sql_views import SqlViews


class OracleGroups(SqlGroups):
    """A grouped read on Oracle (see SqlGroups): a property key held as its JSON text, read back with JSON()."""

    def __init__(self, graph: OracleGraph) -> None:
        super().__init__(graph)

    def _views(self) -> OracleViews:
        return OracleViews(cast(OracleGraph, self.graph))

    def _renderer(self, views: SqlViews, labels_of_class: Callable[[type], list[str]]) -> SqlQueryRenderer:
        return OracleQueryRenderer(cast(OracleViews, views), labels_of_class)

    def _key_column(self, property: str) -> str:
        return f"JSON_SERIALIZE(JSON_QUERY(n.props, '${json_key(property)}' RETURNING JSON))"

    def _key_value(self, column: str) -> str:
        return f"JSON({column})"

    def _id_value(self, property: str) -> str:
        return f"JSON_QUERY(p.props, '${json_key(property)}' RETURNING JSON)"
