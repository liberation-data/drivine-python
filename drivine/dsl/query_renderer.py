from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from drivine.backend.cypher import label_expression, quote_name
from drivine.backend.cypher_grammar import CypherGrammar
from drivine.backend.view_projector import labels_of, pattern, target
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
from drivine.dsl.order_term import OrderTerm
from drivine.dsl.quantify_test import QuantifyTest
from drivine.dsl.query import Query
from drivine.dsl.ref import STORED
from drivine.dsl.rendered_query import RenderedQuery
from drivine.dsl.root_key import RootKey
from drivine.dsl.scope import Scope
from drivine.dsl.seek_value import SeekValue
from drivine.dsl.step import Step
from drivine.model.fragment_model import FragmentModel
from drivine.model.relationship_model import RelationshipModel
from drivine.model.target_model import TargetModel

T = TypeVar("T")

_BINARY = {
    "EQ": "=",
    "NEQ": "<>",
    "GT": ">",
    "GTE": ">=",
    "LT": "<",
    "LTE": "<=",
    "CONTAINS": "CONTAINS",
    "STARTS_WITH": "STARTS WITH",
    "ENDS_WITH": "ENDS WITH",
    "MATCHES": "=~",
}
"""The operators written between the property and its value."""

_LOWERED = {"EQ_LOWERED": "=", "CONTAINS_LOWERED": "CONTAINS"}
"""The comparisons with a lowercased copy, each written between the copy and the value lowercased."""


@dataclass(frozen=True)
class _Aliases:
    """The aliases a path's conditions are written against: the node, and the relationship a fragment holds."""

    node: str
    rel: str | None = None


