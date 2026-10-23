from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from drivine.annotation.direction import Direction
from drivine.backend.cascade_depth_check import CascadeDepthCheck
from drivine.backend.cascade_targets import CascadeTargets
from drivine.backend.cypher import label_expression, quote_name
from drivine.backend.cypher_grammar import CypherGrammar
from drivine.backend.cypher_stamps import STAMP
from drivine.backend.edge_statements import EdgeStatements
from drivine.backend.fragment_batch_statement import FragmentBatchStatement
from drivine.backend.fragment_group import FragmentGroup
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.group_statement import GroupStatement
from drivine.backend.matched_node import MatchedNode
from drivine.backend.matching_search import MatchingSearch
from drivine.backend.nearest_search import NearestSearch
from drivine.backend.page import Page
from drivine.backend.patch_statements import PatchStatements
from drivine.backend.save_statement import SaveStatement
from drivine.backend.save_statement_builder import SaveStatementBuilder
from drivine.backend.view_projection import ViewProjection
from drivine.backend.view_projector import ViewProjector, fragment
from drivine.drivine_error import DrivineError
from drivine.dsl.query import Query
from drivine.dsl.query_renderer import QueryRenderer
from drivine.dsl.rendered_query import RenderedQuery
from drivine.manager.cascade_type import CascadeType
from drivine.manager.group_query import GroupQuery
from drivine.manager.relate_mode import RelateMode
from drivine.manager.save_options import SaveOptions
from drivine.model import stamps
from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.model_registry import ModelRegistry
from drivine.model.target_model import TargetModel
from drivine.schema.schema_item import SchemaItem

STORE_LABEL = "DrivineStore"
"""The label of the node holding the store's identity: drivine4j's, so both read one identity."""

