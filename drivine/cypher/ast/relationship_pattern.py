from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from drivine.cypher.ast.expression import Expression


class Direction(Enum):
    """Which way a relationship points, read from the node before it in the pattern."""

    OUTGOING = "OUTGOING"
    INCOMING = "INCOMING"
    EITHER = "EITHER"


@dataclass(frozen=True)
class Range:
    """How many steps a variable-length relationship takes; a bound left out is open."""

    min: int | None
    max: int | None


@dataclass(frozen=True)
class RelationshipPattern:
    """
    A relationship of a pattern, `-[variable:TYPE|OTHER *min..max {key: value}]->`. Its types are alternatives, any of
    which a relationship may be. With a range, it is a path of such relationships of that many steps.
    """

    variable: str | None
    types: tuple[str, ...]
    direction: Direction
    range: Range | None
    properties: Expression | None
