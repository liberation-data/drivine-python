from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from drivine.dsl.operator import Operator


@dataclass(frozen=True)
class CompareTest:
    """A property compared to a value."""

    operator: Operator
    value: Any = None
