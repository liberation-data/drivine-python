from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from drivine.annotation.hop import Hop
from drivine.model.field_shape import CollectionKind
from drivine.model.fragment_model import FragmentModel


@dataclass(eq=False)
class PathModel:
    """
    A field of a view holding the nodes at the far end of a fixed list of hops.
    """

    field: str
    key: str
    hops: Sequence[Hop]
    is_collection: bool
    required: bool
    resolve: Callable[[], FragmentModel] = dataclasses.field(repr=False)
    _target: FragmentModel | None = dataclasses.field(default=None, repr=False)
    collection: CollectionKind | None = None
    """The kind of collection the field holds, if it holds one."""

    @property
    def target(self) -> FragmentModel:
        """The far node's fragment. Resolved on first use."""
        if self._target is None:
            self._target = self.resolve()
        return self._target
