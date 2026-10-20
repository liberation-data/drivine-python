from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from drivine.annotation.direction import Direction
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.group_statement import COMPARISONS, GroupStatement, whole_number
from drivine.backend.oracle.oracle_graph import OracleGraph, has_labels, json_key, node_document
from drivine.backend.oracle.oracle_query_renderer import OracleQueryRenderer
from drivine.backend.oracle.oracle_views import OracleViews
from drivine.drivine_error import DrivineError
from drivine.manager.group_aggregate import GroupAggregate
from drivine.manager.group_query import GroupQuery
from drivine.manager.path_key import PathKey
from drivine.model.fragment_model import FragmentModel


class OracleGroups:
    """
    A grouped read on Oracle, as one statement: each node once with its keys (a property's value as its JSON text, a
    path key's far node), grouped, numbered, filtered by `having`, ordered (ties broken by the keys) and paged. Returns
    a row for each group, `{k0, …, a0, …}`, as the graph engines do: a property key as its JSON value, a path key as a
    fragment row.
    """

    def __init__(self, graph: OracleGraph) -> None:
        self.graph = graph

    def build(
        self,
        source: FragmentModel,
        given: GroupQuery,
        path_targets: Mapping[str, FragmentModel],
        labels_of_class: Callable[[type], list[str]],
    ) -> GraphStatement:
        views = OracleViews(self.graph)
        query = GroupStatement.resolved(source, given, OracleQueryRenderer(views, labels_of_class))
        keys = list(query.keys.items())
        aggregates = list(query.aggregates.items())
        if not keys:
            raise DrivineError("A grouped read needs at least one key.")
        bindings: dict[str, Any] = dict(query.bindings)
        froms = [f"FROM {self.graph.nodes} n"]
        wheres = [has_labels("n", source.labels), *([f"({query.where})"] if query.where else [])]
        key_columns: list[str] = []
        for i, (name, group_key) in enumerate(keys):
            if not isinstance(group_key, PathKey):
                key_columns.append(
                    f"JSON_SERIALIZE(JSON_QUERY(n.props, '${json_key(str(group_key.property))}' RETURNING JSON)) "
                    f"AS k{i}"
                )
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
            if not aggregate.property:
                return "1"
            return f"JSON_VALUE(n.props, '${json_key(str(aggregate.property))}' RETURNING NUMBER)"

        def filtered(aggregate: GroupAggregate, of: str) -> str:
            return f"CASE WHEN {aggregate.filter} THEN {of} END" if aggregate.filter else of

        aggregate_expressions: list[str] = []
        for _, aggregate in aggregates:
            if aggregate.function == "COUNT":
                aggregate_expressions.append(
                    f"SUM(CASE WHEN {aggregate.filter} THEN 1 ELSE 0 END)" if aggregate.filter else "COUNT(*)"
                )
            elif aggregate.function == "SUM":
                aggregate_expressions.append(f"NVL(SUM({filtered(aggregate, value(aggregate))}), 0)")
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
            having.append(f"{aggregate_expressions[int(column[1:])]} {COMPARISONS[comparison]} $_having{i}")

        def key_of(i: int) -> str:
            name, group_key = keys[i]
            if not isinstance(group_key, PathKey):
                return f"JSON(g.k{i})"
            target = path_targets[name]
            return (
                f"(SELECT JSON_QUERY(p.props, '${json_key(target.node_id_property or '')}' RETURNING JSON) FROM "
                f"{self.graph.nodes} p WHERE p.nid = g.k{i})"
            )

        def column_order(column: str) -> str:
            return key_of(int(column[1:])) if column.startswith("k") else f"g.{column}"

        ordered: list[tuple[str, str]] = []
        for name, direction in query.order_by:
            column = names.get(name)
            if column is None:
                raise DrivineError(f"order_by names {name}, which is not a key or aggregate of the read.")
            ordered.append((column, f"{column_order(column)} {'DESC' if direction == 'desc' else 'ASC'}"))
        ties = [f"{column_order(f'k{i}')} ASC" for i in range(len(keys)) if not any(c == f"k{i}" for c, _ in ordered)]
        projected = [
            f"'k{i}' VALUE JSON(g.k{i})"
            if not isinstance(group_key, PathKey)
            else f"'k{i}' VALUE (SELECT {node_document('p')} FROM {self.graph.nodes} p WHERE p.nid = g.k{i})"
            for i, (_, group_key) in enumerate(keys)
        ] + [f"'a{i}' VALUE g.a{i}" for i in range(len(aggregates))]
        key_names = [f"m.k{i}" for i in range(len(keys))]
        text = [
            f'SELECT JSON_OBJECT({", ".join(projected)} RETURNING JSON) AS "row"',
            "FROM (",
            f"    SELECT {', '.join([*key_names, *aggregate_columns])}",
            f"    FROM (SELECT DISTINCT n.nid, {', '.join(key_columns)} {' '.join(froms)} "
            f"WHERE {' AND '.join(wheres)}) m",
            f"    JOIN {self.graph.nodes} n ON n.nid = m.nid",
            f"    GROUP BY {', '.join(key_names)}",
            *([f"    HAVING {' AND '.join(having)}"] if having else []),
            ") g",
            f"ORDER BY {', '.join([*(it for _, it in ordered), *ties])}",
            *([f"OFFSET {whole_number(query.skip, 'skip')} ROWS"] if query.skip else []),
            *([f"FETCH NEXT {whole_number(query.limit, 'limit')} ROWS ONLY"] if query.limit is not None else []),
        ]
        return GraphStatement("\n".join(text), bindings)