class QueryRenderer:
    """
    Writes a query as Cypher for one engine, as TypeScript's Drivine writes it. Values are bound as parameters (`$_q0`,
    …), never written into the text; a condition across a relationship is an existence check of its own, holding when
    at least one related node meets it.
    """

    def __init__(
        self,
        grammar: CypherGrammar,
        labels_of_class: Callable[[type], list[str]],
        depth: Mapping[str, int] | None = None,
    ) -> None:
        self.grammar = grammar
        self.labels_of_class = labels_of_class
        self.depth = depth or {}
        self.bindings: dict[str, Any] = {}
        self._alias_count = 0

    @staticmethod
    def render_where(
        where: Any, model: TargetModel, grammar: CypherGrammar, alias: str, labels_of_class: Callable[[type], list[str]]
    ) -> tuple[str, dict[str, Any]]:
        """A condition over the class's references, written for the store, with the values it binds."""
        renderer = QueryRenderer(grammar, labels_of_class)
        written = renderer.where(all_of_one(where), scope_of(model), alias)
        return written, renderer.bindings

    @staticmethod
    def render(
        query: Query,
        model: TargetModel,
        grammar: CypherGrammar,
        alias: str,
        labels_of_class: Callable[[type], list[str]],
    ) -> RenderedQuery:
        scope = scope_of(model)
        renderer = QueryRenderer(grammar, labels_of_class, query.depth)
        condition = all_of_one(query.where) if query.where is not None else None
        written = renderer.where(condition, scope, alias) if condition is not None else None
        terms = [classify(it, scope) for it in list_of(query.order_by)]
        keys = [it for it in terms if isinstance(it, RootKey)]
        seek = list_of(query.seek)
        keyset = renderer._keyset(keys, seek, query, alias) if query.seek is not None else None
        by_expression = any(it.term.expression is not None for it in keys)
        order_by = None
        if keys:
            order_by = ", ".join(
                _order(
                    it.term,
                    renderer._expression(it.term.expression, scope, alias)
                    if it.term.expression is not None
                    else f"{alias}.{quote_name(it.property or '')}",
                )
                for it in keys
            )
        return RenderedQuery(
            where=" AND ".join(it for it in (written, keyset) if it is not None) or None,
            order_by=order_by,
            limit=count("limit", query.limit),
            skip=count("skip", query.skip),
            depth=query.depth or {},
            bindings=renderer.bindings,
            ordered=() if by_expression else tuple(it.property or "" for it in keys),
            collection_sorts=tuple(it for it in terms if isinstance(it, CollectionSort)),
            pinned=tuple(pinned_properties(condition, scope)) if condition is not None else (),
            paged=any(it is not None for it in (query.order_by, query.limit, query.skip, query.seek)),
            seeks=query.seek is not None,
        )

    def where(self, condition: Condition, scope: Scope, alias: str) -> str:
        """
        The condition as a whole `WHERE`. Its outermost `AND`, and any `all_of` directly in it, stand without
        parentheses, on every engine: what a statement joins to a `WHERE` it joins by `AND`, and FalkorDB 6 reads a
        condition in parentheses it does not need by scanning the label, where it would have used an index
        (FalkorDB/FalkorDB#3331). An `any_of` or a `not_` in it keeps its own.
        """
        aliases = _Aliases(alias)

        def conjuncts(it: Condition) -> list[Condition]:
            if isinstance(it, CompositeCondition) and it.kind == "AND" and it.conditions:
                return [part for each in it.conditions for part in conjuncts(each)]
            return [it]

        return " AND ".join(self._condition(it, scope, aliases) for it in conjuncts(condition))

    def _keyset(self, keys: list[RootKey], seek: list[SeekValue], query: Query, alias: str) -> str:
        """
        The predicate that continues after a cursor. For `a DESC, b DESC` after `(A, B)`:
        `a <= $A AND (a < $A OR (a = $A AND b < $B))`, each key first held not null where the engine wants it. The
        leading bound, redundant beside the branches, gives the planner a range to seek on, as a top-level OR doesn't.
        """
        check_keyset(keys, seek, query)

        def expression(i: int) -> str:
            return f"{alias}.{quote_name(keys[i].property or '')}"

        values = [self._bind(it.value) for it in seek]

        def compare(i: int, strict: bool) -> str:
            ascending = keys[i].term.direction == "ASC"
            return f"{expression(i)} {'>' if ascending else '<'}{'' if strict else '='} {values[i]}"

        branches: list[str] = []
        for i in range(len(keys)):
            branch = " AND ".join([*(f"{expression(j)} = {values[j]}" for j in range(i)), compare(i, True)])
            branches.append(branch if i == 0 else f"({branch})")
        # Joined by AND with no parentheses around the whole: in them, FalkorDB 6 scans the label rather than seeking
        # on the bound (FalkorDB/FalkorDB#3331).
        return " AND ".join(
            [
                *([f"{expression(i)} IS NOT NULL" for i in range(len(keys))] if self.grammar.keyset_null_guard else []),
                *([compare(0, False)] if len(keys) > 1 else []),
                f"({' OR '.join(branches)})" if len(branches) > 1 else branches[0],
            ]
        )

    def _expression(self, node: ExpressionNode, scope: Scope, alias: str) -> str:
        """An order expression as Cypher over the root's properties, its values bound."""

        def of(it: ExpressionNode) -> str:
            return self._expression(it, scope, alias)

        if node.kind == "PROPERTY":
            return f"{alias}.{quote_name(root_property(node.path, scope, 'An expression'))}"
        if node.kind == "VALUE":
            return self._bind(node.value)
        if node.kind == "ARITHMETIC":
            left, right = node.args
            # toFloat: Cypher divides two integers as integers.
            if node.operator == "/":
                return f"(toFloat({of(left)}) / {of(right)})"
            return f"({of(left)} {node.operator} {of(right)})"
        if node.kind == "NEGATE":
            return f"(-{of(node.args[0])})"
        if node.kind == "FUNCTION":
            return f"{node.name}({', '.join(of(it) for it in node.args)})"
        assert node.otherwise is not None
        branches = " ".join(
            f"WHEN {self._case_condition(when, scope, alias)} THEN {of(then)}" for when, then in node.branches
        )
        return f"CASE {branches} ELSE {of(node.otherwise)} END"

    def _case_condition(self, node: ConditionNode, scope: Scope, alias: str) -> str:
        if node.kind == "COMPARE":
            left, right = node.args
            return f"{self._expression(left, scope, alias)} {node.operator} {self._expression(right, scope, alias)}"
        if node.kind == "HAS_VALUE":
            return f"{self._expression(node.args[0], scope, alias)} IS {'' if node.negate else 'NOT '}NULL"
        if node.kind == "NOT":
            return f"NOT ({self._case_condition(node.conditions[0], scope, alias)})"
        joined = f" {node.kind} ".join(self._case_condition(it, scope, alias) for it in node.conditions)
        return f"({joined})"

    def _condition(self, condition: Condition, scope: Scope, aliases: _Aliases) -> str:
        if isinstance(condition, CompositeCondition):
            parts = [self._condition(it, scope, aliases) for it in condition.conditions]
            if condition.kind == "AND":
                return group(parts, " AND ") if parts else "true"
            if condition.kind == "OR":
                return group(parts, " OR ") if parts else "false"
            return f"NOT {group(parts, ' AND ', True)}"
        return self._walk(condition.path, 0, condition.test, scope, aliases)

    def _walk(self, path: tuple[str, ...], i: int, test: Test, scope: Scope, aliases: _Aliases) -> str:
        if i == len(path):
            return self._at_node(test, path, aliases)
        next_ = step(scope, path[i])
        last = i == len(path) - 1
        if next_.kind == "SAME_NODE":
            assert next_.scope is not None
            return self._walk(path, i + 1, test, next_.scope, aliases)
        if next_.kind == "TARGET":
            assert next_.scope is not None
            return self._walk(path, i + 1, test, next_.scope, _Aliases(aliases.node))
        if next_.kind == "HOP":
            return self._across(next_, path, i, test, aliases)
        if next_.kind == "PROPERTY":
            if not last:
                raise DrivineError(f"{described(path[: i + 1])} is a property; it has no fields.")
            owner = aliases.rel if next_.on == "RELATIONSHIP" else aliases.node
            return self._compare(test, f"{owner}.{quote_name(next_.property)}", next_.json_packed, path)
        if not last or not isinstance(test, CompareTest) or test.operator != "HAS_ITEM":
            raise DrivineError(f"{described(path[: i + 1])} holds the node's labels: test it with has_item.")
        return f"{aliases.node}:{quote_name(str(test.value))}"

    def _across(self, next_: Step, path: tuple[str, ...], i: int, test: Test, aliases: _Aliases) -> str:
        """
        A test across a relationship: it holds when one of the nodes the relationship leads to meets the rest of the
        path's test, or, when the path ends at the relationship and quantifies, all of the quantifier's conditions.
        """
        assert next_.rel is not None and next_.scope is not None
        node = self._alias("x")
        rel = self._alias("r") if next_.scope.kind == "REL_FRAGMENT" else ""
        hop = self._pattern(next_.rel, aliases.node, rel, node)
        quantified = test if i == len(path) - 1 and isinstance(test, QuantifyTest) else None
        inner_scope = next_.scope

        def where(rel_properties: str) -> str:
            into = _Aliases(node, rel_properties)
            if quantified is not None:
                return " AND ".join(self._condition(it, inner_scope, into) for it in quantified.conditions)
            return self._walk(path, i + 1, test, inner_scope, into)

        match = self.grammar.some_match(hop, node, rel, where)
        return f"NOT {match}" if quantified is not None and quantified.negate else match

    def _at_node(self, test: Test, path: tuple[str, ...], aliases: _Aliases) -> str:
        """A test that ended on a node, rather than on one of its properties."""
        where = described(path)
        if isinstance(test, InstanceOfTest):
            return f"{aliases.node}{label_expression(self.labels_of_class(test.type))}"
        if isinstance(test, AnyLabelTest):
            return f"any(_l IN labels({aliases.node}) WHERE _l IN {self._bind(list(test.labels))})"
        if isinstance(test, QuantifyTest):
            raise DrivineError(f"any and none follow a relationship; {where} isn't one.")
        raise DrivineError(f"{where} is a node: compare one of its fields.")

    def _compare(self, test: Test, expression: str, json_packed: bool, path: tuple[str, ...]) -> str:
        if not isinstance(test, CompareTest):
            raise DrivineError(f"{described(path)} is a property, not a node.")

        def packed(value: Any) -> Any:
            return _packed(value) if json_packed else _stored(value)

        operator = test.operator
        if operator in ("IN", "NOT_IN"):
            values: Any = test.value
            if not isinstance(values, list):
                raise DrivineError(f"{described(path)} is tested against a list, not {values!r}.")
            tested = f"{expression} IN {self._bind([packed(it) for it in values])}"  # pyright: ignore[reportUnknownVariableType]
            return f"NOT {tested}" if operator == "NOT_IN" else tested
        if operator in ("CONTAINS_IGNORE_CASE", "EQ_IGNORE_CASE"):
            word = "=" if operator == "EQ_IGNORE_CASE" else "CONTAINS"
            return f"toLower({expression}) {word} {self._bind(str(test.value).lower())}"
        if operator in _LOWERED:
            # A lowercased copy, compared with the value as the engine lowercases it: see lower_copies.
            return f"{expression} {_LOWERED[operator]} toLower({self._bind(str(test.value))})"
        if operator == "HAS_ITEM":
            return f"{self._bind(packed(test.value))} IN {expression}"
        if operator == "IS_NULL":
            return f"{expression} IS NULL"
        if operator == "IS_NOT_NULL":
            return f"{expression} IS NOT NULL"
        return f"{expression} {_BINARY[operator]} {self._bind(packed(test.value))}"

    def _pattern(self, rel: RelationshipModel, origin: str, rel_alias: str, to: str) -> str:
        """The pattern of one relationship from `origin`, its far node labelled; over several hops for a deep list."""
        into = target(rel)
        depth = self.depth.get(rel.field, rel.max_depth)
        hops = f"*1..{depth}" if isinstance(rel.related, FragmentModel) and depth > 1 else ""
        return pattern(rel, origin, rel_alias, f"{to}:{labels_of(into)}", hops)

    def _bind(self, value: Any) -> str:
        name = f"_q{len(self.bindings)}"
        self.bindings[name] = value
        return f"${name}"

    def _alias(self, prefix: str) -> str:
        alias = f"_{prefix}{self._alias_count}"
        self._alias_count += 1
        return alias


