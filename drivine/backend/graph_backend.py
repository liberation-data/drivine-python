from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from drivine.annotation.direction import Direction
from drivine.backend.fragment_group import FragmentGroup
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.matched_node import MatchedNode
from drivine.backend.matching_search import MatchingSearch
from drivine.backend.nearest_search import NearestSearch
from drivine.backend.page import Page
from drivine.backend.save_statement import SaveStatement
from drivine.dsl.query import Query
from drivine.dsl.rendered_query import RenderedQuery
from drivine.manager.cascade_type import CascadeType
from drivine.manager.group_query import GroupQuery
from drivine.manager.relate_mode import RelateMode
from drivine.manager.save_options import SaveOptions
from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.model_registry import ModelRegistry
from drivine.model.target_model import TargetModel
from drivine.schema.schema_item import SchemaItem


class GraphBackend(Protocol):
    """
    What the object manager asks of a store: each statement it runs, ready to bind. Cypher engines have one backend,
    Oracle another, Postgres a third. A `where` given as a string is a predicate in the store's own language over the
    root, `n`; a query over references is written by the backend.

    Where the store is tables an object manager makes (Postgres), the backend has `ensuring` too: the statement that
    makes them when they are not there and does nothing when they are, which a manager runs once, before its first
    statement, outside any transaction. A backend without it has nothing to make, or its connection makes it (Oracle).
    """

    def render_query(
        self,
        query: Query,
        target: TargetModel,
        labels_of_class: Callable[[type], list[str]],
        indexed: Sequence[SchemaItem] = (),
    ) -> RenderedQuery:
        """
        A query over a class's references, written for the store: its predicate, order and paging, and its values.
        `indexed` is the store's range indexes and uniqueness constraints, for a store that reads a filter through them
        (see SchemaGrammar's labelled_indexes); it decides how the query reads, never what it finds.
        """
        ...

    def render_where(
        self, where: Any, target: TargetModel, labels_of_class: Callable[[type], list[str]]
    ) -> tuple[str, dict[str, Any]]: ...

    def load_all_fragments(
        self, model: FragmentModel, where: str | None = None, page: Page | None = None, tested: Sequence[str] = ()
    ) -> GraphStatement:
        """
        Loads every node of the fragment, optionally filtered by a predicate over `n`. `tested` names the labels `where`
        tests itself (a rendered query's labels_tested), which the statement needn't.
        """
        ...

    def load_fragment(self, model: FragmentModel) -> GraphStatement: ...

    def count_fragments(
        self, model: FragmentModel, where: str | None = None, tested: Sequence[str] = ()
    ) -> GraphStatement: ...

    def load_all_views(
        self,
        model: GraphViewModel,
        where: str | None = None,
        depth: Mapping[str, int] | None = None,
        page: Page | None = None,
        tested: Sequence[str] = (),
    ) -> GraphStatement: ...

    def load_view(self, model: GraphViewModel, depth: Mapping[str, int] | None = None) -> GraphStatement: ...

    def count_views(
        self,
        model: GraphViewModel,
        where: str | None = None,
        depth: Mapping[str, int] | None = None,
        tested: Sequence[str] = (),
    ) -> GraphStatement: ...

    def delete(
        self, target: TargetModel, where: str | None = None, cascade: CascadeType = CascadeType.NONE
    ) -> GraphStatement: ...

    def delete_all(
        self, target: TargetModel, where: str | None = None, tested: Sequence[str] = ()
    ) -> GraphStatement: ...

    def nearest(self, target: TargetModel, search: NearestSearch) -> GraphStatement: ...

    def matching(self, target: TargetModel, search: MatchingSearch) -> GraphStatement: ...

    def save(
        self,
        target: TargetModel,
        obj: Any,
        options: SaveOptions | None = None,
        create_only: bool = False,
        registry: ModelRegistry | None = None,
    ) -> SaveStatement: ...

    def fragment_batch(self, groups: list[FragmentGroup], options: SaveOptions) -> GraphStatement: ...

    def patch_one(self, model: FragmentModel, where: str | None = None) -> GraphStatement: ...

    def patch_many(self, model: FragmentModel) -> GraphStatement: ...

    def groups(
        self,
        model: FragmentModel,
        query: GroupQuery,
        path_targets: Mapping[str, FragmentModel],
        labels_of_class: Callable[[type], list[str]],
    ) -> GraphStatement: ...

    def related(
        self, origin: MatchedNode, type: str, direction: Direction, target: FragmentModel
    ) -> GraphStatement: ...

    def relate(
        self, origin: MatchedNode, to: MatchedNode, type: str, properties: Mapping[str, Any], mode: RelateMode
    ) -> GraphStatement: ...

    def unrelate(self, origin: MatchedNode, to: MatchedNode, type: str) -> GraphStatement: ...

    def unrelate_all(self, origin: MatchedNode, type: str, direction: Direction) -> GraphStatement: ...

    def store_stamps(self) -> GraphStatement: ...

    def assign_store_stamp(self) -> GraphStatement: ...

    def schema_marker(self, property: str) -> GraphStatement: ...

    def marker_items(self, stored: str | None) -> list[str] | None: ...

    def record_schema_marker(self) -> GraphStatement: ...

    def prune_schema_markers(self) -> GraphStatement: ...

    def held_properties(self, model: FragmentModel) -> GraphStatement: ...

    def found_stamp(self, model: FragmentModel) -> GraphStatement: ...

    def contended(self, failure: BaseException) -> bool: ...
