from __future__ import annotations

from dataclasses import dataclass

from drivine.annotation.aggregate_function import AggregateFunction
from drivine.annotation.direction import Direction


@dataclass(frozen=True)
class AggregateModel:
    """
    A field of a view computed in the query from the nodes a relationship type leads to.
    """

    field: str
    key: str
    function: AggregateFunction
    type: str
    property: str | None
    direction: Direction
