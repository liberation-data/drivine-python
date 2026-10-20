from __future__ import annotations

from typing import Protocol, TypeVar

from drivine.dsl.condition import Condition

U = TypeVar("U", contravariant=True)


class ListRef(Protocol[U]):
    """A property holding a list, or a set: of a node's labels, for a NodeLabels field."""

    def has_item(self, value: U, /) -> Condition: ...

    def is_null(self) -> Condition: ...

    def is_not_null(self) -> Condition: ...
