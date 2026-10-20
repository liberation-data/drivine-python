from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PropertyKey:
    """Groups by a property of the source node: by its stored name, or a reference to it."""

    property: str | Any
