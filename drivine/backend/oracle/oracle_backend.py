from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from drivine.annotation.direction import Direction
from drivine.backend.cascade_depth_check import CascadeDepthCheck
from drivine.backend.fragment_group import FragmentGroup
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.matched_node import MatchedNode
from drivine.backend.matching_search import FullTextIndexLookup, MatchingSearch
from drivine.backend.nearest_search import NearestSearch
from drivine.backend.oracle.id_keyed_statement import IdKeyedStatement
from drivine.backend.oracle.oracle_graph import OracleGraph, carries_labels, has_labels, node_document, text_of
from drivine.backend.oracle.oracle_groups import OracleGroups
from drivine.backend.oracle.oracle_query_renderer import OracleQueryRenderer
from drivine.backend.oracle.oracle_save_builder import OracleSaveBuilder
from drivine.backend.oracle.oracle_schema_grammar import recorded_as
from drivine.backend.oracle.oracle_search import OracleSearch
from drivine.backend.oracle.oracle_views import OracleViews
from drivine.backend.oracle.oracle_writes import OracleWrites
from drivine.backend.page import Page
from drivine.backend.save_statement import SaveStatement
from drivine.backend.sql.cypher_shorthand import CypherShorthand
from drivine.backend.sql.node_ids import has_id
from drivine.backend.sql.sql_cascade import beyond_count, kept_targets
from drivine.backend.sql.sql_catalog import catalog_properties
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

_CONTENDED = re.compile(r"ORA-(00060|08177)\b")

ROW_ALIAS = "n"
"""What a statement over a fragment calls its node's row. A raw `where` is SQL over it."""


