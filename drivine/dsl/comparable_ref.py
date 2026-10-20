from __future__ import annotations

from typing import Protocol, TypeVar

from drivine.dsl.condition import Condition
from drivine.dsl.value_ref import ValueRef

V = TypeVar("V", contravariant=True)


class ComparableRef(ValueRef[V], Protocol[V]):
    """A property whose values have an order: a number, a date or a time, say."""

    def gt(self, value: V, /) -> Condition: ...

    def gte(self, value: V, /) -> Condition: ...

    def lt(self, value: V, /) -> Condition: ...

    def lte(self, value: V, /) -> Condition: ...
