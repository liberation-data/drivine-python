from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from enum import Enum
from typing import Any

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from drivine.annotation.direction import Direction
from drivine.backend.cypher import label_expression, quote_name
from drivine.backend.cypher_grammar import CypherGrammar
from drivine.backend.cypher_stamps import EXPECTED, LOCK, MADE, STAMP, changes, relink, restamp
from drivine.backend.save_statement import SaveStatement
from drivine.backend.vector_writes import split_vectors, vector_clauses
from drivine.drivine_error import DrivineError
from drivine.manager.null_policy import NullPolicy
from drivine.manager.removed_targets import RemovedTargets
from drivine.manager.save_options import SaveOptions
from drivine.model import stamps
from drivine.model.field_mapping import FieldMapping
from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.model_registry import RESERVED_PREFIX, ModelRegistry
from drivine.model.relationship_fragment_model import RelationshipFragmentModel
from drivine.model.relationship_model import RelationshipModel
from drivine.model.target_model import TargetModel


@dataclass
class _StampedNodes:
    """A stamped fragment among the related nodes a save writes, and its place in the statement."""

    index: int
    model: FragmentModel


@dataclass
class _WritePart:
    """One FOREACH of the statement: the relationship it writes, and the FOREACHes nested in it for a related view."""

    key: str
    rel: RelationshipModel
    children: list[_WritePart] = field(default_factory=lambda: [])


@dataclass
class _Bagged:
    model: FragmentModel
    id: Any
    object: Any


