from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from drivine.backend.graph_statement import GraphStatement
from drivine.backend.matching_search import MatchingSearch
from drivine.backend.nearest_search import NearestSearch
from drivine.backend.oracle.oracle_graph import OracleGraph, has_labels, json_key, node_document
from drivine.backend.oracle.oracle_views import OracleViews
from drivine.backend.oracle.text_query import TextQuery
from drivine.drivine_error import DrivineError
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.target_model import TargetModel


class OracleSearch:
    """
    Vector and full-text search on Oracle, each as one statement returning `{value, score}` for each hit, best first,
    as the graph engines do: the index's candidates first, then the filters (threshold, `where`, a view's
    requirements), then the trim.

    Each goes through the index made for it (a table beside the nodes, see the Oracle schema grammar) where there is
    one, and otherwise reads every node of the label. A vector search scores on Neo4j's scale ((1 + cos) / 2 for cosine,
    1 / (1 + d²) for euclidean): approximate through the index, exact without. A full-text query is the core of Lucene's
    syntax (see TextQuery): Oracle Text scores through the index; without one, how often each term occurs as whole
    words. Either is scaled by the best hit.
    """

    def __init__(self, graph: OracleGraph) -> None:
        self.graph = graph

    def nearest(self, target: TargetModel, search: NearestSearch) -> GraphStatement:
        metric = "COSINE" if search.similarity == "COSINE" else "EUCLIDEAN_SQUARED"

        def score(distance: str) -> str:
            return f"(2 - {distance}) / 2" if search.similarity == "COSINE" else f"1 / (1 + {distance})"

        if search.table:
            # Through the index: its nearest, approximately, then tested for the root's labels as any hit is.
            distance = f"VECTOR_DISTANCE(v.embedding, TO_VECTOR($_vector), {metric})"
            candidates = (
                f"SELECT k.nid, {score('k.distance')} AS score FROM (SELECT v.nid, {distance} AS distance FROM "
                f"{search.table} v ORDER BY distance FETCH APPROX FIRST {search.k} ROWS ONLY) k"
            )
            return self._scored(target, candidates, search.where, search.threshold, search.limit, search.depth)
        k = json_key(search.property)
        value = f"TO_VECTOR(JSON_SERIALIZE(JSON_QUERY(c.props, '${k}')))"
        distance = f"VECTOR_DISTANCE({value}, TO_VECTOR($_vector), {metric})"
        candidates = (
            f"SELECT c.nid, {score(distance)} AS score FROM {self.graph.nodes} c WHERE "
            f"{has_labels('c', [search.label])} AND JSON_EXISTS(c.props, '${k}') ORDER BY {distance} "
            f"FETCH FIRST {search.k} ROWS ONLY"
        )
        return self._scored(target, candidates, search.where, search.threshold, search.limit, search.depth)

    def matching(self, target: TargetModel, search: MatchingSearch) -> GraphStatement:
        if search.text is None:
            raise DrivineError("A full-text search on Oracle is written with its text.")
        query = TextQuery.parse(search.text)
        bindings: dict[str, Any] = {}

        def bind(value: str) -> str:
            name = f"_term{len(bindings)}"
            bindings[name] = value
            return f"${name}"

        if search.table:
            # Through Oracle Text: each property's score added, then scaled by the best. The index holds what has been
            # committed, so a search does not see what its own transaction wrote.
            text = bind(query.oracle_text)
            contains = [
                f"JSON_TEXTCONTAINS(t.doc, '${json_key(it)}', {text}, {i + 1})"
                for i, it in enumerate(search.properties)
            ]
            scores = " + ".join(f"SCORE({i + 1})" for i in range(len(search.properties)))
            candidates = (
                f"SELECT nid, hit_count / MAX(hit_count) OVER () AS score FROM (SELECT t.nid, {scores} AS hit_count "
                f"FROM {search.table} t WHERE {' OR '.join(contains)}) WHERE hit_count > 0"
            )
            return self._scored(
                target, candidates, search.where, search.threshold, search.limit, search.depth, bindings
            )
        # Every node of the label: how often the query's terms occur as whole words in its lower-cased text, each term
        # bound on its own (one bound list named twice gave wrong counts on 23.26, drivine4j found).
        text = " || ' ' || ".join(
            f"LOWER(NVL(JSON_VALUE(c.props, '${json_key(it)}' RETURNING CLOB), ''))" for it in search.properties
        )

        def count(pattern: str) -> str:
            return f"REGEXP_COUNT(x.text, {bind(pattern)})"

        occurrences = " + ".join(count(it) for it in query.scored)
        conditions = [*(f"{count(it)} > 0" for it in query.required), *(f"{count(it)} = 0" for it in query.excluded)]
        candidates = f"""SELECT nid, hit_count / MAX(hit_count) OVER () AS score FROM (
    SELECT x.nid, {occurrences} AS hit_count
    FROM (SELECT c.nid, {text} AS text FROM {self.graph.nodes} c WHERE {has_labels("c", [search.label])}) x
    {f"WHERE {' AND '.join(conditions)}" if conditions else ""}
) WHERE hit_count > 0"""
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
        views = OracleViews(self.graph, depth)
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
                    f"SELECT JSON_OBJECT('value' VALUE {value}, 'score' VALUE s.score RETURNING JSON) AS \"result\"",
                    f"FROM ({candidates}) s JOIN {self.graph.nodes} n ON n.nid = s.nid",
                    f"WHERE {' AND '.join(filters)}",
                    "ORDER BY s.score DESC",
                    *([f"FETCH FIRST {limit} ROWS ONLY"] if limit is not None else []),
                ]
            ),
            dict(bindings or {}),
        )
