from __future__ import annotations

from typing import cast

from drivine.backend.postgres.postgres_graph import PostgresGraph, json_value, number_of
from drivine.backend.postgres.postgres_views import PostgresViews
from drivine.backend.sql.fragment_groups import FragmentGroups
from drivine.backend.sql.sql_views import SqlViews
from drivine.backend.sql.value_layout import held_key
from drivine.cypher.shared_compiler import SharedStore


class PostgresGroups(FragmentGroups):
    """
    A grouped read on Postgres (see FragmentGroups; drivine4j's `PostgresSelect.groups`): a property key held as the
    `jsonb` value the module gives; a sum or a mean adds a property where a number is stored, read by the name the
    layout holds it by. A least or greatest value is the group's column itself, the first of the group's values in
    Cypher's order, and the node that has it is joined back only where the groups are ordered or filtered by it.
    """

    store: SharedStore = "POSTGRES"

    gives_value = True

    def __init__(self, graph: PostgresGraph) -> None:
        super().__init__(graph)

    def _views(self) -> PostgresViews:
        return PostgresViews(cast(PostgresGraph, self.graph))

    def _aggregated(self, views: SqlViews, property: str) -> str:
        """The property where a number is stored, null otherwise, by the name the layout holds it by (`$$score`)."""
        return number_of(self.row, held_key(property))

    def _first(self, of: str, order: str, kept: str) -> str:
        return f"(array_agg({of} ORDER BY {order}) FILTER (WHERE {kept}))[1]"

    def _id_value(self, property: str) -> str:
        return json_value("p", held_key(property))

    def _having_value(self, parameter: str) -> str:
        """
        A number, whatever the aggregate: Postgres takes a value bound without a type as the type of what it is compared
        with, and a count is a whole number, so `0.5` compared with one would be refused.
        """
        return f"CAST({parameter} AS numeric)"
