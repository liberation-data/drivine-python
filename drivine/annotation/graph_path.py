from __future__ import annotations

from collections.abc import Sequence
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


@dataclass(frozen=True)
class GraphPath:
    """
    A field of a view holding the nodes at the far end of a fixed list of hops from the root, each once, skipping the
    nodes between: `directors: Annotated[list[Director], GraphPath([hop("ACTED_IN"), hop("DIRECTED_BY")])] = []`.
    Read-only. A single path typed without `None` is required, and a list is with `required=True`.
    """

    hops: Sequence[Hop]
    required: bool = False

    def __hash__(self) -> int:
        return hash((tuple(self.hops), self.required))
