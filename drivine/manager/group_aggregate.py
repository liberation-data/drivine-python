from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class GroupAggregate:
    function: Literal["COUNT", "SUM", "AVG", "MIN", "MAX"]
    property: str | Any | None = None
    """The property of the source node aggregated: its stored name, or a reference. None for COUNT."""
    filter: str | Any | None = None
    """Only nodes that meet it count toward this number: a condition over the references, or Cypher over `n`."""