class OracleBackend:
    """
    Statements for Oracle, over the tables of an OracleGraph, as TypeScript's Drivine writes them. A load returns the
    rows the graph engines do, `{props, labels}` for a fragment, as JSON. A raw `where` is an SQL predicate over the
    node's row, `n` (its `labels`, `props` and `stamp`).
    """

    def __init__(self, graph: OracleGraph | None = None) -> None:
        self.graph = graph or OracleGraph()

    def render_query(
        self,
        query: Query,
        target: TargetModel,
        labels_of_class: Callable[[type], list[str]],
        indexed: Sequence[SchemaItem] = (),
    ) -> RenderedQuery:
        """A query over a class's references, written for the store: its predicate, order and paging, and values."""
        return OracleQueryRenderer.render(
            query, target, lambda depth: OracleViews(self.graph, depth), ROW_ALIAS, labels_of_class, indexed
        )

    def written(self, predicate: str) -> str:
        """
        A predicate the caller wrote, as the store reads it: each property of the root written as Cypher writes it,
        `n.name`, read as that property, by the name the layout holds it by, so `` n.`$score` `` is the property held
        as `$$score` (see CypherShorthand).
        """
        return CypherShorthand(ROW_ALIAS, ROW_ALIAS, text_of).rewrite(predicate)

    def render_where(
        self, where: Any, target: TargetModel, labels_of_class: Callable[[type], list[str]]
    ) -> tuple[str, dict[str, Any]]:
        """A condition over a class's references, written for the store, with its values."""
        return OracleQueryRenderer.render_where(where, target, OracleViews(self.graph), ROW_ALIAS, labels_of_class)

    def load_all_fragments(
        self, model: FragmentModel, where: str | None = None, page: Page | None = None, tested: Sequence[str] = ()
    ) -> GraphStatement:
        return GraphStatement(
            "\n".join(
                [f'SELECT {node_document(ROW_ALIAS)} AS "result"', self._from(model, where, tested), *self._page(page)]
            )
        )

    def load_fragment(self, model: FragmentModel) -> GraphStatement:
        return IdKeyedStatement(
            "\n".join(
                [f'SELECT {node_document(ROW_ALIAS)} AS "result"', self._found_by_id(model, self._id_predicate(model))]
            )
        )

    def count_fragments(
        self, model: FragmentModel, where: str | None = None, tested: Sequence[str] = ()
    ) -> GraphStatement:
        return GraphStatement("\n".join(['SELECT COUNT(*) AS "count"', self._from(model, where, tested)]))

    def load_all_views(
        self,
        model: GraphViewModel,
        where: str | None = None,
        depth: Mapping[str, int] | None = None,
        page: Page | None = None,
        tested: Sequence[str] = (),
    ) -> GraphStatement:
        views = OracleViews(self.graph, depth)
        return GraphStatement(
            "\n".join(
                [
                    f'SELECT {views.document(model, ROW_ALIAS)} AS "result"',
                    self._from(model.root, self._view_where(model, where, depth), tested),
                    *self._page(page),
                ]
            )
        )

    def load_view(self, model: GraphViewModel, depth: Mapping[str, int] | None = None) -> GraphStatement:
        views = OracleViews(self.graph, depth)
        return IdKeyedStatement(
            "\n".join(
                [
                    f'SELECT {views.document(model, ROW_ALIAS)} AS "result"',
                    self._found_by_id(model.root, self._view_where(model, self._id_predicate(model.root), depth)),
                ]
            )
        )

    def count_views(
        self,
        model: GraphViewModel,
        where: str | None = None,
        depth: Mapping[str, int] | None = None,
        tested: Sequence[str] = (),
    ) -> GraphStatement:
        return GraphStatement(
            "\n".join(
                ['SELECT COUNT(*) AS "count"', self._from(model.root, self._view_where(model, where, depth), tested)]
            )
        )

    def delete(
        self, target: TargetModel, where: str | None = None, cascade: CascadeType = CascadeType.NONE
    ) -> GraphStatement:
        """
        Deletes the root with id `$id` that meets `where`, and for a cascade the nodes a load of the view reaches from
        it through the relationships a save writes (see SqlViews.cascade): every one, or with DELETE_ORPHAN those not
        kept (see kept_targets), which are read before anything is deleted. Returns how many nodes were deleted, the
        roots and what the cascade took (#375): `{deleted}`.
        """
        root = target.root if isinstance(target, GraphViewModel) else target
        filter_ = self._deleted_where(target, where)
        if cascade is CascadeType.NONE:
            return IdKeyedStatement.of(self._deleting(root, filter_, by_id=True))
        if not isinstance(target, GraphViewModel):
            raise DrivineError(f"A cascade follows a view's relationships. {root.type.__name__} is a NodeFragment.")
        cascaded = OracleViews(self.graph).cascade(target, "n.nid IN (SELECT column_value FROM TABLE(v_roots))")
        reached = "\n        UNION ALL ".join(cascaded.reached)
        if not cascaded.reached:
            targets = "NULL;"
        elif cascade is CascadeType.DELETE_ORPHAN:
            # Read both ways round where it follows a relationship of either direction (#390).
            kept = kept_targets(self.graph, cascaded.followed, "UNION ALL", both_ways=True)
            targets = "\n".join(
                [
                    f"WITH cascade_targets AS (SELECT DISTINCT t.nid FROM ({reached}) t "
                    "WHERE t.nid NOT IN (SELECT column_value FROM TABLE(v_roots))),",
                    "    cascade_all AS (SELECT column_value AS nid FROM TABLE(v_roots) UNION SELECT nid FROM "
                    "cascade_targets),",
                    f"    cascade_kept (nid) AS (\n    {kept}",
                    "    ) CYCLE nid SET looped TO 'Y' DEFAULT 'N'",
                    "    SELECT nid BULK COLLECT INTO v_targets FROM cascade_targets WHERE nid NOT IN "
                    "(SELECT nid FROM cascade_kept);",
                ]
            )
        else:
            targets = f"SELECT DISTINCT nid BULK COLLECT INTO v_targets FROM ({reached});"
        return IdKeyedStatement(f"""DECLARE
    v_roots SYS.ODCINUMBERLIST;
    v_targets SYS.ODCINUMBERLIST := SYS.ODCINUMBERLIST();
    v_deleted NUMBER;
    c SYS_REFCURSOR;
BEGIN
    SELECT {ROW_ALIAS}.nid BULK COLLECT INTO v_roots {self._found_by_id(root, filter_)};
    {targets}
    DELETE FROM {self.graph.nodes} WHERE nid IN (SELECT column_value FROM TABLE(v_roots));
    v_deleted := SQL%ROWCOUNT;
    DELETE FROM {self.graph.nodes} x WHERE x.nid IN (SELECT column_value FROM TABLE(v_targets));
    v_deleted := v_deleted + SQL%ROWCOUNT;
    OPEN c FOR SELECT v_deleted AS "deleted" FROM dual;
    DBMS_SQL.RETURN_RESULT(c);
END;""")

    def cascade_depth_checks(self, target: TargetModel, where: str | None = None) -> list[CascadeDepthCheck]:
        if not isinstance(target, GraphViewModel):
            return []
        roots = f"SELECT {ROW_ALIAS}.nid {self._found_by_id(target.root, self._deleted_where(target, where))}"
        cascaded = OracleViews(self.graph).cascade(target, "n.nid IN (SELECT nid FROM cascade_roots)")
        return [
            CascadeDepthCheck(
                it.view, it.field, it.max_depth, IdKeyedStatement(beyond_count(roots, it.within, it.nodes))
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
        return self._deleting(root, self._root_where(target, where), tested)

    def vector_index_table(self, label: str, property: str, dimensions: int | None = None) -> GraphStatement:
        """
        The statement giving the name of the table a vector index is over, for the index over the label's property,
        recorded as drivine4j records it or as this library did before: one row, or none when the index was not made.
        Given `dimensions`, only an index made for embeddings of that size: Oracle refuses to measure a vector against
        an index of another size (ORA-51808), so a search by it measures every node instead.
        """
        recorded, earlier = recorded_as("VECTOR")
        bindings: dict[str, Any] = {
            "kind": recorded,
            "earlier": earlier,
            "label": label,
            "properties": catalog_properties([property]),
        }
        sized = dimensions is not None
        if sized:
            bindings["dimensions"] = dimensions
        return GraphStatement(
            f'SELECT object_name AS "table" FROM {self.graph.catalog} WHERE kind IN ($kind, $earlier) '
            "AND label = $label AND properties = $properties AND object_name IS NOT NULL"
            + (" AND JSON_VALUE(details, '$.dimensions' RETURNING NUMBER) = $dimensions" if sized else ""),
            bindings,
        )

    def full_text_index(self, label: str, properties: Sequence[str]) -> FullTextIndexLookup:
        """
        The table the Oracle Text index of the full-text index was made over, by the shared module's lookup of the
        catalog: one row, or none when it was not made, and the search then scores by BM25 over every node of the label.
        """
        lookup = full_text_index_lookup("ORACLE", self.graph.name, label, properties)
        assert lookup is not None, "Oracle records its full-text indexes in its catalog."
        return FullTextIndexLookup(
            GraphStatement(lookup.statement, lookup.parameters),
            lambda rows: SearchIndex(oracle_text=rows[0]) if rows else SearchIndex(),
        )

    def nearest(self, target: TargetModel, search: NearestSearch) -> GraphStatement:
        return OracleSearch(self.graph).nearest(target, search)

    def matching(self, target: TargetModel, search: MatchingSearch) -> GraphStatement:
        return OracleSearch(self.graph).matching(target, search)

    def save(
        self,
        target: TargetModel,
        obj: Any,
        options: SaveOptions | None = None,
        create_only: bool = False,
        registry: ModelRegistry | None = None,
    ) -> SaveStatement:
        return OracleSaveBuilder(self.graph, registry).build(target, obj, options, create_only)

    def held_properties(self, model: FragmentModel) -> GraphStatement:
        """
        Re-encoded with JSON_TRANSFORM: a document updated in place, as every save updates one, comes back as OSON
        version 2, which the thin drivers can't read (oracle/python-oracledb#622, oracle/node-oracledb#1796).
        """
        return IdKeyedStatement(
            "\n".join(
                [
                    f"SELECT JSON_TRANSFORM({ROW_ALIAS}.props, KEEP '$') AS \"props\"",
                    self._found_by_id(model, self._id_predicate(model)),
                ]
            )
        )

    def found_stamp(self, model: FragmentModel) -> GraphStatement:
        return IdKeyedStatement(
            "\n".join(
                [
                    f"SELECT NVL({ROW_ALIAS}.stamp, '{stamps.NEVER_STAMPED}') AS \"found\"",
                    self._found_by_id(model, self._id_predicate(model)),
                ]
            )
        )

    def patch_one(self, model: FragmentModel, where: str | None = None) -> GraphStatement:
        return OracleWrites(self.graph).patch_one(model, where)

    def patch_many(self, model: FragmentModel) -> GraphStatement:
        return OracleWrites(self.graph).patch_many(model)

    def fragment_batch(self, groups: list[FragmentGroup], options: SaveOptions) -> GraphStatement:
        return OracleWrites(self.graph).fragment_batch(groups, options)

    def groups(
        self,
        model: FragmentModel,
        query: GroupQuery,
        path_targets: Mapping[str, FragmentModel],
        labels_of_class: Callable[[type], list[str]],
    ) -> GraphStatement:
        return OracleGroups(self.graph).build(model, query, path_targets, labels_of_class)

    def related(self, origin: MatchedNode, type: str, direction: Direction, target: FragmentModel) -> GraphStatement:
        return OracleWrites(self.graph).related(origin, type, direction, target)

    def relate(
        self, origin: MatchedNode, to: MatchedNode, type: str, properties: Mapping[str, Any], mode: RelateMode
    ) -> GraphStatement:
        return OracleWrites(self.graph).relate(origin, to, type, properties, mode)

    def unrelate(self, origin: MatchedNode, to: MatchedNode, type: str) -> GraphStatement:
        return OracleWrites(self.graph).unrelate(origin, to, type)

    def unrelate_all(self, origin: MatchedNode, type: str, direction: Direction) -> GraphStatement:
        return OracleWrites(self.graph).unrelate_all(origin, type, direction)

    @property
    def _stamp_and_marker(self) -> StampAndMarker:
        """The store's stamp and schema marker statements, the shared module's (see StampAndMarker)."""
        return StampAndMarker("oracle", self.graph.name)

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
        """ORA-00060 (deadlock) and ORA-08177 (serialization) are another writer's doing: running again may succeed."""
        cause: BaseException | None = failure
        for _ in range(10):
            if cause is None:
                return False
            if _CONTENDED.search(str(cause)):
                return True
            cause = cause.__cause__
        return False

    def _root_where(self, target: TargetModel, where: str | None) -> str | None:
        """A predicate over the root's row and, for a view, the view's requirements."""
        return self._view_where(target, where) if isinstance(target, GraphViewModel) else where

    def _view_where(
        self, model: GraphViewModel, where: str | None, depth: Mapping[str, int] | None = None
    ) -> str | None:
        parts = [*([f"({where})"] if where else []), *OracleViews(self.graph, depth).requirements(model, ROW_ALIAS)]
        return " AND ".join(parts) if parts else None

    def _from(self, model: FragmentModel, where: str | None = None, tested: Sequence[str] = ()) -> str:
        """
        The nodes of the fragment's labels that meet `where`, as the row `n`. A label `where` tests itself (`tested`)
        is not tested again. Beside one, the others are tested in a form no index serves: Oracle takes a label's test
        for a lookup of one row, and would read that label's nodes in place of the index `where` is read through.
        """
        untested = [it for it in model.labels if it not in tested]
        if not tested:
            labels = [has_labels(ROW_ALIAS, untested)]
        elif untested:
            labels = [f"CASE WHEN {has_labels(ROW_ALIAS, untested)} THEN 1 END = 1"]
        else:
            labels = []
        tests = [*labels, *([f"({where})"] if where else [])]
        return f"FROM {self.graph.nodes} {ROW_ALIAS}\nWHERE {' AND '.join(tests)}"

    def _deleting(
        self, model: FragmentModel, where: str | None, tested: Sequence[str] = (), by_id: bool = False
    ) -> GraphStatement:
        """
        Deletes the nodes `where` selects, with their relationships, and returns how many: `{deleted}`. `by_id` says
        `where` finds the node by its id (see _found_by_id).
        """
        found = self._found_by_id(model, where) if by_id else self._from(model, where, tested)
        return GraphStatement(f"""DECLARE
    v_deleted NUMBER;
    c SYS_REFCURSOR;
BEGIN
    DELETE {found};
    v_deleted := SQL%ROWCOUNT;
    OPEN c FOR SELECT v_deleted AS "deleted" FROM dual;
    DBMS_SQL.RETURN_RESULT(c);
END;""")

    def _found_by_id(self, model: FragmentModel, where: str | None = None) -> str:
        """
        The node of the fragment's labels found by its id in `where` (see _id_predicate), as the row `n`: each label
        told by the labels' text (`carries_labels`), so Oracle reads the index over the id for the one node, and not
        every node of the first label, as drivine4j's `OracleSelect.hasId` narrows it.
        """
        tests = [carries_labels(ROW_ALIAS, model.labels), *([f"({where})"] if where else [])]
        return f"FROM {self.graph.nodes} {ROW_ALIAS}\nWHERE {' AND '.join(tests)}"

    @staticmethod
    def _id_predicate(model: FragmentModel) -> str:
        """
        The node whose id is `$id`, compared as the shared module writes the comparison: the property's class and key
        with the id's, which the index over the property of every node is made over and answers (see node_ids). The id
        is bound as the JSON text Oracle's layout holds it as, read `EXTENDED`, as a save finds the node (see
        IdKeyedStatement); the comparison reads it more than once, so it is bound by name.
        """
        if not model.node_id_property:
            raise DrivineError(f"{model.type.__name__} has no NodeId field, so it can't be found by id.")
        return has_id("oracle", ROW_ALIAS, model.node_id_property, "JSON($id EXTENDED)")

    @staticmethod
    def _page(page: Page | None) -> list[str]:
        if page is None:
            return []
        return [
            *([f"ORDER BY {page.order_by}"] if page.order_by else []),
            *([f"OFFSET {page.skip} ROWS"] if page.skip is not None else []),
            *([f"FETCH NEXT {page.limit} ROWS ONLY"] if page.limit is not None else []),
        ]
