from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from drivine.annotation.direction import Direction
from drivine.backend.cascade_depth_check import CascadeDepthCheck
from drivine.backend.fragment_group import FragmentGroup
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.matched_node import MatchedNode
from drivine.backend.matching_search import MatchingSearch
from drivine.backend.nearest_search import NearestSearch
from drivine.backend.page import Page
from drivine.backend.postgres.postgres_save_builder import PostgresSaveBuilder
from drivine.backend.postgres.postgres_statement import PostgresStatement
from drivine.backend.postgres.postgres_writes import PostgresWrites
from drivine.backend.save_statement import SaveStatement
from drivine.backend.sql.cypher_shorthand import CypherShorthand
from drivine.backend.sql.fragment_query_renderer import FragmentQueryRenderer, root_labels
from drivine.backend.sql.fragments import FRAGMENT_ROOT_ROW, Fragments
from drivine.backend.sql.node_ids import has_id
from drivine.backend.sql.sql_cascade import beyond_count, kept_targets
from drivine.backend.sql.sql_graph import sql_text
from drivine.backend.sql.sql_json import to_json
from drivine.backend.sql.value_layout import held_key, write
from drivine.backend.sqlite.sqlite_contention import contended
from drivine.backend.sqlite.sqlite_graph import (
    DELETE,
    JSON_ROWS,
    SqliteGraph,
    has_labels,
    node_document,
    text,
    text_of,
)
from drivine.backend.sqlite.sqlite_groups import SqliteGroups
from drivine.backend.sqlite.sqlite_search import SqliteSearch
from drivine.backend.sqlite.sqlite_views import SqliteViews
from drivine.backend.store_statements import StampAndMarker
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

ROW_ALIAS = FRAGMENT_ROOT_ROW
"""
What a load, a count, a delete, a patch's where, a grouped read and a search call their root's row: `n_`, the row the
query DSL's fragments read (see FragmentQueryRenderer). A raw `where` is SQL over `n`, which `written` reads as it.
"""

NODE_ROW = "n"
"""What the statements that read a node by its id alone call its row: `held_properties` and `found_stamp`."""