def list_of(value: T | Sequence[T] | None) -> list[T]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)  # pyright: ignore[reportUnknownArgumentType]
    return [value]  # pyright: ignore[reportReturnType]


def classify(term: OrderTerm, scope: Scope) -> RootKey | CollectionSort:
    """Which a term is: of the roots, or of a list in each."""
    if term.expression is not None:
        return RootKey(term)
    at = scope
    hops = 0
    last_list = -1
    for i, name in enumerate(term.path):
        next_ = step(at, name)
        if next_.kind == "HOP":
            hops += 1
            assert next_.rel is not None
            if next_.rel.is_collection:
                last_list = i
        if next_.kind in ("PROPERTY", "LABELS"):
            break
        assert next_.scope is not None
        at = next_.scope
    if not hops:
        return RootKey(term, root_property(term.path, scope, "An order"))
    key = term.path[last_list + 1 :]
    if last_list < 0 or not key or any(it.startswith(STORED) for it in key):
        raise DrivineError(
            f"An order is by a property of the root, or of the nodes a list relationship leads to (by field); "
            f"{described(term.path)} is neither."
        )
    return CollectionSort(term.path[: last_list + 1], key, term.direction, term.nulls)


def check_keyset(keys: list[RootKey], seek: list[SeekValue], query: Query) -> None:
    """Refuses a seek that can't continue the order: one value for each root term, of its property, and no skip."""
    if not keys:
        raise DrivineError("seek continues an order: give order_by, with a cursor value for each of its terms.")
    if len(keys) != len(seek):
        raise DrivineError(f"seek has {len(seek)} cursor values for {len(keys)} order_by terms.")
    if query.skip is not None:
        raise DrivineError("seek and skip are two ways to page: give one.")
    for i, key in enumerate(keys):
        if key.term.expression is not None:
            raise DrivineError(
                "seek can't continue an order by an expression: a cursor holds properties' values, and an "
                "expression's is computed for each row."
            )
        if key.term.nulls:
            raise DrivineError(
                f"{'.'.join(key.term.path)} places its nulls, which no cursor reaches: a cursor doesn't say which "
                f"side of the nulls it is on."
            )
        if key.term.path != seek[i].path:
            raise DrivineError(
                f"seek value {i + 1} is of {'.'.join(seek[i].path)}, but order_by term {i + 1} is "
                f"{'.'.join(key.term.path)}."
            )


