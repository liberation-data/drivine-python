from __future__ import annotations

from dataclasses import dataclass

from drivine.annotation.direction import Direction


@dataclass(frozen=True)
class Hop:
    """
    One hop of a path: a relationship type, its direction, and optionally a label the node it reaches must carry,
    and a property of that node to read the path in the order of.
    """

    type: str
    direction: Direction = Direction.OUTGOING
    label: str | None = None
    order_by: str | None = None
    """
    A property of the node this hop reaches, to read the path in its order: a list holds its targets in the order of
    the first node of this hop that leads to each, and a single holds the one the first such node leads to. One hop of
    a path may name one, and a path a group key is read by may name none. A node without the property comes last
    whichever way the path is read, and targets whose first nodes hold equal values come in no set order. A text of
    digits is ordered as text, and an empty text is the lowest text. On Neo4j and the SQL backends a date is ordered by
    its moment. Values of several kinds are ordered by kind as Cypher's ORDER BY does on Neo4j, Memgraph and the SQL
    backends, and on FalkorDB by its own ORDER BY. See docs/06-fragments-and-views.md.
    """
    ascending: bool = True
    """Whether `order_by` reads lowest first."""


def hop(
    type: str,
    direction: Direction = Direction.OUTGOING,
    label: str | None = None,
    order_by: str | None = None,
    ascending: bool = True,
) -> Hop:
    return Hop(type, direction, label, order_by, ascending)
