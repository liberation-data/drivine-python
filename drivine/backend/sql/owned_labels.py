from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class OwnedLabels:
    """
    The labels an open NodeLabels field owns, recorded on the node under `property`. With `clear`, the step drops those
    recorded and no longer held, keeping the `base` labels of the class.
    """

    property: str
    clear: bool
    base: list[str] = field(default_factory=lambda: [])
