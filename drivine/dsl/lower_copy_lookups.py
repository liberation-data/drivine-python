"""
A condition's case-insensitive comparisons of `CaseInsensitive` properties of the root, made plain comparisons with the
properties' lowercased copies, which an index over a copy serves. drivine4j's `LowerCopyLookups`.
"""

from __future__ import annotations

from collections.abc import Set

from drivine.drivine_error import DrivineError
from drivine.dsl.compare_test import CompareTest
from drivine.dsl.composite_condition import CompositeCondition
from drivine.dsl.condition import Condition, Where
from drivine.dsl.model_scope import scope_of
from drivine.dsl.path_condition import PathCondition
from drivine.dsl.query_renderer import root_property
from drivine.dsl.ref import STORED
from drivine.dsl.scope import Scope
from drivine.model import lower_copies
from drivine.model.target_model import TargetModel

_LOWERED = {"EQ_IGNORE_CASE": "EQ_LOWERED", "CONTAINS_IGNORE_CASE": "CONTAINS_LOWERED"}
"""Each case-insensitive comparison, and what it is on a lowercased copy."""


def lowered(where: Where, scope: Scope, filled: Set[str]) -> Where:
    """
    The condition with each case-insensitive comparison of a `CaseInsensitive` property of the root made a plain
    comparison with the property's lowercased copy: `eq_ignore_case` becomes `EQ_LOWERED` on the copy, and
    `contains_ignore_case` `CONTAINS_LOWERED`.

    Only for the properties in `filled`, whose copy every node of a label of the root is known to have: until then a
    node saved before there were copies would be missed, and the comparison lowercases the property itself, as for any
    other. Nor for an empty value, which Oracle keeps no copy of. Only the root's own conditions, within `any_of` and
    `not_` too: the nodes related to a root are read as the maps a load projects, which hold no copy.
    """

    def rewritten(condition: Condition) -> Condition:
        if isinstance(condition, CompositeCondition):
            return CompositeCondition(condition.kind, tuple(rewritten(it) for it in condition.conditions))
        test = condition.test
        if not isinstance(test, CompareTest) or test.operator not in _LOWERED:
            return condition
        if not isinstance(test.value, str) or not test.value:
            return condition
        try:
            property = root_property(condition.path, scope, "A lookup")
        except DrivineError:
            # Not a property of the root: of a related node, say.
            return condition
        if property not in filled:
            return condition
        return PathCondition(
            (*condition.path[:-1], f"{STORED}{lower_copies.of(property)}"),
            CompareTest(_LOWERED[test.operator], test.value),  # pyright: ignore[reportArgumentType]
        )

    if isinstance(where, (CompositeCondition, PathCondition)):
        return rewritten(where)
    return [rewritten(it) for it in where]


def lowered_where(where: Where | None, target: TargetModel, filled: Set[str]) -> Where | None:
    """The condition, written over the copies in `filled` where it can be: see `lowered`."""
    if where is None or not filled:
        return where
    return lowered(where, scope_of(target), filled)
