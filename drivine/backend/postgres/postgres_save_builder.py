from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from drivine.backend.postgres.postgres_graph import PostgresGraph
from drivine.backend.postgres.postgres_save_statement import PostgresSaveStatement
from drivine.backend.postgres.postgres_write_statement import PostgresWriteStatement
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
from drivine.backend.sql.node_writes import NodeWrites
from drivine.backend.sql.owned_labels import OwnedLabels
from drivine.backend.sql.root_step import RootStep
from drivine.backend.sql.sql_json import to_json
from drivine.backend.sql.sql_saves import fragment_of, stamp_excluded
from drivine.backend.sql.write_plan import WritePlan
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
    """The model whose labels and id property the node is found by."""
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


class PostgresSaveBuilder:
    """
    Writes a save as one plan for drivine4j's write function, applied whole or not at all, with or without a
    transaction. It decides what OracleSaveBuilder decides, from the same model: the root found (or made) and locked,
    checked against the object's stamp when it carries one, its properties written and its labels set; the
    relationships named in `replace` trimmed; then each related node written and joined, as deep as the view goes. The
    function keeps stamps by drivine4j's rules, which are the Cypher save's.
    """

    def __init__(self, graph: PostgresGraph, registry: ModelRegistry | None = None) -> None:
        self.graph = graph
        self.registry = registry
        self._plan = WritePlan()
        self._stamped: list[_Stamped] = []
        self._handed: dict[str, tuple[int, Any]] = {}
        """For each index a related node's step was given, its stamped model's place among `_stamped`, and its id."""
        self._options = SaveOptions()

    def build(
        self, target_: TargetModel, obj: Any, options: SaveOptions | None = None, create_only: bool = False
    ) -> SaveStatement:
        self._options = options or SaveOptions()
        if isinstance(target_, FragmentModel):
            if self._options.replace == "all" or self._options.replace:
                raise DrivineError(
                    f"replace names relationship fields of a view. {target_.type.__name__} is a NodeFragment."
                )
            model = self._concrete(target_, obj)
            expected = self._root(model, obj, create_only, False)
            return self._statement(model, obj, expected)
        view = target_
        root_object = getattr(obj, view.root_field, None)
        if root_object is None:
            raise DrivineError(
                f"{view.type.__name__}.{view.root_field}, the root, is None. A view is saved from its root."
            )
        replaced = replaced_fields(view, obj, self._options)
        root = self._concrete(view.root, root_object)
        expected = self._root(root, root_object, create_only, len(replaced) > 0)
        self._trims(view, obj, replaced)
        self._related(view, obj, 0, [])
        return self._statement(root, root_object, expected)

    def _root(self, model: FragmentModel, obj: Any, create_only: bool, replacing: bool) -> str | None:
        """Finds or makes the root, checks it, and writes it. Returns the stamp it was checked against, if any."""
        only, except_ = self._options.only, self._options.except_

        def keep(name: str) -> bool:
            return name in only if only is not None else not (except_ and name in except_)

        clear = self._options.null_policy is NullPolicy.CLEAR
        held = self._options.held_properties
        props = dict(object_props(model, obj, self._options))
        bagged = replace(model, property_bags=[it for it in model.property_bags if keep(it.field)])
        props.update(bag_entries(bagged, obj, list(held), clear))
        labels_clear = clear if model.node_labels and keep(model.node_labels.field) else None
        write = self._node_write(model, obj, props, labels_clear, held)
        expected = getattr(obj, model.stamp_field, None) if model.stamp_field else None
        if create_only and expected is not None:
            raise DrivineError(
                f"A {model.type.__name__} that carries a stamp was loaded, and create makes a node that isn't there. "
                "Save it, or update it."
            )
        # The data token is compared; the whole stamp, and so the links token too, when the save replaces a list.
        root = RootStep(
            carried=expected,
            expect=None if expected is None else expected if replacing else stamps.data_token(expected),
            whole=expected is not None and replacing,
            mode="ABSENT" if create_only else "EITHER",
        )
        self._node(write, None, root)
        return expected

    def _related(self, view: GraphViewModel, obj: Any, from_: int, path: list[RelationshipModel]) -> None:
        """
        Writes each relationship of a view, from the node the plan numbers `from_`: each related node written, then
        joined; a related view's relationships after, as far as max_depth.
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
                props = dict(props_of(concrete, node_object, stamp_excluded(concrete)))
                props.update(bag_entries(concrete, node_object))
                write = self._node_write(
                    concrete, node_object, props, False if concrete.node_labels else None, {}, root
                )
                to = self._node(write, self._handed_back(root, write.id))
                related = rel.related
                rel_props = (
                    props_of(related, item, [related.target_field])
                    if isinstance(related, RelationshipFragmentModel)
                    else {}
                )
                self._plan.link(from_, to, rel.type, rel.direction, present(rel_props), absent(rel_props))
                if isinstance(into, GraphViewModel):
                    self._related(into, node, to, [*path, rel])

    def _trims(self, view: GraphViewModel, obj: Any, replaced: list[str]) -> None:
        """
        Trims each relationship field named in `replace` to what the object holds: relationships of its type and
        direction from the root to nodes of its labels, other than those any field of that type and direction holds.
        """
        held = [stored_key(it) for it in held_ids(view, obj)]
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
            self._plan.unlink(
                0,
                rel.type,
                rel.direction,
                self._plan.shape(into.labels, _id_property(into)),
                [stored_key(it) for it in keep],
                True,
                self._options.removed_targets is RemovedTargets.DELETE_UNREFERENCED,
                held,
            )

    def _node(self, write: _NodeWrite, index: str | None = None, root: RootStep | None = None) -> int:
        """The step that writes a node: found by the labels and id property of the write's model."""
        key = _id_property(write.model)
        props = without_key(write.props, key)
        writes = NodeWrites(
            set=present(props),
            unset=absent(props),
            add=write.add,
            drop=write.remove,
            owned=OwnedLabels(write.record[0], write.record[1], write.model.labels) if write.record else None,
        )
        return self._plan.node(
            self._plan.shape(write.model.labels, key),
            key,
            write.id,
            stored_key(write.id),
            list(dict.fromkeys(write.labels)),
            writes,
            index,
            root,
        )

    def _handed_back(self, model: FragmentModel, id_: Any) -> str | None:
        """The index a related node of the model is handed its stamp back under, where the model declares one."""
        if not model.stamp_field:
            return None
        place = next((i for i, it in enumerate(self._stamped) if it.model is model), None)
        if place is None:
            self._stamped.append(_Stamped(model))
            place = len(self._stamped) - 1
        index = str(len(self._handed))
        self._handed[index] = (place, id_)
        return index

    def _statement(self, root: FragmentModel, root_object: Any, expected: str | None) -> SaveStatement:
        return PostgresSaveStatement.made(
            PostgresWriteStatement.call(self.graph, "STAMPS"),
            {**PostgresWriteStatement.plan_bindings(self._plan), "root": {"id": id_of(root, root_object)}},
            root,
            root_object,
            expected,
            [it.model for it in self._stamped],
            self._handed,
        )

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

    def _concrete(self, target_: FragmentModel, obj: Any) -> FragmentModel:
        """The model of the object's own class, where it is a subclass of the field's target."""
        kind: Any = obj.__class__
        if self.registry is None or kind is target_.type or self.registry.kind_of(kind) != "NODE_FRAGMENT":
            return target_
        return self.registry.fragment(kind)


def stored_key(id: Any) -> str:
    """An id as the store reads its property back with `->>`: text as it is, a number or a boolean as its JSON."""
    return id if isinstance(id, str) else to_json(id)


def without_key(props: Mapping[str, Any], key: str) -> dict[str, Any]:
    """The properties a node step writes: its id is the step's key, which a node is made with and found by, no more."""
    return {name: value for name, value in props.items() if name != key}


def present(props: Mapping[str, Any]) -> dict[str, Any]:
    """The properties of a merge patch that are written: those that are not null."""
    return {key: value for key, value in props.items() if value is not None}


def absent(props: Mapping[str, Any]) -> list[str]:
    """The properties of a merge patch that are removed: those that are null."""
    return [key for key, value in props.items() if value is None]


def _id_property(model: FragmentModel) -> str:
    if not model.node_id_property:
        raise DrivineError(f"A {model.type.__name__} has no NodeId field. A save merges on the id.")
    return model.node_id_property
