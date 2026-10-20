from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any, cast

from pydantic import BaseModel

from drivine.annotation.sorted_by import SortedBy
from drivine.model.field_mapping import FieldMapping
from drivine.model.field_shape import CollectionKind
from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.model_registry import RESERVED_PREFIX, STAMP_PROPERTY, ModelRegistry
from drivine.model.relationship_fragment_model import RelationshipFragmentModel
from drivine.model.relationship_model import RelationshipModel
from drivine.model.target_model import TargetModel


class GraphObjectMapper:
    """
    Turns the rows the object manager's statements return into instances of the mapped classes: `{props, labels}`
    into a fragment, `{root, rels}` into a view, and `{rel, target}` into a relationship fragment.

    A fragment is validated, unless its class says `validate_on_load=False`. A view is constructed from what it holds,
    which is validated already: a required relationship is null at the last level of a recursive view.
    """

    def __init__(self, registry: ModelRegistry) -> None:
        self.registry = registry

    def fragment(self, expected: FragmentModel, row: Mapping[str, Any]) -> Any:
        labels: list[str] = row.get("labels") or []
        props: Mapping[str, Any] = row.get("props") or {}
        model = self.registry.fragment_for(expected, labels) if labels else expected
        values = self._properties(model, props, model)
        if model.stamp_field:
            values[model.stamp_field] = props.get(STAMP_PROPERTY)
        if model.node_labels:
            field = model.node_labels
            held = [
                it for it in labels if (it in field.members if field.members is not None else it not in model.labels)
            ]
            values[field.field] = set(held) if field.is_set else held
        return _instance(model.type, values, model.validate_on_load)

    def view(self, model: GraphViewModel, row: Mapping[str, Any]) -> Any:
        rels: Mapping[str, Any] = row.get("rels") or {}
        values: dict[str, Any] = {model.root_field: self.fragment(model.root, row["root"])}
        for rel in model.relationships:
            value: Any = rels.get(rel.key)
            if rel.collection is not None:
                items = [self._related(rel, it) for it in cast(list[Any], value or [])]
                values[rel.field] = _collected(_sorted(items, rel.sorted_by), rel.collection)
            else:
                values[rel.field] = None if value is None else self._related(rel, value)
        for path in model.paths:
            value: Any = rels.get(path.key)
            if path.collection is not None:
                values[path.field] = _collected(
                    [self.fragment(path.target, it) for it in cast(list[Any], value or [])], path.collection
                )
            else:
                values[path.field] = None if value is None else self.fragment(path.target, value)
        for aggregate in model.aggregates:
            values[aggregate.field] = rels.get(aggregate.key)
        return model.type.model_construct(**values)

    def _related(self, rel: RelationshipModel, value: Mapping[str, Any]) -> Any:
        related = rel.related
        if isinstance(related, RelationshipFragmentModel):
            return self._relationship_fragment(related, value)
        return self._target(related, value)

    def _relationship_fragment(self, model: RelationshipFragmentModel, row: Mapping[str, Any]) -> Any:
        assert model.target is not None
        values = self._properties(model, row.get("rel") or {})
        values[model.target_field] = self._target(model.target, row["target"])
        return _instance(model.type, values, True)

    def _target(self, into: TargetModel, row: Mapping[str, Any]) -> Any:
        return self.view(into, row) if isinstance(into, GraphViewModel) else self.fragment(into, row)

    def _properties(
        self, mapping: FieldMapping, source: Mapping[str, Any], fragment: FragmentModel | None = None
    ) -> dict[str, Any]:
        """
        A node's or relationship's properties, by the fields they are stored for. A property bag takes the properties
        in its namespace, and a flat bag those no field or other bag is stored as.
        """
        by_property = mapping.fields_by_property
        bags = fragment.property_bags if fragment else []
        values: dict[str, Any] = {it.field: {} for it in bags}
        prefixed = [it for it in bags if not it.flat]
        flat = next((it for it in bags if it.flat), None)
        for name, value in source.items():
            if name.startswith(RESERVED_PREFIX):
                continue
            bag = next((it for it in prefixed if name.startswith(it.namespace)), None)
            if bag is not None:
                values[bag.field][name[len(bag.namespace) :]] = value
                continue
            field = by_property.get(name)
            if field is None:
                if flat is not None:
                    values[flat.field][name] = value
                continue
            if value is None and field in mapping.null_as_default:
                continue
            values[field] = _unpacked(value) if field in mapping.json_packed else value
        for field, kind in mapping.empty_when_absent.items():
            if values.get(field) is None:
                values[field] = {} if kind == "dict" else _collected([], kind)
        return values


def _instance(type_: type[BaseModel], values: dict[str, Any], validate: bool) -> Any:
    return type_.model_validate(values) if validate else type_.model_construct(**values)


def _unpacked(value: Any) -> Any:
    """A packed JSON value, unpacked; a value that isn't JSON text is left as it is."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def _collected(items: Iterable[Any], kind: CollectionKind) -> Any:
    if kind == "set":
        return set(items)
    if kind == "tuple":
        return tuple(items)
    return list(items)


def _sorted(items: list[Any], by: SortedBy | None) -> list[Any]:
    """The items sorted by a field path, nulls last; as they are without one."""
    if by is None:
        return items
    path = by.path.split(".")

    def value_of(item: Any) -> Any:
        for key in path:
            item = getattr(item, key, None)
            if item is None:
                return None
        return item

    present = [it for it in items if value_of(it) is not None]
    absent = [it for it in items if value_of(it) is None]
    return sorted(present, key=value_of, reverse=not by.ascending) + absent
