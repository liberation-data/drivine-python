from __future__ import annotations

from drivine.annotation.direction import Direction
from drivine.backend.cypher import label_expression, quote_name
from drivine.backend.cypher_grammar import CypherGrammar
from drivine.backend.view_projector import ViewProjector, label_test, target
from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.relationship_model import RelationshipModel


class CascadeTargets:
    """
    The nodes a view reaches from its root, as one list expression: a pattern comprehension for each position in the
    view, each a flat path from the root, joined with `+`. Paths are flat, with no comprehension nested in another,
    because FalkorDB evaluates nested ones wrongly when they are combined (FalkorDB/FalkorDB#2006, #3386).

    The view's max_depth and the requirements of its nested views apply, so the list holds what loading the view would
    load. A node reached by two paths is listed twice.
    """

    def __init__(self, grammar: CypherGrammar) -> None:
        self.grammar = grammar
        self._alias_count = 0

    def of(self, model: GraphViewModel, root: str) -> str:
        lists = self._view(model, f"({root})", [], [])
        return " + ".join(lists) if lists else "[]"

    def _view(
        self, model: GraphViewModel, chain: str, conditions: list[str], path: list[RelationshipModel]
    ) -> list[str]:
        lists: list[str] = []
        for rel in model.relationships:
            if sum(1 for it in path if it is rel) >= rel.max_depth:
                continue
            self._alias_count += 1
            alias = f"c{self._alias_count}"
            into = target(rel)
            hops = f"*1..{rel.max_depth}" if isinstance(into, FragmentModel) and rel.max_depth > 1 else ""
            labels = into.root.labels if isinstance(into, GraphViewModel) else into.labels
            step = f"{chain}{_hop(rel, hops)}({alias}"
            extended = f"{step}{label_expression(labels)})"
            own = ViewProjector(self.grammar).requirements(into, alias) if isinstance(into, GraphViewModel) else []
            every = [*conditions, *own]
            where = f" WHERE {' AND '.join(every)}" if every else ""
            # Where the grammar says so, the far node's labels are tested after the match, the next level's chain
            # keeping them.
            if self.grammar.far_labels_after_match:
                lists.append(f"[{alias} IN [{step}){where} | {alias}] WHERE {label_test(alias, labels)}]")
            else:
                lists.append(f"[{extended}{where} | {alias}]")
            if isinstance(into, GraphViewModel):
                lists.extend(self._view(into, extended, every, [*path, rel]))
        return lists


def _hop(rel: RelationshipModel, hops: str) -> str:
    edge = f"[:{quote_name(rel.type)}{hops}]"
    if rel.direction is Direction.INCOMING:
        return f"<-{edge}-"
    if rel.direction is Direction.UNDIRECTED:
        return f"-{edge}-"
    return f"-{edge}->"
