from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from drivine.dsl.condition_node import ConditionNode

ExpressionKind = Literal["PROPERTY", "VALUE", "ARITHMETIC", "NEGATE", "FUNCTION", "CASE"]


@dataclass(frozen=True)
class ExpressionNode:
    """
    A node of an order expression: a property of the root (`path`), a bound `value`, arithmetic on two `args`, a
    negation of one, a function (`name`) of several, or a CASE of `branches` and an `otherwise`.
    """

    kind: ExpressionKind
    path: tuple[str, ...] = ()
    value: Any = None
    operator: str = ""
    name: str = ""
    args: tuple[ExpressionNode, ...] = ()
    branches: tuple[tuple[ConditionNode, ExpressionNode], ...] = field(default=())
    otherwise: ExpressionNode | None = None
