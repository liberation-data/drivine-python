from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from drivine.backend.graph_statement import GraphStatement
from drivine.backend.matching_search import MatchingSearch
from drivine.backend.nearest_search import NearestSearch
from drivine.backend.sql.sql_graph import sql_text
from drivine.backend.sql.text_query import TextQuery
from drivine.backend.sqlite.sqlite_graph import (
    JSON_ROWS,
    LOWER,
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
    full-text query is the core of Lucene's syntax (see TextQuery), scored by how often its terms occur as whole words,
    as Postgres and Oracle score one, then scaled by the best hit.
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

        def count(pattern: str) -> str:
            return f"{REGEXP_COUNT}(x.text, {bind(pattern)})"

        occurrences = " + ".join(count(it) for it in query.scored)
        conditions = [*(f"{count(it)} > 0" for it in query.required), *(f"{count(it)} = 0" for it in query.excluded)]
        filtered = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        # Narrowed by the root's every label before the scale, so the best of it is a hit the search can return.
        root = target.root if isinstance(target, GraphViewModel) else target
        labels = [search.label, *(it for it in root.labels if it != search.label)]
        candidates = (
            "SELECT h.nid, CAST(h.hit_count AS REAL) / MAX(h.hit_count) OVER () AS score FROM ("
            f"SELECT x.nid, {occurrences} AS hit_count FROM (SELECT c.nid, {searched_text('c', search.properties)} "
            f"AS text FROM {self.graph.nodes} c WHERE {has_labels('c', labels)}) x{filtered}) h WHERE h.hit_count > 0"
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
    texts = " || ' ' || ".join(f"COALESCE({text_of(alias, it)}, '')" for it in properties)
    return f"{LOWER}({texts})"
