from __future__ import annotations

from typing import Any, Protocol

from drivine.dsl.comparable_ref import ComparableRef
from drivine.dsl.condition import Condition
from drivine.dsl.operator import Operator


class NodeTests(Protocol):
    """
    What every node's references offer beside its fields, as a type checker sees them. A field named as one of these
    is reached with `field`.
    """

    def instance_of(self, type_: type, /) -> Condition:
        """The node is of the class, or a subclass: it carries all of the class's labels."""
        ...

    def has_any_label(self, *labels: str) -> Condition:
        """The node carries at least one of the labels."""
        ...

    def property(self, name: str, /) -> ComparableRef[Any]:
        """
        A property by the name it is stored under, known only at run time: a bag entry's whole name
        (`metadata.source`), say. Of a relationship fragment, the relationship's property.
        """
        ...

    def predicate(self, name: str, operator: Operator, value: Any = None, /) -> Condition:
        """A test of a property by its stored name, from an operator known only at run time."""
        ...

    def field(self, name: str, /) -> Any:
        """The field of this name, for a field named as one of these tests."""
        ...
