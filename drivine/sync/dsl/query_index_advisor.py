# Written by scripts/unasync.py from drivine/dsl/query_index_advisor.py:
# change that, and run the script again.
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Literal

from drivine.drivine_error import DrivineError
from drivine.dsl.index_advice_policy import IndexAdvicePolicy
from drivine.model.fragment_model import FragmentModel
from drivine.schema.schema_item import SchemaItem
from drivine.sync.schema.constraint_manager import ConstraintManager
from drivine.sync.schema.index_manager import IndexManager

logger = logging.getLogger("drivine.dsl")


class QueryIndexAdvisor:
    """
    Checks that an ordered query has a range index over exactly its ordered properties, in order, and says so when it
    has none. The index list is read once and kept: an index made later is unseen until `refresh`, which the object
    manager's own index and constraint managers call when they make or drop one. The list is also what a filter is
    read through, on a store that does (see SchemaGrammar's labelled_indexes).
    """

    def __init__(
        self,
        index_manager: Callable[[], IndexManager],
        constraint_manager: Callable[[], ConstraintManager],
        ranges_per_property: Callable[[], bool],
    ) -> None:
        """`ranges_per_property` tells whether the engine indexes properties one by one, so that an order is served
        when each is indexed."""
        self._index_manager = index_manager
        self._constraint_manager = constraint_manager
        self._ranges_per_property = ranges_per_property
        self._indexes: list[SchemaItem] | None = None
        self._constraints: list[SchemaItem] | None = None
        self._reported: set[str] = set()

    def check(
        self,
        policy: IndexAdvicePolicy,
        model: FragmentModel,
        properties: Sequence[str],
        operation: Literal["order_by", "seek"],
        pinned: Sequence[str] = (),
    ) -> None:
        """
        Advises on an order of `model`'s nodes by `properties` (stored names), unless the query's equalities on
        `pinned` already narrow it to one root, so that its order is over one row.
        """
        if policy == IndexAdvicePolicy.OFF or not properties:
            return
        if self._selects_one(model, pinned) or self._covered(model, properties):
            return
        label = model.labels[0]
        names = ", ".join(repr(it) for it in properties)
        declaration = f"RangeIndex([{names}])" if len(properties) > 1 else "RangeIndex()"
        message = (
            f"{operation} on {label}({', '.join(properties)}) has no matching range index, so the query scans and "
            f"sorts rather than seeking. Declare {declaration} on the fragment, or call "
            f"indexes.ensure(range_index({label!r}, [{names}])). The index must cover exactly these properties, in "
            "this order."
        )
        if policy == IndexAdvicePolicy.FAIL:
            raise DrivineError(message)
        signature = f"{operation}|{label}|{','.join(properties)}"
        if signature not in self._reported:
            self._reported.add(signature)
            logger.warning(message)

    def indexed(self) -> list[SchemaItem]:
        """The range indexes and uniqueness constraints there are, read once and kept as for advice."""
        if self._indexes is None:
            self._indexes = self._read("indexes", lambda: self._index_manager().list("RANGE"))
        if self._constraints is None:
            self._constraints = self._read("constraints", lambda: self._constraint_manager().list())
        return [*self._indexes, *self._constraints]

    def refresh(self) -> None:
        """Forgets the index list, so the next check reads it again."""
        self._indexes = None
        self._constraints = None
        self._reported.clear()

    def _covered(self, model: FragmentModel, properties: Sequence[str]) -> bool:
        if self._indexes is None:
            self._indexes = self._read("indexes", lambda: self._index_manager().list("RANGE"))
        items = [it for it in self._indexes if it.label in model.labels]
        if self._ranges_per_property():
            return all(any(property in it.properties for it in items) for property in properties)
        return any(list(it.properties) == list(properties) for it in items)

    def _selects_one(self, model: FragmentModel, pinned: Sequence[str]) -> bool:
        """Whether equalities on `pinned` select at most one root: on the NodeId, or over a uniqueness constraint."""
        if not pinned:
            return False
        if model.node_id_property and model.node_id_property in pinned:
            return True
        if self._constraints is None:
            self._constraints = self._read("constraints", lambda: self._constraint_manager().list())
        return any(
            it.label in model.labels and all(property in pinned for property in it.properties)
            for it in self._constraints
        )

    @staticmethod
    def _read(what: str, listing: Callable[[], list[SchemaItem]]) -> list[SchemaItem]:
        """Advice never breaks a query: an engine that can't list its schema gives an empty list."""
        try:
            return listing()
        except Exception as e:
            logger.debug("Could not read %s for query index advice: %s", what, e)
            return []
