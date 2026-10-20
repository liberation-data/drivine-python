from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from drivine.manager.group_aggregate import GroupAggregate
from drivine.manager.path_key import PathKey
from drivine.manager.property_key import PropertyKey

Comparison = Literal["eq", "neq", "gt", "gte", "lt", "lte"]


@dataclass(frozen=True)
class GroupQuery:
    """A grouped read. `gom.groups.load` takes these as keyword arguments."""

    keys: Mapping[str, PropertyKey | PathKey]
    """What puts two nodes in one group. Two or more make a compound key."""
    aggregates: Mapping[str, GroupAggregate] = field(default_factory=lambda: {})
    """The numbers about each group."""
    where: str | Any | None = None
    """Which nodes are read at all: a condition over the references, or a Cypher predicate over the node, `n`."""
    bindings: Mapping[str, Any] = field(default_factory=lambda: {})
    having: Sequence[tuple[str, Comparison, float]] = ()
    """Which groups are returned: each compares an aggregate with a value. Joined by AND."""
    order_by: Sequence[tuple[str, Literal["asc", "desc"]]] = ()
    """By key or aggregate name; ties are broken by the keys, ascending."""
    limit: int | None = None
    skip: int | None = None
