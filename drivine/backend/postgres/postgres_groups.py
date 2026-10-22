from __future__ import annotations

from collections.abc import Callable
from typing import cast

from drivine.backend.postgres.postgres_graph import PostgresGraph, json_value
from drivine.backend.postgres.postgres_query_renderer import PostgresQueryRenderer
from drivine.backend.postgres.postgres_views import PostgresViews
from drivine.backend.sql.sql_groups import SqlGroups
from drivine.backend.sql.sql_query_renderer import SqlQueryRenderer
from drivine.backend.sql.sql_views import SqlViews


class PostgresGroups(SqlGroups):
    """A grouped read on Postgres (see SqlGroups): a property key held as its `jsonb` value."""

    def __init__(self, graph: PostgresGraph) -> None:
        super().__init__(graph)

    def _views(self) -> PostgresViews:
        return PostgresViews(cast(PostgresGraph, self.graph))

    def _renderer(self, views: SqlViews, labels_of_class: Callable[[type], list[str]]) -> SqlQueryRenderer:
        return PostgresQueryRenderer(cast(PostgresViews, views), labels_of_class)

    def _key_column(self, property: str) -> str:
        return json_value("n", property)

    def _key_value(self, column: str) -> str:
        return column

    def _id_value(self, property: str) -> str:
        return json_value("p", property)
