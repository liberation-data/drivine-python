from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from drivine.cypher.ast.expression import Expression
    from drivine.cypher.ast.pattern import Pattern


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
class Group:
    """
    The pattern a quantified path pattern repeats, each repetition's first node the last of the one before, and the
    where each repetition meets. To the rest of the statement, a variable of the pattern is the list of what it matched
    in each repetition.
    """

    pattern: Pattern
    where: Expression | None


@dataclass(frozen=True)
class RelationshipPattern:
    """
    A relationship of a pattern, `-[variable:TYPE|OTHER *min..max {key: value}]->`. Its types are alternatives, any of
    which a relationship may be. With a range, it is a path of such relationships of that many steps. With a group, it
    is a quantified path pattern, `((a)-[:R]->(b)){1,3}`, that many repetitions of the group's pattern between the nodes
    beside it, and has no variable, types or properties of its own.
    """

    variable: str | None
    types: tuple[str, ...]
    direction: Direction
    range: Range | None
    properties: Expression | None
    group: Group | None = None
