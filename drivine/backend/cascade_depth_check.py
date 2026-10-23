from __future__ import annotations

from dataclasses import dataclass

from drivine.backend.graph_statement import GraphStatement


@dataclass(frozen=True)
class CascadeDepthCheck:
    """
    A relationship to views of its own class that a cascading delete follows, to `max_depth` levels, and the read that
    counts the nodes one level further that the delete would leave behind: such a node is related to one the cascade
    deletes, and keeps everything it holds. `view` declares `field`. Returns one number: `{beyond}`.
    """

    view: str
    field: str
    max_depth: int
    statement: GraphStatement
