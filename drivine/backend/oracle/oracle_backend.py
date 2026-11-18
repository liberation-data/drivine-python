from __future__ import annotations

import dataclasses
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
from drivine.backend.oracle.oracle_fragments import called, hinted, noted
from drivine.backend.oracle.oracle_graph import (
    OracleGraph,
    carries_labels,
    has_labels,
    module_label_test,
    node_document,
    text_of,
)
from drivine.backend.oracle.oracle_groups import OracleGroups
from drivine.backend.oracle.oracle_save_builder import OracleSaveBuilder
from drivine.backend.oracle.oracle_schema_grammar import recorded_as
from drivine.backend.oracle.oracle_search import OracleSearch
from drivine.backend.oracle.oracle_views import OracleViews
from drivine.backend.oracle.oracle_writes import OracleWrites
from drivine.backend.page import Page
from drivine.backend.save_statement import SaveStatement
from drivine.backend.sql.cypher_shorthand import CypherShorthand
from drivine.backend.sql.fragment_query_renderer import FragmentQueryRenderer, root_labels
from drivine.backend.sql.fragments import FRAGMENT_ROOT_ROW, Fragments
from drivine.backend.sql.node_ids import has_id
from drivine.backend.sql.sql_cascade import beyond_count, kept_targets
from drivine.backend.sql.sql_catalog import catalog_properties
from drivine.backend.store_statements import StampAndMarker
from drivine.cypher.shared_compiler import SearchIndex, full_text_index_lookup
from drivine.drivine_error import DrivineError
from drivine.dsl.composite_condition import CompositeCondition
from drivine.dsl.condition import Condition, all_of_one
from drivine.dsl.model_scope import scope_of
from drivine.dsl.query import Query
from drivine.dsl.query_renderer import pinned_properties
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

ROW_ALIAS = FRAGMENT_ROOT_ROW
"""
What a read of the query DSL calls its root's row (see FRAGMENT_ROOT_ROW). A raw `where` is SQL over `n`, which
`written` reads as it.
"""

NODE_ROW = "n"
"""What the statements that read a node by its id alone call its row."""


