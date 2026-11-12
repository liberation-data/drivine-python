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
from drivine.backend.postgres.postgres_backend import SCHEMA_MARKER, STORE_LABEL
from drivine.backend.postgres.postgres_save_builder import PostgresSaveBuilder, stored_key
from drivine.backend.postgres.postgres_statement import PostgresStatement
from drivine.backend.postgres.postgres_writes import PostgresWrites
from drivine.backend.save_statement import SaveStatement
from drivine.backend.sql.cypher_shorthand import CypherShorthand
from drivine.backend.sql.sql_cascade import beyond_count, kept_targets
from drivine.backend.sql.sql_graph import sql_text
from drivine.backend.sqlite.sqlite_contention import contended
from drivine.backend.sqlite.sqlite_graph import (
    DELETE,
    JSON_ROWS,
    SqliteGraph,
    has_labels,
    label_array,
    node_document,
    number_of,
    text,
    text_of,
)
from drivine.backend.sqlite.sqlite_groups import SqliteGroups
from drivine.backend.sqlite.sqlite_query_renderer import SqliteQueryRenderer
from drivine.backend.sqlite.sqlite_search import SqliteSearch
from drivine.backend.sqlite.sqlite_views import SqliteViews
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

ROW_ALIAS = "n"
"""What a statement over a fragment calls its node's row. A raw `where` is SQL over it."""

_NOW_MILLIS = "CAST(unixepoch('subsec') * 1000 AS INTEGER)"
"""The moment, in milliseconds since the epoch, as the graph engines' timestamp() gives it."""


