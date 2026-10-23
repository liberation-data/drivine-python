from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from drivine.annotation.direction import Direction
from drivine.backend.cascade_depth_check import CascadeDepthCheck
from drivine.backend.fragment_group import FragmentGroup
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.matched_node import MatchedNode
from drivine.backend.matching_search import MatchingSearch
from drivine.backend.nearest_search import NearestSearch
from drivine.backend.page import Page
from drivine.backend.postgres.postgres_graph import PostgresGraph, has_labels, node_document, number_of, text_of
from drivine.backend.postgres.postgres_groups import PostgresGroups
from drivine.backend.postgres.postgres_query_renderer import PostgresQueryRenderer
from drivine.backend.postgres.postgres_save_builder import PostgresSaveBuilder, stored_key
from drivine.backend.postgres.postgres_search import PostgresSearch
from drivine.backend.postgres.postgres_statement import PostgresStatement
from drivine.backend.postgres.postgres_views import PostgresViews
from drivine.backend.postgres.postgres_writes import PostgresWrites
from drivine.backend.save_statement import SaveStatement
from drivine.backend.sql.sql_cascade import beyond_count, kept_targets
from drivine.backend.sql.sql_graph import sql_text
from drivine.backend.sql.sql_json import to_json
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

_NOW_MILLIS = "CAST(floor(extract(epoch FROM clock_timestamp()) * 1000) AS bigint)"
"""The moment, in milliseconds since the epoch, as the graph engines' timestamp() gives it."""

_CONTENDED = ("40001", "40P01")
"""A serialization failure and a deadlock: another writer's doing."""

ROW_ALIAS = "n"
"""What a statement over a fragment calls its node's row. A raw `where` is SQL over it."""


