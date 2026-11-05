from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from drivine.cypher.ast.expression import Expression


@dataclass(frozen=True)
class NodePattern:
    """A node of a pattern, `(variable:Label {key: value})`: each part may be left out."""

    variable: str | None
    labels: tuple[str, ...]
    properties: Expression | None
    mapped: bool = field(default=False, compare=False, kw_only=True)
    """
    Whether the node is written with a map of properties: an empty one, `{}`, is read as no `properties`
    (drivine4j#583), but a `CREATE` or `MERGE` still makes no node bound already with it, as Neo4j refuses it. Left out
    of equality, as it matches alike. A node given `properties` is mapped.
    """

    def __post_init__(self) -> None:
        if self.properties is not None and not self.mapped:
            object.__setattr__(self, "mapped", True)