class SaveStatementBuilder:
    """
    Writes a save as one statement, so that it is applied whole or not at all, with or without a transaction, as
    TypeScript's Drivine writes it.

    The root is merged on its id and given the object's properties that aren't None. A view's relationships are then
    written with a FOREACH each, over a parameter holding a row for each related node, so the statement's text doesn't
    grow with a list. A related view nests its own FOREACHes, as far as the view's max_depth. Relationships named in
    `replace` are first trimmed to what the object holds.

    Every node written is stamped (see drivine.model.stamps): its data token is replaced when its properties change,
    and the links token of each end when a relationship is made, removed, or has its properties changed. A root whose
    object carries a stamp is checked: its data token, and its links token when relationships are replaced, must be as
    the object's.
    """

    def __init__(self, grammar: CypherGrammar, registry: ModelRegistry | None = None) -> None:
        self.grammar = grammar
        self.registry = registry
        self._count = 0
        self._bindings: dict[str, Any] = {}
        self._stamped: list[_StampedNodes] = []
        self._bagged: list[_Bagged] = []
        self._subtypes: dict[int, list[FragmentModel]] = {}
        self._options = SaveOptions()
        self._create_only = False

    @property
    def _carry(self) -> str:
        """What every WITH after the root carries: the root, the stamp it was found with, and related stamps read."""
        return ", ".join(["n", "_found", *(f"before{it.index}" for it in self._stamped)])

    def build(
        self, target: TargetModel, obj: Any, options: SaveOptions | None = None, create_only: bool = False
    ) -> SaveStatement:
        """
        `create_only` writes only if the root isn't there: a root that is, or that another writer makes at the same
        moment, stops the statement before it writes anything, and it returns no row.
        """
        self._bindings["_stamp"] = stamps.fresh()
        self._options = options or SaveOptions()
        self._create_only = create_only
        if isinstance(target, FragmentModel):
            if self._options.replace == "all" or self._options.replace:
                raise DrivineError(
                    f"replace names relationship fields of a view. {target.type.__name__} is a NodeFragment."
                )
            head = self._root(target, obj, False)
            return self._statement([head, f"RETURN {{found: _found, now: n.{STAMP}}} AS result"], target, obj)
        view = target
        root = getattr(obj, view.root_field, None)
        if root is None:
            raise DrivineError(
                f"{view.type.__name__}.{view.root_field}, the root, is None. A view is saved from its root."
            )
        replaced = self._replaced_fields(view, obj)
        # A root of a subclass of the view's root type is written with its own labels.
        parts = [self._root(self._concrete_model(view.root, root), root, len(replaced) > 0)]
        writes = self._write_parts(view, [])
        for part in writes:
            self._bindings[part.key] = self._rows(part, getattr(obj, part.rel.field, None))
        parts.extend(self._read_stamps("before"))
        parts.extend(self._removals(view, obj, replaced, self._options.removed_targets))
        parts.append(f"WITH {self._carry}")
        for part in writes:
            parts.append(self._for_each(part, "n", f"${part.key}", "NODES_AND_EDGES"))
        # Relationship properties are set in a second pass, after a WITH. FalkorDB can't bind a relationship merged
        # earlier in the same clause (FalkorDB/FalkorDB#3388), as a nested view that leads back along its parent's
        # relationship does.
        with_properties = [it for it in writes if _leads_to_relationship_fragment(it)]
        if with_properties:
            parts.append(f"WITH {self._carry}")
            parts.extend(self._for_each(it, "n", f"${it.key}", "EDGE_PROPERTIES") for it in with_properties)
        parts.extend(self._bagged_parts())
        parts.extend(self._read_stamps("after"))
        related = ", ".join(f"{{before: before{it.index}, after: after{it.index}}}" for it in self._stamped)
        parts.append(f"RETURN {{found: _found, now: n.{STAMP}, related: [{related}]}} AS result")
        return self._statement(parts, self._concrete_model(view.root, root), root)

    def _replaced_fields(self, view: GraphViewModel, obj: Any) -> list[str]:
        """
        The relationship fields to trim: those named, or every one of the view's for "all", which needs an object that
        carries a stamp, since the lists of an object built from scratch are its defaults and not what the store holds.
        """
        replace_ = self._options.replace
        if replace_ != "all":
            return list(replace_ or [])
        root = getattr(obj, view.root_field, None)
        if not view.root.stamp_field or getattr(root, view.root.stamp_field, None) is None:
            raise DrivineError(
                f'replace="all" needs a {view.type.__name__} whose root carries a stamp: an object built from scratch '
                f"holds its defaults, not what the store holds. Load it first, or name the fields to replace."
            )
        return [it.field for it in view.relationships if not it.read_only]

    def _read_stamps(self, when: str) -> list[str]:
        """
        Reads the stamp of each stamped related node the save writes, as `{id, stamp}` maps: `before` the writes, into
        `before<i>`, carried to the end, and `after`, into `after<i>`. A node missing from `before` is one the save
        made.
        """
        parts: list[str] = []
        for it in self._stamped:
            a = f"s{when[0]}{it.index}"
            # Before, only the reads made so far are there to carry.
            if when == "before":
                carried = ", ".join(["n", "_found", *(f"before{done.index}" for done in self._stamped[: it.index])])
            else:
                carried = self._carry
            id_prop = quote_name(_id_property(it.model))
            parts.extend(
                [
                    f"WITH {carried}",
                    f"OPTIONAL MATCH ({a}{label_expression(it.model.labels)}) "
                    f"WHERE {a}.{id_prop} IN $stampIds{it.index}",
                    f"WITH {carried}, collect(CASE WHEN {a} IS NOT NULL THEN {{id: {a}.{id_prop}, "
                    f"stamp: coalesce({a}.{STAMP}, '{stamps.NEVER_STAMPED}')}} END) AS {when}{it.index}",
                ]
            )
        return parts

    def _statement(self, parts: list[str], root: FragmentModel, root_object: Any) -> SaveStatement:
        return SaveStatement.of(
            "\n".join(parts),
            self._bindings,
            root,
            root_object,
            self._bindings.get("_expected"),
            [it.model for it in self._stamped],
        )

    def _root(self, model: FragmentModel, obj: Any, replacing: bool) -> str:
        """
        Writes the root, and binds `_found` to the stamp it carried: '' when the save made it, '-' when it had none. An
        object that carries a stamp is checked: the root must be there with the object's data token, and with its links
        token too when `replacing`. Otherwise the statement goes no further, having written nothing.
        """
        props, vectors_ = split_vectors(model, object_props(model, obj, self._options))
        self._bindings["root"] = {"id": id_of(model, obj), "props": props, "vectors": vectors_}
        cleared = [name for name, value in vectors_.items() if value is None]
        vector_sets, vector_changes = vector_clauses(model, self.grammar, "n", "$root.vectors", cleared)
        labels = label_expression(model.labels)
        id_ = f"{{{quote_name(_id_property(model))}: $root.id}}"
        only, except_ = self._options.only, self._options.except_

        def keep(name: str) -> bool:
            return name in only if only is not None else not (except_ and name in except_)

        clear = self._options.null_policy is NullPolicy.CLEAR
        held = self._options.held_properties
        entries = bag_entries(
            replace(model, property_bags=[it for it in model.property_bags if keep(it.field)]), obj, list(held), clear
        )
        bag_sets, bag_changes = self._bag_sets("n", "rootBag", entries)
        if model.node_labels and keep(model.node_labels.field):
            label_sets, label_changes = self._label_clauses(model, obj, "n", clear, held)
        else:
            label_sets, label_changes = [], []
        changed = " OR ".join([changes("n", "$root.props"), *vector_changes, *bag_changes, *label_changes])
        write = [f"SET {restamp('n', changed)}", "SET n += $root.props", *vector_sets, *bag_sets, *label_sets]
        expected = getattr(obj, model.stamp_field, None) if model.stamp_field else None
        if self._create_only:
            if expected is not None:
                raise DrivineError(
                    f"A {model.type.__name__} that carries a stamp was loaded, and create makes a node that isn't "
                    f"there. Save it, or update it."
                )
            return "\n".join(
                [
                    f"MERGE (n{labels} {id_}) ON CREATE SET n.{MADE} = true",
                    # Found rather than made: there already, or made by another writer at the same moment.
                    f"WITH n WHERE n.{MADE} IS NOT NULL",
                    "WITH n, '' AS _found",
                    *write,
                    f"REMOVE n.{MADE}",
                ]
            )
        if expected is None:
            return "\n".join(
                [
                    f"MERGE (n{labels} {id_}) ON CREATE SET n.{MADE} = true",
                    # The lock is taken unchecked too, so that what the save says it changed is true while another
                    # writer is changing the same node.
                    f"SET n.{LOCK} = true REMOVE n.{LOCK}",
                    f"WITH n, CASE WHEN n.{MADE} IS NOT NULL THEN '' ELSE coalesce(n.{STAMP}, "
                    f"'{stamps.NEVER_STAMPED}') END AS _found",
                    *write,
                    f"REMOVE n.{MADE}",
                ]
            )
        self._bindings["_expected"] = expected
        t = stamps.TOKEN
        ok = [f"left(n.{STAMP}, {t}) = left({EXPECTED}, {t})"]
        if replacing:
            ok.append(f"size({EXPECTED}) = {2 * t + 1} AND right(n.{STAMP}, {t}) = right({EXPECTED}, {t})")
        return "\n".join(
            [
                f"MATCH (n{labels} {id_})",
                f"SET n.{LOCK} = true",
                f"WITH n, coalesce({' AND '.join(ok)}, false) AS _ok",
                f"REMOVE n.{LOCK}",
                "WITH n, _ok WHERE _ok",
                f"WITH n, coalesce(n.{STAMP}, '{stamps.NEVER_STAMPED}') AS _found",
                *write,
            ]
        )

    def _removals(
        self, view: GraphViewModel, obj: Any, replaced: list[str], removed_targets: RemovedTargets
    ) -> list[str]:
        parts: list[str] = []
        held = self._held_ids(view, obj)
        for name in replaced:
            rel = next((it for it in view.relationships if it.field == name), None)
            if rel is None:
                raise DrivineError(f"replace names {name}, which is not a relationship field of {view.type.__name__}.")
            if rel.read_only:
                raise DrivineError(
                    f"replace names {view.type.__name__}.{name}, which is read-only: a save never writes it."
                )
            value = getattr(obj, name, None)
            if rel.is_collection and value is None:
                raise DrivineError(
                    f"{view.type.__name__}.{name} is None, so it can't say which relationships to keep. Give an empty "
                    f"list to remove them all."
                )
            self._count += 1
            k = self._count
            into = target(rel)
            id_prop = quote_name(_id_property(root_of(into)))
            keep: list[Any] = []
            for it in view.relationships:
                if it.type == rel.type and it.direction == rel.direction:
                    keep.extend(
                        id_of(root_of(target(it)), _fragment_of(it, item))
                        for item in items(it, getattr(obj, it.field, None))
                    )
            self._bindings[f"keep{k}"] = keep
            parts.extend(
                [
                    f"WITH {self._carry}",
                    f"OPTIONAL MATCH {_pattern(rel, 'n', f'd{k}', f'x{k}{label_expression(root_of(into).labels)}')} "
                    f"WHERE NOT x{k}.{id_prop} IN $keep{k}",
                    f"WITH {self._carry}, collect(d{k}) AS drop{k}, collect(x{k}) AS removed{k}",
                    f"FOREACH (r IN drop{k} | DELETE r)",
                    f"FOREACH (x IN removed{k} | SET {relink('x')})",
                    f"SET {relink('n', f'size(drop{k}) > 0')}",
                ]
            )
            if removed_targets is RemovedTargets.DELETE_UNREFERENCED:
                self._bindings["held"] = held
                y = f"y{k}"
                parts.extend(
                    [
                        f"WITH {self._carry}, removed{k}",
                        f"OPTIONAL MATCH ({y}{label_expression(root_of(into).labels)}) WHERE {y} IN removed{k} "
                        f"AND {y} <> n AND NOT {y}.{id_prop} IN $held "
                        f"AND NOT {self.grammar.existence_check(_referenced_as(rel, y))}",
                        f"WITH {self._carry}, collect({y}) AS unreferenced{k}",
                        f"FOREACH (x IN unreferenced{k} | DETACH DELETE x)",
                    ]
                )
        return parts

    def _write_parts(self, view: GraphViewModel, path: list[RelationshipModel]) -> list[_WritePart]:
        parts: list[_WritePart] = []
        for rel in view.relationships:
            if rel.read_only or sum(1 for it in path if it is rel) >= rel.max_depth:
                continue
            self._count += 1
            part = _WritePart(f"rows{self._count}", rel)
            into = target(rel)
            if isinstance(into, GraphViewModel):
                part.children = self._write_parts(into, [*path, rel])
            parts.append(part)
        return parts

    def _for_each(self, part: _WritePart, parent: str, rows: str, pass_: str) -> str:
        k = part.key[len("rows") :]
        row, t, r = f"row{k}", f"t{k}", f"r{k}"
        root = root_of(target(part.rel))
        merge = f"MERGE ({t}{label_expression(root.labels)} {{{quote_name(_id_property(root))}: {row}.id}})"
        row_vector_sets, row_vector_changes = vector_clauses(root, self.grammar, t, f"{row}.vectors")
        is_fragment = isinstance(part.rel.related, RelationshipFragmentModel)
        rel_changed = changes(r, f"{row}.rel")
        # A related node of a subclass of the field's target carries its own labels too: a MERGE takes no labels from
        # a parameter, so each subtype the object holds sets its own, when the row is of it.
        subtype_labels = [
            f"FOREACH (_ IN CASE WHEN {row}.subtype = {i} THEN [1] ELSE [] END | SET {t}{label_expression(sub.labels)})"
            for i, sub in enumerate(self._subtypes.get(id(root), []))
        ]
        if pass_ == "NODES_AND_EDGES":
            steps = [
                f"{merge} SET {restamp(t, ' OR '.join([changes(t, f'{row}.props'), *row_vector_changes]))} "
                f"SET {t} += {row}.props",
                *row_vector_sets,
                *subtype_labels,
                f"MERGE {_pattern(part.rel, parent, '', t)} ON CREATE SET {relink(parent)}, {relink(t)}",
            ]
            children = part.children
        else:
            steps = [merge]
            if is_fragment:
                steps.append(
                    f"MERGE {_pattern(part.rel, parent, r, t)} "
                    f"SET {relink(parent, rel_changed)}, {relink(t, rel_changed)} SET {r} += {row}.rel"
                )
            children = [it for it in part.children if _leads_to_relationship_fragment(it)]
        steps.extend(self._for_each(child, t, f"coalesce({row}.{child.key}, [])", pass_) for child in children)
        return f"FOREACH ({row} IN {rows} | {' '.join(steps)})"

    def _rows(self, part: _WritePart, value: Any) -> list[dict[str, Any]]:
        rel = part.rel
        into = target(rel)
        result: list[dict[str, Any]] = []
        for item in items(rel, value):
            node = _node_of(rel, item)
            if node is None:
                raise DrivineError(f"A {type(item).__name__} in {rel.field} has no target node.")
            node_object = getattr(node, into.root_field) if isinstance(into, GraphViewModel) else node
            root = root_of(into)
            self._stamped_ids(root, id_of(root, node_object))
            concrete = self._concrete_model(root, node_object)
            props, vectors_ = split_vectors(root, props_of(concrete, node_object, _stamp_excluded(concrete)))
            row: dict[str, Any] = {"id": id_of(root, node_object), "props": props, "vectors": vectors_}
            if concrete is not root:
                row["subtype"] = self._subtype_index(root, concrete)
            if concrete.property_bags or concrete.node_labels:
                self._bagged.append(_Bagged(concrete, row["id"], node_object))
            related = rel.related
            if isinstance(related, RelationshipFragmentModel):
                row["rel"] = props_of(related, item, [related.target_field])
            for child in part.children:
                row[child.key] = self._rows(child, getattr(node, child.rel.field, None))
            result.append(row)
        return result

    def _bagged_parts(self) -> list[str]:
        """A related node with property bags or node labels, written as under IGNORE, by a part of its own."""
        parts: list[str] = []
        for i, it in enumerate(self._bagged):
            alias = f"b{i}"
            bag_sets, bag_changes = self._bag_sets(alias, f"bag{i}_", bag_entries(it.model, it.object))
            if it.model.node_labels:
                label_sets, label_changes = self._label_clauses(it.model, it.object, alias, False)
            else:
                label_sets, label_changes = [], []
            changed = [*bag_changes, *label_changes]
            if not changed and not label_sets:
                continue
            self._bindings[f"bagId{i}"] = it.id
            parts.extend(
                [
                    f"WITH {self._carry}",
                    f"MATCH ({alias}{label_expression(it.model.labels)} "
                    f"{{{quote_name(_id_property(it.model))}: $bagId{i}}})",
                    *([f"SET {restamp(alias, ' OR '.join(changed))}"] if changed else []),
                    *bag_sets,
                    *label_sets,
                ]
            )
        return parts

    def _label_clauses(
        self, model: FragmentModel, obj: Any, alias: str, clear: bool, held: Mapping[str, Any] | None = None
    ) -> tuple[list[str], list[str]]:
        """
        Clauses that give a node the labels its NodeLabels field holds, and under `clear` remove those it owns and no
        longer holds: for a closed field, its other members; for an open one, the labels it recorded writing (from
        `held`, the node's properties read first). An open field records what it wrote.
        """
        assert model.node_labels is not None
        labels_field = model.node_labels
        labels = list(dict.fromkeys(getattr(obj, labels_field.field, None) or []))
        if labels_field.members is not None:
            outside = [it for it in labels if it not in labels_field.members]
            if outside:
                raise DrivineError(
                    f"{model.type.__name__}.{labels_field.field} holds {', '.join(outside)}, which its labels don't "
                    f"include."
                )
        if any(not isinstance(it, str) or it.startswith(RESERVED_PREFIX) for it in labels):
            raise DrivineError(
                f"{model.type.__name__}.{labels_field.field} holds a label that isn't a name, or begins "
                f"{RESERVED_PREFIX!r}."
            )
        owned: Sequence[str] = labels_field.members or (held or {}).get(labels_field.record_property) or []
        removed = [it for it in owned if it not in labels and it not in model.labels] if clear else []
        sets = [
            *(f"SET {alias}:{quote_name(it)}" for it in labels),
            *(f"REMOVE {alias}:{quote_name(it)}" for it in removed),
        ]
        changed = [
            *(f"NOT {alias}:{quote_name(it)}" for it in labels),
            *(f"{alias}:{quote_name(it)}" for it in removed),
        ]
        if labels_field.members is None:
            name = f"labels{self._count}_{alias}"
            self._bindings[name] = labels
            record = f"{alias}.{quote_name(labels_field.record_property)}"
            sets.append(
                f"SET {record} = ${name}"
                if clear
                else f"SET {record} = [x IN coalesce({record}, []) WHERE NOT x IN ${name}] + ${name}"
            )
        return sets, changed

    def _bag_sets(self, alias: str, parameter: str, entries: Mapping[str, Any]) -> tuple[list[str], list[str]]:
        """
        SET clauses for property-bag entries, each with a parameter of its own (a parameter map can't hold a dotted
        key on every engine), and the conditions that say an entry changes the node.
        """
        sets: list[str] = []
        changed: list[str] = []
        for i, (prop, value) in enumerate(entries.items()):
            name = f"{parameter}{self._count}_{i}"
            self._bindings[name] = value
            stored = f"{alias}.{quote_name(prop)}"
            sets.append(f"SET {stored} = ${name}")
            changed.append(f"{stored} IS NOT NULL" if value is None else f"({stored} IS NULL OR {stored} <> ${name})")
        return sets, changed

    def _concrete_model(self, target_: FragmentModel, obj: Any) -> FragmentModel:
        """The model of the object's own class, where it is a subclass of the field's target."""
        kind: Any = obj.__class__
        if self.registry is None or kind is target_.type or self.registry.kind_of(kind) != "NODE_FRAGMENT":
            return target_
        return self.registry.fragment(kind)

    def _subtype_index(self, target_: FragmentModel, concrete: FragmentModel) -> int:
        known = self._subtypes.setdefault(id(target_), [])
        if concrete not in known:
            known.append(concrete)
        return known.index(concrete)

    def _stamped_ids(self, model: FragmentModel, id_: Any) -> None:
        if not model.stamp_field:
            return
        stamped = next((it for it in self._stamped if it.model is model), None)
        if stamped is None:
            stamped = _StampedNodes(len(self._stamped), model)
            self._stamped.append(stamped)
            self._bindings[f"stampIds{stamped.index}"] = []
        ids: list[Any] = self._bindings[f"stampIds{stamped.index}"]
        if id_ not in ids:
            ids.append(id_)

    def _held_ids(self, view: GraphViewModel, obj: Any, seen: set[int] | None = None) -> list[Any]:
        """The ids of every node the object holds, at any depth."""
        seen = set() if seen is None else seen
        if obj is None or id(obj) in seen:
            return []
        seen.add(id(obj))
        root = getattr(obj, view.root_field, None)
        ids = [getattr(root, view.root.node_id_field, None) if view.root.node_id_field else None]
        for rel in view.relationships:
            into = target(rel)
            for item in items(rel, getattr(obj, rel.field, None)):
                node = _node_of(rel, item)
                if isinstance(into, GraphViewModel):
                    ids.extend(self._held_ids(into, node, seen))
                elif into.node_id_field:
                    ids.append(getattr(node, into.node_id_field, None))
        return [it for it in ids if it is not None]


