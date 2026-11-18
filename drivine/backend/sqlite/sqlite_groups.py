from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal, cast

from drivine.backend.cypher import quote_name
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.group_statement import COMPARISONS
from drivine.backend.sql.fragment_query_renderer import FragmentQueryRenderer
from drivine.backend.sql.fragments import FRAGMENT_ROOT_ROW, NODE, Fragments, Names
from drivine.backend.sql.sql_groups import HeldAggregate, HeldOrder, SqlGroups
from drivine.backend.sql.sql_views import SqlViews
from drivine.backend.sqlite.sqlite_graph import SqliteGraph, path
from drivine.backend.sqlite.sqlite_views import SqliteViews
from drivine.manager.group_aggregate import GroupAggregate
from drivine.manager.group_query import GroupQuery
from drivine.model.fragment_model import FragmentModel


class SqliteGroups(SqlGroups):
    """
    A grouped read on SQLite (see SqlGroups), its values the shared module's (drivine-cypher-spec#235, its fragments;
    drivine4j's `SqliteSelect.groups`): a filter is the module's SQL for the Cypher the DSL writes, over the row `n_`; a
    property key is the property's value as the store holds it, so values Cypher tells apart are grouped apart; a sum or
    a mean adds that value where it is a number. A least or greatest value is a node of the group that has it, the first
    in Cypher's order of those that have one, joined back: the value is given, compared by a `having` and ordered there
    as Cypher does, whatever its kind, a date included. A key is ordered as Cypher orders it, through a node of the
    group that has its value, joined back so too. Paged with `LIMIT` and `OFFSET`.

    One statement is built by one SqliteGroups, whose fragments name their parameters apart.
    """

    row = FRAGMENT_ROOT_ROW

    def __init__(self, graph: SqliteGraph) -> None:
        super().__init__(graph)
        self._fragments = Fragments("SQLITE", graph.name, self.row)

    def build(
        self,
        source: FragmentModel,
        given: GroupQuery,
        path_targets: Mapping[str, FragmentModel],
        labels_of_class: Callable[[type], list[str]],
    ) -> GraphStatement:
        self._fragments = Fragments("SQLITE", cast(SqliteGraph, self.graph).name, self.row, source.labels)
        statement = super().build(source, given, path_targets, labels_of_class)
        return GraphStatement(statement.text, {**statement.bindings, **self._fragments.parameters})

    def _views(self) -> SqliteViews:
        return SqliteViews(cast(SqliteGraph, self.graph))

    def _renderer(self, views: SqlViews, labels_of_class: Callable[[type], list[str]]) -> FragmentQueryRenderer:
        return FragmentQueryRenderer(self._fragments, labels_of_class)

    def _key_column(self, property: str) -> str:
        """The property's value as the store holds it, as its JSON: the module's value of it."""
        return self._fragments.value(_cypher_of(property))

    def _key_value(self, column: str) -> str:
        return column

    def _aggregated(self, views: SqlViews, property: str) -> str:
        """The property's value as the store holds it (see `_key_column`), where it is a number; null otherwise."""
        value = self._fragments.value(_cypher_of(property))
        return f"(CASE WHEN json_type({value}) IN ('integer', 'real') THEN {value} ->> '$' END)"

    def _held(self, aggregate: GroupAggregate) -> HeldAggregate | None:
        """
        A least or greatest value, read through the group's first node in Cypher's order by the property, of those that
        have it and meet the aggregate's filter, as drivine4j's SQLite reads one.
        """
        if aggregate.function not in ("MIN", "MAX"):
            return None
        return _HeldExtreme(self._fragments, self.row, _cypher_of(str(aggregate.property)), aggregate.function == "MAX")

    def _held_key(self, property: str) -> HeldOrder:
        """
        A key is ordered as Cypher orders it, whatever its kind, a date included: read through the group's node of
        least `nid`, which has the key's value, as drivine4j's SQLite reads it.
        """
        return lambda holder, descending: self._fragments.ordered(_cypher_of(property), descending, row=holder)

    def _held_path(self, target: FragmentModel) -> HeldOrder:
        """
        A path key is ordered as Cypher orders its node's id, whatever its kind, a date included: read on the node,
        joined back, as drivine4j's SQLite reads it.
        """
        return lambda holder, descending: self._fragments.ordered(
            _cypher_of(target.node_id_property or ""), descending, row=holder
        )

    def _ordered(self, value: str, ascending: bool, kind: Literal["KEY", "PATH", "AGGREGATE"]) -> str:
        """A null last ascending and first descending, as Postgres places it: SQLite would place it first ascending."""
        return f"{value} {'ASC' if ascending else 'DESC'} {'NULLS LAST' if ascending else 'NULLS FIRST'}"

    def _id_value(self, property: str) -> str:
        return f"(p.props ->> {path(property)})"

    def _page(self, skip: int | None, limit: int | None) -> list[str]:
        if skip is None and limit is None:
            return []
        return [f"LIMIT {limit if limit is not None else -1}" + (f" OFFSET {skip}" if skip is not None else "")]


class _HeldExtreme:
    """A grouped `min` or `max` of the property `cypher`, read through the node that has it (see SqliteGroups._held)."""

    def __init__(self, fragments: Fragments, row: str, cypher: str, descending: bool) -> None:
        self._fragments = fragments
        self._row = row
        self._cypher = cypher
        self._descending = descending

    def column(self, filter: str | None) -> str:
        present = self._fragments.condition(f"{self._cypher} IS NOT NULL")
        order = ", ".join(self._fragments.order(self._cypher, self._descending))
        kept = " AND ".join([present, *([f"({filter})"] if filter else [])])
        return f"(json_group_array({self._row}.nid ORDER BY {order}) FILTER (WHERE {kept})) ->> '$[0]'"

    def value(self, holder: str) -> str:
        return self._fragments.value(self._cypher, row=holder)

    def having(self, holder: str, comparison: str, value: Any) -> str:
        def compared(names: Names) -> str:
            return f"{self._cypher} {COMPARISONS[comparison]} {names('value')}"

        return self._fragments.condition(compared, {"value": value}, holder)

    def order(self, holder: str, descending: bool) -> list[str]:
        return self._fragments.order(self._cypher, descending, row=holder)


def _cypher_of(property: str) -> str:
    """The property of `n`, as Cypher reads it by its stored name."""
    return f"{NODE}.{quote_name(property)}"
