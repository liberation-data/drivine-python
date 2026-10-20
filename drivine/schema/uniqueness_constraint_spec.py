from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class UniquenessConstraintSpec:
    """A constraint that no two of a label's nodes hold the same values of the properties."""

    label: str
    properties: tuple[str, ...]
    kind: Literal["UNIQUE"] = "UNIQUE"
