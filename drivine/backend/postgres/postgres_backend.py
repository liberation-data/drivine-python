from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from drivine.annotation.direction import Direction
from drivine.backend.cascade_depth_check import CascadeDepthCheck
from drivine.backend.fragment_group import FragmentGroup
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.matched_node import MatchedNode
from drivine.backend.matching_search import FullTextIndexLookup, MatchingSearch
from drivine.backend.nearest_search import NearestSearch
from drivine.backend.page import Page
from drivine.backend.postgres.postgres_graph import PostgresGraph, has_labels, node_document, text_of
from drivine.backend.postgres.postgres_groups import PostgresGroups
from drivine.backend.postgres.postgres_query_renderer import PostgresQueryRenderer
from drivine.backend.postgres.postgres_save_builder import PostgresSaveBuilder
from drivine.backend.postgres.postgres_search import PostgresSearch
from drivine.backend.postgres.postgres_statement import PostgresStatement
from drivine.backend.postgres.postgres_views import PostgresViews
from drivine.backend.postgres.postgres_writes import PostgresWrites
from drivine.backend.save_statement import SaveStatement
from drivine.backend.sql.cypher_shorthand import CypherShorthand
from drivine.backend.sql.node_ids import has_id
from drivine.backend.sql.sql_cascade import beyond_count, kept_targets
from drivine.backend.sql.sql_catalog import CATALOG_KINDS, catalog_properties
from drivine.backend.sql.sql_graph import self_loop_once
from drivine.backend.sql.sql_json import to_json
from drivine.backend.sql.value_layout import write
from drivine.backend.store_statements import StampAndMarker
from drivine.cypher.shared_compiler import SearchIndex, full_text_index_lookup
from drivine.drivine_error import DrivineError
from drivine.dsl.query import Query
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

_CONTENDED = ("40001", "40P01")
"""A serialization failure and a deadlock: another writer's doing."""

ROW_ALIAS = "n"
"""What a statement over a fragment calls its node's row. A raw `where` is SQL over it."""


