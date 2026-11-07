from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from drivine.backend.graph_statement import GraphStatement
from drivine.backend.matching_search import MatchingSearch
from drivine.backend.nearest_search import NearestSearch
from drivine.backend.sql.bm25 import Bm25Dialect, bm25_ranked
from drivine.backend.sql.sql_graph import sql_text
from drivine.backend.sql.text_query import TextQuery, looking_ahead
from drivine.backend.sqlite.sqlite_graph import (
    JSON_ROWS,
    LOWER,
    MATH,
    REGEXP_COUNT,
    SIMILARITY,
    SqliteGraph,
    column,
    has_labels,
    json_type,
    json_value,
    node_document,
    path,
    text_of,
)
from drivine.backend.sqlite.sqlite_views import SqliteViews
from drivine.drivine_error import DrivineError
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.target_model import TargetModel


class SqliteSearch:
    """
    Vector and full-text search on SQLite, each as one statement returning `{value, score}` for each hit, best first,
    as the graph engines do: the candidates first, then the filters (threshold, `where`, a view's requirements), then
    the trim. drivine4j's search: SQLite has no vector index, and a full-text index is recorded and not made, so a
    search measures every node it may find.

    A vector search scores each node of the label that holds an embedding of the vector's size, by the function Drivine
    registers (see sqlite_functions), on Neo4j's scale: (1 + cos) / 2 for cosine, 1 / (1 + d²) for euclidean. A
    full-text query is the core of Lucene's syntax (see TextQuery), each term counted as a whole word as on Postgres and
    Oracle, scored by BM25 as Neo4j's index scores it (see bm25_ranked), then scaled by the best hit.
    """

    def __init__(self, graph: SqliteGraph) -> None:
        self.graph = graph

    def nearest(self, target: TargetModel, search: NearestSearch) -> GraphStatement:
        score = f"{SIMILARITY}({json_value('c', search.property)}, $_vector, {sql_text(search.similarity)})"
        candidates = (
            f"SELECT k.nid, k.score FROM (SELECT c.nid, {score} AS score FROM {self.graph.nodes} c WHERE "
            f"{has_labels('c', [search.label])} AND {holds('c', search.property, search.dimensions)}) k "
            f"WHERE k.score IS NOT NULL ORDER BY k.score DESC, k.nid LIMIT {search.k}"
        )
        return self._scored(target, candidates, search.where, search.threshold, search.limit, search.depth)

    def matching(self, target: TargetModel, search: MatchingSearch) -> GraphStatement:
        if search.text is None:
            raise DrivineError("A full-text search on SQLite is written with its text.")
        query = TextQuery.parse(search.text)
        bindings: dict[str, Any] = {}

        def bind(value: str) -> str:
            name = f"_term{len(bindings)}"
            bindings[name] = value
            return f"CAST(${name} AS TEXT)"

        # Narrowed by the root's every label before the scale, so the best of it is a hit the search can return.
        root = target.root if isinstance(target, GraphViewModel) else target
        labels = [search.label, *(it for it in root.labels if it != search.label)]
        length = f"length({joined_text('c', search.properties)})"
        labelled = f"{self.graph.nodes} c WHERE {has_labels('c', labels)}"
        candidates = bm25_ranked(
            f"SELECT c.nid, {searched_text('c', search.properties)} AS text, {length} AS len FROM {labelled}",
            f"SELECT {length} AS len FROM {labelled}",
            query,
            Bm25Dialect(
                count=lambda text, term: f"{REGEXP_COUNT}({text}, {bind(looking_ahead(term))})",
                holds=lambda text, term: f"{REGEXP_COUNT}({text}, {bind(term)}) > 0",
                ln=lambda value: f"{MATH}('log', {value})",
                real=lambda value: f"CAST({value} AS REAL)",
            ),
        )
        return self._scored(target, candidates, search.where, search.threshold, search.limit, search.depth, bindings)

    def _scored(
        self,
        target: TargetModel,
        candidates: str,
        where: str | None,
        threshold: bool,
        limit: int | None,
        depth: Mapping[str, int],
        bindings: Mapping[str, Any] | None = None,
    ) -> GraphStatement:
        root = target.root if isinstance(target, GraphViewModel) else target
        views = SqliteViews(self.graph, depth)
        filters = [
            has_labels("n", root.labels),
            *(["s.score >= $_threshold"] if threshold else []),
            *([f"({where})"] if where else []),
            *(views.requirements(target, "n") if isinstance(target, GraphViewModel) else []),
        ]
        value = views.document(target, "n") if isinstance(target, GraphViewModel) else node_document("n")
        return GraphStatement(
            "\n".join(
                [
                    JSON_ROWS,
                    f"SELECT json_object('value', json({value}), 'score', s.score) AS \"result\"",
                    f"FROM ({candidates}) s JOIN {self.graph.nodes} n ON n.nid = s.nid",
                    f"WHERE {' AND '.join(filters)}",
                    "ORDER BY s.score DESC, n.nid",
                    *([f"LIMIT {limit}"] if limit is not None else []),
                ]
            ),
            dict(bindings or {}),
        )


def holds(alias: str, property: str, dimensions: int | None) -> str:
    """
    Whether the row `alias` holds an embedding under the property, of `dimensions` when given: a search by a vector
    measures only those of its size, as an embedding of another size is no point in the same space.
    """
    array = f"{json_type(alias, property)} = 'array'"
    if dimensions is None:
        return array
    return f"{array} AND json_array_length({column(alias, 'props')}, {path(property)}) = {dimensions}"


def searched_text(alias: str, properties: Sequence[str]) -> str:
    """
    The text a full-text search reads of the row `alias`: its properties, each as text, a space between them, in lower
    case, by the function Drivine registers: SQLite's own `lower` lowers only ASCII.
    """
    return f"{LOWER}({joined_text(alias, properties)})"


def joined_text(alias: str, properties: Sequence[str]) -> str:
    """`searched_text` before it is lowered: what a full-text search measures a text's length by (drivine4j#604)."""
    return " || ' ' || ".join(f"COALESCE({text_of(alias, it)}, '')" for it in properties)
