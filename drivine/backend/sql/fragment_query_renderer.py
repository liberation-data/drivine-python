from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from typing import Any

from drivine.backend.cypher_grammar import NEO4J_GRAMMAR
from drivine.backend.sql.fragments import Fragments
from drivine.dsl.condition import Condition, all_of_one
from drivine.dsl.model_scope import scope_of
from drivine.dsl.query import Query
from drivine.dsl.query_renderer import QueryRenderer
from drivine.dsl.rendered_query import RenderedQuery
from drivine.dsl.scope import Scope
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.target_model import TargetModel

ROOT = "n"
"""What the query DSL's Cypher calls the root: the variable its fragments name the root's row by."""


class FragmentQueryRenderer:
    """
    Writes a query of the DSL on a SQL store as the shared module compiles it (drivine-cypher-spec#235, its fragments;
    drivine4j's `SqlitePredicates`): the Cypher the DSL writes for Neo4j (QueryRenderer), its filter and its seek each
    compiled whole as one condition over the root's row, their parameters by the renderer's names (`$_q0`, bound as
    `_cy__q0`), and each of its orders as the terms the module sorts it by, named apart (`$f1__q0`), a null placed by a
    term of its own before it. So a value is compared and ordered as Cypher compares and orders it, whatever its kind, a
    date included, and a property whose name begins with `$` is read as the layout holds it. The root's row ends with
    `_`, as the module asks of a row of the library's own SQL (see fragments.FRAGMENT_ROOT_ROW).

    As a ConditionWriter, it writes a grouped read's filters, each compiled whole, numbered on from the one before, by
    one renderer for the statement.
    """

    def __init__(self, fragments: Fragments, labels_of_class: Callable[[type], list[str]]) -> None:
        """`fragments` are the statement's, over the root's row and labels: they name apart those the library writes."""
        self.fragments = fragments
        self._cypher = QueryRenderer(NEO4J_GRAMMAR, labels_of_class)

    @staticmethod
    def render(
        query: Query, model: TargetModel, fragments: Fragments, labels_of_class: Callable[[type], list[str]]
    ) -> RenderedQuery:
        """The query over the model's references, its parts compiled by `fragments`, which bind its values."""
        parts = QueryRenderer.parts(query, model, NEO4J_GRAMMAR, ROOT, labels_of_class)
        where = [fragments.where(it.cypher, it.bindings) for it in (parts.filter, parts.keyset) if it is not None]
        orders = [
            term
            for it in parts.orders
            for term in fragments.ordered(it.cypher, it.term.direction == "DESC", it.term.nulls, it.bindings)
        ]
        return dataclasses.replace(
            parts.rendered,
            where=" AND ".join(it for it in where if it is not None) or None,
            order_by=", ".join(orders) if orders else None,
            bindings=dict(fragments.parameters),
            labels_tested=(),
            hints=tuple(fragments.hints),
            non_finite=fragments.non_finite,
        )

    @staticmethod
    def render_where(
        where: Any, model: TargetModel, fragments: Fragments, labels_of_class: Callable[[type], list[str]]
    ) -> tuple[str, dict[str, Any]]:
        """The condition over the model's references, compiled whole by `fragments`, with what it binds."""
        renderer = FragmentQueryRenderer(fragments, labels_of_class)
        return renderer.where(all_of_one(where), scope_of(model), ROOT), dict(renderer.bindings)

    @property
    def bindings(self) -> Mapping[str, Any]:
        """What the conditions and the fragments written so far bind, by the names the module gives them."""
        return self.fragments.parameters

    def where(self, condition: Condition, scope: Scope, alias: str) -> str:
        """
        The condition, made in `scope`, over the root's row, which its Cypher calls `alias`: compiled whole, its
        parameters by the renderer's names, numbered on from the conditions written before.
        """
        written = self._cypher.bound(lambda: self._cypher.where(condition, scope, alias))
        compiled = self.fragments.where(written.cypher, written.bindings, alias)
        assert compiled is not None
        return compiled


def root_labels(model: TargetModel) -> list[str]:
    """The labels of the model's root, which a statement over it tests its row for."""
    return list(model.root.labels if isinstance(model, GraphViewModel) else model.labels)
