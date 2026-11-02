from __future__ import annotations

from collections.abc import Callable
from typing import Literal, cast

from drivine.backend.sql.sql_groups import SqlGroups
from drivine.backend.sql.sql_query_renderer import SqlQueryRenderer
from drivine.backend.sql.sql_views import SqlViews
from drivine.backend.sqlite.sqlite_graph import SqliteGraph, json_value, path
from drivine.backend.sqlite.sqlite_query_renderer import SqliteQueryRenderer
from drivine.backend.sqlite.sqlite_views import SqliteViews


class SqliteGroups(SqlGroups):
    """
    A grouped read on SQLite (see SqlGroups): a property key held as its JSON text, which groups the values JSON holds
    alike together and is read back as the JSON it is. Paged with `LIMIT` and `OFFSET`.
    """

    def __init__(self, graph: SqliteGraph) -> None:
        super().__init__(graph)

    def _views(self) -> SqliteViews:
        return SqliteViews(cast(SqliteGraph, self.graph))

    def _renderer(self, views: SqlViews, labels_of_class: Callable[[type], list[str]]) -> SqlQueryRenderer:
        return SqliteQueryRenderer(cast(SqliteViews, views), labels_of_class)

    def _key_column(self, property: str) -> str:
        return json_value("n", property)

    def _key_value(self, column: str) -> str:
        return column

    def _ordered(self, value: str, ascending: bool, kind: Literal["KEY", "PATH", "AGGREGATE"]) -> str:
        """
        As Postgres's `jsonb`, the graph engines and drivine4j's SQLite order a group: a node with no value last
        ascending and first descending, and a property key by its kind first, as `jsonb` ranks them (a JSON null, a
        text, a number, a boolean, an array, an object), then by its value, read from its JSON text as the SQL value it
        holds. Drivine for TypeScript's order, character for character.
        """
        direction = "ASC" if ascending else "DESC"
        nulls = "NULLS LAST" if ascending else "NULLS FIRST"
        if kind != "KEY":
            return f"{value} {direction} {nulls}"
        rank = (
            f"CASE json_type({value}) WHEN 'null' THEN 0 WHEN 'text' THEN 1 WHEN 'integer' THEN 2 WHEN 'real' THEN 2 "
            f"WHEN 'false' THEN 3 WHEN 'true' THEN 3 WHEN 'array' THEN 4 WHEN 'object' THEN 5 END"
        )
        return f"{rank} {direction} {nulls}, ({value} ->> '$') {direction} {nulls}"

    def _id_value(self, property: str) -> str:
        return f"(p.props ->> {path(property)})"

    def _page(self, skip: int | None, limit: int | None) -> list[str]:
        if skip is None and limit is None:
            return []
        return [f"LIMIT {limit if limit is not None else -1}" + (f" OFFSET {skip}" if skip is not None else "")]