class PostgresBackend:
    """
    Statements for Postgres, over the tables of a PostgresGraph: drivine4j's store, so a graph written from the JVM,
    from Node and from Python is one. A load returns the rows the graph engines do, `{props, labels}` for a fragment,
    as `jsonb`. A raw `where` is an SQL predicate over the node's row, `n` (its `labels`, `props` and `stamp`).

    The store is made by the object manager, before its first statement: see `ensuring`.
    """

    def __init__(self, graph: PostgresGraph | None = None) -> None:
        self.graph = graph or PostgresGraph()

    @property
    def ensuring(self) -> str:
        """The statement that makes the store, which the object manager runs once, before its first statement."""
        return self.graph.ensuring

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
        return self.load_all_fragments(model, self._id_predicate(model))

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
        return self.load_all_views(model, self._id_predicate(model.root), depth)

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
        anything is deleted. Returns how many roots were deleted: `{deleted}`.
        """
        root = target.root if isinstance(target, GraphViewModel) else target
        filter_ = self._deleted_where(target, where)
        if cascade is CascadeType.NONE:
            return self._deleting(root, filter_)
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
        return PostgresStatement(
            "\n".join(
                [
                    f"WITH {'RECURSIVE ' if orphans else ''}cascade_roots AS "
                    f"(SELECT {ROW_ALIAS}.nid {self._from(root, filter_)}),",
                    *targets,
                    f"deleted AS (DELETE FROM {self.graph.nodes} WHERE nid IN (SELECT nid FROM cascade_roots) "
                    "RETURNING 1)",
                    'SELECT CAST(COUNT(*) AS integer) AS "deleted" FROM deleted',
                ]
            )
        )

    def cascade_depth_checks(self, target: TargetModel, where: str | None = None) -> list[CascadeDepthCheck]:
        if not isinstance(target, GraphViewModel):
            return []
        roots = f"SELECT {ROW_ALIAS}.nid {self._from(target.root, self._deleted_where(target, where))}"
        cascaded = PostgresViews(self.graph).cascade(target, "n.nid IN (SELECT nid FROM cascade_roots)")
        return [
            CascadeDepthCheck(
                it.view, it.field, it.max_depth, PostgresStatement(beyond_count(roots, cascaded.reached, it.nodes))
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

    def search_index_table(
        self, kind: Literal["VECTOR", "FULLTEXT"], label: str, properties: Sequence[str]
    ) -> GraphStatement:
        """
        Whether a search index was made, which is over the node table: the size of the embeddings a vector index holds,
        and the name of a full-text index. One row, or none when the index was not made, or was dropped by hand.
        """
        made = "details ->> 'dimensions'" if kind == "VECTOR" else "object_name"
        return PostgresStatement(
            f'SELECT {made} AS "table" FROM {self.graph.catalog} WHERE kind = CAST($kind AS text) '
            "AND label = CAST($label AS text) AND properties = CAST($properties AS text) "
            "AND object_name IS NOT NULL AND to_regclass(object_name) IS NOT NULL",
            {"kind": f"{kind}_INDEX", "label": label, "properties": to_json(list(properties))},
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
        return PostgresStatement(
            "\n".join([f'SELECT {ROW_ALIAS}.props AS "props"', self._from(model, self._id_predicate(model))])
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
        return PostgresStatement.of(PostgresGroups(self.graph).build(model, query, path_targets, labels_of_class))

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
            f'SELECT {node_document("n")} AS "result" FROM {self.graph.nodes} n WHERE n.nid IN ('
            f"SELECT x.nid FROM {self.graph.nodes} a JOIN {self.graph.edges} e ON e.type = CAST($x_type AS text) "
            f"JOIN {self.graph.nodes} x ON {join} WHERE {has_labels('a', labels)} AND "
            f"{text_of('a', origin.model.node_id_property)} = CAST($x_from AS text) AND "
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
        """The store's identity stamps, `{id, assignedAt}`, oldest first."""
        return PostgresStatement(
            f"SELECT jsonb_build_object('id', {text_of('n', 'storeId')}, 'assignedAt', {text_of('n', 'assignedAt')}) "
            f'AS "stamp" FROM {self.graph.nodes} n WHERE {has_labels("n", [STORE_LABEL])} '
            f"ORDER BY {text_of('n', 'assignedAt')}, {text_of('n', 'storeId')}"
        )

    def assign_store_stamp(self) -> GraphStatement:
        """Mints the stamp unless it is there; the uniqueness constraint on its key turns away a racing writer's."""
        return PostgresStatement(
            f"INSERT INTO {self.graph.nodes} (labels, props) SELECT {_labelled(STORE_LABEL)}, "
            "jsonb_build_object('key', CAST($key AS text), 'storeId', CAST($storeId AS text), "
            "'assignedAt', CAST($assignedAt AS text), 'engine', CAST($engine AS text)) "
            f"WHERE NOT EXISTS (SELECT 1 FROM {self.graph.nodes} n WHERE {has_labels('n', [STORE_LABEL])} "
            f"AND {text_of('n', 'key')} = CAST($key AS text))"
        )

    def schema_marker(self, property: str) -> GraphStatement:
        return PostgresStatement(
            f'SELECT {text_of("n", property)} AS "value" FROM {self.graph.nodes} n WHERE {self._marker("n")} '
            f"ORDER BY {self._newest_first('n')} LIMIT 1"
        )

    @staticmethod
    def marker_items(stored: str | None) -> list[str] | None:
        """Postgres keeps an empty string apart from null, so an inventory is kept as it is, as drivine4j keeps it."""
        if stored is None:
            return None
        return [] if stored == "" else stored.split("\n")

    def record_schema_marker(self) -> GraphStatement:
        """The newest marker of the scope is written, and one is made where there is none."""
        written = f"'version', CAST($version AS text), 'items', CAST($items AS text), 'appliedAt', {_NOW_MILLIS}"
        nodes = self.graph.nodes
        return PostgresStatement(
            f"WITH found AS (SELECT m.nid FROM {nodes} m WHERE {self._marker('m')} "
            f"ORDER BY {self._newest_first('m')} LIMIT 1), "
            f"written AS (UPDATE {nodes} n SET props = jsonb_strip_nulls(n.props || jsonb_build_object({written})) "
            "FROM found WHERE n.nid = found.nid RETURNING n.nid) "
            f"INSERT INTO {nodes} (labels, props) SELECT {_labelled(SCHEMA_MARKER)}, "
            f"jsonb_strip_nulls(jsonb_build_object('scope', CAST($scope AS text), {written})) "
            "WHERE NOT EXISTS (SELECT 1 FROM found)"
        )

    def prune_schema_markers(self) -> GraphStatement:
        return PostgresStatement(
            f"DELETE FROM {self.graph.nodes} WHERE nid IN (SELECT nid FROM (SELECT n.nid, row_number() OVER (ORDER BY "
            f"{self._newest_first('n')}) AS position FROM {self.graph.nodes} n WHERE {self._marker('n')}) ranked "
            "WHERE position > 1)"
        )

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

    def _marker(self, alias: str) -> str:
        """The markers of the owner bound as `$scope`."""
        return f"{has_labels(alias, [SCHEMA_MARKER])} AND {text_of(alias, 'scope')} = CAST($scope AS text)"

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
    def _id_predicate(model: FragmentModel) -> str:
        """
        The node whose id is `$id`, read as text, as a uniqueness constraint over the id holds it and as drivine4j finds
        a node: a number is its JSON text.
        """
        if not model.node_id_property:
            raise DrivineError(f"{model.type.__name__} has no NodeId field, so it can't be found by id.")
        return f"{text_of(ROW_ALIAS, model.node_id_property)} = CAST($id AS text)"

    @staticmethod
    def _page(page: Page | None) -> list[str]:
        if page is None:
            return []
        return [
            *([f"ORDER BY {page.order_by}"] if page.order_by else []),
            *([f"OFFSET {page.skip} ROWS"] if page.skip is not None else []),
            *([f"FETCH NEXT {page.limit} ROWS ONLY"] if page.limit is not None else []),
        ]


def _labelled(label: str) -> str:
    """The labels of a node that carries the one label, as a literal."""
    return sql_text(f'["{label}"]')
