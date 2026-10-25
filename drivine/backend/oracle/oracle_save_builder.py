from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from drivine.annotation.direction import Direction
from drivine.backend.oracle.oracle_graph import OracleGraph, has_labels, json_key, sql_text
from drivine.backend.save_statement import SaveStatement
from drivine.backend.save_statement_builder import (
    bag_entries,
    held_ids,
    id_of,
    items,
    node_of,
    object_props,
    props_of,
    replaced_fields,
    root_of,
)
from drivine.backend.sql.sql_json import to_json
from drivine.backend.sql.sql_saves import fragment_of, stamp_excluded
from drivine.backend.view_projector import target
from drivine.drivine_error import DrivineError
from drivine.manager.null_policy import NullPolicy
from drivine.manager.removed_targets import RemovedTargets
from drivine.manager.save_options import SaveOptions
from drivine.model import stamps
from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.model_registry import RESERVED_PREFIX, ModelRegistry
from drivine.model.relationship_fragment_model import RelationshipFragmentModel
from drivine.model.relationship_model import RelationshipModel
from drivine.model.target_model import TargetModel


@dataclass
class _NodeWrite:
    """What writing one node takes: how to find it, and the labels and properties to give it."""

    model: FragmentModel
    id: Any
    labels: list[str]
    """The labels it is made with, when it is not there."""
    add: list[str]
    remove: list[str]
    props: dict[str, Any]
    """A merge patch: a null removes the property."""
    record: tuple[str, bool] | None = None
    """For an open NodeLabels field: the property recording the labels it wrote, and whether it is replaced."""


@dataclass
class _Stamped:
    model: FragmentModel
    ids: list[Any] = field(default_factory=lambda: [])


