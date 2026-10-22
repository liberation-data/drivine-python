from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NodesOf:
    """The FROM that reads node rows by their ids, and the hint its SELECT carries, where the store reads hints."""

    from_: str
    hint: str
