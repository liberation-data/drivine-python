from __future__ import annotations

from typing import Literal, cast

from drivine.backend.sql.fragment_groups import FragmentGroups
from drivine.backend.sql.fragments import property_of
from drivine.backend.sql.sql_views import SqlViews
from drivine.backend.sqlite.sqlite_graph import SqliteGraph, path
from drivine.backend.sqlite.sqlite_views import SqliteViews
from drivine.cypher.shared_compiler import SharedStore


class SqliteGroups(FragmentGroups):
    """
    A grouped read on SQLite (see FragmentGroups; drivine4j's `SqliteSelect.groups`): a sum or a mean adds a property's
    value as the store holds it where it is a number. A least or greatest value is read on the group's node that has
    it, joined back: its column is that node's `nid`. Paged with `LIMIT` and `OFFSET`.
    """

    store: SharedStore = "SQLITE"

    def __init__(self, graph: SqliteGraph) -> None:
        super().__init__(graph)

    def _views(self) -> SqliteViews:
        return SqliteViews(cast(SqliteGraph, self.graph))

    def _aggregated(self, views: SqlViews, property: str) -> str:
        """The property's value as the store holds it (see `_key_column`), where it is a number; null otherwise."""
        value = self._fragments.value(property_of(property))
        return f"(CASE WHEN json_type({value}) IN ('integer', 'real') THEN {value} ->> '$' END)"

    def _first(self, of: str, order: str, kept: str) -> str:
        return f"(json_group_array({of} ORDER BY {order}) FILTER (WHERE {kept})) ->> '$[0]'"

    def _ordered(self, value: str, ascending: bool, kind: Literal["KEY", "PATH", "AGGREGATE"]) -> str:
        """A null last ascending and first descending, as Postgres places it: SQLite would place it first ascending."""
        return f"{value} {'ASC' if ascending else 'DESC'} {'NULLS LAST' if ascending else 'NULLS FIRST'}"

    def _id_value(self, property: str) -> str:
        return f"(p.props ->> {path(property)})"

    def _page(self, skip: int | None, limit: int | None) -> list[str]:
        if skip is None and limit is None:
            return []
        return [f"LIMIT {limit if limit is not None else -1}" + (f" OFFSET {skip}" if skip is not None else "")]
