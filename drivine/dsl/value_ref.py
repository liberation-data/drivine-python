from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, TypeVar

from drivine.dsl.condition import Condition
from drivine.dsl.order_term import OrderTerm
from drivine.dsl.seek_value import SeekValue

V = TypeVar("V", contravariant=True)


class ValueRef(Protocol[V]):
    """
    A property, as a type checker sees it: compared to values of its type, and ordered by. For the stubs `python -m
    drivine.stubs` writes; at run time every reference is a Ref.
    """

    def eq(self, value: V, /) -> Condition: ...

    def neq(self, value: V, /) -> Condition: ...

    def in_(self, values: Iterable[V], /) -> Condition: ...

    def not_in(self, values: Iterable[V], /) -> Condition: ...

    def is_null(self) -> Condition: ...

    def is_not_null(self) -> Condition: ...

    def asc(self) -> OrderTerm: ...

    def desc(self) -> OrderTerm: ...

    def after(self, value: V, /) -> SeekValue:
        """The property's value at the end of the last page: a keyset cursor's component, for `seek`."""
        ...
