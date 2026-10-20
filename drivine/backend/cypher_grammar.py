from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from drivine.connection.database_type import DatabaseType
from drivine.drivine_error import DrivineError


def _exists_subquery(pattern: str) -> str:
    """`EXISTS { }` stops at the first match."""
    return f"EXISTS {{ MATCH {pattern} }}"


def _transient_over_bolt(failure: BaseException) -> bool:
    """A transient error over Bolt: a deadlock on Neo4j, a conflicting transaction on Memgraph."""
    try:
        from neo4j.exceptions import Neo4jError
    except ImportError:  # the neo4j extra isn't installed, so no failure came from it
        return False
    return isinstance(failure, Neo4jError) and failure.is_retryable()


def _plain_vector(expression: str) -> str:
    return expression


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
    vector_value: Callable[[str], str]
    """How a vector value is written so the engine's vector index takes it: as it is, or in the engine's own type."""
    contended: Callable[[BaseException], bool]
    """
    Whether the engine turned a statement away because another writer held what it writes, so that running it again
    may succeed. `failure` is the driver's own error.
    """
    orphan_delete_limit: str | None = None
    """Why the engine can't delete with CascadeType.DELETE_ORPHAN; None when it can."""


NEO4J_GRAMMAR = CypherGrammar("Neo4j", "INLINE", _exists_subquery, _plain_vector, _transient_over_bolt)

MEMGRAPH_GRAMMAR = CypherGrammar(
    "Memgraph",
    "INLINE",
    _exists_subquery,
    _plain_vector,
    _transient_over_bolt,
    orphan_delete_limit="Memgraph takes an existence check only in a WHERE clause, not in the list of nodes to delete",
)

_ANOTHER_WRITE = re.compile(r"another write is in progress", re.IGNORECASE)

FALKORDB_GRAMMAR = CypherGrammar(
    "FalkorDB",
    "CALL_SUBQUERY",
    lambda pattern: f"size([{pattern} | 1]) > 0",
    # FalkorDB indexes only its own vector type: a list stored as a list is never found by a vector search.
    lambda expression: f"vecf32({expression})",
    # FalkorDB runs one query at a time, but turns a write away while another is in progress.
    lambda failure: bool(_ANOTHER_WRITE.search(str(failure))),
)
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
