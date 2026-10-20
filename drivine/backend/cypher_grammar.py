from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from drivine.connection.database_type import DatabaseType
from drivine.drivine_error import DrivineError


def _exists_subquery(pattern: str, where: str | None = None) -> str:
    """`EXISTS { }` stops at the first match."""
    return f"EXISTS {{ MATCH {pattern}{f' WHERE {where}' if where else ''} }}"


def _some_match_subquery(pattern: str, node: str, rel: str, where: Callable[[str], str]) -> str:
    return _exists_subquery(pattern, where(rel))


def _falkordb_some_match(pattern: str, node: str, rel: str, where: Callable[[str], str]) -> str:
    # Not as a comprehension's WHERE: two comprehensions of one pattern, each with a WHERE, in one predicate match
    # nothing (FalkorDB/FalkorDB#3392). Nor with the relationship itself in the list: its properties, as #3386 needs.
    if not rel:
        return f"any({node} IN [{pattern} | {node}] WHERE {where('')})"
    it = f"{node}_e"
    return (
        f"any({it} IN [{pattern} | {{node: {node}, rel: properties({rel})}}] "
        f"WHERE any({node} IN [{it}.node] WHERE {where(f'{it}.rel')}))"
    )


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
    some_match: Callable[[str, str, str, Callable[[str], str]], str]
    """
    A predicate that holds when at least one match of `pattern`, which binds its far node to `node` (and its
    relationship to `rel`, unless that is ''), meets the predicate `where` writes. `where` is given the expression the
    relationship's properties are read through.
    """
    keyset_null_guard: bool
    """
    Whether a keyset predicate constrains each key with `IS NOT NULL`: where indexes leave out null-valued nodes
    (Neo4j), that is what lets a composite index serve the page. On Memgraph it costs the range bound.
    """
    vector_value: Callable[[str], str]
    """How a vector value is written so the engine's vector index takes it: as it is, or in the engine's own type."""
    contended: Callable[[BaseException], bool]
    """
    Whether the engine turned a statement away because another writer held what it writes, so that running it again
    may succeed. `failure` is the driver's own error.
    """
    orphan_delete_limit: str | None = None
    """Why the engine can't delete with CascadeType.DELETE_ORPHAN; None when it can."""


NEO4J_GRAMMAR = CypherGrammar(
    "Neo4j", "INLINE", _exists_subquery, _some_match_subquery, True, _plain_vector, _transient_over_bolt
)

MEMGRAPH_GRAMMAR = CypherGrammar(
    "Memgraph",
    "INLINE",
    _exists_subquery,
    _some_match_subquery,
    False,
    _plain_vector,
    _transient_over_bolt,
    orphan_delete_limit="Memgraph takes an existence check only in a WHERE clause, not in the list of nodes to delete",
)

_ANOTHER_WRITE = re.compile(r"another write is in progress", re.IGNORECASE)

FALKORDB_GRAMMAR = CypherGrammar(
    "FalkorDB",
    "CALL_SUBQUERY",
    lambda pattern: f"size([{pattern} | 1]) > 0",
    _falkordb_some_match,
    False,
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
