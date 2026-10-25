from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from drivine.backend.graph_statement import GraphStatement
from drivine.backend.matching_search import MatchingSearch
from drivine.backend.nearest_search import NearestSearch
from drivine.backend.postgres.postgres_graph import PostgresGraph, has_labels, json_value, node_document, text_of
from drivine.backend.postgres.postgres_views import PostgresViews
from drivine.backend.sql.text_query import TextQuery
from drivine.drivine_error import DrivineError
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.target_model import TargetModel


class PostgresSearch:
    """
    Vector and full-text search on Postgres, each as one statement returning `{value, score}` for each hit, best first,
    as the graph engines do: the candidates first, then the filters (threshold, `where`, a view's requirements), then
    the trim. drivine4j's statements: what one library's index serves, the other's search reads through it.

    A vector search is pgvector's: exact over every node of the label that holds an embedding of the vector's size, or
    approximate through the label's HNSW index, which holds the embeddings of its size (see the Postgres schema
    grammar). It scores on Neo4j's scale: (1 + cos) / 2 for cosine, 1 / (1 + d²) for euclidean. A full-text query is
    the core of Lucene's syntax (see TextQuery), scored by how often its terms occur as whole words, as Oracle scores
    one without Oracle Text, then scaled by the best hit; the trigram index of a full-text index narrows it first to the
    nodes whose text holds a scoring term's words, which changes no score.
    """

    def __init__(self, graph: PostgresGraph) -> None:
        self.graph = graph

    def nearest(self, target: TargetModel, search: NearestSearch) -> GraphStatement:
        operator = "<=>" if search.similarity == "COSINE" else "<->"

        def score(distance: str) -> str:
            if search.similarity == "COSINE":
                return f"(2 - ({distance})) / 2"
            return f"1 / (1 + ({distance}) * ({distance}))"

        indexed = int(search.table) if search.table is not None else None
        if indexed is not None and indexed == search.dimensions:
            # Through the index: the embedding read as the index reads it, among the nodes it holds.
            distance = f"{embedding('s', search.property, indexed)} {operator} {_vector(indexed)}"
            candidates = (
                f"SELECT k.nid, {score('k.distance')} AS score FROM (SELECT s.nid, {distance} AS distance "
                f"FROM {self.graph.nodes} s WHERE {has_labels('s', [search.label])} AND "
                f"{holds('s', search.property, indexed)} ORDER BY distance LIMIT {search.k}) k"
            )
            return self._scored(target, candidates, search.where, search.threshold, search.limit, search.depth)
        distance = f"{embedding('c', search.property)} {operator} {_vector()}"
        candidates = (
            f"SELECT c.nid, {score(distance)} AS score FROM {self.graph.nodes} c WHERE "
            f"{has_labels('c', [search.label])} AND {holds('c', search.property, search.dimensions)} "
            f"ORDER BY score DESC, c.nid LIMIT {search.k}"
        )
        return self._scored(target, candidates, search.where, search.threshold, search.limit, search.depth)

    def matching(self, target: TargetModel, search: MatchingSearch) -> GraphStatement:
        if search.text is None:
            raise DrivineError("A full-text search on Postgres is written with its text.")
        query = TextQuery.parse(search.text)
        bindings: dict[str, Any] = {}

        def bind(value: str) -> str:
            name = f"_term{len(bindings)}"
            bindings[name] = value
            return f"CAST(${name} AS text)"

        text = searched_text("c", search.properties)
        # A node that matches a term holds each of its words: what the trigram index answers is whether it holds them.
        narrowed = (
            [
                "("
                + " OR ".join(
                    "(" + " AND ".join(f"{text} LIKE {bind(f'%{_like_text(it)}%')}" for it in words) + ")"
                    for words in query.scored_words
                )
                + ")"
            ]
            if search.table
            else []
        )

        def count(pattern: str) -> str:
            return f"(SELECT count(*) FROM regexp_matches(x.text, {bind(pattern)}, 'g'))"

        occurrences = " + ".join(count(it) for it in query.scored)
        conditions = [*(f"{count(it)} > 0" for it in query.required), *(f"{count(it)} = 0" for it in query.excluded)]
        filtered = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        # Narrowed by the root's every label before the scale, so the best of it is a hit the search can return.
        root = target.root if isinstance(target, GraphViewModel) else target
        labels = [search.label, *(it for it in root.labels if it != search.label)]
        candidates = (
            "SELECT h.nid, CAST(h.hit_count AS double precision) / MAX(h.hit_count) OVER () AS score FROM ("
            f"SELECT x.nid, {occurrences} AS hit_count FROM (SELECT c.nid, {text} AS text FROM {self.graph.nodes} c "
            f"WHERE {' AND '.join([has_labels('c', labels), *narrowed])}) x{filtered}) h WHERE h.hit_count > 0"
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
        views = PostgresViews(self.graph, depth)
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
                    f"SELECT jsonb_build_object('value', {value}, 'score', s.score) AS \"result\"",
                    f"FROM ({candidates}) s JOIN {self.graph.nodes} n ON n.nid = s.nid",
                    f"WHERE {' AND '.join(filters)}",
                    "ORDER BY s.score DESC, n.nid",
                    *([f"FETCH FIRST {limit} ROWS ONLY"] if limit is not None else []),
                ]
            ),
            dict(bindings or {}),
        )


def embedding(alias: str, property: str, dimensions: int | None = None) -> str:
    """
    The embedding stored under the property of the row `alias` as a pgvector `vector`: the JSON array's text, which is
    the text pgvector reads a vector from. Of `dimensions` when given, as an index over it is made, so a statement that
    reads it so is answered by the index.
    """
    return f"CAST(CAST({json_value(alias, property)} AS text) AS vector{f'({dimensions})' if dimensions else ''})"


def holds(alias: str, property: str, dimensions: int | None = None) -> str:
    """
    Whether the row `alias` holds an embedding under the property, of `dimensions` where given: a search measures only
    those, as pgvector refuses to measure vectors of two sizes, and a vector index holds only those.
    """
    value = json_value(alias, property)
    sized = f" AND jsonb_array_length({value}) = {dimensions}" if dimensions else ""
    return f"jsonb_typeof({value}) = 'array'{sized}"


def searched_text(alias: str, properties: Sequence[str]) -> str:
    """
    The text a full-text search reads of the row `alias`: its properties, each as text, a space between them, in lower
    case. The expression of a full-text index too (with an empty alias), so a statement that reads it so is answered by
    the index.
    """
    texts = " || ' ' || ".join(f"COALESCE({text_of(alias, it)}, '')" for it in properties)
    return f"lower({texts})"


def _vector(dimensions: int | None = None) -> str:
    """The vector bound as `$_vector`, a list of numbers, as pgvector's: of `dimensions` where given."""
    return f"CAST(CAST($_vector AS float8[]) AS vector{f'({dimensions})' if dimensions else ''})"


def _like_text(word: str) -> str:
    """A word as text a `LIKE` pattern matches as it is: its own `%`, `_` and backslash taken as themselves."""
    return word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
