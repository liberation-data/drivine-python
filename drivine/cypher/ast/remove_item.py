from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RemoveProperty:
    """`n.key`: the property of that name."""

    variable: str
    key: str


@dataclass(frozen=True)
class RemoveLabels:
    """`n:Label`: the labels taken from a node."""

    variable: str
    labels: tuple[str, ...]


RemoveItem = RemoveProperty | RemoveLabels
"""An item of a `REMOVE`."""
