from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from drivine.dsl.expression_node import ExpressionNode

ConditionKind = Literal["COMPARE", "HAS_VALUE", "AND", "OR", "NOT"]


@dataclass(frozen=True)
class ConditionNode:
    """
    A test within a CASE of an order expression: a comparison of two `args`, whether one has a value (or, `negate`d,
    hasn't), or `conditions` joined with AND or OR, or negated.
    """

    kind: ConditionKind
    operator: str = ""
    args: tuple[ExpressionNode, ...] = ()
    negate: bool = False
    conditions: tuple[ConditionNode, ...] = ()
