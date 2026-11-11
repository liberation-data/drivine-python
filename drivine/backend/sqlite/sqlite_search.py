from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from drivine.backend.graph_statement import GraphStatement
from drivine.backend.matching_search import MatchingSearch, searched_labels
from drivine.backend.nearest_search import NearestSearch
from drivine.backend.sql.sql_graph import sql_text
from drivine.backend.sqlite.sqlite_graph import (
    JSON_ROWS,
    SIMILARITY,
    SqliteGraph,
    column,
    has_labels,
    json_type,
    json_value,
    node_document,
    path,
)
from drivine.backend.sqlite.sqlite_views import SqliteViews
from drivine.cypher.shared_compiler import full_text_search
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
    full-text search's candidates are the shared module's (`full_text_search`): the core of Lucene's syntax, each term
    counted as a whole word by `drivine_regexp_count` (see sqlite_patterns), as on Postgres and Oracle, its text lowered
    by `drivine_lower`, and scored by BM25 as Neo4j's index scores it, by `drivine_math`, then scaled by the best hit.
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
        ranked = full_text_search(
            "SQLITE", self.graph.name, searched_labels(target, search.label), search.properties, search.text
        )
        return self._scored(
            target, ranked.statement, search.where, search.threshold, search.limit, search.depth, ranked.parameters
        )

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
