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
    ignore_case: bool = False
    """
    Whether the constraint is over the lowercased copy of each property, which a `CaseInsensitive` field keeps (see
    lower_copies), so that `A@x.com` is refused beside `a@x.com`. Ensuring it fills the copies in on the label's nodes
    that have none first. It is made under its own name, `<label>_<properties>_ci_unique`. FalkorDB can't make one.
    """
    name: str | None = None
    """The name it is made under, where it is not the one derived from its label and properties: see `item_name`."""
