from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from drivine.dsl.condition import Condition


@dataclass(frozen=True)
class CompositeCondition:
    kind: Literal["AND", "OR", "NOT"]
    conditions: tuple[Condition, ...]
