"""Conditions, and the functions that compose them."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeAlias

from drivine.dsl.any_label_test import AnyLabelTest
from drivine.dsl.compare_test import CompareTest
from drivine.dsl.composite_condition import CompositeCondition
from drivine.dsl.instance_of_test import InstanceOfTest
from drivine.dsl.path_condition import PathCondition
from drivine.dsl.quantify_test import QuantifyTest

Condition: TypeAlias = CompositeCondition | PathCondition
"""A test of a query's nodes, made through its references and composed with `any_of`, `all_of` and `not_`."""

Where: TypeAlias = Condition | Sequence[Condition]
"""A condition, or several, all of which must hold."""

Test: TypeAlias = CompareTest | QuantifyTest | InstanceOfTest | AnyLabelTest


def any_of(*conditions: Where) -> Condition:
    """Holds when at least one of the conditions does."""
    return CompositeCondition("OR", tuple(all_of_one(it) for it in conditions))


def all_of(*conditions: Where) -> Condition:
    """Holds when all of the conditions do."""
    return CompositeCondition("AND", _flat(conditions))


def not_(*conditions: Where) -> Condition:
    """Holds when the conditions, all together, don't."""
    return CompositeCondition("NOT", _flat(conditions))


def all_of_one(where: Where) -> Condition:
    """A Where as one condition."""
    if isinstance(where, (CompositeCondition, PathCondition)):
        return where
    return all_of(*where)


def _flat(conditions: tuple[Where, ...]) -> tuple[Condition, ...]:
    flat: list[Condition] = []
    for it in conditions:
        if isinstance(it, (CompositeCondition, PathCondition)):
            flat.append(it)
        else:
            flat.extend(it)
    return tuple(flat)
