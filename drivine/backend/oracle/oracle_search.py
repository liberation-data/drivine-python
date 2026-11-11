from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from drivine.backend.graph_statement import GraphStatement
from drivine.backend.matching_search import MatchingSearch, searched_labels
from drivine.backend.nearest_search import NearestSearch
from drivine.backend.oracle.oracle_graph import OracleGraph, has_labels, json_key, node_document
from drivine.backend.oracle.oracle_views import OracleViews
from drivine.cypher.shared_compiler import full_text_search
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.target_model import TargetModel


class OracleSearch:
    """
    Vector and full-text search on Oracle, each as one statement returning `{value, score}` for each hit, best first,
    as the graph engines do: the index's candidates first, then the filters (threshold, `where`, a view's
    requirements), then the trim.

    Each goes through the index made for it (a table beside the nodes, see the Oracle schema grammar) where there is
    one, and otherwise reads every node of the label. A vector search goes through an index made for embeddings of the
    vector's size only, and without one measures only the embeddings of that size: Oracle refuses to measure two sizes
    against each other. It scores on Neo4j's scale ((1 + cos) / 2 for cosine, 1 / (1 + d²) for euclidean): approximate
    through the index, exact without. A full-text search's candidates are the shared module's (`full_text_search`), its
    query the core of Lucene's syntax: Oracle Text scores through the index, each property's score added, and sees only
    what has been committed; without one, each term is counted as a whole word and the counts scored by BM25, as Neo4j's
    index scores them. Either is scaled by the best hit.
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
        # Every node of the label that holds an embedding of the vector's size (ORA-51808 for any other), so a node of
        # another size is left out, as the other stores leave it out.
        k = json_key(search.property)
        value = f"TO_VECTOR(JSON_SERIALIZE(JSON_QUERY(c.props, '${k}')))"
        distance = f"VECTOR_DISTANCE({value}, TO_VECTOR($_vector), {metric})"
        held = [has_labels("c", [search.label]), f"JSON_EXISTS(c.props, '${k}')"]
        if search.dimensions is not None:
            held.append(f"VECTOR_DIMENSION_COUNT({value}) = {search.dimensions}")
        candidates = (
            f"SELECT c.nid, {score(distance)} AS score FROM {self.graph.nodes} c WHERE {' AND '.join(held)} "
            f"ORDER BY {distance} FETCH FIRST {search.k} ROWS ONLY"
        )
        return self._scored(target, candidates, search.where, search.threshold, search.limit, search.depth)

    def matching(self, target: TargetModel, search: MatchingSearch) -> GraphStatement:
        ranked = full_text_search(
            "ORACLE",
            self.graph.name,
            searched_labels(target, search.label),
            search.properties,
            search.text,
            search.index,
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
