from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from drivine.annotation.direction import Direction
from drivine.backend.fragment_group import FragmentGroup
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.matched_node import MatchedNode
from drivine.backend.oracle.id_keyed_statement import held_id
from drivine.backend.oracle.oracle_graph import OracleGraph, carries_labels, has_labels, json_key, node_document
from drivine.backend.oracle.oracle_save_builder import prelude
from drivine.backend.save_statement_builder import id_of, object_props
from drivine.backend.sql.node_ids import has_id
from drivine.backend.sql.sql_json import to_json
from drivine.backend.sql.value_layout import held_key, write, write_props
from drivine.drivine_error import DrivineError
from drivine.manager.relate_mode import RelateMode
from drivine.manager.save_options import SaveOptions
from drivine.model import stamps
from drivine.model.fragment_model import FragmentModel
from drivine.query.query_specification import QuerySpecification


class OracleWrites:
    """
    The writes beside a save, each one PL/SQL block over the save block's procedures, so stamps are kept as a save
    keeps them: patch and patch_all, a batch of fragments, and relationships written on their own.
    """

    def __init__(self, graph: OracleGraph) -> None:
        self.graph = graph
        self._pending: dict[str, Any] = {}
        """Bindings the helpers below name, taken by the next block."""
        self._lowers = False
        """Whether the next block writes lowercased copies, and so declares `lower_node`."""

    def patch_one(self, model: FragmentModel, where: str | None = None) -> GraphStatement:
        """Writes `$set` (a merge patch) to the node with id `$id` if it meets `where`: `{found, applied, changed}`."""
        condition = (
            f"SELECT COUNT(*) INTO v_meets FROM {self.graph.nodes} n WHERE n.nid = v_root AND ({where});"
            if where
            else "v_meets := 1;"
        )
        block = self._block(
            ["v_meets NUMBER;", "v_changed BOOLEAN := FALSE;", "v_id VARCHAR2(4000);", "v_patch CLOB;"],
            [
                "v_id := $id;",
                f"v_root := find_node({self._match(model)}, v_id);",
                "IF v_root IS NOT NULL THEN",
                f"    SELECT stamp INTO v_found FROM {self.graph.nodes} WHERE nid = v_root FOR UPDATE;",
                f"    {condition}",
                "    IF v_meets > 0 THEN",
                # EXTENDED, read and written, so each float stays a binary double (see _Patch).
                "        SELECT JSON_SERIALIZE(JSON_MERGEPATCH(JSON($set EXTENDED), "
                "JSON(NVL($vectors, '{}') EXTENDED)) RETURNING CLOB EXTENDED) INTO v_patch FROM dual;",
                "        v_changed := apply_node(v_root, v_patch, '[]', '[]', NULL, FALSE);",
                "        restamp(v_root, v_changed);",
                *(f"        {it}" for it in self._lowering(model, "IF v_changed THEN ", " END IF;")),
                "    END IF;",
                "END IF;",
                "OPEN c FOR SELECT JSON_OBJECT('found' VALUE v_root IS NOT NULL, 'applied' VALUE NVL(v_meets, 0) > 0, "
                "'changed' VALUE v_changed RETURNING CLOB) AS \"result:json\" FROM dual;",
            ],
        )
        return _Patch(block.text, block.bindings)

    def patch_many(self, model: FragmentModel) -> GraphStatement:
        """
        Writes each row of `$rows`, `{id, props, vectors}`, to its node, making none: `{id, found, changed}` per row, in
        the rows' order, each with its row's id as bound (see _RowOrderedPatch).
        """
        block = self._block(
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
                "SELECT JSON_SERIALIZE(id RETURNING CLOB EXTENDED), JSON_SERIALIZE(JSON_MERGEPATCH(NVL(props, "
                "JSON('{}')), NVL(vectors, JSON('{}'))) RETURNING CLOB EXTENDED) BULK COLLECT INTO v_ids, v_patches "
                "FROM JSON_TABLE(JSON($rows EXTENDED), '$[*]' COLUMNS (id JSON PATH '$.id', props JSON PATH '$.props', "
                "vectors JSON PATH '$.vectors'));",
                "FOR i IN 1 .. v_ids.COUNT LOOP",
                f"    v_root := find_node({self._match(model)}, v_ids(i));",
                "    v_changed := FALSE;",
                "    IF v_root IS NOT NULL THEN",
                f"        SELECT stamp INTO v_found FROM {self.graph.nodes} WHERE nid = v_root FOR UPDATE;",
                "        v_changed := apply_node(v_root, v_patches(i), '[]', '[]', NULL, FALSE);",
                "        restamp(v_root, v_changed);",
                *(f"        {it}" for it in self._lowering(model, "IF v_changed THEN ", " END IF;")),
                "    END IF;",
                "    v_out.EXTEND;",
                "    SELECT JSON_SERIALIZE(JSON_OBJECT('id' VALUE JSON(v_ids(i) EXTENDED), "
                "'found' VALUE v_root IS NOT NULL, "
                "'changed' VALUE v_changed)) INTO v_out(v_out.COUNT) FROM dual;",
                "END LOOP;",
                # Rows from a collection, as text: a returned result set with a JSON column gives only its prefetched
                # rows (oracle/node-oracledb#1795).
                'OPEN c FOR SELECT column_value AS "result:json" FROM TABLE(v_out);',
            ],
        )
        return _RowOrderedPatch(block.text, block.bindings)

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
                ids = bind(held_id([id_of(group.model, it) for it in group.objects]))
                body.append(
                    f"SELECT NVL(JSON_ARRAYAGG(JSON_OBJECT('id' VALUE JSON_QUERY(n.props, '${k}'), 'stamp' VALUE "
                    f"NVL(n.stamp, '{stamps.NEVER_STAMPED}')) RETURNING CLOB), '[]') INTO {variable} FROM "
                    f"{self.graph.nodes} n WHERE {has_labels('n', group.model.labels)} AND EXISTS (SELECT 1 FROM "
                    f"JSON_TABLE(JSON({ids} EXTENDED), '$[*]' COLUMNS (v JSON PATH '$')) t WHERE JSON_EXISTS(n.props, "
                    f"'$?(@{k} == $v)' PASSING t.v AS \"v\"));"
                )

        reads("before")
        for group in groups:
            for obj in group.objects:
                props = object_props(group.model, obj, options)
                match = bind(self._match_text(group.model))
                id_ = bind(held_id(id_of(group.model, obj)))
                labels = bind(to_json(group.model.labels))
                held = bind(to_json(write_props(props, "oracle")))
                body.append(f"v_root := write_node({match}, {id_}, {labels}, {held}, '[]', '[]', NULL, FALSE);")
                body.extend(self._lowering(group.model))
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
                "    SELECT COUNT(*), NVL(SUM(CASE WHEN JSON_EQUAL(props, "
                "JSON_MERGEPATCH(props, JSON($x_props EXTENDED))) "
                "THEN 1 ELSE 0 END), 0)",
                f"        INTO v_had, v_same FROM {edges} WHERE src = v_root AND dst = v_to AND type = $x_type;",
                "    IF v_had = 0 THEN",
                f"        INSERT INTO {edges} (src, dst, type, props) VALUES (v_root, v_to, $x_type, "
                "JSON($x_props EXTENDED));",
                "    ELSE",
                f"        UPDATE {edges} SET props = JSON_MERGEPATCH(props, JSON($x_props EXTENDED)) "
                "WHERE src = v_root AND "
                "dst = v_to AND type = $x_type;",
                "    END IF;",
                "    IF v_had = 0 OR v_same < v_had THEN relink(v_root); relink(v_to); END IF;",
                "    v_related := GREATEST(v_had, 1);",
            ]
        else:
            joined = [
                f"    INSERT INTO {edges} (src, dst, type, props) VALUES (v_root, v_to, $x_type, "
                "JSON($x_props EXTENDED));",
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
            {"x_type": type, "x_props": to_json(write_props(written, "oracle"))},
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
            {"x_type": type, "x_from": held_id(from_.id)},
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
        # The node it is from found by its id, as the index over the id is made, its labels told by their text so
        # Oracle reads that index for it (see OracleBackend._found_by_id); the id is read more than once, so bound by
        # name.
        return GraphStatement(
            f'SELECT {node_document("n")} AS "result" FROM {self.graph.nodes} n WHERE n.nid IN ('
            f"SELECT x.nid FROM {self.graph.nodes} a JOIN {self.graph.edges} e ON e.type = $x_type "
            f"JOIN {self.graph.nodes} x ON {join} WHERE "
            f"{has_id('oracle', 'a', _id_property(from_.model), 'JSON($x_from EXTENDED)')} AND "
            f"{carries_labels('a', labels)} AND {has_labels('x', target.labels)})",
            {"x_type": type, "x_from": held_id(from_.id)},
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
            f"{has_labels('n', labels)} AND JSON_EXISTS(n.props, '$?(@{k} == $v)' PASSING JSON(:1 EXTENDED) AS \"v\")"
        )
        self._pending["x_from" if name == "x_frommatch" else "x_to"] = held_id(node.id)
        return f"${name}"

    def _match(self, model: FragmentModel) -> str:
        self._pending["x_match"] = self._match_text(model)
        return "$x_match"

    @staticmethod
    def _match_text(model: FragmentModel) -> str:
        k = json_key(_id_property(model))
        return (
            f"{has_labels('n', model.labels)} AND JSON_EXISTS(n.props, '$?(@{k} == $v)' "
            'PASSING JSON(:1 EXTENDED) AS "v")'
        )

    def _lowering(self, model: FragmentModel, before: str = "", after: str = "") -> list[str]:
        """
        The call that writes the lowercased copies of the model's `CaseInsensitive` properties on the node `v_root`,
        from what it now holds, once its change is told (see lower_copies), between `before` and `after`: none for a
        model with none. The block it is in declares `lower_node`.
        """
        if not model.lower_copied:
            return []
        self._lowers = True
        name = f"x_lower{len(self._pending)}"
        self._pending[name] = to_json([held_key(it) for it in model.lower_copied])
        return [f"{before}lower_node(v_root, ${name});{after}"]

    def _block(
        self, declarations: list[str], body: list[str], bindings: Mapping[str, Any] | None = None
    ) -> GraphStatement:
        lines = "\n".join(f"    {it}" for it in body)
        lowers, self._lowers = self._lowers, False
        text = f"""{prelude(self.graph, declarations, lowers)}
BEGIN
{lines}
    DBMS_SQL.RETURN_RESULT(c);
END;"""
        taken = {**self._pending, **(bindings or {}), "x_stamp": stamps.fresh()}
        self._pending.clear()
        return GraphStatement(text, taken)


@dataclass(frozen=True)
class _Patch(GraphStatement):
    """
    A patch's block, bound with its values as Oracle's store layout holds them (see value_layout), as a save's are:
    `$set` and `$vectors`, or each of `$rows`' `props` and `vectors`, each by its name as the layout holds it, and `$id`
    or each row's id (see held_id). Each float is extended JSON's double, which the block reads `JSON(… EXTENDED)`, so a
    float patched in stays a binary double. A null `$id` is bound as it is, and finds no node.
    """

    def spec(self, more: Mapping[str, Any] | None = None) -> QuerySpecification[Any]:
        given = dict(more or {})
        for name in ("set", "vectors"):
            if given.get(name) is not None:
                given[name] = write_props(given[name], "oracle")
        if given.get("id") is not None:
            given["id"] = held_id(given["id"])
        if given.get("rows") is not None:
            given["rows"] = [
                {
                    **row,
                    "id": write(row.get("id"), "oracle"),
                    **{name: write_props(row[name], "oracle") for name in ("props", "vectors") if row.get(name)},
                }
                for row in given["rows"]
            ]
        return super().spec(given)


@dataclass(frozen=True)
class _RowOrderedPatch(_Patch):
    """
    patch_many's block, each of whose results, one per row of `$rows` in their order, is handed the id of its row as
    the manager gave it, which the manager matches the results to its objects by. The id the block gives back is
    Oracle's text of the id the node holds, plain JSON, where a float is a binary double whose text need not read back
    as the number given (`0.1 + 0.2`): its object would be left with no result.
    """

    def spec(self, more: Mapping[str, Any] | None = None) -> QuerySpecification[Any]:
        rows: list[Any] = list((more or {}).get("rows") or [])
        return super().spec(more).add_post_processors(_InRowOrder(rows))


@dataclass(frozen=True)
class _InRowOrder:
    """Hands each result the id of the row of `$rows` at its place."""

    rows: list[Any]

    def apply(self, results: list[Any]) -> list[Any]:
        return [{**it, "id": self.rows[i].get("id") if i < len(self.rows) else None} for i, it in enumerate(results)]


def _id_property(model: FragmentModel) -> str:
    if not model.node_id_property:
        raise DrivineError(f"{model.type.__name__} has no NodeId property, so a node of it can't be referred to by id.")
    return model.node_id_property


def _assert_type(type: str) -> None:
    if not type or not type.strip():
        raise DrivineError("A relationship needs a type.")
