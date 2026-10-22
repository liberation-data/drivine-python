from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from drivine.backend.sql.owned_labels import OwnedLabels


@dataclass(frozen=True)
class NodeWrites:
    """What a `node` step writes to a node, beyond its properties."""

    set: dict[str, Any] = field(default_factory=lambda: {})
    """The properties to write, by the names they are stored under."""
    unset: list[str] = field(default_factory=lambda: [])
    """The properties to remove."""
    add: list[str] = field(default_factory=lambda: [])
    """The labels to give the node."""
    drop: list[str] = field(default_factory=lambda: [])
    """The labels to take from it."""
    owned: OwnedLabels | None = None