class OracleSaveBuilder:
    """
    Writes a save as one PL/SQL block, applied whole or not at all, with or without a transaction: the root found (or
    made) and locked, checked against the object's stamp when it carries one, its properties merged and its labels
    set; the relationships named in `replace` trimmed; then each related node written and joined, as deep as the view
    goes. Every value is bound. Stamps follow drivine4j's rules, as the Cypher save does. The block returns
    `{found, now, related}` as one row, or no row when a checked save found the root changed or gone (or a create found
    it there), having written nothing.
    """

    def __init__(self, graph: OracleGraph, registry: ModelRegistry | None = None) -> None:
        self.graph = graph
        self.registry = registry
        self._bindings: dict[str, Any] = {}
        self._body: list[str] = []
        self._declarations: list[str] = []
        self._stamped: list[_Stamped] = []
        self._count = 0
        self._options = SaveOptions()

    def build(
        self, target_: TargetModel, obj: Any, options: SaveOptions | None = None, create_only: bool = False
    ) -> SaveStatement:
        self._options = options or SaveOptions()
        self._bindings["x_stamp"] = stamps.fresh()
        if isinstance(target_, FragmentModel):
            if self._options.replace == "all" or self._options.replace:
                raise DrivineError(
                    f"replace names relationship fields of a view. {target_.type.__name__} is a NodeFragment."
                )
            expected = self._root(self._concrete(target_, obj), obj, create_only, False)
            return self._statement(target_, obj, expected)
        view = target_
        root_object = getattr(obj, view.root_field, None)
        if root_object is None:
            raise DrivineError(
                f"{view.type.__name__}.{view.root_field}, the root, is None. A view is saved from its root."
            )
        replaced = replaced_fields(view, obj, self._options)
        root = self._concrete(view.root, root_object)
        expected = self._root(root, root_object, create_only, len(replaced) > 0)
        writes: list[str] = []
        self._related(view, obj, "v_root", [], writes)
        self._body.extend(self._read_stamps("before"))
        self._trims(view, obj, replaced)
        self._body.extend(writes)
        self._body.extend(self._read_stamps("after"))
        return self._statement(root, root_object, expected)

    def _root(self, model: FragmentModel, obj: Any, create_only: bool, replacing: bool) -> str | None:
        """Finds or makes the root, checks it, and writes it. Returns the stamp it was checked against, if any."""
        only, except_ = self._options.only, self._options.except_

        def keep(name: str) -> bool:
            return name in only if only is not None else not (except_ and name in except_)

        clear = self._options.null_policy is NullPolicy.CLEAR
        held = self._options.held_properties
        props = dict(object_props(model, obj, self._options))
        props.update(bag_entries(model, obj, list(held), clear, keep))
        labels_clear = clear if model.node_labels and keep(model.node_labels.field) else None
        write = self._node_write(model, obj, props, labels_clear, held)
        expected = getattr(obj, model.stamp_field, None) if model.stamp_field else None
        p = self._node_params(write)
        if create_only and expected is not None:
            raise DrivineError(
                f"A {model.type.__name__} that carries a stamp was loaded, and create makes a node that isn't there. "
                "Save it, or update it."
            )
        self._body.append(f"v_root := find_node({p['match']}, {p['id']});")
        if create_only:
            self._body.append("IF v_root IS NOT NULL THEN v_ok := FALSE; END IF;")
        elif expected is not None:
            self._bindings["x_expected"] = expected
            t = stamps.TOKEN
            links = (
                f" AND LENGTH($x_expected) = {2 * t + 1} AND SUBSTR(v_found, {t + 2}) = SUBSTR($x_expected, {t + 2})"
                if replacing
                else ""
            )
            self._body.extend(
                [
                    "IF v_root IS NULL THEN v_ok := FALSE; ELSE",
                    f"    SELECT stamp INTO v_found FROM {self.graph.nodes} WHERE nid = v_root FOR UPDATE;",
                    f"    IF NOT (NVL(SUBSTR(v_found, 1, {t}) = SUBSTR($x_expected, 1, {t}), FALSE){links}) THEN "
                    "v_ok := FALSE; END IF;",
                    "END IF;",
                ]
            )
        self._body.extend(
            [
                "IF v_ok THEN",
                "    IF v_root IS NULL THEN",
                f"        v_root := make_node({p['labels']});",
                "        v_made := TRUE;",
                "    ELSIF v_found IS NULL THEN",
                f"        SELECT NVL(stamp, '{stamps.NEVER_STAMPED}') INTO v_found FROM {self.graph.nodes} "
                "WHERE nid = v_root FOR UPDATE;",
                "    END IF;",
                f"    restamp(v_root, apply_node(v_root, {p['props']}, {p['add']}, {p['remove']}, {p['record']}, "
                f"{p['replace_record']}) OR v_made);",
                "END IF;",
            ]
        )
        return expected

    def _related(
        self, view: GraphViewModel, obj: Any, from_: str, path: list[RelationshipModel], out: list[str]
    ) -> None:
        """
        Writes each relationship of a view, from the node `from_` (a PL/SQL variable), into `out`: each related node
        written, then joined; a related view's relationships after, as far as max_depth.
        """
        for rel in view.relationships:
            if rel.read_only or sum(1 for it in path if it is rel) >= rel.max_depth:
                continue
            into = target(rel)
            for item in items(rel, getattr(obj, rel.field, None)):
                node = node_of(rel, item)
                if node is None:
                    raise DrivineError(f"A {rel.related.type.__name__} in {rel.field} has no target node.")
                node_object = getattr(node, into.root_field, None) if isinstance(into, GraphViewModel) else node
                root = root_of(into)
                concrete = self._concrete(root, node_object)
                self._stamped_id(root, id_of(root, node_object))
                props = dict(props_of(concrete, node_object, stamp_excluded(concrete)))
                props.update(bag_entries(concrete, node_object))
                write = self._node_write(
                    concrete, node_object, props, False if concrete.node_labels else None, {}, root
                )
                p = self._node_params(write)
                variable = self._variable()
                out.append(
                    f"{variable} := write_node({p['match']}, {p['id']}, {p['labels']}, {p['props']}, {p['add']}, "
                    f"{p['remove']}, {p['record']}, {p['replace_record']});"
                )
                related = rel.related
                rel_props = (
                    self._bind(to_json(props_of(related, item, [related.target_field])))
                    if isinstance(related, RelationshipFragmentModel)
                    else "NULL"
                )
                out.append(
                    f"link({from_}, {variable}, {sql_text(rel.type)}, '{_direction(rel.direction)}', {rel_props});"
                )
                if isinstance(into, GraphViewModel):
                    self._related(into, node, variable, [*path, rel], out)

    def _trims(self, view: GraphViewModel, obj: Any, replaced: list[str]) -> None:
        """
        Trims each relationship field named in `replace` to what the object holds: relationships of its type and
        direction from the root to nodes of its labels, other than those any field of that type and direction holds.
        """
        held = held_ids(view, obj)
        for field_ in replaced:
            rel = next((it for it in view.relationships if it.field == field_), None)
            if rel is None:
                raise DrivineError(f"replace names {field_}, which isn't a relationship field of {view.type.__name__}.")
            if rel.read_only:
                raise DrivineError(
                    f"replace names {view.type.__name__}.{field_}, which is read-only: a save never writes it."
                )
            value = getattr(obj, field_, None)
            if rel.is_collection and value is None:
                raise DrivineError(
                    f"{view.type.__name__}.{field_} is None, so it can't say which relationships to keep. Give an "
                    "empty list to remove them all."
                )
            into = root_of(target(rel))
            keep = [
                id_of(root_of(target(it)), fragment_of(it, item))
                for it in view.relationships
                if it.type == rel.type and it.direction is rel.direction
                for item in items(it, getattr(obj, it.field, None))
            ]
            assert into.node_id_property is not None
            k = json_key(into.node_id_property)

            def not_in(listed: str, k: str = k) -> str:
                return (
                    f"NOT EXISTS (SELECT 1 FROM JSON_TABLE({listed}, '$[*]' COLUMNS (v JSON PATH '$')) t "
                    f"WHERE JSON_EXISTS(x.props, '$?(@{k} == $v)' PASSING t.v AS \"v\"))"
                )

            if rel.direction is Direction.INCOMING:
                on, root = "x.nid = e.src", "e.dst = q.r"
            elif rel.direction is Direction.UNDIRECTED:
                on, root = "((e.src = q.r AND x.nid = e.dst) OR (e.dst = q.r AND x.nid = e.src))", "1 = 1"
            else:
                on, root = "x.nid = e.dst", "e.src = q.r"
            # The root's id once, from a row of its own: a dynamic statement's binds are by position.
            dropped = (
                f"SELECT e.eid, x.nid FROM (SELECT :1 AS r FROM dual) q CROSS JOIN {self.graph.edges} e "
                f"JOIN {self.graph.nodes} x ON {on} "
                f"WHERE {root} AND e.type = {sql_text(rel.type)} AND {has_labels('x', into.labels)} AND {not_in(':2')}"
            )
            unreferenced = (
                f"SELECT COUNT(*) FROM {self.graph.nodes} x WHERE x.nid = :1 AND {not_in(':2')} AND NOT EXISTS "
                f"(SELECT 1 FROM {self.graph.edges} r WHERE {_referenced_as(rel.direction)})"
                if self._options.removed_targets is RemovedTargets.DELETE_UNREFERENCED
                else None
            )
            self._body.append(
                f"trim_edges(v_root, {self._bind(dropped)}, {self._bind(to_json(keep))}, "
                f"{self._bind(unreferenced) if unreferenced else 'NULL'}, {self._bind(to_json(held))});"
            )

    def _read_stamps(self, when: str) -> list[str]:
        """Reads `{id, stamp}` of each stamped related node, before or after the writes, into a variable each."""
        reads: list[str] = []
        for i, it in enumerate(self._stamped):
            assert it.model.node_id_property is not None
            k = json_key(it.model.node_id_property)
            variable = f"v_{when}{i}"
            if f"{variable} CLOB;" not in self._declarations:
                self._declarations.append(f"{variable} CLOB;")
            reads.append(
                f"SELECT NVL(JSON_ARRAYAGG(JSON_OBJECT('id' VALUE JSON_QUERY(n.props, '${k}'), 'stamp' VALUE "
                f"NVL(n.stamp, '{stamps.NEVER_STAMPED}')) RETURNING CLOB), '[]') INTO {variable} FROM "
                f"{self.graph.nodes} n WHERE {has_labels('n', it.model.labels)} AND EXISTS (SELECT 1 FROM "
                f"JSON_TABLE({self._bind(to_json(it.ids))}, '$[*]' COLUMNS (v JSON PATH '$')) t WHERE "
                f"JSON_EXISTS(n.props, '$?(@{k} == $v)' PASSING t.v AS \"v\"));"
            )
        return reads

    def _statement(self, root: FragmentModel, root_object: Any, expected: str | None) -> SaveStatement:
        if self._stamped:
            pairs = ", ".join(
                f"JSON_OBJECT('before' VALUE JSON(v_before{i}), 'after' VALUE JSON(v_after{i}) RETURNING JSON)"
                for i in range(len(self._stamped))
            )
            related = f"JSON_ARRAY({pairs} RETURNING JSON)"
        else:
            related = "JSON('[]')"
        result = (
            "SELECT JSON_OBJECT('found' VALUE CASE WHEN v_made THEN NULL ELSE v_found END, "
            f"'now' VALUE (SELECT stamp FROM {self.graph.nodes} WHERE nid = v_root), 'related' VALUE {related} "
            'RETURNING CLOB) AS "result:json" FROM dual'
        )
        body = "\n".join(f"    {it}" for it in self._body)
        text = f"""{prelude(self.graph, self._declarations)}
BEGIN
{body}
    IF v_ok THEN
        OPEN c FOR {result};
    ELSE
        OPEN c FOR SELECT 1 AS "result" FROM dual WHERE 1 = 0;
    END IF;
    DBMS_SQL.RETURN_RESULT(c);
END;"""
        self._bindings["root"] = {"id": id_of(root, root_object)}
        return SaveStatement.of(text, self._bindings, root, root_object, expected, [it.model for it in self._stamped])

    def _node_write(
        self,
        model: FragmentModel,
        obj: Any,
        props: dict[str, Any],
        labels_clear: bool | None,
        held: Mapping[str, Any],
        found: FragmentModel | None = None,
    ) -> _NodeWrite:
        """The write of a node: found by the labels of `found` (by default its own class's) and its id."""
        found = found or model
        write = _NodeWrite(
            found,
            id_of(model, obj),
            list(model.labels),
            [] if model is found else [it for it in model.labels if it not in found.labels],
            [],
            props,
        )
        if labels_clear is not None and model.node_labels:
            labels_field = model.node_labels
            labels = list(dict.fromkeys(getattr(obj, labels_field.field, None) or []))
            outside = [it for it in labels if it not in labels_field.members] if labels_field.members else []
            if outside:
                raise DrivineError(
                    f"{model.type.__name__}.{labels_field.field} holds {', '.join(outside)}, which its labels don't "
                    "include."
                )
            if any(not isinstance(it, str) or it.startswith(RESERVED_PREFIX) for it in labels):
                raise DrivineError(
                    f"{model.type.__name__}.{labels_field.field} holds a label that isn't a name, or begins "
                    f"{RESERVED_PREFIX!r}."
                )
            owned: Sequence[str] = labels_field.members or held.get(labels_field.record_property) or []
            write.add.extend(labels)
            write.labels.extend(labels)
            write.remove = [it for it in owned if it not in labels and it not in model.labels] if labels_clear else []
            if labels_field.members is None:
                write.record = (labels_field.record_property, labels_clear)
        return write

    def _node_params(self, write: _NodeWrite) -> dict[str, str]:
        assert write.model.node_id_property is not None
        k = json_key(write.model.node_id_property)
        labels = has_labels("n", write.model.labels)
        match = f"{labels} AND JSON_EXISTS(n.props, '$?(@{k} == $v)' PASSING JSON(:1) AS \"v\")"
        return {
            "match": self._bind(match),
            "id": self._bind(to_json(write.id)),
            "labels": self._bind(to_json(list(dict.fromkeys(write.labels)))),
            "props": self._bind(to_json(write.props)),
            "add": self._bind(to_json(write.add)),
            "remove": self._bind(to_json(write.remove)),
            "record": self._bind(write.record[0]) if write.record else "NULL",
            "replace_record": "TRUE" if write.record and write.record[1] else "FALSE",
        }

    def _concrete(self, target_: FragmentModel, obj: Any) -> FragmentModel:
        """The model of the object's own class, where it is a subclass of the field's target."""
        kind: Any = obj.__class__
        if self.registry is None or kind is target_.type or self.registry.kind_of(kind) != "NODE_FRAGMENT":
            return target_
        return self.registry.fragment(kind)

    def _stamped_id(self, model: FragmentModel, id_: Any) -> None:
        if not model.stamp_field:
            return
        stamped = next((it for it in self._stamped if it.model is model), None)
        if stamped is None:
            stamped = _Stamped(model)
            self._stamped.append(stamped)
        if id_ not in stamped.ids:
            stamped.ids.append(id_)

    def _variable(self) -> str:
        self._count += 1
        name = f"v_n{self._count}"
        self._declarations.append(f"{name} NUMBER;")
        return name

    def _bind(self, value: Any) -> str:
        self._count += 1
        name = f"x_{self._count}"
        self._bindings[name] = value
        return f"${name}"


