from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from drivine.dsl.scope import Scope
from drivine.model.relationship_model import RelationshipModel


@dataclass(frozen=True)
class Step:
    """
    Where one field of a path leads: to the same node seen another way (a view's root, a fragment's property bag),
    from a relationship fragment to its TARGET, along a view's relationship (a HOP), to a PROPERTY of the node or of the
    relationship a relationship fragment holds, or to a NodeLabels field's LABELS.
    """

    kind: Literal["SAME_NODE", "TARGET", "HOP", "PROPERTY", "LABELS"]
    scope: Scope | None = None
    rel: RelationshipModel | None = None
    property: str = ""
    on: Literal["NODE", "RELATIONSHIP"] = "NODE"
    json_packed: bool = False
