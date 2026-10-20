from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic_core import to_jsonable_python

from drivine.backend.oracle.oracle_graph import RELATED_SUBQUERY, has_labels, holding, json_key
from drivine.backend.oracle.oracle_json import storable
from drivine.backend.oracle.oracle_views import OracleViews
from drivine.backend.save_statement_builder import stored
from drivine.backend.view_projector import target
from drivine.drivine_error import DrivineError
from drivine.dsl.any_label_test import AnyLabelTest
from drivine.dsl.collection_sort import CollectionSort
from drivine.dsl.compare_test import CompareTest
from drivine.dsl.composite_condition import CompositeCondition
from drivine.dsl.condition import Condition, Test, all_of_one
from drivine.dsl.condition_node import ConditionNode
from drivine.dsl.expression_node import ExpressionNode
from drivine.dsl.instance_of_test import InstanceOfTest
from drivine.dsl.model_scope import scope_of, step
from drivine.dsl.quantify_test import QuantifyTest
from drivine.dsl.query import Query
from drivine.dsl.query_renderer import (
    check_keyset,
    classify,
    count,
    described,
    group,
    list_of,
    pinned_properties,
    root_property,
)
from drivine.dsl.rendered_query import RenderedQuery
from drivine.dsl.root_key import RootKey
from drivine.dsl.scope import Scope
from drivine.dsl.seek_value import SeekValue
from drivine.dsl.step import Step
from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.target_model import TargetModel

_PATH_OPERATORS = {"EQ": "==", "NEQ": "!=", "GT": ">", "GTE": ">=", "LT": "<", "LTE": "<="}


@dataclass(frozen=True)
class _Rows:
    """The rows a path's conditions read: the node's, and the relationship's that a relationship fragment holds."""

    node: str
    rel: str | None = None


