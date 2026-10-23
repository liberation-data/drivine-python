from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CascadeBeyond:
    """
    A relationship of a view to views of its own class, which a cascade follows to its max_depth, and the nodes one
    level further: a list expression in Cypher, a query of their ids in SQL. Those are what the cascade would leave
    behind, with all they hold. `view` declares `field`.
    """

    view: str
    field: str
    max_depth: int
    nodes: str
