from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from drivine.annotation.direction import Direction
from drivine.backend.fragment_group import FragmentGroup
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.matched_node import MatchedNode
from drivine.backend.matching_search import MatchingSearch
from drivine.backend.nearest_search import NearestSearch
from drivine.backend.oracle.oracle_graph import OracleGraph, has_labels, json_key, node_document
from drivine.backend.oracle.oracle_groups import OracleGroups
from drivine.backend.oracle.oracle_query_renderer import OracleQueryRenderer
from drivine.backend.oracle.oracle_save_builder import OracleSaveBuilder
from drivine.backend.oracle.oracle_search import OracleSearch
from drivine.backend.oracle.oracle_views import OracleViews
from drivine.backend.oracle.oracle_writes import OracleWrites
from drivine.backend.page import Page
from drivine.backend.save_statement import SaveStatement
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

_ITEMS_MARK = "items:"
"""What a marker's inventory is kept behind, as drivine4j keeps it: Oracle has no empty string."""

_NOW_MILLIS = "ROUND((CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS DATE) - DATE '1970-01-01') * 86400000)"
"""The moment, in milliseconds since the epoch, as the graph engines' timestamp() gives it."""

_CONTENDED = re.compile(r"ORA-(00060|08177)\b")

ROW_ALIAS = "n"
"""What a statement over a fragment calls its node's row. A raw `where` is SQL over it."""


