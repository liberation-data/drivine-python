from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RangeIndexSpec:
    """A range index over one or more properties of a label's nodes."""

    label: str
    properties: tuple[str, ...]
    kind: Literal["RANGE"] = "RANGE"
