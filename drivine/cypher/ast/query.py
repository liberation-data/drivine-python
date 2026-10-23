from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from drivine.cypher.ast.clause import Clause


@dataclass(frozen=True)
class Union:
    """A query joined to the one before it by `UNION`, its rows after that query's, or with `all` every row kept."""

    all: bool
    clauses: tuple[Clause, ...]


@dataclass(frozen=True)
class Query:
    """
    A query: its clauses in order, ending in a `RETURN`, or in a write that returns nothing; then the queries `unions`
    joins to it, each ending in a `RETURN` of the same names.
    """

    clauses: tuple[Clause, ...]
    unions: tuple[Union, ...] = ()