def _stamp_excluded(model: FragmentModel) -> list[str]:
    return [model.stamp_field] if model.stamp_field else []


def _leads_to_relationship_fragment(part: _WritePart) -> bool:
    return isinstance(part.rel.related, RelationshipFragmentModel) or any(
        _leads_to_relationship_fragment(it) for it in part.children
    )


def target(rel: RelationshipModel) -> TargetModel:
    related = rel.related
    if isinstance(related, RelationshipFragmentModel):
        assert related.target is not None
        return related.target
    return related


def root_of(into: TargetModel) -> FragmentModel:
    return into.root if isinstance(into, GraphViewModel) else into


def _node_of(rel: RelationshipModel, item: Any) -> Any:
    """The node an item of a relationship field leads to: for a relationship fragment, its target."""
    related = rel.related
    return getattr(item, related.target_field, None) if isinstance(related, RelationshipFragmentModel) else item


def _fragment_of(rel: RelationshipModel, item: Any) -> Any:
    """The fragment an item of a relationship field leads to: for a view, its root."""
    node = _node_of(rel, item)
    into = target(rel)
    return getattr(node, into.root_field, None) if isinstance(into, GraphViewModel) else node


def items(rel: RelationshipModel, value: Any) -> list[Any]:
    if value is None:
        return []
    if rel.is_collection:
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise DrivineError(f"{rel.field} is a list relationship, but holds a {type(value).__name__}.")
        return list(value)  # pyright: ignore[reportUnknownArgumentType]
    return [value]