SCHEMA_MARKER = "_DrivineSchema"
"""The label of the nodes recording what each schema owner applied: drivine4j's, so both read one marker."""

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

    def render_query(
        self,
        query: Query,
        target: TargetModel,
        labels_of_class: Callable[[type], list[str]],
        indexed: Sequence[SchemaItem] = (),
    ) -> RenderedQuery:
        """
        A query over a class's references, written for the store: its predicate, order and paging, and values. A graph
        engine reads no filter through `indexed`: it chooses its indexes itself.
        """
        return QueryRenderer.render(query, target, self.grammar, NODE_ALIAS, labels_of_class)

    def render_where(
        self, where: Any, target: TargetModel, labels_of_class: Callable[[type], list[str]]
    ) -> tuple[str, dict[str, Any]]:
        """A condition over a class's references, written for the store, with its values."""
        return QueryRenderer.render_where(where, target, self.grammar, NODE_ALIAS, labels_of_class)

    def load_all_fragments(
        self, model: FragmentModel, where: str | None = None, page: Page | None = None, tested: Sequence[str] = ()
    ) -> GraphStatement:
        """Loads every node of the fragment, optionally filtered by a predicate over `n`. No label is `tested`."""
        return GraphStatement(
            "".join([self._match(model), self._where(where), self._project_fragment(), self._page(page)])
        )

    def load_fragment(self, model: FragmentModel) -> GraphStatement:
        """Loads the node of the fragment whose id is the parameter `id`."""
        return self.load_all_fragments(model, self._id_predicate(model))

    def count_fragments(
        self, model: FragmentModel, where: str | None = None, tested: Sequence[str] = ()
    ) -> GraphStatement:
        return GraphStatement(
            "".join([self._match(model), self._where(where), f"\nRETURN count({NODE_ALIAS}) AS count"])
        )

    def load_all_views(
        self,
        model: GraphViewModel,
        where: str | None = None,
        depth: Mapping[str, int] | None = None,
        page: Page | None = None,
        tested: Sequence[str] = (),
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
        self,
        model: GraphViewModel,
        where: str | None = None,
        depth: Mapping[str, int] | None = None,
        tested: Sequence[str] = (),
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

    def delete(
        self, target: TargetModel, where: str | None = None, cascade: CascadeType = CascadeType.NONE
    ) -> GraphStatement:
        """
        Deletes the root whose id is the parameter `id`, if it meets the optional predicate (and, for a view, the view's
        requirements), with its relationships. A cascade, for a view only, deletes nodes the view reaches too, through
        the relationships a save writes: every one, or with DELETE_ORPHAN those not kept. Returns the number of roots
        deleted.
        """
        root = target.root if isinstance(target, GraphViewModel) else target
        head = self._deleted(target, where)
        if cascade is CascadeType.NONE:
            return GraphStatement(f"{head}\nDETACH DELETE {NODE_ALIAS}\nRETURN count(*) AS deleted")
        if not isinstance(target, GraphViewModel):
            raise DrivineError(f"A cascade follows a view's relationships. {root.type.__name__} is a NodeFragment.")
        if cascade is CascadeType.DELETE_ORPHAN and self.grammar.orphan_delete_limit:
            raise DrivineError(
                f"CascadeType.DELETE_ORPHAN is not available on {self.grammar.engine}: "
                f"{self.grammar.orphan_delete_limit}."
            )
        cascaded = CascadeTargets(self.grammar).cascade(target, NODE_ALIAS)
        # WITH before the list: FalkorDB errors on a WHERE and a projection that both hold comprehensions
        # (FalkorDB/FalkorDB#3387).
        targets = [f"\nWITH {NODE_ALIAS}", f"\nWITH {NODE_ALIAS}, {cascaded.targets} AS targets"]
        if cascade is CascadeType.DELETE_ALL:
            return GraphStatement(
                "".join(
                    [
                        head,
                        *targets,
                        f"\nDETACH DELETE {NODE_ALIAS}",
                        "\nWITH targets",
                        "\nFOREACH (t IN targets | DETACH DELETE t)",
                        "\nRETURN count(*) AS deleted",
                    ]
                )
            )
        # A node is kept when a node outside the cascade refers to it, or when it is one step of a relationship the
        # cascade follows below a kept one: a kept message keeps its attachments, and its session is still deleted.
        # What is kept is read before anything is deleted, so a node's relationships to others the cascade takes do
        # not keep it. Holders chain through the targets (a part of two sessions starts a second run of levels), so the
        # walk takes as many steps as there are targets; `found` is what is kept, and how much of it the step before
        # had: each step reads holders among the newly kept only, and once one keeps nothing new the rest pass it on.
        return GraphStatement(
            "".join(
                [
                    head,
                    *targets,
                    # A node reached by two paths, or the root reached again, is in the targets once, and the root not
                    # at all.
                    f"\nWITH {NODE_ALIAS}, reduce(seen = [], t IN targets | "
                    f"CASE WHEN t IN seen OR t = {NODE_ALIAS} THEN seen ELSE seen + t END) AS targets",
                    f"\nWITH {NODE_ALIAS}, targets, [t IN targets WHERE any(m IN [(t)--(m) | m] "
                    f"WHERE m <> {NODE_ALIAS} AND NOT m IN targets)] AS kept",
                    f"\nWITH {NODE_ALIAS}, targets, reduce(found = [kept, 0], step IN range(1, size(targets)) | "
                    "CASE WHEN found[1] = size(found[0]) THEN found ELSE [found[0] + [t IN targets WHERE NOT t IN "
                    f"found[0] AND any(h IN {cascaded.holders('t')} WHERE h IN found[0][found[1]..])], size(found[0])] "
                    "END)[0] AS kept",
                    f"\nWITH {NODE_ALIAS}, [t IN targets WHERE NOT t IN kept] AS orphans",
                    f"\nDETACH DELETE {NODE_ALIAS}",
                    "\nWITH orphans",
                    "\nFOREACH (t IN orphans | DETACH DELETE t)",
                    "\nRETURN count(*) AS deleted",
                ]
            )
        )

    def cascade_depth_checks(self, target: TargetModel, where: str | None = None) -> list[CascadeDepthCheck]:
        """
        The reads a cascading `delete` of the same root runs first, one for each relationship to views of its own class
        that the cascade follows: see CascadeDepthCheck. Empty when it follows none.
        """
        if not isinstance(target, GraphViewModel):
            return []
        cascaded = CascadeTargets(self.grammar).cascade(target, NODE_ALIAS)
        return [
            CascadeDepthCheck(
                beyond.view,
                beyond.field,
                beyond.max_depth,
                GraphStatement(
                    "".join(
                        [
                            self._deleted(target, where),
                            f"\nWITH {NODE_ALIAS}",
                            f"\nWITH {NODE_ALIAS}, {cascaded.targets} AS targets, {beyond.nodes} AS beyond",
                            "\nUNWIND beyond AS b",
                            f"\nWITH {NODE_ALIAS}, targets, b WHERE NOT b IN targets AND b <> {NODE_ALIAS}",
                            "\nRETURN count(DISTINCT b) AS beyond",
                        ]
                    )
                ),
            )
            for beyond in cascaded.beyond
        ]

    def _deleted(self, target: TargetModel, where: str | None) -> str:
        """The match of the root a delete by `$id` takes, if it meets `where`, and for a view its requirements."""
        root = target.root if isinstance(target, GraphViewModel) else target
        id_ = self._id_predicate(root)
        return "".join([self._match(root), self._root_where(target, f"{id_} AND ({where})" if where else id_)])

    def delete_all(self, target: TargetModel, where: str | None = None, tested: Sequence[str] = ()) -> GraphStatement:
        """Deletes every root a load would find, with its relationships. Returns the number deleted."""
        root = target.root if isinstance(target, GraphViewModel) else target
        return GraphStatement(
            "".join(
                [
                    self._match(root),
                    self._root_where(target, where),
                    f"\nDETACH DELETE {NODE_ALIAS}\nRETURN count(*) AS deleted",
                ]
            )
        )

    def nearest(self, target: TargetModel, search: NearestSearch) -> GraphStatement:
        """The roots nearest `$_vector` by the label's vector index, each as `{value, score}`, nearest first."""
        call, score = self.grammar.vector_search(search.label, search.property, search.similarity)
        head = [call.replace("$_k", str(search.k)), f"WITH {NODE_ALIAS}, {score} AS _score"]
        return self._scored(target, head, search.where, search.threshold, search.limit, search.depth)

    def matching(self, target: TargetModel, search: MatchingSearch) -> GraphStatement:
        """The roots matching `$_text` by the full-text index, each as `{value, score}`, most relevant first."""
        # Scaled by the best hit, so the scale is [0, 1] on every engine; a best score of 0 (FalkorDB) gives each 1.
        head = [
            self.grammar.full_text_search(search.label, search.properties),
            "WITH collect({node: node, score: score}) AS _hits, max(score) AS _best",
            "UNWIND _hits AS _hit",
            f"WITH _hit.node AS {NODE_ALIAS}, CASE WHEN _best > 0 THEN _hit.score / _best ELSE 1.0 END AS _score",
        ]
        return self._scored(target, head, search.where, search.threshold, search.limit, search.depth)

    def save(
        self,
        target: TargetModel,
        obj: Any,
        options: SaveOptions | None = None,
        create_only: bool = False,
        registry: ModelRegistry | None = None,
    ) -> SaveStatement:
        """Writes the object as one statement: see SaveStatementBuilder."""
        return SaveStatementBuilder(self.grammar, registry).build(target, obj, options, create_only)

    def fragment_batch(self, groups: list[FragmentGroup], options: SaveOptions) -> GraphStatement:
        """Saves fragments of several classes in one statement; returns the stamps before and after for each class."""
        return FragmentBatchStatement.build(groups, options, self.grammar)

    def patch_one(self, model: FragmentModel, where: str | None = None) -> GraphStatement:
        """
        Writes `$set` (and `$vectors`) to the node with id `$id` if it meets `where`. Returns `{found, applied,
        changed}`.
        """
        return PatchStatements.one(model, self.grammar, where)

    def patch_many(self, model: FragmentModel) -> GraphStatement:
        """Writes each row of `$rows`, `{id, props, vectors}`, to its node. Returns `{id, found, changed}` per row."""
        return PatchStatements.many(model, self.grammar)

    def related(self, origin: MatchedNode, type: str, direction: Direction, target: FragmentModel) -> GraphStatement:
        return EdgeStatements.related(origin, type, direction, target)

    def relate(
        self, origin: MatchedNode, to: MatchedNode, type: str, properties: Mapping[str, Any], mode: RelateMode
    ) -> GraphStatement:
        return EdgeStatements.relate(origin, to, type, properties, mode)

    def unrelate(self, origin: MatchedNode, to: MatchedNode, type: str) -> GraphStatement:
        return EdgeStatements.unrelate(origin, to, type)

    def unrelate_all(self, origin: MatchedNode, type: str, direction: Direction) -> GraphStatement:
        return EdgeStatements.unrelate_all(origin, type, direction)

    def groups(
        self,
        model: FragmentModel,
        query: GroupQuery,
        path_targets: Mapping[str, FragmentModel],
        labels_of_class: Callable[[type], list[str]],
    ) -> GraphStatement:
        """A grouped read. The query may hold references and conditions, which the backend writes."""
        renderer = QueryRenderer(self.grammar, labels_of_class)
        return GroupStatement.build(model, GroupStatement.resolved(model, query, renderer), path_targets)

    def store_stamps(self) -> GraphStatement:
        """The store's identity stamps, `{id, assignedAt}`, oldest first."""
        return GraphStatement(
            f"MATCH (s:{STORE_LABEL}) RETURN {{id: s.storeId, assignedAt: s.assignedAt}} AS stamp "
            f"ORDER BY stamp.assignedAt, stamp.id"
        )

    def assign_store_stamp(self) -> GraphStatement:
        """Mints the identity stamp keyed `$key` unless it is there: `$storeId`, `$assignedAt`, `$engine`."""
        return GraphStatement(
            f"MERGE (s:{STORE_LABEL} {{key: $key}}) ON CREATE SET s.storeId = $storeId, s.assignedAt = $assignedAt, "
            f"s.engine = $engine"
        )

    def schema_marker(self, property: str) -> GraphStatement:
        """
        A property ('version', or 'items', the newline-joined inventory) of the newest schema marker of the owner
        `$scope`, as text: one row, or none when there is no marker.
        """
        return GraphStatement(
            f"MATCH (m:{SCHEMA_MARKER} {{scope: $scope}}) RETURN m.{property} ORDER BY m.appliedAt DESC LIMIT 1"
        )

    @staticmethod
    def marker_items(stored: str | None) -> list[str] | None:
        """The inventory keys a marker's stored `items` holds; None for none recorded."""
        return None if stored is None else [] if stored == "" else stored.split("\n")

    def record_schema_marker(self) -> GraphStatement:
        """Writes the owner `$scope`'s marker, making it if it isn't there: `$version`, `$items`, and when."""
        return GraphStatement(
            f"MERGE (m:{SCHEMA_MARKER} {{scope: $scope}}) SET m.version = $version, m.items = $items, "
            f"m.appliedAt = timestamp()"
        )

    def prune_schema_markers(self) -> GraphStatement:
        """
        Removes every marker of the owner `$scope` but the newest: what a race before the marker's constraint left.
        """
        return GraphStatement(
            f"MATCH (m:{SCHEMA_MARKER} {{scope: $scope}}) WITH m ORDER BY m.appliedAt DESC SKIP 1 DELETE m"
        )

    def held_properties(self, model: FragmentModel) -> GraphStatement:
        """The properties of the node with id `$id`: one row, or none when there is no such node."""
        return GraphStatement(
            f"{self._match(model)}\nWHERE {self._id_predicate(model)}\nRETURN properties({NODE_ALIAS}) AS props"
        )

    def found_stamp(self, model: FragmentModel) -> GraphStatement:
        """The stamp the node with id `$id` carries ('-' for none); no row when there is no such node."""
        return GraphStatement(
            f"{self._match(model)}\nWHERE {self._id_predicate(model)}\n"
            f"RETURN coalesce({NODE_ALIAS}.{STAMP}, '{stamps.NEVER_STAMPED}') AS found"
        )

    def contended(self, failure: BaseException) -> bool:
        """Whether a cause of the failure is the engine turning a statement away for contention."""
        cause: BaseException | None = failure
        for _ in range(10):
            if cause is None:
                return False
            if self.grammar.contended(cause):
                return True
            cause = cause.__cause__
        return False

    def _scored(
        self,
        target: TargetModel,
        head: list[str],
        where: str | None,
        threshold: bool,
        limit: int | None,
        depth: Mapping[str, int],
    ) -> GraphStatement:
        """The tail of a scored search: filter after the search, project, order by score, trim."""
        root = target.root if isinstance(target, GraphViewModel) else target
        filters = [
            f"{NODE_ALIAS}{label_expression(root.labels)}",
            *(["_score >= $_threshold"] if threshold else []),
            *([f"({where})"] if where else []),
            *(
                ViewProjector(self.grammar, depth).requirements(target, NODE_ALIAS)
                if isinstance(target, GraphViewModel)
                else []
            ),
        ]
        projection = (
            ViewProjector(self.grammar, depth).project(target, NODE_ALIAS)
            if isinstance(target, GraphViewModel)
            else ViewProjection(fragment(NODE_ALIAS))
        )
        return GraphStatement(
            "\n".join(
                [
                    *head,
                    # A MATCH of the bound node holds the filter: Memgraph takes no existence check in a WITH's WHERE.
                    f"MATCH ({NODE_ALIAS}) WHERE {' AND '.join(filters)}",
                    *projection.calls,
                    f"RETURN {{value: {projection.expression}, score: _score}} AS result",
                    "ORDER BY result.score DESC",
                    *([f"LIMIT {limit}"] if limit is not None else []),
                ]
            )
        )

    def _root_where(self, target: TargetModel, where: str | None) -> str:
        return self._view_where(target, where) if isinstance(target, GraphViewModel) else self._where(where)

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
