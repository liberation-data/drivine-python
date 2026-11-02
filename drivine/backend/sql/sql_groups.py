from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from typing import Any, Literal

from drivine.annotation.direction import Direction
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.group_statement import COMPARISONS, GroupStatement, whole_number
from drivine.backend.sql.sql_graph import SqlGraph
from drivine.backend.sql.sql_query_renderer import SqlQueryRenderer
from drivine.backend.sql.sql_views import SqlViews
from drivine.drivine_error import DrivineError
from drivine.manager.group_aggregate import GroupAggregate
from drivine.manager.group_query import GroupQuery
from drivine.manager.path_key import PathKey
from drivine.model.fragment_model import FragmentModel


class SqlGroups(ABC):
    """
    A grouped read on a SQL store, as one statement: each node once with its keys (a property's value as JSON, a path
    key's far node), grouped, numbered, filtered by `having`, ordered (ties broken by the keys) and paged. Returns a
    row for each group, `{k0, …, a0, …}`, as the graph engines do: a property key as its JSON value, a path key as a
    fragment row. What is each store's own is how it holds a key's JSON value and reads it back.
    """

    def __init__(self, graph: SqlGraph) -> None:
        self.graph = graph

    def build(
        self,
        source: FragmentModel,
        given: GroupQuery,
        path_targets: Mapping[str, FragmentModel],
        labels_of_class: Callable[[type], list[str]],
    ) -> GraphStatement:
        views = self._views()
        query = GroupStatement.resolved(source, given, self._renderer(views, labels_of_class))
        keys = list(query.keys.items())
        aggregates = list(query.aggregates.items())
        if not keys:
            raise DrivineError("A grouped read needs at least one key.")
        bindings: dict[str, Any] = dict(query.bindings)
        froms = [f"FROM {self.graph.nodes} n"]
        wheres = [views.has_labels("n", source.labels), *([f"({query.where})"] if query.where else [])]
        key_columns: list[str] = []
        for i, (name, group_key) in enumerate(keys):
            if not isinstance(group_key, PathKey):
                key_columns.append(f"{self._key_column(str(group_key.property))} AS k{i}")
                continue
            if not group_key.hops:
                raise DrivineError(f"The path key {name} needs at least one hop.")
            at = "n"
            for h, hop in enumerate(group_key.hops):
                last = h == len(group_key.hops) - 1
                labels = [*([hop.label] if hop.label else []), *(path_targets[name].labels if last else [])]
                step = views.step(hop.type, hop.direction or Direction.OUTGOING, at, labels)
                from_clause, where_clause = step.from_.split("\nWHERE ")
                froms.append(from_clause.replace("FROM ", "CROSS JOIN ", 1))
                wheres.extend([where_clause, step.x])
                at = step.node
            key_columns.append(f"{at}.nid AS k{i}")

        def value(aggregate: GroupAggregate) -> str:
            return views.number("n", str(aggregate.property)) if aggregate.property else "1"

        def filtered(aggregate: GroupAggregate, of: str) -> str:
            return f"CASE WHEN {aggregate.filter} THEN {of} END" if aggregate.filter else of

        aggregate_expressions: list[str] = []
        for _, aggregate in aggregates:
            if aggregate.function == "COUNT":
                aggregate_expressions.append(
                    f"SUM(CASE WHEN {aggregate.filter} THEN 1 ELSE 0 END)" if aggregate.filter else "COUNT(*)"
                )
            elif aggregate.function == "SUM":
                aggregate_expressions.append(views.zero_if_null(f"SUM({filtered(aggregate, value(aggregate))})"))
            else:
                aggregate_expressions.append(f"{aggregate.function}({filtered(aggregate, value(aggregate))})")
        aggregate_columns = [f"{it} AS a{i}" for i, it in enumerate(aggregate_expressions)]
        names = {name: f"k{i}" for i, (name, _) in enumerate(keys)}
        names.update({name: f"a{i}" for i, (name, _) in enumerate(aggregates)})
        having: list[str] = []
        for i, (name, comparison, compared) in enumerate(query.having):
            column = names.get(name)
            if column is None or not column.startswith("a"):
                raise DrivineError(f"having names {name}, which is not an aggregate of the read.")
            bindings[f"_having{i}"] = compared
            compared_with = self._having_value(f"$_having{i}")
            having.append(f"{aggregate_expressions[int(column[1:])]} {COMPARISONS[comparison]} {compared_with}")

        def key_of(i: int) -> str:
            name, group_key = keys[i]
            if not isinstance(group_key, PathKey):
                return self._key_value(f"g.k{i}")
            target = path_targets[name]
            return (
                f"(SELECT {self._id_value(target.node_id_property or '')} FROM {self.graph.nodes} p "
                f"WHERE p.nid = g.k{i})"
            )

        def column_order(column: str, ascending: bool) -> str:
            if not column.startswith("k"):
                return self._ordered(f"g.{column}", ascending, "AGGREGATE")
            i = int(column[1:])
            return self._ordered(key_of(i), ascending, "PATH" if isinstance(keys[i][1], PathKey) else "KEY")

        ordered: list[tuple[str, str]] = []
        for name, direction in query.order_by:
            column = names.get(name)
            if column is None:
                raise DrivineError(f"order_by names {name}, which is not a key or aggregate of the read.")
            ordered.append((column, column_order(column, direction != "desc")))
        ties = [column_order(f"k{i}", True) for i in range(len(keys)) if not any(c == f"k{i}" for c, _ in ordered)]
        projected = [
            (f"k{i}", self._key_value(f"g.k{i}"))
            if not isinstance(group_key, PathKey)
            else (f"k{i}", f"(SELECT {views.node_document('p')} FROM {self.graph.nodes} p WHERE p.nid = g.k{i})")
            for i, (_, group_key) in enumerate(keys)
        ] + [(f"a{i}", f"g.a{i}") for i in range(len(aggregates))]
        key_names = [f"m.k{i}" for i in range(len(keys))]
        text = [
            f'SELECT {views.object(projected)} AS "row"',
            "FROM (",
            f"    SELECT {', '.join([*key_names, *aggregate_columns])}",
            f"    FROM (SELECT DISTINCT n.nid, {', '.join(key_columns)} {' '.join(froms)} "
            f"WHERE {' AND '.join(wheres)}) m",
            f"    JOIN {self.graph.nodes} n ON n.nid = m.nid",
            f"    GROUP BY {', '.join(key_names)}",
            *([f"    HAVING {' AND '.join(having)}"] if having else []),
            ") g",
            f"ORDER BY {', '.join([*(it for _, it in ordered), *ties])}",
            *self._page(
                whole_number(query.skip, "skip") if query.skip else None,
                whole_number(query.limit, "limit") if query.limit is not None else None,
            ),
        ]
        return GraphStatement("\n".join(text), bindings)

    def _page(self, skip: int | None, limit: int | None) -> list[str]:
        """The clauses that skip `skip` groups and keep `limit`, where either is given."""
        return [
            *([f"OFFSET {skip} ROWS"] if skip is not None else []),
            *([f"FETCH NEXT {limit} ROWS ONLY"] if limit is not None else []),
        ]

    def _having_value(self, parameter: str) -> str:
        """The bound value `having` compares an aggregate with, as the store reads a number bound without a type."""
        return parameter

    @abstractmethod
    def _views(self) -> SqlViews:
        """The store's views, which write the steps of a path key and its JSON."""

    @abstractmethod
    def _renderer(self, views: SqlViews, labels_of_class: Callable[[type], list[str]]) -> SqlQueryRenderer:
        """The store's query renderer over the views, which writes a group's filter."""

    @abstractmethod
    def _key_column(self, property: str) -> str:
        """The property of the node row `n`, as the JSON value a group's key holds."""

    @abstractmethod
    def _key_value(self, column: str) -> str:
        """A group's key column `column`, as the JSON value it is ordered and given back as."""

    def _ordered(self, value: str, ascending: bool, kind: Literal["KEY", "PATH", "AGGREGATE"]) -> str:
        """
        An order term of `value`: a property key's JSON value, a path key's far node's id, or an aggregate. The store
        orders its JSON as the graph engines do, a null last ascending, unless said.
        """
        return f"{value} {'ASC' if ascending else 'DESC'}"

    @abstractmethod
    def _id_value(self, property: str) -> str:
        """The id property of the node row `p`, as the JSON value a path key is ordered by."""
