from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from drivine.annotation.direction import Direction
from drivine.backend.fragment_group import FragmentGroup
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.matched_node import MatchedNode
from drivine.backend.oracle.oracle_graph import OracleGraph, has_labels, json_key, node_document
from drivine.backend.oracle.oracle_save_builder import prelude
from drivine.backend.save_statement_builder import id_of, object_props
from drivine.backend.sql.sql_json import to_json
from drivine.drivine_error import DrivineError
from drivine.manager.relate_mode import RelateMode
from drivine.manager.save_options import SaveOptions
from drivine.model import stamps
from drivine.model.fragment_model import FragmentModel


class OracleWrites:
    """
    The writes beside a save, each one PL/SQL block over the save block's procedures, so stamps are kept as a save
    keeps them: patch and patch_all, a batch of fragments, and relationships written on their own.
    """

    def __init__(self, graph: OracleGraph) -> None:
        self.graph = graph
        self._pending: dict[str, Any] = {}
        """Bindings the helpers below name, taken by the next block."""

    def patch_one(self, model: FragmentModel, where: str | None = None) -> GraphStatement:
        """Writes `$set` (a merge patch) to the node with id `$id` if it meets `where`: `{found, applied, changed}`."""
        condition = (
            f"SELECT COUNT(*) INTO v_meets FROM {self.graph.nodes} n WHERE n.nid = v_root AND ({where});"
            if where
            else "v_meets := 1;"
        )
        return self._block(
            ["v_meets NUMBER;", "v_changed BOOLEAN := FALSE;", "v_id VARCHAR2(4000);", "v_patch CLOB;"],
            [
                "SELECT JSON_SERIALIZE(JSON_SCALAR($id)) INTO v_id FROM dual;",
                f"v_root := find_node({self._match(model)}, v_id);",
                "IF v_root IS NOT NULL THEN",
                f"    SELECT stamp INTO v_found FROM {self.graph.nodes} WHERE nid = v_root FOR UPDATE;",
                f"    {condition}",
                "    IF v_meets > 0 THEN",
                "        SELECT JSON_SERIALIZE(JSON_MERGEPATCH(JSON($set), JSON(NVL($vectors, '{}'))) RETURNING CLOB) "
                "INTO v_patch FROM dual;",
                "        v_changed := apply_node(v_root, v_patch, '[]', '[]', NULL, FALSE);",
                "        restamp(v_root, v_changed);",
                "    END IF;",
                "END IF;",
                "OPEN c FOR SELECT JSON_OBJECT('found' VALUE v_root IS NOT NULL, 'applied' VALUE NVL(v_meets, 0) > 0, "
                "'changed' VALUE v_changed RETURNING CLOB) AS \"result:json\" FROM dual;",
            ],
        )

    def patch_many(self, model: FragmentModel) -> GraphStatement:
        """Writes each row of `$rows`, `{id, props, vectors}`, to its node, making none: `{id, found, changed}`."""
        return self._block(
            [
                "v_changed BOOLEAN;",
                "v_out SYS.ODCIVARCHAR2LIST := SYS.ODCIVARCHAR2LIST();",
                "TYPE texts IS TABLE OF CLOB;",
                "v_ids texts;",
                "v_patches texts;",
            ],
            [
                # Read whole before any write: a cursor loop over the bound rows, with writes in it, stopped after the
                # first node it changed (23.26), silently.
                "SELECT JSON_SERIALIZE(id), JSON_SERIALIZE(JSON_MERGEPATCH(NVL(props, JSON('{}')), "
                "NVL(vectors, JSON('{}'))) RETURNING CLOB) BULK COLLECT INTO v_ids, v_patches FROM "
                "JSON_TABLE($rows, '$[*]' COLUMNS (id JSON PATH '$.id', props JSON PATH '$.props', "
                "vectors JSON PATH '$.vectors'));",
                "FOR i IN 1 .. v_ids.COUNT LOOP",
                f"    v_root := find_node({self._match(model)}, v_ids(i));",
                "    v_changed := FALSE;",
                "    IF v_root IS NOT NULL THEN",
                f"        SELECT stamp INTO v_found FROM {self.graph.nodes} WHERE nid = v_root FOR UPDATE;",
                "        v_changed := apply_node(v_root, v_patches(i), '[]', '[]', NULL, FALSE);",
                "        restamp(v_root, v_changed);",
                "    END IF;",
                "    v_out.EXTEND;",
                "    SELECT JSON_SERIALIZE(JSON_OBJECT('id' VALUE JSON(v_ids(i)), 'found' VALUE v_root IS NOT NULL, "
                "'changed' VALUE v_changed)) INTO v_out(v_out.COUNT) FROM dual;",
                "END LOOP;",
                # Rows from a collection, as text: a returned result set with a JSON column gives only its prefetched
                # rows (oracle/node-oracledb#1795).
                'OPEN c FOR SELECT column_value AS "result:json" FROM TABLE(v_out);',
            ],
        )

    def fragment_batch(self, groups: Sequence[FragmentGroup], options: SaveOptions) -> GraphStatement:
        """
        Saves fragments of several classes, adding to what each node holds and checking no stamp: `{groups: [{before,
        after}]}`, the `{id, stamp}` of each class's nodes before and after.
        """
        bindings: dict[str, Any] = {}
        count = 0

        def bind(value: Any) -> str:
            nonlocal count
            count += 1
            bindings[f"x_{count}"] = value
            return f"$x_{count}"

        declarations: list[str] = []
        body: list[str] = []

        def reads(when: str) -> None:
            for i, group in enumerate(groups):
                k = json_key(_id_property(group.model))
                variable = f"v_{when}{i}"
                if when == "before":
                    declarations.extend([f"{variable} CLOB;", f"v_after{i} CLOB;"])
                ids = bind(to_json([id_of(group.model, it) for it in group.objects]))
                body.append(
                    f"SELECT NVL(JSON_ARRAYAGG(JSON_OBJECT('id' VALUE JSON_QUERY(n.props, '${k}'), 'stamp' VALUE "
                    f"NVL(n.stamp, '{stamps.NEVER_STAMPED}')) RETURNING CLOB), '[]') INTO {variable} FROM "
                    f"{self.graph.nodes} n WHERE {has_labels('n', group.model.labels)} AND EXISTS (SELECT 1 FROM "
                    f"JSON_TABLE({ids}, '$[*]' COLUMNS (v JSON PATH '$')) t WHERE JSON_EXISTS(n.props, "
                    f"'$?(@{k} == $v)' PASSING t.v AS \"v\"));"
                )

        reads("before")
        for group in groups:
            for obj in group.objects:
                props = object_props(group.model, obj, options)
                match = bind(self._match_text(group.model))
                id_ = bind(to_json(id_of(group.model, obj)))
                labels = bind(to_json(group.model.labels))
                body.append(
                    f"v_root := write_node({match}, {id_}, {labels}, {bind(to_json(props))}, '[]', '[]', NULL, FALSE);"
                )
        reads("after")
        result = ", ".join(
            f"JSON_OBJECT('before' VALUE JSON(v_before{i}), 'after' VALUE JSON(v_after{i}) RETURNING JSON)"
            for i in range(len(groups))
        )
        body.append(
            f"OPEN c FOR SELECT JSON_OBJECT('groups' VALUE JSON_ARRAY({result} RETURNING JSON) RETURNING CLOB) "
            'AS "result:json" FROM dual;'
        )
        return self._block(declarations, body, bindings)

    def relate(
        self, from_: MatchedNode, to: MatchedNode, type: str, properties: Mapping[str, Any], mode: RelateMode
    ) -> GraphStatement:
        """Joins `from_` to `to`: how many such relationships there are after, 0 when either node is absent."""
        _assert_type(type)
        written = {key: value for key, value in properties.items() if value is not None}
        edges = self.graph.edges
        if mode is RelateMode.MERGE:
            joined = [
                "    SELECT COUNT(*), NVL(SUM(CASE WHEN JSON_EQUAL(props, JSON_MERGEPATCH(props, JSON($x_props))) "
                "THEN 1 ELSE 0 END), 0)",
                f"        INTO v_had, v_same FROM {edges} WHERE src = v_root AND dst = v_to AND type = $x_type;",
                "    IF v_had = 0 THEN",
                f"        INSERT INTO {edges} (src, dst, type, props) VALUES (v_root, v_to, $x_type, JSON($x_props));",
                "    ELSE",
                f"        UPDATE {edges} SET props = JSON_MERGEPATCH(props, JSON($x_props)) WHERE src = v_root AND "
                "dst = v_to AND type = $x_type;",
                "    END IF;",
                "    IF v_had = 0 OR v_same < v_had THEN relink(v_root); relink(v_to); END IF;",
                "    v_related := GREATEST(v_had, 1);",
            ]
        else:
            joined = [
                f"    INSERT INTO {edges} (src, dst, type, props) VALUES (v_root, v_to, $x_type, JSON($x_props));",
                "    relink(v_root);",
                "    relink(v_to);",
                "    v_related := 1;",
            ]
        return self._block(
            ["v_to NUMBER;", "v_had NUMBER := 0;", "v_same NUMBER := 0;", "v_related NUMBER := 0;"],
            [
                *self._ends(from_, to),
                "IF v_root IS NOT NULL AND v_to IS NOT NULL THEN",
                # Both locked, in the order of their keys, so two relates of one pair don't wait on each other.
                f"    SELECT stamp INTO v_found FROM {self.graph.nodes} WHERE nid = LEAST(v_root, v_to) FOR UPDATE;",
                f"    SELECT stamp INTO v_found FROM {self.graph.nodes} WHERE nid = GREATEST(v_root, v_to) FOR UPDATE;",
                *joined,
                "END IF;",
                'OPEN c FOR SELECT v_related AS "related" FROM dual;',
            ],
            {"x_type": type, "x_props": to_json(written)},
        )

    def unrelate(self, from_: MatchedNode, to: MatchedNode, type: str) -> GraphStatement:
        """Removes every relationship of the type from `from_` to `to`: how many there were."""
        _assert_type(type)
        return self._block(
            ["v_to NUMBER;", "v_removed NUMBER := 0;"],
            [
                *self._ends(from_, to),
                "IF v_root IS NOT NULL AND v_to IS NOT NULL THEN",
                f"    DELETE FROM {self.graph.edges} WHERE src = v_root AND dst = v_to AND type = $x_type;",
                "    v_removed := SQL%ROWCOUNT;",
                "    IF v_removed > 0 THEN relink(v_root); relink(v_to); END IF;",
                "END IF;",
                'OPEN c FOR SELECT v_removed AS "removed" FROM dual;',
            ],
            {"x_type": type},
        )

    def unrelate_all(self, from_: MatchedNode, type: str, direction: Direction) -> GraphStatement:
        """Removes every relationship of the type `from_` has in the direction: how many there were."""
        _assert_type(type)
        if direction is Direction.INCOMING:
            mine = "dst = v_root"
        elif direction is Direction.UNDIRECTED:
            mine = "(src = v_root OR dst = v_root)"
        else:
            mine = "src = v_root"
        edges = self.graph.edges
        return self._block(
            ["v_removed NUMBER := 0;", "v_others SYS.ODCINUMBERLIST;"],
            [
                f"v_root := find_node({self._match_of(from_)}, $x_from);",
                "IF v_root IS NOT NULL THEN",
                f"    SELECT CASE WHEN src = v_root THEN dst ELSE src END BULK COLLECT INTO v_others FROM {edges} "
                f"WHERE type = $x_type AND {mine};",
                f"    DELETE FROM {edges} WHERE type = $x_type AND {mine};",
                "    v_removed := SQL%ROWCOUNT;",
                "    FOR i IN 1 .. v_others.COUNT LOOP relink(v_others(i)); END LOOP;",
                "    IF v_removed > 0 THEN relink(v_root); END IF;",
                "END IF;",
                'OPEN c FOR SELECT v_removed AS "removed" FROM dual;',
            ],
            {"x_type": type, "x_from": to_json(from_.id)},
        )

    def related(self, from_: MatchedNode, type: str, direction: Direction, target: FragmentModel) -> GraphStatement:
        """The nodes of `target` joined to `from_` by the type in the direction, each once, as fragment rows."""
        _assert_type(type)
        if direction is Direction.INCOMING:
            join = "e.dst = a.nid AND x.nid = e.src"
        elif direction is Direction.UNDIRECTED:
            join = "((e.src = a.nid AND x.nid = e.dst) OR (e.dst = a.nid AND x.nid = e.src))"
        else:
            join = "e.src = a.nid AND x.nid = e.dst"
        labels = list(dict.fromkeys([*from_.model.labels, *from_.labels]))
        return GraphStatement(
            f'SELECT {node_document("n")} AS "result" FROM {self.graph.nodes} n WHERE n.nid IN ('
            f"SELECT x.nid FROM {self.graph.nodes} a JOIN {self.graph.edges} e ON e.type = $x_type "
            f"JOIN {self.graph.nodes} x ON {join} WHERE {has_labels('a', labels)} AND "
            f"JSON_EXISTS(a.props, '$?(@{json_key(_id_property(from_.model))} == $v)' PASSING JSON($x_from) AS \"v\") "
            f"AND {has_labels('x', target.labels)})",
            {"x_type": type, "x_from": to_json(from_.id)},
        )

    def _ends(self, from_: MatchedNode, to: MatchedNode) -> list[str]:
        """Finds both ends, into v_root and v_to."""
        return [
            f"v_root := find_node({self._match_of(from_)}, $x_from);",
            f"v_to := find_node({self._match_of(to, 'x_tomatch')}, $x_to);",
        ]

    def _match_of(self, node: MatchedNode, name: str = "x_frommatch") -> str:
        labels = list(dict.fromkeys([*node.model.labels, *node.labels]))
        k = json_key(_id_property(node.model))
        self._pending[name] = (
            f"{has_labels('n', labels)} AND JSON_EXISTS(n.props, '$?(@{k} == $v)' PASSING JSON(:1) AS \"v\")"
        )
        self._pending["x_from" if name == "x_frommatch" else "x_to"] = to_json(node.id)
        return f"${name}"

    def _match(self, model: FragmentModel) -> str:
        self._pending["x_match"] = self._match_text(model)
        return "$x_match"

    @staticmethod
    def _match_text(model: FragmentModel) -> str:
        k = json_key(_id_property(model))
        return f"{has_labels('n', model.labels)} AND JSON_EXISTS(n.props, '$?(@{k} == $v)' PASSING JSON(:1) AS \"v\")"

    def _block(
        self, declarations: list[str], body: list[str], bindings: Mapping[str, Any] | None = None
    ) -> GraphStatement:
        lines = "\n".join(f"    {it}" for it in body)
        text = f"""{prelude(self.graph, declarations)}
BEGIN
{lines}
    DBMS_SQL.RETURN_RESULT(c);
END;"""
        taken = {**self._pending, **(bindings or {}), "x_stamp": stamps.fresh()}
        self._pending.clear()
        return GraphStatement(text, taken)


def _id_property(model: FragmentModel) -> str:
    if not model.node_id_property:
        raise DrivineError(f"{model.type.__name__} has no NodeId property, so a node of it can't be referred to by id.")
    return model.node_id_property


def _assert_type(type: str) -> None:
    if not type or not type.strip():
        raise DrivineError("A relationship needs a type.")