class SqliteBackend:
    """
    Statements for SQLite, over the tables of a SqliteGraph: drivine4j's store, so a graph file written from the JVM,
    from Node and from Python is one. A load returns the rows the graph engines do, `{props, labels}` for a fragment,
    as JSON text that a statement opening with JSON_ROWS has read back. A raw `where` is an SQL predicate over the
    node's row, `n` (its `labels`, `props` and `stamp`).

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
        A query over a class's references, written for the store. A partial index over the nodes of a label is the
        planner's to choose, so `indexed` changes nothing here.
        """
        return SqliteQueryRenderer.render(
            query, target, lambda depth: SqliteViews(self.graph, depth), ROW_ALIAS, labels_of_class
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
        return SqliteQueryRenderer.render_where(where, target, SqliteViews(self.graph), ROW_ALIAS, labels_of_class)

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
        return self.load_all_fragments(model, self._id_predicate(model))

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
        return self.load_all_views(model, self._id_predicate(model.root), depth)

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
            return self._deleting(root, filter_)
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
        return PostgresStatement(
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

    def cascade_depth_checks(self, target: TargetModel, where: str | None = None) -> list[CascadeDepthCheck]:
        if not isinstance(target, GraphViewModel):
            return []
        roots = f"SELECT {ROW_ALIAS}.nid {self._from(target.root, self._deleted_where(target, where))}"
        cascaded = SqliteViews(self.graph).cascade(target, "n.nid IN (SELECT nid FROM cascade_roots)")
        return [
            CascadeDepthCheck(
                it.view, it.field, it.max_depth, PostgresStatement(beyond_count(roots, it.within, it.nodes))
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
        return PostgresStatement(
            "\n".join([JSON_ROWS, f'SELECT {ROW_ALIAS}.props AS "props"', self._from(model, self._id_predicate(model))])
        )

    def found_stamp(self, model: FragmentModel) -> GraphStatement:
        return PostgresStatement(
            "\n".join(
                [
                    f"SELECT COALESCE({ROW_ALIAS}.stamp, '{stamps.NEVER_STAMPED}') AS \"found\"",
                    self._from(model, self._id_predicate(model)),
                ]
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
            f"JOIN {self.graph.nodes} x ON {join} WHERE {has_labels('a', labels)} AND "
            f"{text_of('a', origin.model.node_id_property)} = {text('$x_from')} AND "
            f"{has_labels('x', target.labels)})",
            {"x_type": type, "x_from": stored_key(origin.id)},
        )

    def relate(
        self, origin: MatchedNode, to: MatchedNode, type: str, properties: Mapping[str, Any], mode: RelateMode
    ) -> GraphStatement:
        return PostgresWrites(self.graph).relate(origin, to, type, properties, mode)

    def unrelate(self, origin: MatchedNode, to: MatchedNode, type: str) -> GraphStatement:
        return PostgresWrites(self.graph).unrelate(origin, to, type)

    def unrelate_all(self, origin: MatchedNode, type: str, direction: Direction) -> GraphStatement:
        return PostgresWrites(self.graph).unrelate_all(origin, type, direction)

    def store_stamps(self) -> GraphStatement:
        """The store's identity stamps, `{id, assignedAt}`, oldest first. Each is a map, read back as the JSON it is."""
        return PostgresStatement(
            f"{JSON_ROWS}\n"
            f"SELECT json_object('id', {text_of('n', 'storeId')}, 'assignedAt', {text_of('n', 'assignedAt')}) "
            f'AS "stamp" FROM {self.graph.nodes} n WHERE {has_labels("n", [STORE_LABEL])} '
            f"ORDER BY {text_of('n', 'assignedAt')}, {text_of('n', 'storeId')}"
        )

    def assign_store_stamp(self) -> GraphStatement:
        """Mints the stamp unless it is there; the uniqueness constraint on its key turns away a racing writer's."""
        return PostgresStatement(
            f"INSERT INTO {self.graph.nodes} (labels, props) SELECT {label_array(STORE_LABEL)}, "
            f"json_object('key', {text('$key')}, 'storeId', {text('$storeId')}, "
            f"'assignedAt', {text('$assignedAt')}, 'engine', {text('$engine')}) "
            f"WHERE NOT EXISTS (SELECT 1 FROM {self.graph.nodes} n WHERE {has_labels('n', [STORE_LABEL])} "
            f"AND {text_of('n', 'key')} = {text('$key')})"
        )

    def schema_marker(self, property: str) -> GraphStatement:
        return PostgresStatement(
            f'SELECT {text_of("n", property)} AS "value" FROM {self.graph.nodes} n WHERE {self._marker("n")} '
            f"ORDER BY {self._newest_first('n')} LIMIT 1"
        )

    @staticmethod
    def marker_items(stored: str | None) -> list[str] | None:
        """SQLite keeps an empty string apart from null, so an inventory is kept as it is, as drivine4j keeps it."""
        if stored is None:
            return None
        return [] if stored == "" else stored.split("\n")

    def record_schema_marker(self) -> GraphStatement:
        """
        The newest marker of the scope is written, and one is made where there is none: the row is inserted with the
        `nid` of the marker found, or none, and a marker that is there is written instead. A merge patch leaves out
        what is null, so a version that is null removes the one the marker had.
        """
        written = f"'version', {text('$version')}, 'items', {text('$items')}, 'appliedAt', {_NOW_MILLIS}"
        nodes = self.graph.nodes
        return PostgresStatement(
            f"INSERT INTO {nodes} (nid, labels, props) "
            f"SELECT (SELECT m.nid FROM {nodes} m WHERE {self._marker('m')} ORDER BY {self._newest_first('m')} "
            f"LIMIT 1), {label_array(SCHEMA_MARKER)}, "
            f"json_patch('{{}}', json_object('scope', {text('$scope')}, {written})) "
            f"WHERE 1 ON CONFLICT (nid) DO UPDATE SET props = json_patch(props, json_object({written}))"
        )

    def prune_schema_markers(self) -> GraphStatement:
        return PostgresStatement(
            f"DELETE FROM {self.graph.nodes} WHERE nid IN (SELECT nid FROM (SELECT n.nid, row_number() OVER (ORDER BY "
            f"{self._newest_first('n')}) AS position FROM {self.graph.nodes} n WHERE {self._marker('n')}) ranked "
            "WHERE position > 1)"
        )

    def contended(self, failure: BaseException) -> bool:
        """Another connection held the file's lock, or a table's, the failure or a cause under it says (see locked)."""
        return contended(failure)

    def _marker(self, alias: str) -> str:
        """The markers of the owner bound as `$scope`."""
        return f"{has_labels(alias, [SCHEMA_MARKER])} AND {text_of(alias, 'scope')} = {text('$scope')}"

    @staticmethod
    def _newest_first(alias: str) -> str:
        """The marker applied last first, then the one made later, so the marker read, written and kept are one."""
        return f"{number_of(alias, 'appliedAt')} DESC NULLS LAST, {alias}.nid DESC"

    def _root_where(self, target: TargetModel, where: str | None) -> str | None:
        """A predicate over the root's row and, for a view, the view's requirements."""
        return self._view_where(target, where) if isinstance(target, GraphViewModel) else where

    def _view_where(
        self, model: GraphViewModel, where: str | None, depth: Mapping[str, int] | None = None
    ) -> str | None:
        parts = [*([f"({where})"] if where else []), *SqliteViews(self.graph, depth).requirements(model, ROW_ALIAS)]
        return " AND ".join(parts) if parts else None

    def _from(self, model: FragmentModel, where: str | None = None) -> str:
        """The nodes of the fragment's labels that meet `where`, as the row `n`."""
        tests = [has_labels(ROW_ALIAS, model.labels), *([f"({where})"] if where else [])]
        return f"FROM {self.graph.nodes} {ROW_ALIAS}\nWHERE {' AND '.join(tests)}"

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
    def _id_predicate(model: FragmentModel) -> str:
        """
        The node whose id is `$id`, read as text, as a uniqueness constraint over the id holds it and as drivine4j finds
        a node: a number is its JSON text.
        """
        if not model.node_id_property:
            raise DrivineError(f"{model.type.__name__} has no NodeId field, so it can't be found by id.")
        return f"{text_of(ROW_ALIAS, model.node_id_property)} = {text('$id')}"

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
