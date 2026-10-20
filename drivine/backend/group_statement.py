from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from drivine.annotation.direction import Direction
from drivine.backend.cypher import label_expression, quote_name
from drivine.backend.graph_statement import GraphStatement
from drivine.drivine_error import DrivineError
from drivine.dsl.condition import all_of_one
from drivine.dsl.condition_writer import ConditionWriter
from drivine.dsl.model_scope import scope_of
from drivine.dsl.query_renderer import root_property
from drivine.dsl.ref import Ref
from drivine.manager.group_aggregate import GroupAggregate
from drivine.manager.group_query import GroupQuery
from drivine.manager.path_key import PathKey
from drivine.manager.property_key import PropertyKey
from drivine.model.fragment_model import FragmentModel

COMPARISONS = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}

NODE_ALIAS = "n"


class GroupStatement:
    """
    One statement that groups the nodes of a fragment by keys and gives numbers about each group. A node is one member
    of each group it reaches, however many ways lead there. Returns a row for each group, `{k0, k1, ..., a0, a1, ...}`,
    a path key as a fragment row.
    """

    @staticmethod
    def resolved(model: FragmentModel, query: GroupQuery, renderer: ConditionWriter) -> GroupQuery:
        """The query with its references as stored names and its conditions in the store's language over `n`."""
        scope = scope_of(model)

        def name(prop: Any, what: str) -> str:
            if isinstance(prop, str):
                return prop
            if not isinstance(prop, Ref):
                raise DrivineError("A group key or aggregate takes a property: its stored name, or a reference to it.")
            return root_property(prop.path, scope, what)

        def cypher(where: Any) -> str | None:
            if where is None or isinstance(where, str):
                return where
            return renderer.where(all_of_one(where), scope, NODE_ALIAS)

        keys = {
            it: PropertyKey(name(group_key.property, "A group key"))
            if isinstance(group_key, PropertyKey)
            else group_key
            for it, group_key in query.keys.items()
        }
        aggregates = {
            it: GroupAggregate(
                aggregate.function,
                None if aggregate.property is None else name(aggregate.property, "An aggregate"),
                cypher(aggregate.filter),
            )
            for it, aggregate in query.aggregates.items()
        }
        where = cypher(query.where)
        return dataclasses.replace(
            query, keys=keys, aggregates=aggregates, where=where, bindings={**query.bindings, **renderer.bindings}
        )

    @staticmethod
    def build(source: FragmentModel, query: GroupQuery, path_targets: Mapping[str, FragmentModel]) -> GraphStatement:
        keys = list(query.keys.items())
        aggregates = list(query.aggregates.items())
        if not keys:
            raise DrivineError("A grouped read needs at least one key.")
        names: dict[str, str] = {}
        for i, (it, _) in enumerate(keys):
            names[it] = f"k{i}"
        for i, (it, _) in enumerate(aggregates):
            names[it] = f"a{i}"
        bindings: dict[str, Any] = dict(query.bindings)
        parts = [f"MATCH (n{label_expression(source.labels)})"]
        path_aliases: list[str] = []
        for i, (it, group_key) in enumerate(keys):
            if isinstance(group_key, PathKey):
                parts.append(f"MATCH {_path_pattern(group_key, f'k{i}', path_targets[it])}")
                path_aliases.append(f"k{i}")
        if query.where:
            parts.append(f"WHERE {query.where}")
        if path_aliases:
            parts.append(f"WITH DISTINCT n, {', '.join(path_aliases)}")
        key_columns = [
            f"k{i}" if isinstance(group_key, PathKey) else f"{_property(group_key.property)} AS k{i}"
            for i, (_, group_key) in enumerate(keys)
        ]
        aggregate_columns = [f"{_aggregate_of(aggregate)} AS a{i}" for i, (_, aggregate) in enumerate(aggregates)]
        parts.append(f"WITH {', '.join([*key_columns, *aggregate_columns])}")
        having: list[str] = []
        for i, (it, comparison, value) in enumerate(query.having):
            column = names.get(it)
            if column is None or not column.startswith("a"):
                raise DrivineError(f"having names {it}, which is not an aggregate of the read.")
            bindings[f"_having{i}"] = value
            having.append(f"{column} {COMPARISONS[comparison]} $_having{i}")
        if having:
            parts.append(f"WHERE {' AND '.join(having)}")
        projected = [
            f"k{i}: {{props: properties(k{i}), labels: labels(k{i})}}"
            if isinstance(group_key, PathKey)
            else f"k{i}: k{i}"
            for i, (_, group_key) in enumerate(keys)
        ] + [f"a{i}: a{i}" for i in range(len(aggregates))]
        parts.append(f"RETURN {{{', '.join(projected)}}} AS row")

        def order_of(column: str) -> str:
            index = int(column[1:])
            if column.startswith("k") and isinstance(keys[index][1], PathKey):
                target = path_targets[keys[index][0]]
                return f"{column}.{quote_name(target.node_id_property or '')}"
            return column

        ordered: list[tuple[str, str]] = []
        for it, direction in query.order_by:
            column = names.get(it)
            if column is None:
                raise DrivineError(f"order_by names {it}, which is not a key or aggregate of the read.")
            ordered.append((column, f"{order_of(column)} {'DESC' if direction == 'desc' else 'ASC'}"))
        # Ties are broken by the keys the order left out, so a page of groups is the same on every engine.
        ties = [f"{order_of(f'k{i}')} ASC" for i in range(len(keys)) if not any(c == f"k{i}" for c, _ in ordered)]
        parts.append(f"ORDER BY {', '.join([*(text for _, text in ordered), *ties])}")
        if query.skip:
            parts.append(f"SKIP {whole_number(query.skip, 'skip')}")
        if query.limit is not None:
            parts.append(f"LIMIT {whole_number(query.limit, 'limit')}")
        return GraphStatement("\n".join(parts), bindings)


def _property(name: Any) -> str:
    return f"n.{quote_name(str(name))}"


def _aggregate_of(aggregate: GroupAggregate) -> str:
    value = _property(aggregate.property) if aggregate.property else "1"
    filtered = f"CASE WHEN {aggregate.filter} THEN {value} END" if aggregate.filter else value
    if aggregate.function == "COUNT":
        return f"sum(CASE WHEN {aggregate.filter} THEN 1 ELSE 0 END)" if aggregate.filter else "count(n)"
    if aggregate.function == "SUM":
        return f"coalesce(sum({filtered}), 0)"
    return f"{aggregate.function.lower()}({filtered})"


def _path_pattern(group_key: PathKey, alias: str, target: FragmentModel) -> str:
    steps: list[str] = []
    for i, hop in enumerate(group_key.hops):
        last = i == len(group_key.hops) - 1
        edge = f"[:{quote_name(hop.type)}]"
        if hop.direction is Direction.INCOMING:
            arrow = f"<-{edge}-"
        elif hop.direction is Direction.UNDIRECTED:
            arrow = f"-{edge}-"
        else:
            arrow = f"-{edge}->"
        own = [hop.label] if hop.label else []
        steps.append(f"{arrow}({alias if last else ''}{label_expression([*own, *target.labels] if last else own)})")
    return f"(n){''.join(steps)}"


def whole_number(value: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise DrivineError(f"{what} must be a whole number of at least 0.")
    return value
