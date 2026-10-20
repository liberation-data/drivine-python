from __future__ import annotations

from typing import Protocol

from drivine.dsl.comparable_ref import ComparableRef
from drivine.dsl.condition import Condition


class StringRef(ComparableRef[str], Protocol):
    """A property holding text."""

    def contains(self, text: str, /) -> Condition: ...

    def starts_with(self, text: str, /) -> Condition: ...

    def ends_with(self, text: str, /) -> Condition: ...

    def matches(self, regex: str, /) -> Condition:
        """The whole value matches the regular expression, in the engine's own dialect (Java's on Neo4j)."""
        ...

    def contains_ignore_case(self, text: str, /) -> Condition: ...

    def eq_ignore_case(self, text: str, /) -> Condition: ...
