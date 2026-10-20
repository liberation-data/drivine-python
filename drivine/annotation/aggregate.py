from __future__ import annotations

from dataclasses import dataclass

from drivine.annotation.aggregate_function import AggregateFunction
from drivine.annotation.direction import Direction


@dataclass(frozen=True)
class Aggregate:
    """
    A field of a view computed in the query from the nodes one relationship type leads to from the root: their
    number, or the SUM, AVG, MIN or MAX of one of their properties, named as stored. SUM and COUNT of none are 0; AVG,
    MIN and MAX of none are None. Read-only.
    """

    function: AggregateFunction
    type: str
    property: str | None = None
    direction: Direction = Direction.OUTGOING


def Count(type: str, direction: Direction = Direction.OUTGOING) -> Aggregate:  # noqa: N802 - reads as a marker
    """The number of nodes a relationship type leads to from the view's root. Read-only."""
    return Aggregate(AggregateFunction.COUNT, type, None, direction)