def prelude(graph: OracleGraph, declarations: Sequence[str]) -> str:
    """
    The declarations every writing block holds: its state, and the procedures its body calls. The offered stamp is
    bound as `$x_stamp`.
    """
    nodes = graph.nodes
    edges = graph.edges
    t = stamps.TOKEN
    declared = "\n".join(f"    {it}" for it in declarations)
    return f"""DECLARE
    v_offered VARCHAR2(40) := $x_stamp;
    v_root NUMBER;
    v_found VARCHAR2(40);
    v_made BOOLEAN := FALSE;
    v_ok BOOLEAN := TRUE;
    c SYS_REFCURSOR;
{declared}

    FUNCTION two_tokens(s VARCHAR2) RETURN BOOLEAN IS
    BEGIN
        RETURN s IS NOT NULL AND LENGTH(s) = {2 * t + 1} AND SUBSTR(s, {t + 1}, 1) = ':';
    END;

    PROCEDURE restamp(p NUMBER, p_changed BOOLEAN) IS
        s VARCHAR2(40);
    BEGIN
        SELECT stamp INTO s FROM {nodes} WHERE nid = p;
        IF p_changed OR NOT two_tokens(s) THEN
            IF two_tokens(s) THEN s := SUBSTR(v_offered, 1, {t}) || SUBSTR(s, {t + 1});
            ELSE s := v_offered; END IF;
            UPDATE {nodes} SET stamp = s WHERE nid = p;
        END IF;
    END;

    PROCEDURE relink(p NUMBER) IS
        s VARCHAR2(40);
    BEGIN
        SELECT stamp INTO s FROM {nodes} WHERE nid = p;
        IF two_tokens(s) THEN s := SUBSTR(s, 1, {t + 1}) || SUBSTR(v_offered, {t + 2});
        ELSE s := v_offered; END IF;
        UPDATE {nodes} SET stamp = s WHERE nid = p;
    END;

    FUNCTION find_node(p_match VARCHAR2, p_id VARCHAR2) RETURN NUMBER IS
        ids SYS.ODCINUMBERLIST;
    BEGIN
        EXECUTE IMMEDIATE 'SELECT nid FROM {nodes} n WHERE ' || p_match BULK COLLECT INTO ids USING p_id;
        IF ids.COUNT > 1 THEN
            RAISE_APPLICATION_ERROR(-20001, ids.COUNT || ' nodes have the id ' || p_id || '. An id is one node''s.');
        END IF;
        RETURN CASE WHEN ids.COUNT = 1 THEN ids(1) END;
    END;

    FUNCTION make_node(p_labels VARCHAR2) RETURN NUMBER IS
        v NUMBER;
    BEGIN
        INSERT INTO {nodes} (labels, props) VALUES (JSON(p_labels), JSON('{{}}')) RETURNING nid INTO v;
        RETURN v;
    END;

    FUNCTION has(a JSON_ARRAY_T, s VARCHAR2) RETURN BOOLEAN IS
    BEGIN
        FOR i IN 0 .. a.get_size - 1 LOOP
            IF a.get_string(i) = s THEN RETURN TRUE; END IF;
        END LOOP;
        RETURN FALSE;
    END;

    -- Merges the patch into the node's properties, adds and removes labels, and records an open label field's labels.
    -- Whether any of it changed the node.
    FUNCTION apply_node(p NUMBER, p_props CLOB, p_add VARCHAR2, p_remove VARCHAR2, p_record VARCHAR2, p_replace BOOLEAN)
        RETURN BOOLEAN IS
        old_props JSON;
        new_props JSON;
        old_labels JSON;
        labels JSON_ARRAY_T;
        kept JSON_ARRAY_T := JSON_ARRAY_T();
        additions JSON_ARRAY_T := JSON_ARRAY_T.parse(p_add);
        removals JSON_ARRAY_T := JSON_ARRAY_T.parse(p_remove);
        props JSON_OBJECT_T;
        recorded JSON_ARRAY_T;
        same NUMBER;
        relabelled BOOLEAN := FALSE;
        kept_text VARCHAR2(4000);
    BEGIN
        SELECT props, labels INTO old_props, old_labels FROM {nodes} WHERE nid = p FOR UPDATE;
        SELECT JSON_MERGEPATCH(old_props, JSON(p_props)) INTO new_props FROM dual;
        labels := JSON_ARRAY_T.parse(JSON_SERIALIZE(old_labels));
        FOR i IN 0 .. labels.get_size - 1 LOOP
            IF has(removals, labels.get_string(i)) THEN relabelled := TRUE; ELSE kept.append(labels.get_string(i)); END IF;
        END LOOP;
        FOR i IN 0 .. additions.get_size - 1 LOOP
            IF NOT has(kept, additions.get_string(i)) THEN kept.append(additions.get_string(i)); relabelled := TRUE; END IF;
        END LOOP;
        IF p_record IS NOT NULL THEN
            props := JSON_OBJECT_T.parse(JSON_SERIALIZE(new_props RETURNING CLOB));
            IF p_replace OR NOT props.has(p_record) THEN recorded := JSON_ARRAY_T();
            ELSE recorded := props.get_array(p_record); END IF;
            FOR i IN 0 .. additions.get_size - 1 LOOP
                IF NOT has(recorded, additions.get_string(i)) THEN recorded.append(additions.get_string(i)); END IF;
            END LOOP;
            props.put(p_record, recorded);
            new_props := JSON(props.to_clob());
        END IF;
        SELECT CASE WHEN JSON_EQUAL(old_props, new_props) THEN 1 ELSE 0 END INTO same FROM dual;
        -- An object type's methods are PL/SQL's: called in SQL, they are refused (ORA-40573).
        kept_text := kept.to_string();
        UPDATE {nodes} SET props = new_props, labels = JSON(kept_text) WHERE nid = p;
        RETURN same = 0 OR relabelled;
    END;

    FUNCTION write_node(p_match VARCHAR2, p_id VARCHAR2, p_labels VARCHAR2, p_props CLOB, p_add VARCHAR2,
        p_remove VARCHAR2, p_record VARCHAR2, p_replace BOOLEAN) RETURN NUMBER IS
        v NUMBER := find_node(p_match, p_id);
        made BOOLEAN := v IS NULL;
    BEGIN
        IF made THEN v := make_node(p_labels); END IF;
        restamp(v, apply_node(v, p_props, p_add, p_remove, p_record, p_replace) OR made);
        RETURN v;
    END;

    -- Joins two nodes by a relationship of the type, unless one is there (in either direction, for BOTH), and merges
    -- its properties. Both ends are marked when one is made or its properties change.
    PROCEDURE link(p_from NUMBER, p_to NUMBER, p_type VARCHAR2, p_dir VARCHAR2, p_props CLOB) IS
        e NUMBER;
        old_props JSON;
        new_props JSON;
        same NUMBER;
    BEGIN
        BEGIN
            SELECT eid, props INTO e, old_props FROM {edges}
            WHERE type = p_type AND ((p_dir IN ('OUT', 'BOTH') AND src = p_from AND dst = p_to)
                OR (p_dir IN ('IN', 'BOTH') AND src = p_to AND dst = p_from))
            FETCH FIRST 1 ROW ONLY;
        EXCEPTION WHEN NO_DATA_FOUND THEN e := NULL;
        END;
        IF e IS NULL THEN
            INSERT INTO {edges} (src, dst, type, props) VALUES (CASE WHEN p_dir = 'IN' THEN p_to ELSE p_from END,
                CASE WHEN p_dir = 'IN' THEN p_from ELSE p_to END, p_type, JSON(NVL(p_props, '{{}}')));
            relink(p_from);
            relink(p_to);
        ELSIF p_props IS NOT NULL THEN
            SELECT JSON_MERGEPATCH(old_props, JSON(p_props)) INTO new_props FROM dual;
            SELECT CASE WHEN JSON_EQUAL(old_props, new_props) THEN 1 ELSE 0 END INTO same FROM dual;
            IF same = 0 THEN
                UPDATE {edges} SET props = new_props WHERE eid = e;
                relink(p_from);
                relink(p_to);
            END IF;
        END IF;
    END;

    -- Removes the relationships p_dropped selects from the root, marks both ends, and deletes each removed node
    -- p_unreferenced counts as no longer referred to.
    PROCEDURE trim_edges(p_root NUMBER, p_dropped VARCHAR2, p_keep VARCHAR2, p_unreferenced VARCHAR2, p_held VARCHAR2) IS
        eids SYS.ODCINUMBERLIST;
        nids SYS.ODCINUMBERLIST;
        unreferenced NUMBER;
    BEGIN
        EXECUTE IMMEDIATE p_dropped BULK COLLECT INTO eids, nids USING p_root, p_keep;
        FOR i IN 1 .. eids.COUNT LOOP
            DELETE FROM {edges} WHERE eid = eids(i);
        END LOOP;
        FOR i IN 1 .. nids.COUNT LOOP
            relink(nids(i));
        END LOOP;
        IF eids.COUNT > 0 THEN relink(p_root); END IF;
        IF p_unreferenced IS NOT NULL THEN
            FOR i IN 1 .. nids.COUNT LOOP
                IF nids(i) <> p_root THEN
                    EXECUTE IMMEDIATE p_unreferenced INTO unreferenced USING nids(i), p_held;
                    IF unreferenced > 0 THEN DELETE FROM {nodes} WHERE nid = nids(i); END IF;
                END IF;
            END LOOP;
        END IF;
    END;"""


def _direction(value: Direction) -> str:
    if value is Direction.INCOMING:
        return "IN"
    return "BOTH" if value is Direction.UNDIRECTED else "OUT"


def _referenced_as(value: Direction) -> str:
    """A test over the relationship row `r` that holds when it still refers to the node `x` as the field did."""
    if value is Direction.INCOMING:
        return "r.src = x.nid"
    if value is Direction.UNDIRECTED:
        return "(r.src = x.nid OR r.dst = x.nid)"
    return "r.dst = x.nid"
