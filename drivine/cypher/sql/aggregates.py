"""
Where a statement aggregates as Neo4j refuses it when it plans the statement: see `check_aggregates`. As drivine4j's
Aggregates.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import NoReturn

from drivine.cypher.ast.clause import (
    Call,
    Clause,
    Create,
    Delete,
    Foreach,
    Match,
    Merge,
    Return,
    SetClause,
    Unwind,
    With,
)
from drivine.cypher.ast.expression import (
    CountStar,
    Exists,
    Expression,
    Function,
    ListComprehension,
    PatternComprehension,
    PatternPredicate,
    Quantifier,
    Reduce,
    Subquery,
)
from drivine.cypher.ast.pattern import Pattern
from drivine.cypher.ast.projection import Projection
from drivine.cypher.ast.query import Query
from drivine.cypher.ast.set_item import SetAdd, SetItem, SetProperty, SetReplace
from drivine.drivine_error import DrivineError


def check_aggregates(
    query: Query,
    is_aggregate: Callable[[Expression], bool],
    children: Callable[[Expression], list[Expression]],
) -> None:
    """
    Refuses the statement where it aggregates as Cypher does not: an aggregate in a `WHERE` (Neo4j's 42I24,
    InvalidAggregation), of a `MATCH`, a `WITH`, a comprehension, a quantifier, a pattern's or `EXISTS { … }`'s, in a
    `CALL { … }` or a subquery too. Read from the statement alone, before it is compiled, so a statement that compiles
    is compiled as it was. A subquery's own query, `COUNT { MATCH … RETURN count(*) }`, aggregates in its own
    projections. The `WHERE` of a `WITH` that aggregates, or is `DISTINCT`, filters its groups, and holds an aggregate
    as Neo4j has it (#313).

    `is_aggregate` and `children` are the compiler's, passed in so that neither module imports the other.
    """
    _Aggregates(is_aggregate, children).query(query)


class _Aggregates:
    def __init__(
        self,
        is_aggregate: Callable[[Expression], bool],
        children: Callable[[Expression], list[Expression]],
    ) -> None:
        self._is_aggregate = is_aggregate
        self._children = children

    def query(self, query: Query) -> None:
        for clauses in (query.clauses, *(it.clauses for it in query.unions)):
            self._clauses(clauses)

    def _clauses(self, clauses: Sequence[Clause]) -> None:
        for clause in clauses:
            if isinstance(clause, Match):
                for pattern in clause.patterns:
                    self._pattern(pattern)
                self._where(clause.where)
            elif isinstance(clause, Unwind):
                self._expression(clause.list)
            elif isinstance(clause, With):
                self._projection(clause.projection)
                # One that is DISTINCT groups its rows by its items, as one that aggregates does by those that do not.
                projection = clause.projection
                if projection.distinct or any(self._grouped(it.expression) is not None for it in projection.items):
                    self._having(clause.where)
                else:
                    self._where(clause.where)
            elif isinstance(clause, Create):
                for pattern in clause.patterns:
                    self._pattern(pattern)
            elif isinstance(clause, Merge):
                self._pattern(clause.pattern)
                for item in (*clause.on_create, *clause.on_match):
                    self._set_item(item)
            elif isinstance(clause, SetClause):
                for item in clause.items:
                    self._set_item(item)
            elif isinstance(clause, Delete):
                for expression in clause.expressions:
                    self._expression(expression)
            elif isinstance(clause, Call):
                self.query(clause.query)
            elif isinstance(clause, Foreach):
                self._expression(clause.list)
                self._clauses(clause.clauses)
            elif isinstance(clause, Return):
                self._projection(clause.projection)
            # A REMOVE holds no expression.

    def _set_item(self, item: SetItem) -> None:
        if isinstance(item, (SetProperty, SetReplace, SetAdd)):
            self._expression(item.value)

    def _projection(self, projection: Projection) -> None:
        for item in projection.items:
            self._expression(item.expression)
        for sort in projection.order_by:
            self._expression(sort.expression)
        self._expression(projection.skip)
        self._expression(projection.limit)

    def _pattern(self, pattern: Pattern) -> None:
        for node in pattern.nodes:
            self._expression(node.properties)
        for relationship in pattern.relationships:
            self._expression(relationship.properties)
            if relationship.group is not None:
                self._pattern(relationship.group.pattern)
                self._where(relationship.group.where)

    def _where(self, where: Expression | None) -> None:
        """Refuses a `WHERE` that aggregates outside a subquery's own query; then reads it as any expression."""
        if where is None:
            return
        aggregate = self._aggregate_in(where)
        if aggregate is not None:
            _refuse(aggregate)
        self._expression(where)

    def _having(self, where: Expression | None) -> None:
        """
        The `WHERE` of a `WITH` that aggregates: it filters the groups, as SQL's HAVING does, so an aggregate is read in
        it as Neo4j reads it (#313), but not in a comprehension's, a quantifier's, a reduce's or a pattern's own
        expressions.
        """
        if where is None:
            return
        aggregate = self._beyond_groups(where)
        if aggregate is not None:
            _refuse(aggregate)
        self._expression(where)

    def _grouped(self, expression: Expression) -> Expression | None:
        """
        The first aggregate the expression holds outside a comprehension's, a quantifier's, a reduce's and a pattern's
        own expressions and a subquery's own query, where `children` finds its parts: one the compiler reads over a
        WITH's groups.
        """
        if self._is_aggregate(expression):
            return expression
        for part in self._children(expression):
            found = self._grouped(part)
            if found is not None:
                return found
        return None

    def _beyond_groups(self, expression: Expression) -> Expression | None:
        """The first aggregate the expression holds that `_grouped` does not find, but `_aggregate_in` does."""
        if self._is_aggregate(expression):
            return None
        near = self._children(expression)
        for part in near:
            found = self._beyond_groups(part)
            if found is not None:
                return found
        for part in self._parts(expression):
            if part not in near:
                found = self._aggregate_in(part)
                if found is not None:
                    return found
        return None

    def _aggregate_in(self, expression: Expression) -> Expression | None:
        """The first aggregate the expression holds, a comprehension's, a pattern's or `EXISTS { … }`'s among them, but
        not one a subquery's own query holds."""
        if self._is_aggregate(expression):
            return expression
        for part in self._parts(expression):
            found = self._aggregate_in(part)
            if found is not None:
                return found
        return None

    def _expression(self, expression: Expression | None) -> None:
        """Reads the expression for the `WHERE`s and subqueries it holds."""
        if expression is None:
            return
        if isinstance(expression, Subquery):
            self.query(expression.query)
        elif isinstance(expression, ListComprehension):
            self._expression(expression.list)
            self._where(expression.where)
            self._expression(expression.projection)
        elif isinstance(expression, Quantifier):
            self._expression(expression.list)
            self._where(expression.where)
        elif isinstance(expression, PatternComprehension):
            self._pattern(expression.pattern)
            self._where(expression.where)
            self._expression(expression.projection)
        elif isinstance(expression, Exists):
            for pattern in expression.patterns:
                self._pattern(pattern)
            self._where(expression.where)
        elif isinstance(expression, PatternPredicate):
            self._pattern(expression.pattern)
        else:
            for part in self._parts(expression):
                self._expression(part)

    def _parts(self, expression: Expression) -> list[Expression]:
        """Each expression the expression holds directly: a comprehension's, a quantifier's, a reduce's and a
        pattern's parts too."""
        if isinstance(expression, ListComprehension):
            return _present(expression.list, expression.where, expression.projection)
        if isinstance(expression, Quantifier):
            return _present(expression.list, expression.where)
        if isinstance(expression, Reduce):
            return [expression.initial, expression.list, expression.expression]
        if isinstance(expression, PatternComprehension):
            return [*_pattern_parts(expression.pattern), *_present(expression.where, expression.projection)]
        if isinstance(expression, PatternPredicate):
            return _pattern_parts(expression.pattern)
        if isinstance(expression, Exists):
            return [
                *(it for pattern in expression.patterns for it in _pattern_parts(pattern)),
                *_present(expression.where),
            ]
        if isinstance(expression, Subquery):
            return []
        return self._children(expression)


def _refuse(aggregate: Expression) -> NoReturn:
    raise DrivineError(
        f"{_named(aggregate)} aggregates, and a WHERE holds no aggregate unless its WITH aggregates, as in Neo4j: "
        "aggregate in a WITH first, and filter what it gives in that WITH's WHERE, as in WITH x, count(*) AS c WHERE "
        "c > 1."
    )


def _named(aggregate: Expression) -> str:
    """An aggregate's name, as a message gives it."""
    if isinstance(aggregate, CountStar):
        return "count(*)"
    assert isinstance(aggregate, Function)
    return f"{aggregate.name}()"


def _pattern_parts(pattern: Pattern) -> list[Expression]:
    parts = [node.properties for node in pattern.nodes if node.properties is not None]
    for relationship in pattern.relationships:
        parts += _present(relationship.properties)
        if relationship.group is not None:
            parts += [*_pattern_parts(relationship.group.pattern), *_present(relationship.group.where)]
    return parts


def _present(*items: Expression | None) -> list[Expression]:
    return [it for it in items if it is not None]
