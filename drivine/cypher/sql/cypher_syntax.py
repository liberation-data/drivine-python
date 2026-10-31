"""
What the compiler and `check_variable_kinds` both read from a statement's syntax alone, as Drivine for TypeScript's
CypherSyntax: kept apart from the compiler, which imports `check_variable_kinds`, so neither imports the other.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from drivine.cypher.ast.clause import Call, Clause, With
from drivine.cypher.ast.expression import Variable
from drivine.cypher.ast.projection import ProjectionItem
from drivine.drivine_error import DrivineError


def imports(call: Call, clauses: Sequence[Clause], names: Iterable[str]) -> set[str]:
    """
    The variables of the row, of `names`, that the query `clauses` of the call imports, as Neo4j has it: those its
    brackets name, `CALL (a, b) { … }`, or each with `CALL (*) { … }`; with no brackets, those its leading `WITH` names
    as they are, or each with `WITH *`; none where it has neither, `CALL () { … }` or `CALL { MATCH … }`. In a `UNION`,
    each query's own leading `WITH`. Refuses a bracketed name not of the row, and, as Neo4j does (its 42I28), a leading
    `WITH` that names a variable of the row, or `*`, with anything more: such a `WITH` imports, and one that names none
    is any `WITH`, `CALL { WITH 1 AS one WHERE … }`.
    """
    all_ = set(names)
    if call.imports_all:
        return all_
    if call.imports is not None:
        missing = next((it for it in call.imports if it not in all_), None)
        if missing is not None:
            raise DrivineError(
                f"{missing} is not defined: a variable is bound by a pattern, a WITH or an UNWIND before it is used."
            )
        return set(call.imports)
    first = clauses[0] if clauses else None
    if not isinstance(first, With):
        return set()
    leading = first.projection

    def named(item: ProjectionItem) -> str | None:
        return item.expression.name if isinstance(item.expression, Variable) else None

    if not leading.star and not any(named(it) in all_ for it in leading.items):
        return set()
    if not all(named(it) == it.name for it in leading.items) or not plain_with(first):
        raise DrivineError(
            "The importing WITH of a CALL { … } names variables of the row alone, as in Cypher: no alias, expression, "
            "DISTINCT, WHERE, ORDER BY, SKIP or LIMIT. Import them with CALL (a, b) { … }, and work on them in a WITH "
            "after."
        )
    return all_ if leading.star else {it.name for it in leading.items if it.name in all_}


def plain_with(clause: With) -> bool:
    """Whether the `WITH` has no `WHERE`, `DISTINCT`, `ORDER BY`, `SKIP` or `LIMIT`: one that only names what it passes
    on."""
    projection = clause.projection
    return (
        clause.where is None
        and not projection.distinct
        and not projection.order_by
        and projection.skip is None
        and projection.limit is None
    )


def bracketed(call: Call) -> bool:
    """Whether the call imports by its brackets, `CALL (a) { … }` or `CALL (*) { … }`: what it imports so it sees
    throughout."""
    return call.imports is not None or call.imports_all