class OracleQueryRenderer:
    """
    Writes a query over a class's references as SQL over Oracle's tables, the root's row bound to an alias, as
    TypeScript's Drivine writes it. A property is tested in a JSON path filter against a bound value
    (`JSON_EXISTS(props, '$?(@."k" > $v)' PASSING …)`), so numbers, text and booleans compare as their JSON types and a
    missing property meets no comparison, as in Cypher. A condition across a relationship is an EXISTS over the
    relationship table. Values are bound (`$_q0`, …), never written in.
    """

    def __init__(self, views: OracleViews, labels_of_class: Callable[[type], list[str]]) -> None:
        self.views = views
        self.labels_of_class = labels_of_class
        self.bindings: dict[str, Any] = {}

    @staticmethod
    def render(
        query: Query,
        model: TargetModel,
        views: Callable[[dict[str, int]], OracleViews],
        alias: str,
        labels_of_class: Callable[[type], list[str]],
    ) -> RenderedQuery:
        scope = scope_of(model)
        renderer = OracleQueryRenderer(views(dict(query.depth or {})), labels_of_class)
        condition = all_of_one(query.where) if query.where is not None else None
        written = renderer.where(condition, scope, alias) if condition is not None else None
        terms = [classify(it, scope) for it in list_of(query.order_by)]
        keys = [it for it in terms if isinstance(it, RootKey)]
        seek = list_of(query.seek)
        keyset = renderer._keyset(keys, seek, query, alias) if query.seek is not None else None
        return RenderedQuery(
            where=" AND ".join(it for it in (written, keyset) if it is not None) or None,
            order_by=", ".join(renderer._order(it, scope, alias) for it in keys) if keys else None,
            limit=count("limit", query.limit),
            skip=count("skip", query.skip),
            depth=query.depth or {},
            bindings=renderer.bindings,
            ordered=()
            if any(it.term.expression is not None for it in keys)
            else tuple(it.property or "" for it in keys),
            collection_sorts=tuple(it for it in terms if isinstance(it, CollectionSort)),
            pinned=tuple(pinned_properties(condition, scope)) if condition is not None else (),
            paged=any(it is not None for it in (query.order_by, query.limit, query.skip, query.seek)),
            seeks=query.seek is not None,
        )

    @staticmethod
    def render_where(
        where: Any, model: TargetModel, views: OracleViews, alias: str, labels_of_class: Callable[[type], list[str]]
    ) -> tuple[str, dict[str, Any]]:
        """A condition over the class's references, written for the store, with the values it binds."""
        renderer = OracleQueryRenderer(views, labels_of_class)
        written = renderer.where(all_of_one(where), scope_of(model), alias)
        return written, renderer.bindings

    def where(self, condition: Condition, scope: Scope, alias: str) -> str:
        """A condition over the node row `alias`."""
        return self._condition(condition, scope, _Rows(alias))

    def _condition(self, condition: Condition, scope: Scope, rows: _Rows) -> str:
        if isinstance(condition, CompositeCondition):
            parts = [self._condition(it, scope, rows) for it in condition.conditions]
            if condition.kind == "AND":
                return group(parts, " AND ") if parts else "1 = 1"
            if condition.kind == "OR":
                return group(parts, " OR ") if parts else "1 = 0"
            return f"NOT {group(parts, ' AND ', True)}"
        return self._walk(condition.path, 0, condition.test, scope, rows)

    def _walk(self, path: tuple[str, ...], i: int, test: Test, scope: Scope, rows: _Rows) -> str:
        if i == len(path):
            return self._at_node(test, path, rows)
        next_ = step(scope, path[i])
        last = i == len(path) - 1
        if next_.kind == "SAME_NODE":
            assert next_.scope is not None
            return self._walk(path, i + 1, test, next_.scope, rows)
        if next_.kind == "TARGET":
            assert next_.scope is not None
            return self._walk(path, i + 1, test, next_.scope, _Rows(rows.node))
        if next_.kind == "HOP":
            return self._across(next_, path, i, test, rows)
        if next_.kind == "PROPERTY":
            if not last:
                raise DrivineError(f"{described(path[: i + 1])} is a property; it has no fields.")
            column = f"{rows.rel}.props" if next_.on == "RELATIONSHIP" else holding(rows.node, next_.property)
            return self._compare(test, column, next_.property, next_.json_packed, path)
        if not last or not isinstance(test, CompareTest) or test.operator != "HAS_ITEM":
            raise DrivineError(f"{described(path[: i + 1])} holds the node's labels: test it with has_item.")
        return has_labels(rows.node, [str(test.value)])

    def _across(self, next_: Step, path: tuple[str, ...], i: int, test: Test, rows: _Rows) -> str:
        """
        At least one node the relationship leads to meets the rest of the path's test, or a quantifier's conditions.
        """
        rel = next_.rel
        assert rel is not None and next_.scope is not None
        into = target(rel)
        labels = (into.root if isinstance(into, GraphViewModel) else into).labels
        depth = rel.max_depth
        if isinstance(rel.related, FragmentModel) and depth > 1:
            node = self.views.next_alias("x")
            reached = self.views.reached(rel.type, rel.direction, rows.node, depth)
            clause = (
                f"FROM {self.views.graph_nodes} {node} WHERE {node}.nid IN ({reached}) AND {has_labels(node, labels)}"
            )
            inner = _Rows(node)
        else:
            hop = self.views.step(rel.type, rel.direction, rows.node, labels)
            clause = f"{hop.from_} AND {hop.x}"
            inner = _Rows(hop.node, hop.edge)
        quantified = test if i == len(path) - 1 and isinstance(test, QuantifyTest) else None
        if quantified is not None:
            tested = " AND ".join(self._condition(it, next_.scope, inner) for it in quantified.conditions)
        else:
            tested = self._walk(path, i + 1, test, next_.scope, inner)
        exists = f"EXISTS ({RELATED_SUBQUERY} {clause} AND {tested})"
        return f"NOT {exists}" if quantified is not None and quantified.negate else exists

    def _at_node(self, test: Test, path: tuple[str, ...], rows: _Rows) -> str:
        where = described(path)
        if isinstance(test, InstanceOfTest):
            return has_labels(rows.node, self.labels_of_class(test.type))
        if isinstance(test, AnyLabelTest):
            if not test.labels:
                return "1 = 0"
            passing = ", ".join(f'{self._bind(label)} AS "l{i}"' for i, label in enumerate(test.labels))
            tested = " || ".join(f"@ == $l{i}" for i in range(len(test.labels)))
            return f"JSON_EXISTS({rows.node}.labels, '$?({tested})' PASSING {passing})"
        if isinstance(test, QuantifyTest):
            raise DrivineError(f"any and none follow a relationship; {where} isn't one.")
        raise DrivineError(f"{where} is a node: compare one of its fields.")

    def _compare(self, test: Test, column: str, name: str, json_packed: bool, path: tuple[str, ...]) -> str:
        """A test of the property `name` of the JSON column `column`."""
        if not isinstance(test, CompareTest):
            raise DrivineError(f"{described(path)} is a property, not a node.")
        k = json_key(name)

        def packed(value: Any) -> Any:
            return json.dumps(to_jsonable_python(value)) if json_packed else storable(stored(value))

        def filtered(expression: str, values: list[Any]) -> str:
            passing = ", ".join(f'{self._bind(packed(value))} AS "v{i}"' for i, value in enumerate(values))
            return f"JSON_EXISTS({column}, '$?({expression})'{f' PASSING {passing}' if passing else ''})"

        present = f"JSON_EXISTS({column}, '$?(@{k} != null)')"
        operator = test.operator
        if operator in _PATH_OPERATORS:
            return filtered(f"@{k} {_PATH_OPERATORS[operator]} $v0", [test.value])
        if operator in ("IN", "NOT_IN"):
            values: Any = test.value
            if not isinstance(values, list):
                raise DrivineError(f"{described(path)} is tested against a list, not {values!r}.")
            listed: list[Any] = values  # pyright: ignore[reportUnknownVariableType]
            any_ = filtered(" || ".join(f"@{k} == $v{i}" for i in range(len(listed))), listed) if listed else "1 = 0"
            return any_ if operator == "IN" else f"{present} AND NOT {any_}"
        if operator == "HAS_ITEM":
            return f"JSON_EXISTS({column}, '${k}[*]?(@ == $v0)' PASSING {self._bind(packed(test.value))} AS \"v0\")"
        if operator == "IS_NULL":
            return f"NOT {present}"
        if operator == "IS_NOT_NULL":
            return present
        return self._compare_text(operator, test.value, f"JSON_VALUE({column}, '${k}')")

    def _compare_text(self, operator: str, value: Any, text: str) -> str:
        """A test of a property's text."""
        if operator == "CONTAINS":
            return f"INSTR({text}, {self._bind(value)}) > 0"
        if operator == "STARTS_WITH":
            return f"INSTR({text}, {self._bind(value)}) = 1"
        if operator == "ENDS_WITH":
            bound = self._bind(value)
            return f"SUBSTR({text}, -LENGTH({bound})) = {bound}"
        if operator == "MATCHES":
            return f"REGEXP_LIKE({text}, '^(' || {self._bind(value)} || ')$')"
        if operator == "CONTAINS_IGNORE_CASE":
            return f"INSTR(LOWER({text}), {self._bind(str(value).lower())}) > 0"
        return f"LOWER({text}) = {self._bind(str(value).lower())}"

    def _keyset(self, keys: list[RootKey], seek: list[SeekValue], query: Query, alias: str) -> str:
        """
        The predicate that continues after a cursor, as on the graph engines: a leading bound on the first key, then
        the lexicographic branches, each a JSON path filter so the cursor's values compare as their JSON types.
        """
        check_keyset(keys, seek, query)
        values = [self._bind(storable(it.value)) for it in seek]

        def tested(i: int, operator: str) -> str:
            prop = keys[i].property or ""
            return (
                f"JSON_EXISTS({holding(alias, prop)}, '$?(@{json_key(prop)} {operator} $v)' PASSING {values[i]} "
                'AS "v")'
            )

        def compare(i: int, strict: bool) -> str:
            return tested(i, f"{'>' if keys[i].term.direction == 'ASC' else '<'}{'' if strict else '='}")

        branches: list[str] = []
        for i in range(len(keys)):
            branch = " AND ".join([*(tested(j, "==") for j in range(i)), compare(i, True)])
            branches.append(branch if i == 0 else f"({branch})")
        return " AND ".join(
            [
                *([compare(0, False)] if len(keys) > 1 else []),
                f"({' OR '.join(branches)})" if len(branches) > 1 else branches[0],
            ]
        )

    def _order(self, key: RootKey, scope: Scope, alias: str) -> str:
        """An order term: a property by its JSON type (numbers as numbers), or an expression; nulls as placed."""
        if key.term.expression is not None:
            value = self._expression(key.term.expression, scope, alias, "NUMBER")
        else:
            prop = key.property or ""
            value = f"JSON_QUERY({holding(alias, prop)}, '${json_key(prop)}' RETURNING JSON)"
        nulls = f" NULLS {key.term.nulls}" if key.term.nulls else ""
        return f"{value}{' DESC' if key.term.direction == 'DESC' else ''}{nulls}"

    def _expression(self, node: ExpressionNode, scope: Scope, alias: str, as_: Literal["NUMBER", "TEXT"]) -> str:
        """An order expression over the root's properties: as numbers, or as text where it is compared with text."""

        def of(it: ExpressionNode) -> str:
            return self._expression(it, scope, alias, "NUMBER")

        if node.kind == "PROPERTY":
            prop = root_property(node.path, scope, "An expression")
            k = json_key(prop)
            if as_ == "NUMBER":
                return f"JSON_VALUE({holding(alias, prop)}, '${k}' RETURNING NUMBER)"
            return f"JSON_VALUE({holding(alias, prop)}, '${k}')"
        if node.kind == "VALUE":
            return self._bind(storable(node.value))
        if node.kind == "ARITHMETIC":
            left, right = node.args
            return f"({of(left)} {node.operator} {of(right)})"
        if node.kind == "NEGATE":
            return f"(-{of(node.args[0])})"
        if node.kind == "FUNCTION":
            return f"{node.name.upper()}({', '.join(of(it) for it in node.args)})"
        assert node.otherwise is not None
        branches = " ".join(
            f"WHEN {self._case_condition(when, scope, alias)} THEN {of(then)}" for when, then in node.branches
        )
        return f"CASE {branches} ELSE {of(node.otherwise)} END"

    def _case_condition(self, node: ConditionNode, scope: Scope, alias: str) -> str:
        if node.kind == "COMPARE":
            textual = any(it.kind == "VALUE" and not _numeric(it.value) for it in node.args)
            as_: Literal["NUMBER", "TEXT"] = "TEXT" if textual else "NUMBER"
            left, right = node.args
            return (
                f"{self._expression(left, scope, alias, as_)} {node.operator} "
                f"{self._expression(right, scope, alias, as_)}"
            )
        if node.kind == "HAS_VALUE":
            operand = node.args[0]
            if operand.kind == "PROPERTY":
                prop = root_property(operand.path, scope, "An expression")
                tested = f"JSON_EXISTS({holding(alias, prop)}, '$?(@{json_key(prop)} != null)')"
            else:
                tested = f"{self._expression(operand, scope, alias, 'NUMBER')} IS NOT NULL"
            return f"NOT {tested}" if node.negate else tested
        if node.kind == "NOT":
            return f"NOT ({self._case_condition(node.conditions[0], scope, alias)})"
        joined = f" {node.kind} ".join(self._case_condition(it, scope, alias) for it in node.conditions)
        return f"({joined})"

    def _bind(self, value: Any) -> str:
        name = f"_q{len(self.bindings)}"
        self.bindings[name] = value
        return f"${name}"


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
