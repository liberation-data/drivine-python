from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Literal

from drivine.dsl.expression_node import ExpressionNode


@dataclass(frozen=True)
class OrderTerm:
    """
    One term of an order: by a property of the root, by an expression over the root's properties, or by a property of
    the nodes a list relationship leads to, which orders that list in each object rather than the roots.
    """

    path: tuple[str, ...]
    direction: Literal["ASC", "DESC"]
    nulls: Literal["FIRST", "LAST"] | None = None
    """Where rows with no value go; None, where the engine puts them."""
    expression: ExpressionNode | None = None

    def nulls_first(self) -> OrderTerm:
        """This term, with rows that have no value before every row that has one."""
        return dataclasses.replace(self, nulls="FIRST")

    def nulls_last(self) -> OrderTerm:
        """This term, with rows that have no value after every row that has one."""
        return dataclasses.replace(self, nulls="LAST")
