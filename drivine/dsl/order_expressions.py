"""
Order expressions: a ranking computed for each root from its properties, ordered by in the database with the query's
limit. Every value given is bound as a parameter.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, TypeAlias

from drivine.drivine_error import DrivineError
from drivine.dsl.case_branch import CaseBranch
from drivine.dsl.case_condition import CaseCondition
from drivine.dsl.condition_node import ConditionNode
from drivine.dsl.expression import Expression
from drivine.dsl.expression_node import ExpressionNode
from drivine.dsl.ref import Ref

Operand: TypeAlias = Expression | Ref | int | float
"""What arithmetic takes: a numeric property of the root, an expression, or a number (bound)."""

Comparand: TypeAlias = Any
"""What a comparison takes: a property, an expression, or a value (bound)."""


def plus(left: Operand, right: Operand) -> Expression:
    return _arithmetic("+", left, right)


def minus(left: Operand, right: Operand) -> Expression:
    return _arithmetic("-", left, right)


def times(left: Operand, right: Operand) -> Expression:
    return _arithmetic("*", left, right)


def div(left: Operand, right: Operand) -> Expression:
    """Division of real numbers on every engine: Cypher divides two integers as integers."""
    return _arithmetic("/", left, right)


def negate(operand: Operand) -> Expression:
    return Expression(ExpressionNode("NEGATE", args=(node_of(operand),)))


def exp(operand: Operand) -> Expression:
    """e raised to the operand."""
    return Expression(ExpressionNode("FUNCTION", name="exp", args=(node_of(operand),)))


def floor(operand: Operand) -> Expression:
    """The largest whole number no greater than the operand."""
    return Expression(ExpressionNode("FUNCTION", name="floor", args=(node_of(operand),)))


def coalesce(*operands: Comparand) -> Expression:
    """The first operand that has a value."""
    return Expression(ExpressionNode("FUNCTION", name="coalesce", args=tuple(node_of(it) for it in operands)))


def epoch_seconds(moment: datetime) -> float:
    """A moment of the caller's as seconds since the epoch, with the fraction: folded here and bound as a number."""
    return moment.timestamp()


def param(value: Any) -> Expression:
    """Any value, bound."""
    return Expression(ExpressionNode("VALUE", value=value))


def case_of(branches: Sequence[CaseBranch], otherwise: Operand) -> Expression:
    """The value of the first branch whose condition holds, else `otherwise`."""
    if not branches:
        raise DrivineError("case_of needs a branch: with none, give the otherwise value itself.")
    return Expression(
        ExpressionNode("CASE", branches=tuple((it.when, it.then) for it in branches), otherwise=node_of(otherwise))
    )


def above(left: Comparand, right: Comparand) -> CaseCondition:
    return _compare(">", left, right)


def below(left: Comparand, right: Comparand) -> CaseCondition:
    return _compare("<", left, right)


def at_least(left: Comparand, right: Comparand) -> CaseCondition:
    return _compare(">=", left, right)


def at_most(left: Comparand, right: Comparand) -> CaseCondition:
    return _compare("<=", left, right)


def equal_to(left: Comparand, right: Comparand) -> CaseCondition:
    return _compare("=", left, right)


def not_equal_to(left: Comparand, right: Comparand) -> CaseCondition:
    return _compare("<>", left, right)


def has_value(operand: Comparand) -> CaseCondition:
    return CaseCondition(ConditionNode("HAS_VALUE", args=(node_of(operand),)))


def has_no_value(operand: Comparand) -> CaseCondition:
    return CaseCondition(ConditionNode("HAS_VALUE", args=(node_of(operand),), negate=True))


def node_of(operand: Comparand) -> ExpressionNode:
    if isinstance(operand, Ref):
        return ExpressionNode("PROPERTY", path=operand.path)
    if isinstance(operand, Expression):
        return operand.node
    if operand is None:
        raise DrivineError("An expression's value can't be None: use has_no_value, or coalesce.")
    if isinstance(operand, (CaseCondition, CaseBranch)):
        raise DrivineError("An expression takes properties of the root, expressions and values; not a condition.")
    return ExpressionNode("VALUE", value=operand)


def _arithmetic(operator: str, left: Operand, right: Operand) -> Expression:
    return Expression(ExpressionNode("ARITHMETIC", operator=operator, args=(node_of(left), node_of(right))))


def _compare(operator: str, left: Comparand, right: Comparand) -> CaseCondition:
    return CaseCondition(ConditionNode("COMPARE", operator=operator, args=(node_of(left), node_of(right))))
