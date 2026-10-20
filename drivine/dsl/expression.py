from __future__ import annotations

from dataclasses import dataclass

from drivine.dsl.expression_node import ExpressionNode
from drivine.dsl.order_term import OrderTerm


@dataclass(frozen=True)
class Expression:
    """A value computed for each root from its properties, ordered by with `asc()` or `desc()`."""

    node: ExpressionNode

    def asc(self) -> OrderTerm:
        return OrderTerm((), "ASC", expression=self.node)

    def desc(self) -> OrderTerm:
        return OrderTerm((), "DESC", expression=self.node)