def _id_property(model: FragmentModel) -> str:
    if not model.node_id_property:
        raise DrivineError(f"{model.type.__name__} has no NodeId field, so it can't be saved: a save merges on the id.")
    return model.node_id_property


def id_of(model: FragmentModel, obj: Any) -> Any:
    _id_property(model)
    assert model.node_id_field is not None
    value = getattr(obj, model.node_id_field, None)
    if value is None:
        raise DrivineError(f"A {model.type.__name__} to save has no {model.node_id_field}. A save merges on the id.")
    return value


def object_props(model: FragmentModel, obj: Any, options: SaveOptions) -> dict[str, Any]:
    """The properties a save writes of an object (of a view, its root), as the options' null policy, `only` and
    `except_` say."""
    only, except_ = options.only, options.except_
    if only is not None and except_ is not None:
        raise DrivineError("Give only or except_, not both.")
    reserved = [it for it in (model.node_id_field, model.stamp_field) if it and only is not None and it in only]
    if reserved:
        raise DrivineError(f"only names {' and '.join(reserved)}, which a save doesn't write from the object.")
    clear = options.null_policy is NullPolicy.CLEAR
    props = props_of(model, obj, _stamp_excluded(model), clear, only or ())
    by_property = model.fields_by_property

    def keep(name: str) -> bool:
        field_ = by_property.get(name, name)
        return field_ in only if only is not None else not (except_ and field_ in except_)

    return {name: value for name, value in props.items() if keep(name)}


