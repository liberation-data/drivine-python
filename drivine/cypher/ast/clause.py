from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from drivine.cypher.ast.pattern import Pattern
from drivine.cypher.ast.projection import Projection
from drivine.cypher.ast.remove_item import RemoveItem
from drivine.cypher.ast.set_item import SetItem

if TYPE_CHECKING:
    from drivine.cypher.ast.expression import Expression
    from drivine.cypher.ast.query import Query


@dataclass(frozen=True)
class Match:
    """`MATCH` or `OPTIONAL MATCH`: the paths that must be there, or with `optional` may be."""

    optional: bool
    patterns: tuple[Pattern, ...]
    where: Expression | None


@dataclass(frozen=True)
class Unwind:
    """`UNWIND`: a row for each item of a list, bound to `variable`."""

    list: Expression
    variable: str


@dataclass(frozen=True)
class With:
    """`WITH`: what the rest of the query sees, and the rows it keeps."""

    projection: Projection
    where: Expression | None


@dataclass(frozen=True)
class Create:
    """`CREATE`: the nodes and relationships of its patterns made, for each row."""

    patterns: tuple[Pattern, ...]


@dataclass(frozen=True)
class Merge:
    """
    `MERGE`: for each row, the pattern found, each way it is there, or made whole where it is not, with `on_create` set
    on what it made and `on_match` on what it found.
    """

    pattern: Pattern
    on_create: tuple[SetItem, ...]
    on_match: tuple[SetItem, ...]


@dataclass(frozen=True)
class SetClause:
    """`SET`: its items written in turn, for each row."""

    items: tuple[SetItem, ...]


@dataclass(frozen=True)
class Remove:
    """`REMOVE`: its items taken away in turn, for each row."""

    items: tuple[RemoveItem, ...]


@dataclass(frozen=True)
class Delete:
    """`DELETE` of nodes and relationships, or with `detach` a node with its relationships."""

    detach: bool
    expressions: tuple[Expression, ...]


@dataclass(frozen=True)
class Call:
    """
    `CALL { … }`: for each row, the rows its query gives, read from the variables around it, with what it returns bound
    for the clauses after; with `optional`, a row it gives none for is kept, with nulls. `imports` are the variables its
    brackets import, `CALL (a, b) { … }`, and with `imports_all` it imports each, `CALL (*) { … }`, as a procedure's
    call does, `CALL db.labels() YIELD label WHERE …`, whose `WHERE` reads each variable around it; with no brackets,
    None, its leading `WITH` imports them.
    """

    optional: bool
    query: Query
    imports: tuple[str, ...] | None = None
    imports_all: bool = False


@dataclass(frozen=True)
class Foreach:
    """`FOREACH (x IN list | …)`: its clauses written for each item of the list, bound to `variable`; the rows are left
    as they were."""

    variable: str
    list: Expression
    clauses: tuple[Clause, ...]


@dataclass(frozen=True)
class Return:
    """`RETURN`: what the query gives back."""

    projection: Projection


Clause = Match | Unwind | With | Create | Merge | SetClause | Remove | Delete | Call | Foreach | Return
"""A clause of a query, in the order the query reads them."""
