from __future__ import annotations

import dataclasses
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
    within: list[str] = dataclasses.field(default_factory=lambda: [])
    """
    The nodes of the view the cascade takes, wherever it reaches the view: the levels of the relationship, and the node
    a level is first reached from, unless that is the cascade's root, which the check leaves out on its own. A node one
    level further that is one of these is not left behind. Only these are read, and not every target, since a read of
    each comprehension the cascade has is a statement the graph engines take long to plan.
    """
