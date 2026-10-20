from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from drivine.dsl.condition import Where
from drivine.dsl.order_term import OrderTerm
from drivine.dsl.seek_value import SeekValue


@dataclass(frozen=True)
class Query:
    """What a query asks of a load beyond its class. `load_all` takes these as keyword arguments."""

    where: Where | None = None
    order_by: OrderTerm | Sequence[OrderTerm] | None = None
    """Terms applied in turn, each breaking the ties of those before."""
    limit: int | None = None
    """At most this many objects (for a view, roots), after the order."""
    skip: int | None = None
    seek: SeekValue | Sequence[SeekValue] | None = None
    """
    Continues after a keyset cursor: one value for each term of `order_by`, of the same properties in the same order.
    Not with `skip`, nor with an order that places nulls.
    """
    depth: Mapping[str, int] | None = None
