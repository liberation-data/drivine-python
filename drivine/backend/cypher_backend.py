from __future__ import annotations

from collections.abc import Mapping

from drivine.backend.cypher import label_expression, quote_name
from drivine.backend.cypher_grammar import CypherGrammar
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.page import Page
from drivine.backend.view_projector import ViewProjector, fragment
from drivine.drivine_error import DrivineError
from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view_model import GraphViewModel

NODE_ALIAS = "n"
"""What a statement over a fragment calls its node. Filters are written against it."""


class CypherBackend:
    """
    Writes the object manager's statements for a Cypher engine, as TypeScript's Drivine writes them, so that the two
    can be checked against each other. A load returns one row per object, each a single map: `{props, labels}` for a
    fragment, and `{root, rels}` for a view.
    """

    def __init__(self, grammar: CypherGrammar) -> None:
        self.grammar = grammar

    def load_all_fragments(
        self, model: FragmentModel, where: str | None = None, page: Page | None = None
    ) -> GraphStatement:
        """Loads every node of the fragment, optionally filtered by a predicate over `n`."""
        return GraphStatement(
            "".join([self._match(model), self._where(where), self._project_fragment(), self._page(page)])
        )

    def load_fragment(self, model: FragmentModel) -> GraphStatement:
        """Loads the node of the fragment whose id is the parameter `id`."""
        return self.load_all_fragments(model, self._id_predicate(model))

    def count_fragments(self, model: FragmentModel, where: str | None = None) -> GraphStatement:
        return GraphStatement(
            "".join([self._match(model), self._where(where), f"\nRETURN count({NODE_ALIAS}) AS count"])
        )

    def load_all_views(
        self,
        model: GraphViewModel,
        where: str | None = None,
        depth: Mapping[str, int] | None = None,
        page: Page | None = None,
    ) -> GraphStatement:
        """Loads every view whose root meets the view's requirements and, optionally, a predicate over `n`."""
        projection = ViewProjector(self.grammar, depth).project(model, NODE_ALIAS)
        return GraphStatement(
            "".join(
                [
                    self._match(model.root),
                    self._view_where(model, where, depth),
                    *(f"\n{it}" for it in projection.calls),
                    f"\nRETURN {projection.expression} AS result",
                    self._page(page),
                ]
            )
        )

    def load_view(self, model: GraphViewModel, depth: Mapping[str, int] | None = None) -> GraphStatement:
        """Loads the view whose root's id is the parameter `id`."""
        return self.load_all_views(model, self._id_predicate(model.root), depth)

    def count_views(
        self, model: GraphViewModel, where: str | None = None, depth: Mapping[str, int] | None = None
    ) -> GraphStatement:
        return GraphStatement(
            "".join(
                [
                    self._match(model.root),
                    self._view_where(model, where, depth),
                    f"\nRETURN count({NODE_ALIAS}) AS count",
                ]
            )
        )

    def _view_where(self, model: GraphViewModel, where: str | None, depth: Mapping[str, int] | None = None) -> str:
        requirements = ViewProjector(self.grammar, depth).requirements(model, NODE_ALIAS)
        if not requirements:
            return self._where(where)
        # Parentheses only where needed: FalkorDB 6 plans a WHERE worse with ones it doesn't need.
        return self._where(" AND ".join([*([f"({where})"] if where else []), *requirements]))

    @staticmethod
    def _page(page: Page | None) -> str:
        if page is None:
            return ""
        return "".join(
            [
                f"\nORDER BY {page.order_by}" if page.order_by else "",
                f"\nSKIP {page.skip}" if page.skip is not None else "",
                f"\nLIMIT {page.limit}" if page.limit is not None else "",
            ]
        )

    @staticmethod
    def _match(model: FragmentModel) -> str:
        return f"MATCH ({NODE_ALIAS}{label_expression(model.labels)})"

    @staticmethod
    def _where(predicate: str | None) -> str:
        return f"\nWHERE {predicate}" if predicate else ""

    @staticmethod
    def _project_fragment() -> str:
        """
        The whole node, with its labels apart from its properties, so a property called `labels` can't be mistaken
        for them.
        """
        return f"\nRETURN {fragment(NODE_ALIAS)} AS result"

    @staticmethod
    def _id_predicate(model: FragmentModel) -> str:
        if not model.node_id_property:
            raise DrivineError(f"{model.type.__name__} has no NodeId field, so it can't be loaded by id.")
        return f"{NODE_ALIAS}.{quote_name(model.node_id_property)} = $id"
