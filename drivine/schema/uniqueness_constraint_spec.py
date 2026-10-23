from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class UniquenessConstraintSpec:
    """A constraint that no two of a label's nodes hold the same values of the properties."""

    label: str
    properties: tuple[str, ...]
    kind: Literal["UNIQUE"] = "UNIQUE"
    node_id_property: str | None = None
    """
    The property a node is identified by, the NodeId of the fragment the spec was read from; None for one declared by
    hand. Not part of what is made: a violation's sample names each conflicting node by it, beside the engine's own id,
    and two declarations that differ only in it are one item.
    """
