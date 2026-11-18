from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from typing import Any, Literal, Protocol

from drivine.annotation.direction import Direction
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.group_statement import COMPARISONS, GroupStatement, whole_number
from drivine.backend.sql.sql_graph import SqlGraph
from drivine.backend.sql.sql_views import SqlViews
from drivine.drivine_error import DrivineError
from drivine.dsl.condition_writer import ConditionWriter
from drivine.manager.group_aggregate import GroupAggregate
from drivine.manager.group_query import GroupQuery
from drivine.manager.path_key import PathKey
from drivine.model.fragment_model import FragmentModel


class HeldAggregate(Protocol):
    """
    A least or greatest value a store reads through the node of the group that has it, as drivine4j's SQLite and
    Postgres do: the statement joins that node back, as the row `holder`, to compare and order the value there. On
    SQLite the aggregate's column is that node's `nid`, and the value is given there too. Where the store `gives_value`,
    as drivine4j's Postgres does, the value is the aggregate's column itself (see `given`), the node's `nid` a column of
    its own, with `r` after the aggregate's, and the node is joined only where the groups are ordered or filtered by
    the value.
    """

    gives_value: bool
    """Whether the aggregate's column is the value itself (see `given`), not the `nid` of the node that has it."""

    def column(self, filter: str | None) -> str:
        """The `nid` of the group's first node in the order the aggregate takes, of those that meet `filter`."""
        ...

    def given(self, filter: str | None) -> str:
        """The value of that node, as the group's column gives it, where the store `gives_value`."""
        ...

    def value(self, holder: str) -> str:
        """The value, as the JSON the group's row gives."""
        ...

    def having(self, holder: str, comparison: str, value: Any) -> str:
        """The condition of a `having` that compares the value by `comparison` (`gte`, …) with `value`, bound."""
        ...

    def order(self, holder: str, descending: bool) -> list[str]:
        """The terms the groups are ordered by the value by."""
        ...


HeldOrder = Callable[[str, bool], list[str]]
"""The terms a key is ordered by through a node joined back as the row `holder`, descending where asked."""


