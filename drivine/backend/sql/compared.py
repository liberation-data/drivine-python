from __future__ import annotations

from dataclasses import dataclass

from drivine.dsl.condition import Test


@dataclass(frozen=True)
class Compared:
    """The comparison a test of a property makes: what it compares, of which property, and how it is stored."""

    test: Test
    row: str
    """The row the property is read from: a node's, or a relationship's."""
    on_relationship: bool
    """Whether the row is a relationship's."""
    name: str
    json_packed: bool
    path: tuple[str, ...]
