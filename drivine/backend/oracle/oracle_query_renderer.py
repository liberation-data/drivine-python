from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic_core import to_jsonable_python

from drivine.backend.oracle.oracle_graph import has_labels, holding, json_key, labelled_value, related_subquery
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
from drivine.model import stamps
from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.target_model import TargetModel
from drivine.schema.schema_item import SchemaItem

_PATH_OPERATORS = {"EQ": "==", "NEQ": "!=", "GT": ">", "GTE": ">=", "LT": "<", "LTE": "<="}

_SQL_OPERATORS = {"EQ": "=", "GT": ">", "GTE": ">=", "LT": "<", "LTE": "<="}
"""The comparisons an index answers, as SQL writes them."""


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

    Oracle takes a test of a node's labels for a lookup of one row, so it reads a label's nodes even where a range
    index or a uniqueness constraint would find a few (drivine4j#65). So a comparison with text that every root meets,
    of a property such an index of one of the root's labels begins with, is also written as the index's expression,
    which Oracle reads through the index: see `_through_index`.
    """

    def __init__(
        self,
        views: OracleViews,
        labels_of_class: Callable[[type], list[str]],
        root: str = "",
        root_labels: Sequence[str] = (),
        indexed: Sequence[SchemaItem] = (),
    ) -> None:
        """
        `root` is the root's row, `root_labels` its labels, and `indexed` the indexes a comparison of one of its
        properties may be read through. Without them, none is.
        """
        self.views = views
        self.labels_of_class = labels_of_class
        self.bindings: dict[str, Any] = {}
        self.root = root
        self.root_labels = list(root_labels)
        self.indexed = list(indexed)
        self.tested: list[str] = []
        """The root's labels whose test a comparison makes, through a labelled index."""

    @staticmethod
    def render(
        query: Query,
        model: TargetModel,
        views: Callable[[dict[str, int]], OracleViews],
        alias: str,
        labels_of_class: Callable[[type], list[str]],
        indexed: Sequence[SchemaItem] = (),
    ) -> RenderedQuery:
        scope = scope_of(model)
        root = model.root if isinstance(model, GraphViewModel) else model
        renderer = OracleQueryRenderer(views(dict(query.depth or {})), labels_of_class, alias, root.labels, indexed)
        condition = all_of_one(query.where) if query.where is not None else None
        written = renderer._condition(condition, scope, _Rows(alias), True) if condition is not None else None
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
            labels_tested=tuple(renderer.tested),
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

    def _condition(self, condition: Condition, scope: Scope, rows: _Rows, top: bool = False) -> str:
        """A condition, which every root meets when it is `top`: the query's, or one of those it holds all of."""
        if isinstance(condition, CompositeCondition):
            within = top and condition.kind == "AND"
            parts = [self._condition(it, scope, rows, within) for it in condition.conditions]
            if condition.kind == "AND":
                return group(parts, " AND ") if parts else "1 = 1"
            if condition.kind == "OR":
                return group(parts, " OR ") if parts else "1 = 0"
            return f"NOT {group(parts, ' AND ', True)}"
        return self._walk(condition.path, 0, condition.test, scope, rows, top)

    def _walk(self, path: tuple[str, ...], i: int, test: Test, scope: Scope, rows: _Rows, top: bool = False) -> str:
        if i == len(path):
            return self._at_node(test, path, rows)
        next_ = step(scope, path[i])
        last = i == len(path) - 1
        if next_.kind == "SAME_NODE":
            assert next_.scope is not None
            return self._walk(path, i + 1, test, next_.scope, rows, top)
        if next_.kind == "TARGET":
            assert next_.scope is not None
            return self._walk(path, i + 1, test, next_.scope, _Rows(rows.node))
        if next_.kind == "HOP":
            return self._across(next_, path, i, test, rows)
        if next_.kind == "PROPERTY":
            if not last:
                raise DrivineError(f"{described(path[: i + 1])} is a property; it has no fields.")
            return self._property(next_, test, rows, path, top)
        if not last or not isinstance(test, CompareTest) or test.operator != "HAS_ITEM":
            raise DrivineError(f"{described(path[: i + 1])} holds the node's labels: test it with has_item.")
        return has_labels(rows.node, [str(test.value)])

    def _property(self, next_: Step, test: Test, rows: _Rows, path: tuple[str, ...], top: bool) -> str:
        """A test of a property: of the root, which every root meets when `top`, through an index where it can be."""
        assert next_.property is not None
        column = f"{rows.rel}.props" if next_.on == "RELATIONSHIP" else holding(rows.node, next_.property)
        indexed = (
            self._through_index(test, next_.property, next_.json_packed)
            if top and next_.on != "RELATIONSHIP" and rows.node == self.root
            else None
        )
        compared = self._compare(test, column, next_.property, next_.json_packed, path)
        return f"{indexed} AND {compared}" if indexed else compared

    def _through_index(self, test: Test, name: str, json_packed: bool) -> str | None:
        """
        The comparison of the root's property `name` with text as the expression of a range index or a uniqueness
        constraint of one of the root's labels that begins with the property, the first label with one: None where
        there is none, or where the comparison isn't `eq`, `gt`, `gte`, `lt` or `lte` with text of at most 4,000 bytes.

        It is written beside the JSON test, which still decides: the expression is the property as text, so it holds of
        every node the test holds of, and of a number written as the same text too. It is null for a node without the
        label, so it tests the label, and the statement needn't (see labels_tested).
        """
        if not isinstance(test, CompareTest) or json_packed or name == stamps.PROPERTY:
            return None
        operator = _SQL_OPERATORS.get(test.operator)
        value = storable(stored(test.value))
        if operator is None or not isinstance(value, str) or len(value.encode()) > 4000:
            return None
        label = next(
            (it for it in self.root_labels if any(i.label == it and i.properties[:1] == (name,) for i in self.indexed)),
            None,
        )
        if label is None:
            return None
        if label not in self.tested:
            self.tested.append(label)
        return f"{labelled_value(self.root, label, name)} {operator} {self._bind(value)}"

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
            nodes = self.views.nodes_of(reached, node)
            clause = f"{nodes.from_} WHERE {has_labels(node, labels)}"
            hints = [nodes.hint]
            inner = _Rows(node)
        else:
            hop = self.views.step(rel.type, rel.direction, rows.node, labels)
            clause = f"{hop.from_} AND {hop.x}"
            hints = []
            inner = _Rows(hop.node, hop.edge)
        quantified = test if i == len(path) - 1 and isinstance(test, QuantifyTest) else None
        if quantified is not None:
            tested = " AND ".join(self._condition(it, next_.scope, inner) for it in quantified.conditions)
        else:
            tested = self._walk(path, i + 1, test, next_.scope, inner)
        exists = f"EXISTS ({related_subquery(*hints)} {clause} AND {tested})"
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
