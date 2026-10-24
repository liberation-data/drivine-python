from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from drivine.cypher.ast.expression import Expression


@dataclass(frozen=True)
class ProjectionItem:
    """One item: its expression, and the name it is known by: its alias, or the text it was written as."""

    expression: Expression
    name: str


@dataclass(frozen=True)
class SortItem:
    """An order term."""

    expression: Expression
    descending: bool


@dataclass(frozen=True)
class Projection:
    """
    What a `WITH` or a `RETURN` gives the next part of a query, or the caller: its items, each under the name it is
    known by, then the order of the rows, how many are skipped and how many are kept. With `star`, every variable in
    scope is carried as well.
    """

    distinct: bool
    star: bool
    items: tuple[ProjectionItem, ...]
    order_by: tuple[SortItem, ...]
    skip: Expression | None
    limit: Expression | None
