from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from drivine.dsl.case_branch import CaseBranch
from drivine.dsl.condition_node import ConditionNode

if TYPE_CHECKING:
    from drivine.dsl.order_expressions import Operand


@dataclass(frozen=True)
class CaseCondition:
    """A test within a `case_of`, combined with `and_`, `or_` and `not_`."""

    node: ConditionNode

    def and_(self, other: CaseCondition) -> CaseCondition:
        return CaseCondition(ConditionNode("AND", conditions=(self.node, other.node)))

    def or_(self, other: CaseCondition) -> CaseCondition:
        return CaseCondition(ConditionNode("OR", conditions=(self.node, other.node)))

    def not_(self) -> CaseCondition:
        return CaseCondition(ConditionNode("NOT", conditions=(self.node,)))

    def then(self, value: Operand) -> CaseBranch:
        """The branch this condition chooses: `above(q.x, 1).then(0)`."""
        from drivine.dsl.order_expressions import node_of

        return CaseBranch(self.node, node_of(value))
