from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from drivine.connection.database_type import DatabaseType
from drivine.drivine_error import DrivineError


def _exists_subquery(pattern: str) -> str:
    """`EXISTS { }` stops at the first match."""
    return f"EXISTS {{ MATCH {pattern} }}"


@dataclass(frozen=True)
class CypherGrammar:
    """
    Where an engine's Cypher differs from another's. Each difference is added as the statements that meet it are
    written, rather than ahead of them, as in TypeScript's Drivine.
    """

    engine: str
    view_projection: Literal["INLINE", "CALL_SUBQUERY"]
    """
    How a view's relationships are projected: INLINE, as pattern comprehensions within the RETURN, or CALL_SUBQUERY,
    each in a `CALL { }` of its own before the RETURN.
    """
    existence_check: Callable[[str], str]
    """A predicate that holds when the pattern, such as `(n)-[:R]->(:T)`, matches at least once."""


NEO4J_GRAMMAR = CypherGrammar("Neo4j", "INLINE", _exists_subquery)

MEMGRAPH_GRAMMAR = CypherGrammar("Memgraph", "INLINE", _exists_subquery)

FALKORDB_GRAMMAR = CypherGrammar("FalkorDB", "CALL_SUBQUERY", lambda pattern: f"size([{pattern} | 1]) > 0")
"""
FalkorDB takes neither `EXISTS { }` nor a pattern in `exists()`. Its views are projected with CALL subqueries: on
FalkorDB 6.0 a pattern comprehension that names its relationship, as a relationship fragment must, leaves every later
comprehension in the same projection empty, without an error (FalkorDB/FalkorDB#3386).
"""


def cypher_grammar_for(type: DatabaseType) -> CypherGrammar:
    grammars = {
        DatabaseType.NEO4J: NEO4J_GRAMMAR,
        DatabaseType.MEMGRAPH: MEMGRAPH_GRAMMAR,
        DatabaseType.FALKORDB: FALKORDB_GRAMMAR,
    }
    grammar = grammars.get(type)
    if grammar is None:
        raise DrivineError(f"{type} does not have a Cypher grammar for the object manager.")
    return grammar
