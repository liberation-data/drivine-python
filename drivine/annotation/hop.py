from __future__ import annotations

from dataclasses import dataclass

from drivine.annotation.direction import Direction


@dataclass(frozen=True)
class Hop:
    """
    One hop of a path: a relationship type, its direction, and optionally a label the node it reaches must carry,
    and a property of that node to read the path in the order of. See TypeScript's `Hop` for how an order is read.
    """

    type: str
    direction: Direction = Direction.OUTGOING
    label: str | None = None
    order_by: str | None = None
    ascending: bool = True


def hop(
    type: str,
    direction: Direction = Direction.OUTGOING,
    label: str | None = None,
    order_by: str | None = None,
    ascending: bool = True,
) -> Hop:
    return Hop(type, direction, label, order_by, ascending)
