from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NodeLabelsModel:
    field: str
    members: tuple[str, ...] | None
    """The labels a closed field owns; None for an open one."""
    is_set: bool
    """Whether the field holds a set, rather than a list."""
    record_property: str
    """Where an open field records the labels it wrote."""
