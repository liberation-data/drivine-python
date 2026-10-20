from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PatchResult:
    found: bool
    """A node with the id was there. A patch never makes one."""
    applied: bool
    """The patch's condition held, so its fields were written. Without a condition, the same as `found`."""
    changed: bool
    """A value written differed from what the node held, so the node has a new stamp."""
