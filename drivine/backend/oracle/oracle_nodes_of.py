from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OracleNodesOf:
    """The FROM that reads node rows by their ids, and the hint its SELECT carries."""

    from_: str
    hint: str