def _newest_first(alias: str) -> str:
    """The newest marker first, then the one made later, so the marker read, written and kept are one."""
    return f"JSON_VALUE({alias}.props, '$.appliedAt' RETURNING NUMBER) DESC NULLS LAST, {alias}.nid DESC"


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
        return self.load_all_fragments(model, self._id_predicate(model))

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
        return self.load_all_views(model, self._id_predicate(model.root), depth)

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
        root = target.root if isinstance(target, GraphViewModel) else target
        id_ = self._id_predicate(root)
        filter_ = self._root_where(target, f"{id_} AND ({where})" if where else id_)
        if cascade is CascadeType.NONE:
            return self._deleting(root, filter_)
        if not isinstance(target, GraphViewModel):
            raise DrivineError(f"A cascade follows a view's relationships. {root.type.__name__} is a NodeFragment.")
        reached = OracleViews(self.graph).cascaded(target, "v_roots")
        deletable = (
            f" AND NOT EXISTS (SELECT 1 FROM {self.graph.edges} e WHERE e.src = x.nid OR e.dst = x.nid)"
            if cascade is CascadeType.DELETE_ORPHAN
            else ""
        )
        union = "\n        UNION ALL ".join(reached)
        targets = f"SELECT DISTINCT nid BULK COLLECT INTO v_targets FROM ({union});" if reached else "NULL;"
        return GraphStatement(f"""DECLARE
    v_roots SYS.ODCINUMBERLIST;
    v_targets SYS.ODCINUMBERLIST := SYS.ODCINUMBERLIST();
    v_deleted NUMBER;
    c SYS_REFCURSOR;
BEGIN
    SELECT {ROW_ALIAS}.nid BULK COLLECT INTO v_roots {self._from(root, filter_)};
    {targets}
    DELETE FROM {self.graph.nodes} WHERE nid IN (SELECT column_value FROM TABLE(v_roots));
    v_deleted := SQL%ROWCOUNT;
    -- The roots' relationships went with them (the relationship table cascades), so an orphan is now one with none.
    DELETE FROM {self.graph.nodes} x WHERE x.nid IN (SELECT column_value FROM TABLE(v_targets)){deletable};
    OPEN c FOR SELECT v_deleted AS "deleted" FROM dual;
    DBMS_SQL.RETURN_RESULT(c);
END;""")

    def delete_all(self, target: TargetModel, where: str | None = None, tested: Sequence[str] = ()) -> GraphStatement:
        root = target.root if isinstance(target, GraphViewModel) else target
        return self._deleting(root, self._root_where(target, where), tested)

    def search_index_table(
        self, kind: Literal["VECTOR", "FULLTEXT"], label: str, properties: Sequence[str]
    ) -> GraphStatement:
        """
        The statement giving the name of the table an index made for a search is over, for the index over the label's
        properties: one row, or none when the index was not made.
        """
        return GraphStatement(
            f'SELECT object_name AS "table" FROM {self.graph.catalog} WHERE kind = $kind AND label = $label '
            "AND properties = $properties AND object_name IS NOT NULL",
            {"kind": kind, "label": label, "properties": to_json(list(properties))},
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
        return GraphStatement(
            "\n".join(
                [
                    f"SELECT JSON_TRANSFORM({ROW_ALIAS}.props, KEEP '$') AS \"props\"",
                    self._from(model, self._id_predicate(model)),
                ]
            )
        )

    def found_stamp(self, model: FragmentModel) -> GraphStatement:
        return GraphStatement(
            "\n".join(
                [
                    f"SELECT NVL({ROW_ALIAS}.stamp, '{stamps.NEVER_STAMPED}') AS \"found\"",
                    self._from(model, self._id_predicate(model)),
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

    def store_stamps(self) -> GraphStatement:
        """The store's identity stamps, `{id, assignedAt}`, oldest first."""
        return GraphStatement(
            "SELECT JSON_OBJECT('id' VALUE JSON_VALUE(n.props, '$.storeId'), 'assignedAt' VALUE "
            "JSON_VALUE(n.props, '$.assignedAt') "
            f'RETURNING JSON) AS "stamp" FROM {self.graph.nodes} n WHERE {has_labels("n", [STORE_LABEL])} '
            "ORDER BY JSON_VALUE(n.props, '$.assignedAt'), JSON_VALUE(n.props, '$.storeId')"
        )

    def assign_store_stamp(self) -> GraphStatement:
        """Mints the stamp unless it is there; the uniqueness constraint on its key turns away a racing writer's."""
        return GraphStatement(
            f"INSERT INTO {self.graph.nodes} (labels, props) SELECT JSON('[\"{STORE_LABEL}\"]'), "
            "JSON_OBJECT('key' VALUE $key, 'storeId' VALUE $storeId, 'assignedAt' VALUE $assignedAt, 'engine' VALUE "
            f"$engine) FROM dual WHERE NOT EXISTS (SELECT 1 FROM {self.graph.nodes} n WHERE "
            f"{has_labels('n', [STORE_LABEL])} AND JSON_VALUE(n.props, '$.key') = $key)"
        )

    def schema_marker(self, property: str) -> GraphStatement:
        """An inventory is kept behind 'items:', so an empty one reads back empty, not absent (drivine4j's mark)."""
        return GraphStatement(
            f"SELECT JSON_VALUE(n.props, '$.{property}' RETURNING CLOB) AS \"value\" "
            f"FROM {self.graph.nodes} n WHERE {self._marker('n')} ORDER BY {_newest_first('n')} "
            "FETCH FIRST 1 ROWS ONLY"
        )

    @staticmethod
    def marker_items(stored: str | None) -> list[str] | None:
        """The inventory behind its mark: stripped here, as SQL would make an empty one null."""
        if stored is None or not stored.startswith(_ITEMS_MARK):
            return None
        items = stored[len(_ITEMS_MARK) :]
        return [] if items == "" else items.split("\n")

    def record_schema_marker(self) -> GraphStatement:
        """
        The marker found by a query and matched by its id: matched by its scope, the MERGE would update the column it
        matches on, which Oracle refuses (ORA-38104). Where a race left several, the newest is written.
        """
        nodes = self.graph.nodes
        return GraphStatement(
            f"MERGE INTO {nodes} n USING (SELECT (SELECT m.nid FROM {nodes} m WHERE {self._marker('m')} ORDER BY "
            f"{_newest_first('m')} FETCH FIRST 1 ROWS ONLY) AS nid FROM dual) found ON (n.nid = found.nid) "
            "WHEN MATCHED THEN UPDATE SET n.props = JSON_MERGEPATCH(n.props, JSON_OBJECT('version' VALUE $version, "
            f"'items' VALUE '{_ITEMS_MARK}' || $items, 'appliedAt' VALUE {_NOW_MILLIS})) "
            f"WHEN NOT MATCHED THEN INSERT (labels, props) VALUES (JSON('[\"{SCHEMA_MARKER}\"]'), JSON_OBJECT('scope' "
            f"VALUE $scope, 'version' VALUE $version, 'items' VALUE '{_ITEMS_MARK}' || $items, 'appliedAt' VALUE "
            f"{_NOW_MILLIS} ABSENT ON NULL))"
        )

    def prune_schema_markers(self) -> GraphStatement:
        return GraphStatement(
            f"DELETE FROM {self.graph.nodes} WHERE nid IN (SELECT nid FROM (SELECT n.nid, ROW_NUMBER() OVER (ORDER BY "
            f"{_newest_first('n')}) AS position FROM {self.graph.nodes} n WHERE {self._marker('n')}) "
            "WHERE position > 1)"
        )

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

    def _marker(self, alias: str) -> str:
        """The markers of the owner bound as `$scope`."""
        return f"{has_labels(alias, [SCHEMA_MARKER])} AND JSON_VALUE({alias}.props, '$.scope') = $scope"

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

    def _deleting(self, model: FragmentModel, where: str | None, tested: Sequence[str] = ()) -> GraphStatement:
        """Deletes the nodes `where` selects, with their relationships, and returns how many: `{deleted}`."""
        return GraphStatement(f"""DECLARE
    v_deleted NUMBER;
    c SYS_REFCURSOR;
BEGIN
    DELETE {self._from(model, where, tested)};
    v_deleted := SQL%ROWCOUNT;
    OPEN c FOR SELECT v_deleted AS "deleted" FROM dual;
    DBMS_SQL.RETURN_RESULT(c);
END;""")

    @staticmethod
    def _id_predicate(model: FragmentModel) -> str:
        """The node whose id is `$id`, compared as JSON compares, so a number is not taken for text."""
        if not model.node_id_property:
            raise DrivineError(f"{model.type.__name__} has no NodeId field, so it can't be found by id.")
        k = json_key(model.node_id_property)
        return f"JSON_EXISTS({ROW_ALIAS}.props, '$?(@{k} == $id__)' PASSING $id AS \"id__\")"

    @staticmethod
    def _page(page: Page | None) -> list[str]:
        if page is None:
            return []
        return [
            *([f"ORDER BY {page.order_by}"] if page.order_by else []),
            *([f"OFFSET {page.skip} ROWS"] if page.skip is not None else []),
            *([f"FETCH NEXT {page.limit} ROWS ONLY"] if page.limit is not None else []),
        ]