def props_of(
    mapping: FieldMapping, obj: Any, excluded: Sequence[str], clear_nulls: bool = False, named: Iterable[str] = ()
) -> dict[str, Any]:
    """
    The properties an object writes: its fields that aren't None, under their stored names, leaving out those in
    `excluded`. With `clear_nulls`, a field that is None is written as null if it was assigned, or is `named`.
    """
    assigned: set[str] = set(obj.model_fields_set) if isinstance(obj, BaseModel) else set()
    assigned.update(named)
    props: dict[str, Any] = {}
    for field_, name in mapping.graph_properties.items():
        if field_ in excluded:
            continue
        value = getattr(obj, field_, None)
        if value is not None:
            props[name] = json.dumps(to_jsonable_python(value)) if field_ in mapping.json_packed else stored(value)
        elif clear_nulls and field_ in assigned:
            props[name] = None
    return props


def bag_entries(model: FragmentModel, obj: Any, held: Sequence[str] = (), clear: bool = False) -> dict[str, Any]:
    """
    The node properties a fragment's property bags write: each entry under its bag's namespace, and, given the keys the
    node holds and `clear`, null for each held key of a bag that the bag no longer has.
    """
    entries: dict[str, Any] = {}
    for bag in model.property_bags:
        values = getattr(obj, bag.field, None)
        if values is not None and not isinstance(values, Mapping):
            raise DrivineError(f"The PropertyBag {model.type.__name__}.{bag.field} must hold a dict of entries.")
        for key, value in (values or {}).items():  # pyright: ignore[reportUnknownVariableType]
            if not _storable(value):
                raise DrivineError(
                    f"The entry {key!r} of {model.type.__name__}.{bag.field} can't be stored as a property: a bag "
                    f"holds strings, numbers, booleans, dates, or lists of them."
                )
            if value is not None:
                entries[f"{bag.namespace}{key}"] = stored(value)
        if clear and not bag.flat:
            for it in held:
                if it.startswith(bag.namespace) and it not in entries:
                    entries[it] = None
    return entries


