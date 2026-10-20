from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NodeRef:
    """A stored node, by its fragment class, its id, and any labels it carries beyond the class's."""

    type: type
    id: Any
    labels: Sequence[str] = ()


def node_ref(type: type, id: Any, *labels: str) -> NodeRef:
    return NodeRef(type, id, labels)
