"""Keys and aggregates for grouped reads: `gom.groups.load(Review, keys={"page": key(q.page)}, ...)`."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from drivine.annotation.hop import Hop
from drivine.drivine_error import DrivineError
from drivine.manager.group_aggregate import GroupAggregate
from drivine.manager.path_key import PathKey
from drivine.manager.property_key import PropertyKey


def key(property: str | Any) -> PropertyKey:
    """Groups by a property of the source node: its stored name, or a reference to it."""
    return PropertyKey(property)


def path_key(target: type, hops: Sequence[Hop]) -> PathKey:
    """
    Groups by the node of `target` the source reaches through the hops. The key comes back as that node, mapped. A key
    is the node at the end, which every way to it gives alike, so no hop may name an order.
    """
    if any(it.order_by for it in hops):
        raise DrivineError("A path a key is read by is read in no order; no hop of it may name one.")
    return PathKey(target, tuple(hops))


def count(filter: str | Any | None = None) -> GroupAggregate:
    """The number of nodes in the group, or of those that meet `filter`."""
    return GroupAggregate("COUNT", None, filter)


def sum_(property: str | Any, filter: str | Any | None = None) -> GroupAggregate:
    """The sum of a property over the group: 0 over no values."""
    return GroupAggregate("SUM", property, filter)


def avg(property: str | Any, filter: str | Any | None = None) -> GroupAggregate:
    """The average of a property over the group: None over no values."""
    return GroupAggregate("AVG", property, filter)


def min_(property: str | Any, filter: str | Any | None = None) -> GroupAggregate:
    return GroupAggregate("MIN", property, filter)


def max_(property: str | Any, filter: str | Any | None = None) -> GroupAggregate:
    return GroupAggregate("MAX", property, filter)