class SqlGroups(ABC):
    """
    A grouped read on a SQL store, as one statement: each node once with its keys (a property's value as JSON, a path
    key's far node), grouped, numbered, filtered by `having`, ordered (ties broken by the keys) and paged. Returns a
    row for each group, `{k0, …, a0, …}`, as the graph engines do: a property key as its JSON value, a path key as a
    fragment row. What is each store's own is how it holds a key's JSON value and reads it back.
    """

    row = "n"
    """What the statement calls the row of a node of the source."""

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
        for name, group_key in keys:
            if isinstance(group_key, PathKey) and not path_targets[name].node_id_property:
                raise DrivineError(
                    f"{path_targets[name].type.__name__} has no NodeId field, so a grouped read can't key by it: "
                    f"the path key {name} is ordered by the id of the node it reaches."
                )
        bindings: dict[str, Any] = dict(query.bindings)
        n = self.row
        froms = [f"FROM {self.graph.nodes} {n}"]
        wheres = [views.has_labels(n, source.labels), *([f"({query.where})"] if query.where else [])]
        key_columns: list[str] = []
        for i, (name, group_key) in enumerate(keys):
            if not isinstance(group_key, PathKey):
                key_columns.append(f"{self._key_column(str(group_key.property))} AS k{i}")
                continue
            if not group_key.hops:
                raise DrivineError(f"The path key {name} needs at least one hop.")
            at = n
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
            return self._aggregated(views, str(aggregate.property)) if aggregate.property else "1"

        def filtered(aggregate: GroupAggregate, of: str) -> str:
            return f"CASE WHEN {aggregate.filter} THEN {of} END" if aggregate.filter else of

        held = [self._held(aggregate) for _, aggregate in aggregates]
        # Whether the groups are ordered or filtered by the aggregate: a given value is then joined back by its node.
        ordered_or_filtered = [any(it[0] == name for it in [*query.order_by, *query.having]) for name, _ in aggregates]

        def holds(i: int) -> bool:
            """Whether the aggregate's value is read through its node, joined back as its holder."""
            held_aggregate = held[i]
            return held_aggregate is not None and (not held_aggregate.gives_value or ordered_or_filtered[i])

        held_keys = [
            self._held_path(path_targets[name])
            if isinstance(group_key, PathKey)
            else self._held_key(str(group_key.property))
            for name, group_key in keys
        ]

        def held_by(i: int) -> str:
            """The column of the node a held key is ordered through: a property key's representative, a path key's."""
            return f"g.k{i}" if isinstance(keys[i][1], PathKey) else f"g.k{i}r"

        def holder(i: int) -> str:
            return f"a{i}_"

        def key_holder(i: int) -> str:
            return f"k{i}_"

        aggregate_expressions: list[str] = []
        aggregate_columns: list[str] = []
        for i, (_, aggregate) in enumerate(aggregates):
            held_aggregate = held[i]
            if held_aggregate is not None and held_aggregate.gives_value:
                aggregate_expressions.append(held_aggregate.given(aggregate.filter))
                aggregate_columns.append(
                    ", ".join(
                        [
                            f"{aggregate_expressions[i]} AS a{i}",
                            *(
                                [f"{held_aggregate.column(aggregate.filter)} AS a{i}r"]
                                if ordered_or_filtered[i]
                                else []
                            ),
                        ]
                    )
                )
                continue
            if held_aggregate is not None:
                aggregate_expressions.append(held_aggregate.column(aggregate.filter))
            elif aggregate.function == "COUNT":
                aggregate_expressions.append(
                    f"SUM(CASE WHEN {aggregate.filter} THEN 1 ELSE 0 END)" if aggregate.filter else "COUNT(*)"
                )
            elif aggregate.function == "SUM":
                aggregate_expressions.append(views.zero_if_null(f"SUM({filtered(aggregate, value(aggregate))})"))
            else:
                aggregate_expressions.append(f"{aggregate.function}({filtered(aggregate, value(aggregate))})")
            aggregate_columns.append(f"{aggregate_expressions[i]} AS a{i}")
        names = {name: f"k{i}" for i, (name, _) in enumerate(keys)}
        names.update({name: f"a{i}" for i, (name, _) in enumerate(aggregates)})
        having: list[str] = []
        held_having: list[str] = []
        for i, (name, comparison, compared) in enumerate(query.having):
            column = names.get(name)
            if column is None or not column.startswith("a"):
                raise DrivineError(f"having names {name}, which is not an aggregate of the read.")
            held_aggregate = held[int(column[1:])]
            if held_aggregate is not None:
                held_having.append(held_aggregate.having(holder(int(column[1:])), comparison, compared))
                continue
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
            i = int(column[1:])
            if not column.startswith("k"):
                held_aggregate = held[i]
                if held_aggregate is not None:
                    return ", ".join(held_aggregate.order(holder(i), not ascending))
                return self._ordered(f"g.{column}", ascending, "AGGREGATE")
            held_key = held_keys[i]
            if held_key is not None:
                return ", ".join(held_key(key_holder(i), not ascending))
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
        ] + [(f"a{i}", f"g.a{i}" if it is None or it.gives_value else it.value(holder(i))) for i, it in enumerate(held)]
        holders = [
            *(
                f"LEFT JOIN {self.graph.nodes} {key_holder(i)} ON {key_holder(i)}.nid = {held_by(i)}"
                for i, it in enumerate(held_keys)
                if it is not None
            ),
            *(
                f"LEFT JOIN {self.graph.nodes} {holder(i)} ON {holder(i)}.nid = g.a{i}{'r' if it.gives_value else ''}"
                for i, it in enumerate(held)
                if it is not None and holds(i)
            ),
        ]
        representatives = [
            f"MIN({n}.nid) AS k{i}r"
            for i, it in enumerate(held_keys)
            if it is not None and not isinstance(keys[i][1], PathKey)
        ]
        key_names = [f"m.k{i}" for i in range(len(keys))]
        text = [
            f'SELECT {views.object(projected)} AS "row"',
            "FROM (",
            f"    SELECT {', '.join([*key_names, *aggregate_columns, *representatives])}",
            f"    FROM (SELECT DISTINCT {n}.nid, {', '.join(key_columns)} {' '.join(froms)} "
            f"WHERE {' AND '.join(wheres)}) m",
            f"    JOIN {self.graph.nodes} {n} ON {n}.nid = m.nid",
            f"    GROUP BY {', '.join(key_names)}",
            *([f"    HAVING {' AND '.join(having)}"] if having else []),
            ") g",
            *holders,
            *([f"WHERE {' AND '.join(held_having)}"] if held_having else []),
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

    def _aggregated(self, views: SqlViews, property: str) -> str:
        """
        The property of the source's row read as the number a sum or a mean adds, and by default a least or greatest
        value: null where it holds anything else.
        """
        return views.number(self.row, property)

    def _held(self, aggregate: GroupAggregate) -> HeldAggregate | None:
        """How a least or greatest value is read through the node that has it, where the store does so; else None."""
        return None

    def _held_key(self, property: str) -> HeldOrder | None:
        """
        How a property key is ordered through a node of the group that has its value, as drivine4j's SQLite orders one:
        the statement gives the group's least `nid` as the key's column with `r` after it, joins that node back as the
        row `holder`, and orders by the terms this gives for `property` there. None where the store orders the key's
        column itself (see `_ordered`).
        """
        return None

    def _held_path(self, target: FragmentModel) -> HeldOrder | None:
        """
        How a path key is ordered through its node, `target`, joined back by the key's column as the row `holder`, as
        drivine4j's SQLite orders one: by the terms this gives for the node's id there. None where the store orders the
        key by its node's id value (see `_id_value`).
        """
        return None

    @abstractmethod
    def _views(self) -> SqlViews:
        """The store's views, which write the steps of a path key and its JSON."""

    @abstractmethod
    def _renderer(self, views: SqlViews, labels_of_class: Callable[[type], list[str]]) -> ConditionWriter:
        """The store's writer of a group's filter, over the source's row, which binds its values."""

    @abstractmethod
    def _key_column(self, property: str) -> str:
        """The property of the source's row, as the JSON value a group's key holds."""

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
