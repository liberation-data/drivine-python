from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from drivine.annotation.direction import Direction
from drivine.annotation.sorted_by import SortedBy
from drivine.model.field_shape import CollectionKind
from drivine.model.fragment_model import FragmentModel

if TYPE_CHECKING:
    from drivine.model.target_model import RelatedModel


@dataclass(eq=False)
class RelationshipModel:
    """
    A relationship field of a view.
    """

    field: str
    key: str
    """The name the field goes by in a statement: its stored name, by the view's property naming."""
    type: str
    direction: Direction
    max_depth: int
    required: bool
    is_collection: bool
    marked_read_only: bool
    sorted_by: SortedBy | None
    resolve: Callable[[], RelatedModel] = dataclasses.field(repr=False)
    _related: RelatedModel | None = dataclasses.field(default=None, repr=False)
    collection: CollectionKind | None = None
    """The kind of collection the field holds, if it holds one."""

    @property
    def related(self) -> RelatedModel:
        """What the field holds. Resolved on first use, so a view may refer to itself."""
        if self._related is None:
            self._related = self.resolve()
        return self._related

    @property
    def read_only(self) -> bool:
        """Loaded and never written: marked so, or a list of fragments read over several hops."""
        return self.marked_read_only or (isinstance(self.related, FragmentModel) and self.max_depth > 1)
