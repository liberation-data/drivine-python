from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from drivine.cypher.ast.clause import Clause, Match, Return, Unwind, With
from drivine.cypher.ast.expression import Expression, Function, Variable
from drivine.cypher.ast.node_pattern import NodePattern
from drivine.cypher.ast.pattern import Pattern
from drivine.cypher.ast.projection import Projection, ProjectionItem
from drivine.cypher.ast.query import Query
from drivine.cypher.ast.relationship_pattern import Direction, RelationshipPattern
from drivine.drivine_error import DrivineError

_ROW = "#procedure"
_VALUE = "#procedureValue"
"""
The query's own variables, named with `#`, which a statement cannot write, so they meet none of the statement's: a
`CALL` within a query reads the variables around it, for a `WHERE` after its `YIELD`.
"""


@dataclass(frozen=True)
class Procedure:
    """
    A procedure the compiler takes, `CALL db.labels()`, read as the query that gives its rows: a SQL store keeps no
    catalog of labels or types beside its nodes and relationships, so they are read from those, each once, as Neo4j
    lists only the labels and types that something has now. Its one `field` is what it yields. drivine4j's Procedure,
    ported.
    """

    qualified_name: str
    field: str
    reading: Callable[[], tuple[Clause, ...]]
    """The clauses that read each label or type, once for each node or relationship that has it."""
    value: Callable[[], Expression]
    """The label or type, in the scope `reading` gives."""

    def query(
        self, arguments: Sequence[Expression], yields: Sequence[tuple[str, str]], where: Expression | None
    ) -> Query:
        """
        The procedure's rows, each value once, as the query that returns them: each field yielded under its variable,
        `yields` pairs of the two, and with `where`, only the rows that meet it.
        """
        if arguments:
            raise DrivineError(f"{self.qualified_name}() takes no argument.")
        for yielded, _ in yields:
            if yielded != self.field:
                raise DrivineError(f"{self.qualified_name}() yields {self.field}, not {yielded}.")
        variables = [variable for _, variable in yields]

        def projection(distinct: bool, items: Sequence[ProjectionItem]) -> Projection:
            return Projection(distinct, False, tuple(items), (), None, None)

        distinct = projection(True, [ProjectionItem(self.value(), it) for it in variables])
        if where is None:
            return Query((*self.reading(), Return(distinct)))
        kept = projection(False, [ProjectionItem(Variable(it), it) for it in variables])
        return Query((*self.reading(), With(distinct, where), Return(kept)))


def _labels() -> tuple[Clause, ...]:
    return (
        Match(False, (Pattern(None, (NodePattern(_ROW, (), None),), ()),), None),
        Unwind(Function("labels", False, (Variable(_ROW),)), _VALUE),
    )


def _relationship_types() -> tuple[Clause, ...]:
    relationship = RelationshipPattern(_ROW, (), Direction.OUTGOING, None, None)
    nodes = (NodePattern(None, (), None), NodePattern(None, (), None))
    return (Match(False, (Pattern(None, nodes, (relationship,)),), None),)


PROCEDURES = (
    # `db.labels()`: each label a node has.
    Procedure("db.labels", "label", _labels, lambda: Variable(_VALUE)),
    # `db.relationshipTypes()`: each type a relationship has.
    Procedure(
        "db.relationshipTypes",
        "relationshipType",
        _relationship_types,
        lambda: Function("type", False, (Variable(_ROW),)),
    ),
)


def procedure_named(name: str) -> Procedure | None:
    """The procedure of the name, as Neo4j names it, case and all; None for one the compiler does not take."""
    return next((it for it in PROCEDURES if it.qualified_name == name), None)