def stored(value: Any) -> Any:
    """A value as a driver takes it: an Enum as its value, and a set or tuple as a list."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, tuple, frozenset)):
        return [stored(it) for it in value]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list):
        return [stored(it) for it in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def _storable(value: Any) -> bool:
    def scalar(it: Any) -> bool:
        return it is None or isinstance(it, (str, int, float, bool, date, datetime, time, Enum))

    if isinstance(value, (list, tuple)):
        return all(it is not None and scalar(it) for it in value)  # pyright: ignore[reportUnknownVariableType]
    return scalar(value)


def _pattern(rel: RelationshipModel, origin: str, rel_alias: str, to: str) -> str:
    edge = f"[{rel_alias}:{quote_name(rel.type)}]"
    if rel.direction is Direction.INCOMING:
        return f"({origin})<-{edge}-({to})"
    if rel.direction is Direction.UNDIRECTED:
        return f"({origin})-{edge}-({to})"
    return f"({origin})-{edge}->({to})"


def _referenced_as(rel: RelationshipModel, node: str) -> str:
    """A pattern that matches when a node is still referred to the way the field referred to it."""
    if rel.direction is Direction.INCOMING:
        return f"({node})-->()"
    if rel.direction is Direction.UNDIRECTED:
        return f"({node})--()"
    return f"({node})<--()"