class OracleBackend:
    """
    Statements for Oracle, over the tables of an OracleGraph, as TypeScript's Drivine writes them. A load returns the
    rows the graph engines do, `{props, labels}` for a fragment, as JSON. A raw `where` is an SQL predicate over the
    node's row, `n` (its `labels`, `props` and `stamp`), in which a property of the node may be written as Cypher
    writes it, `n.name` (see `written`). A filter, a seek and an order of the query DSL are the shared module's SQL
    (FragmentQueryRenderer), over the row `n_`; what its fragments ask of the statement, its hints and the stored
    functions they call, travels with their text and is written into each statement here (see oracle_fragments).
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
        """
        The query, as the shared module's fragments write it (FragmentQueryRenderer), what they ask of its statement
        noted on its filter and its order (see oracle_fragments). An equality of a property the first property of a
        range index or a uniqueness constraint of one of the root's labels is (`indexed`) is read through the index's
        expression, as the module writes it for the labels the root is given (drivine4j#65): a label that such an
        equality at the top of the filter tests is then not tested again by the statement (`labels_tested`, see
        `_from`).
        """
        fragments = self._fragments(target, indexed)
        rendered = FragmentQueryRenderer.render(query, target, fragments, labels_of_class)
        return dataclasses.replace(
            rendered,
            where=noted(rendered.where, fragments),
            order_by=noted(rendered.order_by, fragments),
            labels_tested=tuple(
                labels_implied(
                    rendered.where,
                    root_labels(target),
                    self._indexed_equalities(query, target, labels_of_class, indexed),
                )
            ),
        )

    def written(self, predicate: str, bindings: Mapping[str, Any] | None = None) -> str:
        """
        A predicate the caller wrote over `n`, as the store reads it over the row a read calls its root, `n_`: each
        property of the root written as Cypher writes it, `n.name`, read as that property, by the name the layout holds
        it by, so `` n.`$score` `` is the property held as `$$score`, and one compared with a boolean of `bindings` by
        the boolean's text (see CypherShorthand).
        """
        return CypherShorthand(NODE_ROW, ROW_ALIAS, text_of).rewrite(predicate, bindings)

    def render_where(
        self, where: Any, target: TargetModel, labels_of_class: Callable[[type], list[str]]
    ) -> tuple[str, dict[str, Any]]:
        """The condition, as the shared module's fragments write it, what they ask of its statement noted on it."""
        fragments = self._fragments(target)
        written, bindings = FragmentQueryRenderer.render_where(where, target, fragments, labels_of_class)
        return noted(written, fragments) or written, bindings

    def _fragments(self, target: TargetModel, indexed: Sequence[SchemaItem] = ()) -> Fragments:
        """The fragments of one statement over the target's root, its row `n_`, reading an equality by `indexed`."""
        return Fragments(
            "ORACLE",
            self.graph.name,
            ROW_ALIAS,
            root_labels(target),
            [(it.label, it.properties[0]) for it in indexed if it.properties],
        )

    def _indexed_equalities(
        self,
        query: Query,
        target: TargetModel,
        labels_of_class: Callable[[type], list[str]],
        indexed: Sequence[SchemaItem],
    ) -> str | None:
        """
        The plain equalities at the top of the query's filter of a property `indexed` for a label of the root, as the
        shared module's fragments write them alone: None where there are none. The module reads some values through
        the index (a label test in their term) and others not (a list, a map, `""`), as it alone knows.
        """
        labels = root_labels(target)
        properties = {it.properties[0] for it in indexed if it.label in labels and it.properties}
        if query.where is None or not properties:
            return None
        scope = scope_of(target)

        def equalities(condition: Condition) -> list[Condition]:
            if isinstance(condition, CompositeCondition) and condition.kind == "AND":
                return [it for part in condition.conditions for it in equalities(part)]
            return [condition] if any(it in properties for it in pinned_properties(condition, scope)) else []

        pinned = equalities(all_of_one(query.where))
        if not pinned:
            return None
        return FragmentQueryRenderer.render_where(pinned, target, self._fragments(target, indexed), labels_of_class)[0]

    def load_all_fragments(
        self, model: FragmentModel, where: str | None = None, page: Page | None = None, tested: Sequence[str] = ()
    ) -> GraphStatement:
        return self._statement(
            "\n".join(
                [f'SELECT {node_document(ROW_ALIAS)} AS "result"', self._from(model, where, tested), *self._page(page)]
            )
        )

    def load_fragment(self, model: FragmentModel) -> GraphStatement:
        return IdKeyedStatement.of(
            self._statement(
                "\n".join(
                    [
                        f'SELECT {node_document(ROW_ALIAS)} AS "result"',
                        self._found_by_id(model, self._id_predicate(model)),
                    ]
                )
            )
        )

    def count_fragments(
        self, model: FragmentModel, where: str | None = None, tested: Sequence[str] = ()
    ) -> GraphStatement:
        return self._statement("\n".join(['SELECT COUNT(*) AS "count"', self._from(model, where, tested)]))

    def load_all_views(
        self,
        model: GraphViewModel,
        where: str | None = None,
        depth: Mapping[str, int] | None = None,
        page: Page | None = None,
        tested: Sequence[str] = (),
    ) -> GraphStatement:
        views = OracleViews(self.graph, depth)
        return self._statement(
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
        return IdKeyedStatement.of(
            self._statement(
                "\n".join(
                    [
                        f'SELECT {views.document(model, ROW_ALIAS)} AS "result"',
                        self._found_by_id(model.root, self._view_where(model, self._id_predicate(model.root), depth)),
                    ]
                )
            )
        )

    def count_views(
        self,
        model: GraphViewModel,
        where: str | None = None,
        depth: Mapping[str, int] | None = None,
        tested: Sequence[str] = (),
    ) -> GraphStatement:
        return self._statement(
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
        roots = hinted(f"SELECT {ROW_ALIAS}.nid BULK COLLECT INTO v_roots {self._found_by_id(root, filter_)};")
        return IdKeyedStatement.of(
            self._statement(f"""DECLARE
    v_roots SYS.ODCINUMBERLIST;
    v_targets SYS.ODCINUMBERLIST := SYS.ODCINUMBERLIST();
    v_deleted NUMBER;
    c SYS_REFCURSOR;
BEGIN
    {roots}
    {targets}
    DELETE FROM {self.graph.nodes} WHERE nid IN (SELECT column_value FROM TABLE(v_roots));
    v_deleted := SQL%ROWCOUNT;
    DELETE FROM {self.graph.nodes} x WHERE x.nid IN (SELECT column_value FROM TABLE(v_targets));
    v_deleted := v_deleted + SQL%ROWCOUNT;
    OPEN c FOR SELECT v_deleted AS "deleted" FROM dual;
    DBMS_SQL.RETURN_RESULT(c);
END;""")
        )

    def cascade_depth_checks(self, target: TargetModel, where: str | None = None) -> list[CascadeDepthCheck]:
        if not isinstance(target, GraphViewModel):
            return []
        roots = hinted(f"SELECT {ROW_ALIAS}.nid {self._found_by_id(target.root, self._deleted_where(target, where))}")
        cascaded = OracleViews(self.graph).cascade(target, "n.nid IN (SELECT nid FROM cascade_roots)")
        return [
            CascadeDepthCheck(
                it.view,
                it.field,
                it.max_depth,
                IdKeyedStatement.of(self._statement(beyond_count(roots, it.within, it.nodes))),
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
                    f"SELECT JSON_TRANSFORM({NODE_ROW}.props, KEEP '$') AS \"props\"",
                    self._found_by_id(model, self._id_predicate(model, NODE_ROW), NODE_ROW),
                ]
            )
        )

    def found_stamp(self, model: FragmentModel) -> GraphStatement:
        return IdKeyedStatement(
            "\n".join(
                [
                    f"SELECT NVL({NODE_ROW}.stamp, '{stamps.NEVER_STAMPED}') AS \"found\"",
                    self._found_by_id(model, self._id_predicate(model, NODE_ROW), NODE_ROW),
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
        The nodes of the fragment's labels that meet `where`, as the row `n_`. A label `where` tests itself (`tested`)
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
        deleted = hinted(f"DELETE {found}")
        return self._statement(f"""DECLARE
    v_deleted NUMBER;
    c SYS_REFCURSOR;
BEGIN
    {deleted};
    v_deleted := SQL%ROWCOUNT;
    OPEN c FOR SELECT v_deleted AS "deleted" FROM dual;
    DBMS_SQL.RETURN_RESULT(c);
END;""")

    def _found_by_id(self, model: FragmentModel, where: str | None = None, row: str = ROW_ALIAS) -> str:
        """
        The node of the fragment's labels found by its id in `where` (see _id_predicate), as the row `row` (`n_` unless
        given): each label told by the labels' text (`carries_labels`), so Oracle reads the index over the id for the
        one node, and not every node of the first label, as drivine4j's `OracleSelect.hasId` narrows it.
        """
        tests = [carries_labels(row, model.labels), *([f"({where})"] if where else [])]
        return f"FROM {self.graph.nodes} {row}\nWHERE {' AND '.join(tests)}"

    def _statement(self, text: str, bindings: Mapping[str, Any] | None = None) -> GraphStatement:
        """
        The statement `text`, with the hints the fragments in it ask for written after its first `SELECT` or `DELETE`,
        and the stored functions they call said (see oracle_fragments). A block hints each of its statements itself
        first.
        """
        made, calls = called(hinted(text), self.graph.name)
        return GraphStatement(made, dict(bindings or {}), calls=calls)

    @staticmethod
    def _id_predicate(model: FragmentModel, row: str = ROW_ALIAS) -> str:
        """
        The node whose id is `$id`, compared as the shared module writes the comparison: the property's class and key
        with the id's, which the index over the property of every node is made over and answers (see node_ids). The id
        is bound as the JSON text Oracle's layout holds it as, read `EXTENDED`, as a save finds the node (see
        IdKeyedStatement); the comparison reads it more than once, so it is bound by name.
        """
        if not model.node_id_property:
            raise DrivineError(f"{model.type.__name__} has no NodeId field, so it can't be found by id.")
        return has_id("oracle", row, model.node_id_property, "JSON($id EXTENDED)")

    @staticmethod
    def _page(page: Page | None) -> list[str]:
        if page is None:
            return []
        return [
            *([f"ORDER BY {page.order_by}"] if page.order_by else []),
            *([f"OFFSET {page.skip} ROWS"] if page.skip is not None else []),
            *([f"FETCH NEXT {page.limit} ROWS ONLY"] if page.limit is not None else []),
        ]


def labels_implied(where: str | None, labels: Sequence[str], equalities: str | None) -> list[str]:
    """
    The labels of `labels` the condition `where`, as the shared module's fragments write it, tests at its top: those of
    whose properties an equality is read through a labelled index there (see module_label_test), as drivine4j's
    `OraclePredicates.predicate` finds them. None for no condition.

    Only a plain equality at the top of the DSL's filter of an indexed property of the label, read through the index
    (`equalities`, see OracleBackend._indexed_equalities), implies it. An ignore-case equality of a lowercased copy
    (`EQ_LOWERED`, see lower_copy_lookups) is read through the same index, but past 4,000 bytes the module's term holds
    for every node, the label's test inside it then not deciding it: the statement tests the label itself. Retires once
    the module keeps the label's test inside that guard (liberation-data/drivine-cypher-spec#373).
    """
    if not where or not equalities:
        return []
    return [
        it
        for it in labels
        if module_label_test(ROW_ALIAS, it) in where and module_label_test(ROW_ALIAS, it) in equalities
    ]
