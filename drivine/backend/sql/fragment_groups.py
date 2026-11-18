from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable, Mapping
from typing import Any

from drivine.backend.graph_statement import GraphStatement
from drivine.backend.group_statement import COMPARISONS
from drivine.backend.sql.fragment_query_renderer import FragmentQueryRenderer
from drivine.backend.sql.fragments import FRAGMENT_ROOT_ROW, Fragments, Names, property_of
from drivine.backend.sql.sql_graph import SqlGraph
from drivine.backend.sql.sql_groups import HeldAggregate, HeldOrder, SqlGroups
from drivine.backend.sql.sql_views import SqlViews
from drivine.cypher.shared_compiler import SharedStore
from drivine.manager.group_aggregate import GroupAggregate
from drivine.manager.group_query import GroupQuery
from drivine.model.fragment_model import FragmentModel


class FragmentGroups(SqlGroups):
    """
    A grouped read whose values are the shared module's (drivine-cypher-spec#235, its fragments; drivine4j's
    `SqliteSelect.groups`, `PostgresSelect.groups` and `OracleSelect.groups`), on each SQL store: a filter is the
    module's SQL for the Cypher the DSL writes, over the row `n_`; a property key is the property's value as the
    store holds it, so values Cypher tells apart are grouped apart. A least or greatest value is the first of the
    group's nodes in Cypher's order of those that have one, joined back to be compared by a `having` and ordered
    there as Cypher does, whatever its kind, a date included. A key is ordered as Cypher orders it, through a node
    of the group that has its value, joined back so too. What is each store's own is how it takes the first of a
    group's values in an order, and adds a number.

    One statement is built by one instance, whose fragments name their parameters apart.
    """

    row = FRAGMENT_ROOT_ROW

    store: SharedStore
    """The store whose module's fragments the statement's values are."""

    gives_value = False
    """
    Whether a least or greatest value is the group's column itself, as drivine4j's Postgres gives it, its node joined
    back only where the groups are ordered or filtered by it; or read on its node, as drivine4j's SQLite reads it.
    """

    def __init__(self, graph: SqlGraph) -> None:
        super().__init__(graph)
        self._fragments = Fragments(self.store, graph.name, self.row)

    def build(
        self,
        source: FragmentModel,
        given: GroupQuery,
        path_targets: Mapping[str, FragmentModel],
        labels_of_class: Callable[[type], list[str]],
    ) -> GraphStatement:
        """
        The statement, its fragments new for it, over the source's row and labels, so their names start from the first,
        and what they bind bound beside the read's own.
        """
        self._fragments = Fragments(self.store, self.graph.name, self.row, source.labels)
        statement = super().build(source, given, path_targets, labels_of_class)
        return self._made(statement.text, {**statement.bindings, **self._fragments.parameters})

    def _made(self, text: str, bindings: Mapping[str, Any]) -> GraphStatement:
        """The statement of the text built, and what it binds: what the fragments ask of it is the store's to write."""
        return GraphStatement(text, bindings)

    def _renderer(self, views: SqlViews, labels_of_class: Callable[[type], list[str]]) -> FragmentQueryRenderer:
        return FragmentQueryRenderer(self._fragments, labels_of_class)

    def _key_column(self, property: str) -> str:
        """The property's value as the store holds it, as its JSON: the module's value of it."""
        return self._fragments.value(property_of(property))

    def _key_value(self, column: str) -> str:
        return column

    def _held(self, aggregate: GroupAggregate) -> HeldAggregate | None:
        """
        A least or greatest value, of the group's first node in Cypher's order by the property, of those that have it
        and meet the aggregate's filter, as drivine4j reads one: compared and ordered on that node, joined back.
        """
        if aggregate.function not in ("MIN", "MAX"):
            return None
        return _HeldExtreme(
            self._fragments,
            self.row,
            property_of(str(aggregate.property)),
            aggregate.function == "MAX",
            self._first,
            self.gives_value,
        )

    def _held_key(self, property: str) -> HeldOrder:
        """
        A key is ordered as Cypher orders it, whatever its kind, a date included: read through the group's node of
        least `nid`, which has the key's value, as drivine4j reads it.
        """
        return lambda holder, descending: self._fragments.ordered(property_of(property), descending, row=holder)

    def _held_path(self, target: FragmentModel) -> HeldOrder:
        """
        A path key is ordered as Cypher orders its node's id, whatever its kind, a date included: read on the node,
        joined back, as drivine4j reads it.
        """
        return lambda holder, descending: self._fragments.ordered(
            property_of(target.node_id_property or ""), descending, row=holder
        )

    @abstractmethod
    def _first(self, of: str, order: str, kept: str) -> str:
        """The first of the group's `of`, in the order `order`, of the rows that meet `kept`: null for none."""


class _HeldExtreme:
    """A grouped `min` or `max` of the property `cypher`, read through the node that has it (FragmentGroups._held)."""

    def __init__(
        self,
        fragments: Fragments,
        row: str,
        cypher: str,
        descending: bool,
        first: Callable[[str, str, str], str],
        gives_value: bool,
    ) -> None:
        self._fragments = fragments
        self._row = row
        self._cypher = cypher
        self._descending = descending
        self._first = first
        self.gives_value = gives_value
        self._taken: tuple[str, str] | None = None

    def _taken_by(self, filter: str | None) -> tuple[str, str]:
        """The order and the condition the first is taken by, compiled once for the node's `nid` and its value."""
        if self._taken is None:
            present = self._fragments.condition(f"{self._cypher} IS NOT NULL")
            order = ", ".join(self._fragments.order(self._cypher, self._descending))
            self._taken = (order, " AND ".join([present, *([f"({filter})"] if filter else [])]))
        return self._taken

    def column(self, filter: str | None) -> str:
        order, kept = self._taken_by(filter)
        return self._first(f"{self._row}.nid", order, kept)

    def given(self, filter: str | None) -> str:
        order, kept = self._taken_by(filter)
        return self._first(self._fragments.value(self._cypher), order, kept)

    def value(self, holder: str) -> str:
        return self._fragments.value(self._cypher, row=holder)

    def having(self, holder: str, comparison: str, value: Any) -> str:
        def compared(names: Names) -> str:
            return f"{self._cypher} {COMPARISONS[comparison]} {names('value')}"

        return self._fragments.condition(compared, {"value": value}, holder)

    def order(self, holder: str, descending: bool) -> list[str]:
        return self._fragments.order(self._cypher, descending, row=holder)