class PostgresBackend:
    """
    Statements for Postgres, over the tables of a PostgresGraph: drivine4j's store, so a graph written from the JVM,
    from Node and from Python is one. A load returns the rows the graph engines do, `{props, labels}` for a fragment,
    as `jsonb`. A raw `where` is an SQL predicate over the node's row, `n` (its `labels`, `props` and `stamp`).

    The store is made by the connection provider, on its first connection (see GraphTables).
    """

    def __init__(self, graph: PostgresGraph | None = None) -> None:
        self.graph = graph or PostgresGraph()

    def render_query(
        self,
        query: Query,
        target: TargetModel,
        labels_of_class: Callable[[type], list[str]],
        indexed: Sequence[SchemaItem] = (),
    ) -> RenderedQuery:
        """A query over a class's references, written for the store: its predicate, order and paging, and values."""
        return PostgresQueryRenderer.render(
            query, target, lambda depth: PostgresViews(self.graph, depth), ROW_ALIAS, labels_of_class
        )

    def written(self, predicate: str) -> str:
        """
        A predicate the caller wrote, as the store reads it: each property of the root written as Cypher writes it,
        `n.name`, read as that property (see CypherShorthand).
        """
        return CypherShorthand(ROW_ALIAS, ROW_ALIAS, text_of).rewrite(predicate)

    def render_where(
        self, where: Any, target: TargetModel, labels_of_class: Callable[[type], list[str]]
    ) -> tuple[str, dict[str, Any]]:
        """A condition over a class's references, written for the store, with its values."""
        return PostgresQueryRenderer.render_where(where, target, PostgresViews(self.graph), ROW_ALIAS, labels_of_class)

    def load_all_fragments(
        self, model: FragmentModel, where: str | None = None, page: Page | None = None, tested: Sequence[str] = ()
    ) -> GraphStatement:
        return PostgresStatement(
            "\n".join([f'SELECT {node_document(ROW_ALIAS)} AS "result"', self._from(model, where), *self._page(page)])
        )

    def load_fragment(self, model: FragmentModel) -> GraphStatement:
        return self._by_id(self.load_all_fragments(model, self._id_predicate(model)))

    def count_fragments(
        self, model: FragmentModel, where: str | None = None, tested: Sequence[str] = ()
    ) -> GraphStatement:
        return PostgresStatement("\n".join(['SELECT CAST(COUNT(*) AS integer) AS "count"', self._from(model, where)]))

    def load_all_views(
        self,
        model: GraphViewModel,
        where: str | None = None,
        depth: Mapping[str, int] | None = None,
        page: Page | None = None,
        tested: Sequence[str] = (),
    ) -> GraphStatement:
        views = PostgresViews(self.graph, depth)
        return PostgresStatement(
            "\n".join(
                [
                    f'SELECT {views.document(model, ROW_ALIAS)} AS "result"',
                    self._from(model.root, self._view_where(model, where, depth)),
                    *self._page(page),
                ]
            )
        )

    def load_view(self, model: GraphViewModel, depth: Mapping[str, int] | None = None) -> GraphStatement:
        return self._by_id(self.load_all_views(model, self._id_predicate(model.root), depth))

    def count_views(
        self,
        model: GraphViewModel,
        where: str | None = None,
        depth: Mapping[str, int] | None = None,
        tested: Sequence[str] = (),
    ) -> GraphStatement:
        return PostgresStatement(
            "\n".join(
                [
                    'SELECT CAST(COUNT(*) AS integer) AS "count"',
                    self._from(model.root, self._view_where(model, where, depth)),
                ]
            )
        )

    def delete(
        self, target: TargetModel, where: str | None = None, cascade: CascadeType = CascadeType.NONE
    ) -> GraphStatement:
        """
        Deletes the root with id `$id` that meets `where`, and for a cascade the nodes a load of the view reaches from
        it through the relationships a save writes (see SqlViews.cascade): every one, or with DELETE_ORPHAN those not
        kept (see kept_targets). Postgres runs a statement's deletes on one snapshot, so what is kept is read before
        anything is deleted. Returns how many nodes were deleted, the roots and what the cascade took (#375):
        `{deleted}`.
        """
        root = target.root if isinstance(target, GraphViewModel) else target
        filter_ = self._deleted_where(target, where)
        if cascade is CascadeType.NONE:
            return self._by_id(self._deleting(root, filter_))
        if not isinstance(target, GraphViewModel):
            raise DrivineError(f"A cascade follows a view's relationships. {root.type.__name__} is a NodeFragment.")
        cascaded = PostgresViews(self.graph).cascade(target, "n.nid IN (SELECT nid FROM cascade_roots)")
        orphans = cascade is CascadeType.DELETE_ORPHAN and bool(cascaded.reached)
        union = "\n    UNION ALL ".join(cascaded.reached)
        kept = (
            [
                "cascade_all AS (SELECT nid FROM cascade_roots UNION SELECT nid FROM cascade_targets),",
                f"cascade_kept (nid) AS (\n    {kept_targets(self.graph, cascaded.followed, 'UNION')}),",
            ]
            if orphans
            else []
        )
        not_kept = " AND x.nid NOT IN (SELECT nid FROM cascade_kept)" if orphans else ""
        targets = (
            [
                f"cascade_targets AS (SELECT DISTINCT t.nid FROM ({union}) t "
                "WHERE t.nid NOT IN (SELECT nid FROM cascade_roots)),",
                *kept,
                f"deleted_targets AS (DELETE FROM {self.graph.nodes} x WHERE x.nid IN (SELECT nid FROM cascade_targets)"
                f"{not_kept} RETURNING 1),",
            ]
            if cascaded.reached
            else []
        )
        return self._by_id(
            PostgresStatement(
                "\n".join(
                    [
                        f"WITH {'RECURSIVE ' if orphans else ''}cascade_roots AS "
                        f"(SELECT {ROW_ALIAS}.nid {self._from(root, filter_)}),",
                        *targets,
                        f"deleted AS (DELETE FROM {self.graph.nodes} WHERE nid IN (SELECT nid FROM cascade_roots) "
                        "RETURNING 1)",
                        # Every node deleted, the roots and what the cascade took, as drivine4j counts them (#375).
                        "SELECT CAST((SELECT COUNT(*) FROM deleted)"
                        f"{' + (SELECT COUNT(*) FROM deleted_targets)' if cascaded.reached else ''}"
                        ' AS integer) AS "deleted"',
                    ]
                )
            )
        )

    def cascade_depth_checks(self, target: TargetModel, where: str | None = None) -> list[CascadeDepthCheck]:
        if not isinstance(target, GraphViewModel):
            return []
        roots = f"SELECT {ROW_ALIAS}.nid {self._from(target.root, self._deleted_where(target, where))}"
        cascaded = PostgresViews(self.graph).cascade(target, "n.nid IN (SELECT nid FROM cascade_roots)")
        return [
            CascadeDepthCheck(
                it.view,
                it.field,
                it.max_depth,
                self._by_id(PostgresStatement(beyond_count(roots, it.within, it.nodes))),
            )
            for it in cascaded.beyond
        ]

    def _deleted_where(self, target: TargetModel, where: str | None) -> str | None:
        """What the root a delete by `$id` takes meets: the id, `where`, and for a view the view's requirements."""
        root = target.root if isinstance(target, GraphViewModel) else target
        id_ = self._id_predicate(root)
        return self._root_where(target, f"{id_} AND ({where})" if where else id_)

    def delete_all(self, target: TargetModel, where: str | None = None, tested: Sequence[str] = ()) -> GraphStatement:
        root = target.root if isinstance(target, GraphViewModel) else target
        return self._deleting(root, self._root_where(target, where))

    def vector_index_table(self, label: str, property: str, dimensions: int | None = None) -> GraphStatement:
        """
        Whether a vector index was made, which is over the node table: the size of the embeddings it holds. One row, or
        none when the index was not made, or was dropped by hand. `dimensions` is not needed here: the search compares
        the index's size with the vector's itself.
        """
        return PostgresStatement(
            f"SELECT details ->> 'dimensions' AS \"table\" FROM {self.graph.catalog} WHERE kind = CAST($kind AS text) "
            "AND label = CAST($label AS text) AND properties = CAST($properties AS text) "
            "AND object_name IS NOT NULL AND to_regclass(object_name) IS NOT NULL",
            {"kind": CATALOG_KINDS["VECTOR"], "label": label, "properties": catalog_properties([property])},
        )

    def full_text_index(self, label: str, properties: Sequence[str]) -> FullTextIndexLookup:
        """
        Whether the full-text index's trigram index was made, by the shared module's lookup of the catalog: a row where
        it is, none where it was not made, or was dropped by hand. The search narrows the nodes it counts by it.
        """
        lookup = full_text_index_lookup("POSTGRES", self.graph.name, label, properties)
        assert lookup is not None, "Postgres records its full-text indexes in its catalog."
        return FullTextIndexLookup(
            PostgresStatement(lookup.statement, lookup.parameters), lambda rows: SearchIndex(trigram=len(rows) > 0)
        )

    def nearest(self, target: TargetModel, search: NearestSearch) -> GraphStatement:
        return PostgresStatement.of(PostgresSearch(self.graph).nearest(target, search))

    def matching(self, target: TargetModel, search: MatchingSearch) -> GraphStatement:
        return PostgresStatement.of(PostgresSearch(self.graph).matching(target, search))

    def save(
        self,
        target: TargetModel,
        obj: Any,
        options: SaveOptions | None = None,
        create_only: bool = False,
        registry: ModelRegistry | None = None,
    ) -> SaveStatement:
        return PostgresSaveBuilder(self.graph, registry).build(target, obj, options, create_only)

    def held_properties(self, model: FragmentModel) -> GraphStatement:
        return self._by_id(
            PostgresStatement(
                "\n".join([f'SELECT {ROW_ALIAS}.props AS "props"', self._from(model, self._id_predicate(model))])
            )
        )

    def found_stamp(self, model: FragmentModel) -> GraphStatement:
        return self._by_id(
            PostgresStatement(
                "\n".join(
                    [
                        f"SELECT COALESCE({ROW_ALIAS}.stamp, '{stamps.NEVER_STAMPED}') AS \"found\"",
                        self._from(model, self._id_predicate(model)),
                    ]
                )
            )
        )

    def patch_one(self, model: FragmentModel, where: str | None = None) -> GraphStatement:
        return PostgresWrites(self.graph).patch_one(model, where)

    def patch_many(self, model: FragmentModel) -> GraphStatement:
        return PostgresWrites(self.graph).patch_many(model)

    def fragment_batch(self, groups: list[FragmentGroup], options: SaveOptions) -> GraphStatement:
        return PostgresWrites(self.graph).fragment_batch(groups, options)

    def groups(
        self,
        model: FragmentModel,
        query: GroupQuery,
        path_targets: Mapping[str, FragmentModel],
        labels_of_class: Callable[[type], list[str]],
    ) -> GraphStatement:
        return PostgresStatement.of(PostgresGroups(self.graph).build(model, query, path_targets, labels_of_class))

    def related(self, origin: MatchedNode, type: str, direction: Direction, target: FragmentModel) -> GraphStatement:
        """The nodes of the target's labels one relationship of the type leads to from `origin`, each once."""
        if not type or not type.strip():
            raise DrivineError("A relationship needs a type.")
        if not origin.model.node_id_property:
            raise DrivineError(
                f"{origin.model.type.__name__} has no NodeId field, so a node of it can't be referred to by id."
            )
        edges = self.graph.edges
        if direction is Direction.INCOMING:
            join = "e.dst = a.nid AND x.nid = e.src"
        elif direction is Direction.UNDIRECTED:
            # Either way, from the relationships read both ways round and joined on an equality: with an OR across the
            # two directions, Postgres read every relationship of the type, 136 ms at 40,000 against 0.2 (#390).
            edges = self.graph.both_ways()
            join = f"e.a = a.nid AND {self_loop_once('e')} AND x.nid = e.b"
        else:
            join = "e.src = a.nid AND x.nid = e.dst"
        labels = list(dict.fromkeys([*origin.model.labels, *origin.labels]))
        # The node it is from found by its id's JSON, as the index over the id is made (see node_ids).
        return PostgresStatement(
            f'SELECT {node_document("n")} AS "result" FROM {self.graph.nodes} n WHERE n.nid IN ('
            f"SELECT x.nid FROM {self.graph.nodes} a JOIN {edges} e ON e.type = CAST($x_type AS text) "
            f"JOIN {self.graph.nodes} x ON {join} WHERE "
            f"{has_id('postgres', 'a', origin.model.node_id_property, '$x_from')} AND "
            f"{has_labels('a', labels)} AND {has_labels('x', target.labels)})",
            {"x_type": type, "x_from": id_json(origin.id)},
        )

    def relate(
        self, origin: MatchedNode, to: MatchedNode, type: str, properties: Mapping[str, Any], mode: RelateMode
    ) -> GraphStatement:
        return PostgresWrites(self.graph).relate(origin, to, type, properties, mode)

    def unrelate(self, origin: MatchedNode, to: MatchedNode, type: str) -> GraphStatement:
        return PostgresWrites(self.graph).unrelate(origin, to, type)

    def unrelate_all(self, origin: MatchedNode, type: str, direction: Direction) -> GraphStatement:
        return PostgresWrites(self.graph).unrelate_all(origin, type, direction)

    @property
    def _stamp_and_marker(self) -> StampAndMarker:
        """The store's stamp and schema marker statements, the shared module's (see StampAndMarker)."""
        return StampAndMarker("postgres", self.graph.name, PostgresStatement.of)

    def store_stamps(self) -> GraphStatement:
        return self._stamp_and_marker.store_stamps()

    def assign_store_stamp(self) -> GraphStatement:
        return self._stamp_and_marker.assign_store_stamp()

    def schema_marker(self, property: str) -> GraphStatement:
        return self._stamp_and_marker.schema_marker(property)

    def marker_items(self, stored: str | None) -> list[str] | None:
        return self._stamp_and_marker.marker_items(stored)

    def record_schema_marker(self) -> GraphStatement:
        return self._stamp_and_marker.record_schema_marker()

    def prune_schema_markers(self) -> GraphStatement:
        return self._stamp_and_marker.prune_schema_markers()

    def contended(self, failure: BaseException) -> bool:
        """
        A serialization failure (40001) or a deadlock (40P01) is another writer's doing: running again may succeed.
        """
        cause: BaseException | None = failure
        for _ in range(10):
            if cause is None:
                return False
            if getattr(cause, "sqlstate", None) in _CONTENDED:
                return True
            cause = cause.__cause__
        return False

    def _root_where(self, target: TargetModel, where: str | None) -> str | None:
        """A predicate over the root's row and, for a view, the view's requirements."""
        return self._view_where(target, where) if isinstance(target, GraphViewModel) else where

    def _view_where(
        self, model: GraphViewModel, where: str | None, depth: Mapping[str, int] | None = None
    ) -> str | None:
        parts = [*([f"({where})"] if where else []), *PostgresViews(self.graph, depth).requirements(model, ROW_ALIAS)]
        return " AND ".join(parts) if parts else None

    def _from(self, model: FragmentModel, where: str | None = None) -> str:
        """The nodes of the fragment's labels that meet `where`, as the row `n`."""
        tests = [has_labels(ROW_ALIAS, model.labels), *([f"({where})"] if where else [])]
        return f"FROM {self.graph.nodes} {ROW_ALIAS}\nWHERE {' AND '.join(tests)}"

    def _deleting(self, model: FragmentModel, where: str | None) -> GraphStatement:
        """Deletes the nodes `where` selects, with their relationships, and returns how many: `{deleted}`."""
        return PostgresStatement(
            f"WITH deleted AS (DELETE {self._from(model, where)} RETURNING 1)\n"
            'SELECT CAST(COUNT(*) AS integer) AS "deleted" FROM deleted'
        )

    @staticmethod
    def _by_id(statement: GraphStatement) -> GraphStatement:
        """`statement`, which finds a node by `$id`, binding the id as the JSON text the node holds it as (id_json)."""
        return PostgresStatement.of(statement).keying_id(id_json)

    @staticmethod
    def _id_predicate(model: FragmentModel) -> str:
        """
        The node whose id is `$id`, the JSON the node holds it as, as `_by_id` binds it: the property's JSON compared
        with it, as the shared module writes the comparison, which the index over the id answers (see node_ids).
        """
        if not model.node_id_property:
            raise DrivineError(f"{model.type.__name__} has no NodeId field, so it can't be found by id.")
        return has_id("postgres", ROW_ALIAS, model.node_id_property, "$id")

    @staticmethod
    def _page(page: Page | None) -> list[str]:
        if page is None:
            return []
        return [
            *([f"ORDER BY {page.order_by}"] if page.order_by else []),
            *([f"OFFSET {page.skip} ROWS"] if page.skip is not None else []),
            *([f"FETCH NEXT {page.limit} ROWS ONLY"] if page.limit is not None else []),
        ]


def id_json(id: Any) -> str:
    """An id as the JSON text a node on Postgres holds it as, as a save writes it (see value_layout.write)."""
    return to_json(write(id, "postgres"))
