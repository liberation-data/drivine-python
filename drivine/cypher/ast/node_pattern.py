from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from drivine.cypher.ast.expression import Expression


@dataclass(frozen=True)
class NodePattern:
    """A node of a pattern, `(variable:Label {key: value})`: each part may be left out."""

    variable: str | None
    labels: tuple[str, ...]
    properties: Expression | None
