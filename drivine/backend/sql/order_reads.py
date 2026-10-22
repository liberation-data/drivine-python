from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OrderReads:
    """What a path ordered by a hop reads of the hop's node: its value as a number, as text, and whether it is `""`."""

    num: str
    txt: str
    emp: str
