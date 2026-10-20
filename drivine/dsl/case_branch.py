from __future__ import annotations

from dataclasses import dataclass

from drivine.dsl.condition_node import ConditionNode
from drivine.dsl.expression_node import ExpressionNode


@dataclass(frozen=True)
class CaseBranch:
    """A branch of a `case_of`: its value when its condition holds."""

    when: ConditionNode
    then: ExpressionNode