def root_property(path: tuple[str, ...], scope: Scope, what: str) -> str:
    """The stored name of the root property a path leads to; `what` names what wants one."""
    at = scope
    for i, name in enumerate(path):
        next_ = step(at, name)
        if next_.kind == "SAME_NODE" and next_.scope is not None:
            at = next_.scope
        elif next_.kind == "PROPERTY" and next_.on == "NODE" and i == len(path) - 1:
            return next_.property
        else:
            break
    raise DrivineError(f"{what} is by a property of the root; {described(path)} isn't one.")


def _order(term: OrderTerm, expression: str) -> str:
    key = f"{expression}{' DESC' if term.direction == 'DESC' else ''}"
    # A placement as its own key, before the property: false sorts before true on every engine.
    if term.nulls == "FIRST":
        return f"{expression} IS NULL DESC, {key}"
    return f"{expression} IS NULL, {key}" if term.nulls == "LAST" else key


def pinned_properties(condition: Condition, scope: Scope) -> list[str]:
    """The root properties a condition holds equal to a value at its top level: one within any_of holds none."""
    if isinstance(condition, CompositeCondition):
        if condition.kind != "AND":
            return []
        return [it for part in condition.conditions for it in pinned_properties(part, scope)]
    if not isinstance(condition.test, CompareTest):
        return []
    if condition.test.operator != "EQ":
        return []
    try:
        return [root_property(condition.path, scope, "A pin")]
    except DrivineError:
        return []


def group(parts: list[str], separator: str, always: bool = False) -> str:
    """Joined, in parentheses when there is more than one (or always, when `always`)."""
    return f"({separator.join(parts)})" if len(parts) > 1 or always else parts[0]


def count(name: str, value: int | None) -> int | None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise DrivineError(f"{name} is a whole number of objects, not {value!r}.")
    return value


def _stored(value: Any) -> Any:
    from drivine.backend.save_statement_builder import stored

    return stored(value)


def _packed(value: Any) -> str:
    from drivine.backend.save_statement_builder import packed

    return packed(value)


def described(path: Sequence[str]) -> str:
    return ".".join(it.replace(STORED, "") for it in path) if path else "the root"
