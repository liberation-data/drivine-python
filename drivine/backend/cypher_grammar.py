from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from drivine.backend.cypher import quote_string
from drivine.connection.database_type import DatabaseType
from drivine.drivine_error import DrivineError
from drivine.schema.schema_specs import full_text_index, item_name, vector_index
from drivine.schema.vector_index_spec import SimilarityFunction


def _full_text_index_name(label: str, properties: Sequence[str]) -> str:
    """The full-text index Drivine makes over a label's properties (see full_text_index), by the name it gives it."""
    return item_name(full_text_index(label, properties))


def _vector_index_name(label: str, property: str) -> str:
    """The vector index Drivine makes over a label's property (see vector_index), by the name it gives it."""
    return item_name(vector_index(label, property, 1))


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


_TRANSIENT_CODE = re.compile(r"^[^.]+\.TransientError\.")


def _transient_over_bolt(failure: BaseException) -> bool:
    """
    A transient error over Bolt: a deadlock on Neo4j (`Neo.TransientError.*`), a conflicting transaction on Memgraph
    (`Memgraph.TransientError.*`). The driver already gives a terminated transaction a client error's code.

    A lost connection (`ServiceUnavailable`, `SessionExpired`) or an expired authorization is not run again, though the
    driver calls it retryable: the save may have been applied before the connection went, and a second run would find
    its own stamp and report a save that was applied as refused.
    """
    try:
        from neo4j.exceptions import Neo4jError
    except ImportError:  # the neo4j extra isn't installed, so no failure came from it
        return False
    return isinstance(failure, Neo4jError) and _TRANSIENT_CODE.match(failure.code or "") is not None


def _plain_vector(expression: str) -> str:
    return expression


def _neo4j_full_text_search(label: str, properties: Sequence[str], limit: int) -> str:
    # Asked for its best `limit` hits, so Lucene collects no more than that.
    name = quote_string(_full_text_index_name(label, properties))
    return f"CALL db.index.fulltext.queryNodes({name}, $_text, {{limit: {limit}}}) YIELD node, score"


def _neo4j_vector_search(label: str, property: str, similarity: SimilarityFunction) -> tuple[str, str]:
    # Neo4j's score is already (1 + cos) / 2, or 1 / (1 + d²).
    name = quote_string(_vector_index_name(label, property))
    return f"CALL db.index.vector.queryNodes({name}, $_k, $_vector) YIELD node AS n, score AS _raw", "_raw"


MEMGRAPH_TEXT_SEARCH_LIMIT_CAP = 1_000_000
"""
The most hits a full-text call asks Memgraph's `text_search` for. Memgraph allocates the limit up front: 3.14 accepts
100,000,000 and aborts at 500,000,000, however few hits there are (memgraph/memgraph#5200). A million is far more than a
search can scale in memory, and far below where the server fails.
"""


def _memgraph_full_text_search(label: str, properties: Sequence[str], limit: int) -> str:
    # Given the search's index limit, because without one text_search yields its best 1,000 hits and no more.
    name = quote_string(_full_text_index_name(label, properties))
    capped = min(limit, MEMGRAPH_TEXT_SEARCH_LIMIT_CAP)
    return f"CALL text_search.search_all({name}, $_text, {{limit: {capped}}}) YIELD node, score"


def _memgraph_vector_search(label: str, property: str, similarity: SimilarityFunction) -> tuple[str, str]:
    # Memgraph's distance is 1 - cos, or d². Its similarity is not read: for a cos index it is the absolute value of
    # 1 - distance, so a cosine of -0.6 would score as 0.6 does, and opposite vectors as equal ones
    # (memgraph/memgraph#5199). Its vector search returns nodes deleted since its last garbage collection, and any read
    # of one fails ('Trying to access labels on a deleted node') until it collects: FREE MEMORY forces it
    # (memgraph/memgraph#4975).
    name = quote_string(_vector_index_name(label, property))
    return (
        f"CALL vector_search.search({name}, $_k, $_vector) YIELD node AS n, distance AS _raw",
        "1 - _raw / 2.0" if similarity == "COSINE" else "1.0 / (1 + _raw)",
    )


def _falkordb_full_text_search(label: str, properties: Sequence[str], limit: int) -> str:
    # One full-text index to a label, found by the label. The call takes no limit, and has no cap: the hits are cut
    # after it.
    return f"CALL db.idx.fulltext.queryNodes({quote_string(label)}, $_text) YIELD node, score"


def _falkordb_vector_search(label: str, property: str, similarity: SimilarityFunction) -> tuple[str, str]:
    # FalkorDB's score is a distance: 1 - cos, or d.
    return (
        f"CALL db.idx.vector.queryNodes({quote_string(label)}, {quote_string(property)}, $_k, vecf32($_vector)) "
        "YIELD node AS n, score AS _raw",
        "1 - _raw / 2.0" if similarity == "COSINE" else "1.0 / (1 + _raw * _raw)",
    )


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
    vector_search: Callable[[str, str, SimilarityFunction], tuple[str, str]]
    """
    The call that asks the vector index over a label's property for `$_k` nodes nearest `$_vector`, binding each as
    `n`, and the expression of its score as a similarity, higher nearer, on one scale on every engine: (1 + cos) / 2
    for cosine, 1 / (1 + d²) for euclidean.
    """
    full_text_search: Callable[[str, Sequence[str], int], str]
    """
    The call that asks the full-text index over a label's properties for nodes matching `$_text`, yielding
    `node, score`. Where the engine's call takes a limit, it is asked for no more than the limit given, so the index
    stops rather than yield hits the search would drop; the search cuts them to that many after the call on every
    engine (see CypherBackend.matching).
    """
    orphan_delete_limit: str | None = None
    """Why the engine can't delete with CascadeType.DELETE_ORPHAN; None when it can."""
    far_labels_after_match: bool = False
    """
    Whether a view's paths, and a cascade's patterns, match their far node without its labels, and test them after,
    outside the match: FalkorDB 6 plans a match of several hops from a labelled far end, reading every relationship of
    the last hop's type, whether the label is in the pattern or in its WHERE (FalkorDB/FalkorDB#3332).
    """
    reads_without_snapshot: bool = False
    """
    Whether a read takes no snapshot, so one query can find a node's properties as different commits left them. Neo4j
    reads under read committed without a lock: a load that runs while another writer commits can be handed that
    writer's stamp with the data from before its write, however the query orders its reads. The checked save of what
    `update` loaded, or of a save given `from_`, then compares the root's fields as loaded too, under the node's lock.
    Memgraph reads from a snapshot, and FalkorDB runs no read beside a write.
    """


NEO4J_GRAMMAR = CypherGrammar(
    "Neo4j",
    "INLINE",
    _exists_subquery,
    _some_match_subquery,
    True,
    _plain_vector,
    _transient_over_bolt,
    _neo4j_vector_search,
    _neo4j_full_text_search,
    reads_without_snapshot=True,
)

MEMGRAPH_GRAMMAR = CypherGrammar(
    "Memgraph",
    "INLINE",
    _exists_subquery,
    _some_match_subquery,
    False,
    _plain_vector,
    _transient_over_bolt,
    _memgraph_vector_search,
    _memgraph_full_text_search,
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
    _falkordb_vector_search,
    _falkordb_full_text_search,
    far_labels_after_match=True,
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
