from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import Any, Literal

from drivine.backend.sql.compared import Compared
from drivine.backend.sql.rows import Rows
from drivine.backend.sql.sql_json import storable
from drivine.backend.sql.sql_views import SqlViews
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


class SqlQueryRenderer(ABC):
    """
    Writes a query over a class's references as SQL over a SQL store's tables, the root's row bound to an alias, as
    TypeScript's Drivine writes it. A condition across a relationship is an EXISTS over the relationship table. Values
    are bound (`$_q0`, …), never written in. What is each store's own is how it compares a property, tests labels and
    reads a property for an order or an expression.
    """

    def __init__(self, views: SqlViews, labels_of_class: Callable[[type], list[str]]) -> None:
        self.views = views
        self.labels_of_class = labels_of_class
        self.bindings: dict[str, Any] = {}

    def where(self, condition: Condition, scope: Scope, alias: str) -> str:
        """A condition over the node row `alias`."""
        return self._condition(condition, scope, Rows(alias))

    def _render(self, scope: Scope, query: Query, alias: str) -> RenderedQuery:
        """The query, made in `scope`, its root's row bound to `alias`."""
        condition = all_of_one(query.where) if query.where is not None else None
        written = self._condition(condition, scope, Rows(alias), True) if condition is not None else None
        terms = [classify(it, scope) for it in list_of(query.order_by)]
        keys = [it for it in terms if isinstance(it, RootKey)]
        seek = list_of(query.seek)
        keyset = self._keyset(keys, seek, query, alias) if query.seek is not None else None
        return RenderedQuery(
            where=" AND ".join(it for it in (written, keyset) if it is not None) or None,
            order_by=", ".join(self._order(it, scope, alias) for it in keys) if keys else None,
            limit=count("limit", query.limit),
            skip=count("skip", query.skip),
            depth=query.depth or {},
            bindings=self.bindings,
            ordered=()
            if any(it.term.expression is not None for it in keys)
            else tuple(it.property or "" for it in keys),
            collection_sorts=tuple(it for it in terms if isinstance(it, CollectionSort)),
            pinned=tuple(pinned_properties(condition, scope)) if condition is not None else (),
            paged=any(it is not None for it in (query.order_by, query.limit, query.skip, query.seek)),
            seeks=query.seek is not None,
            labels_tested=tuple(self._labels_tested()),
        )

    def _render_only(self, where: Any, model: TargetModel, alias: str) -> tuple[str, dict[str, Any]]:
        """The condition over the class's references, over the node row `alias`, with the values it binds."""
        written = self.where(all_of_one(where), scope_of(model), alias)
        return written, self.bindings

    def _labels_tested(self) -> list[str]:
        """The root's labels whose test a comparison made, where the store reads one through an index."""
        return []

    def _storable(self, value: Any) -> Any:
        """A value as the store holds it: a date as ISO 8601 text, as a save writes it."""
        return storable(value)

    # ----- Conditions -----

    def _condition(self, condition: Condition, scope: Scope, rows: Rows, top: bool = False) -> str:
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

    def _list(self, value: Any, path: tuple[str, ...]) -> list[Any]:
        if not isinstance(value, list):
            raise DrivineError(f"{described(path)} is tested against a list, not {value!r}.")
        return value  # pyright: ignore[reportUnknownVariableType]

    def _bind(self, value: Any) -> str:
        name = f"_q{len(self.bindings)}"
        self.bindings[name] = value
        return f"${name}"

    def _walk(self, path: tuple[str, ...], i: int, test: Test, scope: Scope, rows: Rows, top: bool = False) -> str:
        if i == len(path):
            return self._at_node(test, path, rows)
        next_ = step(scope, path[i])
        last = i == len(path) - 1
        if next_.kind == "SAME_NODE":
            assert next_.scope is not None
            return self._walk(path, i + 1, test, next_.scope, rows, top)
        if next_.kind == "TARGET":
            assert next_.scope is not None
            return self._walk(path, i + 1, test, next_.scope, Rows(rows.node))
        if next_.kind == "HOP":
            return self._across(next_, path, i, test, rows)
        if next_.kind == "PROPERTY":
            if not last:
                raise DrivineError(f"{described(path[: i + 1])} is a property; it has no fields.")
            on_relationship = next_.on == "RELATIONSHIP"
            row = rows.rel if on_relationship and rows.rel is not None else rows.node
            return self._property(
                Compared(test, row, on_relationship, next_.property, next_.json_packed, path),
                top,
            )
        if not last or not isinstance(test, CompareTest) or test.operator != "HAS_ITEM":
            raise DrivineError(f"{described(path[: i + 1])} holds the node's labels: test it with has_item.")
        return self.views.has_labels(rows.node, [str(test.value)])

    def _across(self, next_: Step, path: tuple[str, ...], i: int, test: Test, rows: Rows) -> str:
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
            clause = f"{nodes.from_} WHERE {self.views.has_labels(node, labels)}"
            hints = [nodes.hint]
            inner = Rows(node)
        else:
            hop = self.views.step(rel.type, rel.direction, rows.node, labels)
            clause = f"{hop.from_} AND {hop.x}"
            hints = []
            inner = Rows(hop.node, hop.edge)
        quantified = test if i == len(path) - 1 and isinstance(test, QuantifyTest) else None
        if quantified is not None:
            tested = " AND ".join(self._condition(it, next_.scope, inner) for it in quantified.conditions)
        else:
            tested = self._walk(path, i + 1, test, next_.scope, inner)
        exists = f"EXISTS ({self.views.related_subquery(*hints)} {clause} AND {tested})"
        return f"NOT {exists}" if quantified is not None and quantified.negate else exists

    def _at_node(self, test: Test, path: tuple[str, ...], rows: Rows) -> str:
        where = described(path)
        if isinstance(test, InstanceOfTest):
            return self.views.has_labels(rows.node, self.labels_of_class(test.type))
        if isinstance(test, AnyLabelTest):
            return self._any_label(rows.node, test.labels) if test.labels else "1 = 0"
        if isinstance(test, QuantifyTest):
            raise DrivineError(f"any and none follow a relationship; {where} isn't one.")
        raise DrivineError(f"{where} is a node: compare one of its fields.")

    # ----- Paging and ordering -----

    def _keyset(self, keys: list[RootKey], seek: list[SeekValue], query: Query, alias: str) -> str:
        """
        The predicate that continues after a cursor, as on the graph engines: a leading bound on the first key, then
        the lexicographic branches.
        """
        check_keyset(keys, seek, query)
        values = [self._bind(self._storable(it.value)) for it in seek]

        def tested(i: int, operator: str) -> str:
            return self._seek_test(alias, keys[i].property or "", operator, values[i])

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
        """An order term: a property, or an expression; nulls as placed."""
        direction: Literal["ASC", "DESC"] = "DESC" if key.term.direction == "DESC" else "ASC"
        if key.term.expression is None:
            return self._ordered_property(alias, key.property or "", direction, key.term.nulls)
        nulls = f" NULLS {key.term.nulls}" if key.term.nulls else ""
        value = self._expression(key.term.expression, scope, alias, "NUMBER")
        return f"{value}{' DESC' if direction == 'DESC' else ''}{nulls}"

    def _expression(self, node: ExpressionNode, scope: Scope, alias: str, as_: Literal["NUMBER", "TEXT"]) -> str:
        """An order expression over the root's properties: as numbers, or as text where it is compared with text."""

        def of(it: ExpressionNode) -> str:
            return self._expression(it, scope, alias, "NUMBER")

        if node.kind == "PROPERTY":
            return self._read(alias, root_property(node.path, scope, "An expression"), as_)
        if node.kind == "VALUE":
            return self._bind(self._storable(node.value))
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
                tested = self._present(alias, root_property(operand.path, scope, "An expression"))
            else:
                tested = f"{self._expression(operand, scope, alias, 'NUMBER')} IS NOT NULL"
            return f"NOT {tested}" if node.negate else tested
        if node.kind == "NOT":
            return f"NOT ({self._case_condition(node.conditions[0], scope, alias)})"
        joined = f" {node.kind} ".join(self._case_condition(it, scope, alias) for it in node.conditions)
        return f"({joined})"

    # ----- The dialect -----

    @abstractmethod
    def _property(self, compared: Compared, top: bool) -> str:
        """A test of a property: of the root, every root meeting it when `top`."""

    @abstractmethod
    def _any_label(self, row: str, labels: Sequence[str]) -> str:
        """Whether the node row carries any of the labels, of which there is at least one."""

    @abstractmethod
    def _seek_test(self, alias: str, property: str, operator: str, value: str) -> str:
        """
        The test of a cursor's value: the root's property `property` compared by `operator` with the bound `value`.
        """

    @abstractmethod
    def _ordered_property(
        self, alias: str, property: str, direction: Literal["ASC", "DESC"], nulls: str | None = None
    ) -> str:
        """An order term of a property of the root, without its direction."""

    @abstractmethod
    def _read(self, alias: str, property: str, as_: Literal["NUMBER", "TEXT"]) -> str:
        """The root's property read in an expression, as a number or as text."""

    @abstractmethod
    def _present(self, alias: str, property: str) -> str:
        """Whether the root has the property."""


def _numeric(value: Any) -> bool:
    """Whether the value is stored as a number: an Enum of numbers, or a Decimal, is the number a save writes."""
    stored = storable(value)
    return isinstance(stored, (int, float)) and not isinstance(stored, bool)
