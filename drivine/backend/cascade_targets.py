from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from drivine.annotation.direction import Direction
from drivine.backend.cascade_beyond import CascadeBeyond
from drivine.backend.cypher import label_expression, quote_name
from drivine.backend.cypher_grammar import CypherGrammar
from drivine.backend.view_projector import ViewProjector, label_test, target
from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.relationship_model import RelationshipModel


@dataclass(frozen=True)
class Cascade:
    """
    The nodes a cascade reaches from a root, as a list expression, and where it stops short. `holders` gives the nodes
    one step of a relationship the cascade follows above a node, each type and direction once, as a list expression: a
    node is held by those.
    """

    targets: str
    holders: Callable[[str], str]
    beyond: list[CascadeBeyond]


class CascadeTargets:
    """
    The nodes a view reaches from its root, as one list expression: a pattern comprehension for each position in the
    view, each a flat path from the root, joined with `+`. Paths are flat, with no comprehension nested in another,
    because FalkorDB evaluates nested ones wrongly when they are combined (FalkorDB/FalkorDB#2006, #3386).

    The view's max_depth and the requirements of its nested views apply, so the list holds what loading the view would
    load through the relationships a save writes. A read-only field is not followed: other code writes what it loads. A
    node reached by two paths is listed twice. A view that contains itself is read a level at a time, each level
    holding what the root does.
    """

    def __init__(self, grammar: CypherGrammar) -> None:
        self.grammar = grammar
        self._alias_count = 0
        self._followed: list[RelationshipModel] = []
        self._beyond: list[CascadeBeyond] = []
        self._beyond_views: list[type] = []
        """The class of the view each of `_beyond` is a relationship of."""
        self._views: dict[type, list[str]] = {}
        """The lists of the roots of each view the cascade reaches, by the view's class."""

    def of(self, model: GraphViewModel, root: str) -> str:
        return self.cascade(model, root).targets

    def cascade(self, model: GraphViewModel, root: str) -> Cascade:
        """
        What a cascade from the node `root` reaches, with each relationship it follows, and for a relationship to views
        of the same class the nodes one level below its max_depth.
        """
        lists = self._view(model, f"({root})", [], [])
        for beyond, view in zip(self._beyond, self._beyond_views, strict=True):
            beyond.within.extend(self._views.get(view, []))
        steps: list[RelationshipModel] = []
        for rel in self._followed:
            if not any(it.type == rel.type and it.direction is rel.direction for it in steps):
                steps.append(rel)

        def holders(node: str) -> str:
            return " + ".join(f"[(h){_hop(rel)}({node}) | h]" for rel in steps) if steps else "[]"

        return Cascade(" + ".join(lists) if lists else "[]", holders, self._beyond)

    def _view(
        self, model: GraphViewModel, chain: str, conditions: list[str], path: list[RelationshipModel]
    ) -> list[str]:
        lists: list[str] = []
        for rel in model.relationships:
            # A read-only field (marked so, or a list of fragments read over several hops) is not the view's to delete.
            # A view nested in itself is not one: each level is one relationship from the level above.
            if rel.read_only:
                continue
            into = target(rel)
            if sum(1 for it in path if it is rel) >= rel.max_depth:
                if isinstance(into, GraphViewModel) and into.type is model.type:
                    beyond, _, _ = self._step(rel, into, chain, conditions)
                    self._beyond.append(CascadeBeyond(model.type.__name__, rel.field, rel.max_depth, beyond))
                    self._beyond_views.append(model.type)
                continue
            self._followed.append(rel)
            here, extended, every = self._step(rel, into, chain, conditions)
            lists.append(here)
            if isinstance(into, GraphViewModel):
                self._views.setdefault(into.type, []).append(here)
                lists.extend(self._view(into, extended, every, [*path, rel]))
        return lists

    def _step(
        self, rel: RelationshipModel, into: GraphViewModel | FragmentModel, chain: str, conditions: list[str]
    ) -> tuple[str, str, list[str]]:
        """The nodes one step of the relationship reaches from the end of `chain`, and the chain that ends in them."""
        self._alias_count += 1
        alias = f"c{self._alias_count}"
        labels = into.root.labels if isinstance(into, GraphViewModel) else into.labels
        step = f"{chain}{_hop(rel)}({alias}"
        extended = f"{step}{label_expression(labels)})"
        own = ViewProjector(self.grammar).requirements(into, alias) if isinstance(into, GraphViewModel) else []
        every = [*conditions, *own]
        where = f" WHERE {' AND '.join(every)}" if every else ""
        # Where the grammar says so, the far node's labels are tested after the match, the next level's chain
        # keeping them.
        if self.grammar.far_labels_after_match:
            return f"[{alias} IN [{step}){where} | {alias}] WHERE {label_test(alias, labels)}]", extended, every
        return f"[{extended}{where} | {alias}]", extended, every


def _hop(rel: RelationshipModel) -> str:
    edge = f"[:{quote_name(rel.type)}]"
    if rel.direction is Direction.INCOMING:
        return f"<-{edge}-"
    if rel.direction is Direction.UNDIRECTED:
        return f"-{edge}-"
    return f"-{edge}->"