class SqliteBackend:
    """
    Statements for SQLite, over the tables of a SqliteGraph: drivine4j's store, so a graph file written from the JVM,
    from Node and from Python is one. A load returns the rows the graph engines do, `{props, labels}` for a fragment,
    as JSON text that a statement opening with JSON_ROWS has read back. A raw `where` is an SQL predicate over the
    node's row, `n` (its `labels`, `props` and `stamp`), in which a property of the node may be written as Cypher writes
    it, `n.name` (see `written`). A filter, a seek and an order of the query DSL are the shared module's SQL
    (FragmentQueryRenderer), over the row `n_`.

    A save, a patch and a relationship are the plan of steps Postgres carries out (see PostgresSaveBuilder and
    PostgresWrites), carried out by the function Drivine registers on each connection (see sqlite_functions). A delete
    that counts selects what it deletes and hands it to another. A search measures every node it may find: SQLite has
    no vector index, and a full-text index is not made yet.

    The store is made by the connection provider, on its first connection (see GraphTables).
    """

    def __init__(self, graph: SqliteGraph | None = None) -> None:
        self.graph = graph or SqliteGraph()

    def render_query(
        self,
        query: Query,
        target: TargetModel,
        labels_of_class: Callable[[type], list[str]],
        indexed: Sequence[SchemaItem] = (),
    ) -> RenderedQuery:
        """
        A query over a class's references, written for the store by the shared module's fragments (see
        FragmentQueryRenderer). A partial index over the nodes of a label is the planner's to choose, so `indexed`
        changes nothing here.
        """
        return FragmentQueryRenderer.render(query, target, self._fragments(target), labels_of_class)

    def written(self, predicate: str) -> str:
        """
        A predicate the caller wrote over `n`, as the store reads it over the row a read calls its root, `n_`: each
        property of the root written as Cypher writes it, `n.name`, read as that property, by the name the layout holds
        it by, so `` n.`$score` `` is the property held as `$$score` (see CypherShorthand).
        """
        return CypherShorthand(NODE_ROW, ROW_ALIAS, lambda row, key: text_of(row, held_key(key))).rewrite(predicate)

    def render_where(
        self, where: Any, target: TargetModel, labels_of_class: Callable[[type], list[str]]
    ) -> tuple[str, dict[str, Any]]:
        """A condition over a class's references, written for the store by the shared module's fragments."""
        return FragmentQueryRenderer.render_where(where, target, self._fragments(target), labels_of_class)

    def _fragments(self, target: TargetModel) -> Fragments:
        """The fragments of one statement over the target's root, its row `n_`."""
        return Fragments("SQLITE", self.graph.name, ROW_ALIAS, root_labels(target))

    def load_all_fragments(
        self, model: FragmentModel, where: str | None = None, page: Page | None = None, tested: Sequence[str] = ()
    ) -> GraphStatement:
        return PostgresStatement(
            "\n".join(
                [
                    JSON_ROWS,
                    f'SELECT {node_document(ROW_ALIAS)} AS "result"',
                    self._from(model, where),
                    *self._page(page),
                ]
            )
        )

    def load_fragment(self, model: FragmentModel) -> GraphStatement:
        return self._by_id(self.load_all_fragments(model, self._id_predicate(model)))

    def count_fragments(
        self, model: FragmentModel, where: str | None = None, tested: Sequence[str] = ()
    ) -> GraphStatement:
        return PostgresStatement("\n".join(['SELECT COUNT(*) AS "count"', self._from(model, where)]))

    def load_all_views(
        self,
        model: GraphViewModel,
        where: str | None = None,
        depth: Mapping[str, int] | None = None,
        page: Page | None = None,
        tested: Sequence[str] = (),
    ) -> GraphStatement:
        views = SqliteViews(self.graph, depth)
        return PostgresStatement(
            "\n".join(
                [
                    JSON_ROWS,
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
            "\n".join(['SELECT COUNT(*) AS "count"', self._from(model.root, self._view_where(model, where, depth))])
        )

    def delete(
        self, target: TargetModel, where: str | None = None, cascade: CascadeType = CascadeType.NONE
    ) -> GraphStatement:
        """
        Deletes the root with id `$id` that meets `where`, and for a cascade the nodes a load of the view reaches from
        it through the relationships a save writes (see SqlViews.cascade): every one, or with DELETE_ORPHAN those not
        kept (see kept_targets). Every node is selected before any is deleted, so what is kept is read first. Returns
        how many nodes were deleted, the roots and what the cascade took (#375): `{deleted}`.
        """
        root = target.root if isinstance(target, GraphViewModel) else target
        filter_ = self._deleted_where(target, where)
        if cascade is CascadeType.NONE:
            return self._by_id(self._deleting(root, filter_))
        if not isinstance(target, GraphViewModel):
            raise DrivineError(f"A cascade follows a view's relationships. {root.type.__name__} is a NodeFragment.")
        cascaded = SqliteViews(self.graph).cascade(target, "n.nid IN (SELECT nid FROM cascade_roots)")
        orphans = cascade is CascadeType.DELETE_ORPHAN and bool(cascaded.reached)
        targets: list[str] = []
        deleted = "SELECT nid FROM cascade_roots"
        if cascaded.reached:
            union = "\n    UNION ALL ".join(cascaded.reached)
            targets.append(
                f"cascade_targets AS (SELECT DISTINCT t.nid FROM ({union}) t "
                "WHERE t.nid NOT IN (SELECT nid FROM cascade_roots)),"
            )
            deleted = "SELECT nid FROM cascade_roots UNION SELECT nid FROM cascade_targets"
        if orphans:
            # Read both ways round where it follows a relationship of either direction (#390).
            kept = kept_targets(self.graph, cascaded.followed, "UNION", both_ways=True)
            targets += [
                "cascade_all AS (SELECT nid FROM cascade_roots UNION SELECT nid FROM cascade_targets),",
                f"cascade_kept (nid) AS (\n    {kept}),",
            ]
            deleted += " WHERE nid NOT IN (SELECT nid FROM cascade_kept)"
        # Every node deleted is counted, the roots and what the cascade took, as drivine4j counts them (#375).
        return self._by_id(
            PostgresStatement(
                "\n".join(
                    [
                        f"WITH {'RECURSIVE ' if orphans else ''}cascade_roots AS "
                        f"(SELECT {ROW_ALIAS}.nid {self._from(root, filter_)}),",
                        *targets,
                        f"gone AS ({deleted})",
                        f"SELECT {DELETE}({sql_text(self.graph.name)}, "
                        '(SELECT json_group_array(nid) FROM gone)) AS "deleted"',
                    ]
                )
            )
        )

    def cascade_depth_checks(self, target: TargetModel, where: str | None = None) -> list[CascadeDepthCheck]:
        if not isinstance(target, GraphViewModel):
            return []
        roots = f"SELECT {ROW_ALIAS}.nid {self._from(target.root, self._deleted_where(target, where))}"
        cascaded = SqliteViews(self.graph).cascade(target, "n.nid IN (SELECT nid FROM cascade_roots)")
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

    def nearest(self, target: TargetModel, search: NearestSearch) -> GraphStatement:
        return PostgresStatement.of(SqliteSearch(self.graph).nearest(target, search))

    def matching(self, target: TargetModel, search: MatchingSearch) -> GraphStatement:
        return PostgresStatement.of(SqliteSearch(self.graph).matching(target, search))

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
                "\n".join(
                    [
                        JSON_ROWS,
                        f'SELECT {NODE_ROW}.props AS "props"',
                        self._from(model, self._id_predicate(model, NODE_ROW), NODE_ROW),
                    ]
                )
            )
        )

    def found_stamp(self, model: FragmentModel) -> GraphStatement:
        return self._by_id(
            PostgresStatement(
                "\n".join(
                    [
                        f"SELECT COALESCE({NODE_ROW}.stamp, '{stamps.NEVER_STAMPED}') AS \"found\"",
                        self._from(model, self._id_predicate(model, NODE_ROW), NODE_ROW),
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
        statement = SqliteGroups(self.graph).build(model, query, path_targets, labels_of_class)
        return PostgresStatement(f"{JSON_ROWS}\n{statement.text}", statement.bindings)

    def related(self, origin: MatchedNode, type: str, direction: Direction, target: FragmentModel) -> GraphStatement:
        """The nodes of the target's labels one relationship of the type leads to from `origin`, each once."""
        if not type or not type.strip():
            raise DrivineError("A relationship needs a type.")
        if not origin.model.node_id_property:
            raise DrivineError(
                f"{origin.model.type.__name__} has no NodeId field, so a node of it can't be referred to by id."
            )
        if direction is Direction.INCOMING:
            join = "e.dst = a.nid AND x.nid = e.src"
        elif direction is Direction.UNDIRECTED:
            join = "((e.src = a.nid AND x.nid = e.dst) OR (e.dst = a.nid AND x.nid = e.src))"
        else:
            join = "e.src = a.nid AND x.nid = e.dst"
        labels = list(dict.fromkeys([*origin.model.labels, *origin.labels]))
        return PostgresStatement(
            f'{JSON_ROWS}\nSELECT {node_document("n")} AS "result" FROM {self.graph.nodes} n WHERE n.nid IN ('
            f"SELECT x.nid FROM {self.graph.nodes} a JOIN {self.graph.edges} e ON e.type = {text('$x_type')} "
            f"JOIN {self.graph.nodes} x ON {join} WHERE "
            f"{has_id('sqlite', 'a', origin.model.node_id_property, '$x_from')} AND "
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
        return StampAndMarker("sqlite", self.graph.name, PostgresStatement.of)

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
        """Another connection held the file's lock, or a table's, the failure or a cause under it says (see locked)."""
        return contended(failure)

    def _root_where(self, target: TargetModel, where: str | None) -> str | None:
        """A predicate over the root's row and, for a view, the view's requirements."""
        return self._view_where(target, where) if isinstance(target, GraphViewModel) else where

    def _view_where(
        self, model: GraphViewModel, where: str | None, depth: Mapping[str, int] | None = None
    ) -> str | None:
        parts = [*([f"({where})"] if where else []), *SqliteViews(self.graph, depth).requirements(model, ROW_ALIAS)]
        return " AND ".join(parts) if parts else None

    def _from(self, model: FragmentModel, where: str | None = None, row: str = ROW_ALIAS) -> str:
        """The nodes of the fragment's labels that meet `where`, as the row `row`."""
        tests = [has_labels(row, model.labels), *([f"({where})"] if where else [])]
        return f"FROM {self.graph.nodes} {row}\nWHERE {' AND '.join(tests)}"

    def _deleting(self, model: FragmentModel, where: str | None) -> GraphStatement:
        """
        Deletes the nodes `where` selects, with their relationships, and returns how many: `{deleted}`. SQLite has no
        delete that a query reads, so the nodes are selected first and deleted by the function Drivine registers.
        """
        return PostgresStatement(
            f"SELECT {DELETE}({sql_text(self.graph.name)}, (SELECT json_group_array({ROW_ALIAS}.nid) "
            f'{self._from(model, where)})) AS "deleted"'
        )

    @staticmethod
    def _by_id(statement: GraphStatement) -> GraphStatement:
        """`statement`, which finds a node by `$id`, binding the id as the JSON text the node holds it as (id_json)."""
        return PostgresStatement.of(statement).keying_id(id_json)

    @staticmethod
    def _id_predicate(model: FragmentModel, row: str = ROW_ALIAS) -> str:
        """
        The node whose id is `$id`, the JSON text the node holds it as, as `_by_id` binds it: the property's class and
        key compared with the id's, as the shared module writes the comparison, which the index over the id answers
        (see node_ids). It reads `$id` more than once, which is bound by name.
        """
        if not model.node_id_property:
            raise DrivineError(f"{model.type.__name__} has no NodeId field, so it can't be found by id.")
        return has_id("sqlite", row, model.node_id_property, "$id")

    @staticmethod
    def _page(page: Page | None) -> list[str]:
        if page is None:
            return []
        return [
            *([f"ORDER BY {page.order_by}"] if page.order_by else []),
            *(
                [
                    f"LIMIT {page.limit if page.limit is not None else -1}"
                    + (f" OFFSET {page.skip}" if page.skip is not None else "")
                ]
                if page.limit is not None or page.skip is not None
                else []
            ),
        ]


def id_json(id: Any) -> str:
    """An id as the JSON text a node on SQLite holds it as, as a save writes it (see value_layout.write)."""
    return to_json(write(id, "sqlite"))
