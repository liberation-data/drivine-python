from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RangeIndexSpec:
    """A range index over one or more properties of a label's nodes."""

    label: str
    properties: tuple[str, ...]
    kind: Literal["RANGE"] = "RANGE"
    ignore_case: bool = False
    """
    Whether the index is over the lowercased copy of each property, which a `CaseInsensitive` field keeps (see
    lower_copies), so that it serves `eq_ignore_case`. Ensuring it fills the copies in on the label's nodes that have
    none, and records that they all have one. It is made under its own name, `<label>_<properties>_ci_range`.
    """
    name: str | None = None
    """The name it is made under, where it is not the one derived from its label and properties: see `item_name`."""
