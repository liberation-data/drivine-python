from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from drivine.cypher.ast.node_pattern import NodePattern
from drivine.cypher.ast.relationship_pattern import RelationshipPattern


class Shortest(Enum):
    """`shortestPath(…)`, one of the shortest paths, or `allShortestPaths(…)`, every one."""

    ONE = "ONE"
    ALL = "ALL"


@dataclass(frozen=True)
class Pattern:
    """
    A path of a `MATCH`, a `CREATE`, a `MERGE`, an `EXISTS`, a pattern comprehension or a pattern predicate: nodes
    joined by relationships, one more node than relationships. With a variable, the path itself is bound to it. With
    `shortest`, only the paths of the least length are found: one of them, or all.
    """

    variable: str | None
    nodes: tuple[NodePattern, ...]
    relationships: tuple[RelationshipPattern, ...]
    shortest: Shortest | None = None

    def __post_init__(self) -> None:
        if len(self.nodes) != len(self.relationships) + 1:
            raise ValueError("A path has one node more than it has relationships.")
