from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from drivine.cypher.ast.expression import Expression


@dataclass(frozen=True)
class SetProperty:
    """`n.key = value`: the property of that name, removed where the value is null."""

    variable: str
    key: str
    value: Expression


@dataclass(frozen=True)
class SetReplace:
    """`n = map`: every property replaced by the map's, or a node's or a relationship's properties."""

    variable: str
    value: Expression


@dataclass(frozen=True)
class SetAdd:
    """`n += map`: the map's entries set over the properties, a null one removing its key."""

    variable: str
    value: Expression


@dataclass(frozen=True)
class SetLabels:
    """`n:Label`: the labels added to a node."""

    variable: str
    labels: tuple[str, ...]


SetItem = SetProperty | SetReplace | SetAdd | SetLabels
"""An item of a `SET`, or of a `MERGE`'s `ON CREATE SET` or `ON MATCH SET`."""
