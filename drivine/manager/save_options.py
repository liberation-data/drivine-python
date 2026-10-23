from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from drivine.manager.null_policy import NullPolicy
from drivine.manager.removed_targets import RemovedTargets


@dataclass(frozen=True)
class SaveOptions:
    """
    How a save writes an object. `StatelessGraphObjectManager.save` takes these as keyword arguments.
    """

    replace: Sequence[str] | Literal["all"] | None = None
    """
    Relationship fields of the view whose value is the whole of the relationship: what the store holds and the field
    doesn't is removed. Without it, a save only adds. "all" replaces every relationship field of the view itself, and
    needs an object whose root carries a stamp.
    """
    removed_targets: RemovedTargets = RemovedTargets.KEEP
    null_policy: NullPolicy = NullPolicy.IGNORE
    only: Sequence[str] | None = None
    """Fields of the object (of a view, its root) to write. Not the id or the stamp."""
    except_: Sequence[str] | None = None
    """Fields of the object (of a view, its root) not to write."""
    held_properties: Mapping[str, Any] = field(default_factory=lambda: {})
    """
    Internal: the root's properties, read first under CLEAR to remove property-bag entries a dict no longer has and
    labels a NodeLabels field no longer holds.
    """
    loaded: Any = None
    """
    Internal: the root as `update` loaded it, before its change ran, or the root of `save`'s `from_`. Where a read
    takes no snapshot (Neo4j), the checked save compares the node with it under the node's lock, as well as the stamp:
    the stamp a load was handed may be newer than the data it read.
    """
    loaded_properties: Mapping[str, Any] = field(default_factory=lambda: {})
    """
    Internal: the root's properties as `update`'s load read them, by stored name. A JsonPacked field is compared as the
    text the node held, not as its value written again: text another writer wrote with other spacing would differ.
    `save`'s `from_` has none, the object holding only the value, so a packed field is then compared as a save packs it.
    """
