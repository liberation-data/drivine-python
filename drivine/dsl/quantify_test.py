from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from drivine.dsl.condition import Condition


@dataclass(frozen=True)
class QuantifyTest:
    """One of the nodes a relationship leads to meets all the conditions together (`any`), or none does (`none`)."""

    negate: bool
    conditions: tuple[Condition, ...]
