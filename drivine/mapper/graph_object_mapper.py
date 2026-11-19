from __future__ import annotations

import json
import re
import types
import typing
from collections.abc import Iterable, Mapping
from datetime import date, time, timedelta
from typing import Any, cast

from pydantic import BaseModel

from drivine.annotation.sorted_by import SortedBy
from drivine.backend.sql.value_layout import TagText
from drivine.drivine_error import DrivineError
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
        _refuse_temporal_id(model, props)
        values = self._properties(model, props, model)
        if model.stamp_field:
            values[model.stamp_field] = props.get(STAMP_PROPERTY)
        if model.node_labels:
            field = model.node_labels
            held = [
                it for it in labels if (it in field.members if field.members is not None else it not in model.labels)
            ]
            values[field.field] = set(held) if field.is_set else held
        instance = _instance(model.type, values, model.validate_on_load)
        _refuse_temporal_id_changed(model, props, instance)
        return instance

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

        A set or tuple field of a fragment constructed without validation is turned from the stored list here; a
        validated one, as a relationship fragment always is, is left to pydantic, which also builds its items.
        """
        validated = fragment is None or fragment.validate_on_load
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
            value = _unpacked(value) if field in mapping.json_packed else value
            if field in mapping.bytes_fields:
                # Store layout 2 holds bytes as the list of their signed values, and a collection of bytes each so.
                value = _held_bytes(value, mapping.bytes_fields[field])
            kind = mapping.collections.get(field)
            if kind is not None and value == {}:
                # Drivine for TypeScript saved an empty set as {} on Memgraph and as JSON text before 3.3.
                value = _collected([], kind)
            elif kind is not None and not validated and isinstance(value, (list, tuple, set, frozenset)):
                value = _constructed(cast(Iterable[Any], value), kind)
            values[field] = value
        for field, kind in mapping.empty_when_absent.items():
            if values.get(field) is None:
                values[field] = empty_collection(kind)
        return values


_TEMPORAL_IDS = (date, time, timedelta)
"""The NodeId types a temporal value's tag loads as (a datetime is a date): any other is refused one."""


def _refuse_temporal_id(model: FragmentModel, props: Mapping[str, Any]) -> None:
    """
    Refuses, by name, a node whose NodeId a SQL store holds as a temporal value's tag (a date another writer stored)
    where `model` declares the id a `str`, a number or any other type no tag loads as (see _refuses_tags). The id would
    load as its text, which a lookup by the id never finds, as it compares the id's kind too: a save of the node would
    make a second one, or, with a NodeStamp, be refused as stale (liberation-data/drivine#813, drivine-python#560).
    Drivine for TypeScript and drivine4j refuse it in the same words.
    """
    if model.node_id_field is None:
        return
    held = props.get(model.node_id_property or model.node_id_field)
    if not isinstance(held, TagText) or not _refuses_tags(model.node_id_type):
        return
    raise _held_as_text(model, held)


def _held_as_text(model: FragmentModel, held: TagText) -> DrivineError:
    """The refusal of a node whose temporal id `model` would load as text: the words the three libraries share."""
    field = model.node_id_field
    return DrivineError(
        f"{model.type.__name__}'s NodeId {field} is declared a {_declared_name(model.node_id_type)}, but the node whose"
        f" id is '{held}' holds it as a {held.kind}: declare {field} as a {held.kind} to load it."
    )


_PYTHON_TYPE_OF_TAG = {
    "date": "date",
    "datetime": "datetime",
    "localdatetime": "datetime",
    "time": "time",
    "localtime": "time",
    "duration": "timedelta",
}
"""The Python type each kind of tag loads as, and is saved back as that kind from."""

_BELOW_THE_MICROSECOND = re.compile(r"\.\d{7,}")
"""A fraction of a second past the microsecond, which no Python temporal value holds."""


def _refuse_temporal_id_changed(model: FragmentModel, props: Mapping[str, Any], instance: Any) -> None:
    """
    Refuses, by name, a node whose NodeId a SQL store holds as a temporal value's tag that the id `model` declares
    loads as another value: a `date` id over a `localdatetime` or a `datetime`, a `datetime` id over a `date`, a
    time below the microsecond, which Python holds to the microsecond, or a `timedelta` id over a duration it doesn't
    write back as the same text (`P1M`, `PT24H`). A save of the node would write the id it loaded
    as, which a lookup by the id never finds, so it would make a second node (drivine-python#561, as
    liberation-data/drivine#815). An id that holds the tag as it was read, as `Any` does (TagText), is saved back as
    that tag, and isn't refused; one a union with `Any` validated as text (`str | Any`) is refused as text is.
    """
    field = model.node_id_field
    if field is None:
        return
    held = props.get(model.node_id_property or field)
    loaded = getattr(instance, field, None)
    if not isinstance(held, TagText) or isinstance(loaded, TagText) or loaded is None:
        return
    from drivine.cypher.shared_compiler import written

    request = written(loaded)
    if not isinstance(request, dict) or "$temporal" not in request:
        # A union with Any that validated the text as its `str`, `str | Any`: the id loaded as text.
        raise _held_as_text(model, held)
    kind = cast(str, request["$temporal"]).lower().replace("_", "")
    below = _BELOW_THE_MICROSECOND.search(held) is not None
    # A duration of months, or one written another way (`PT24H`, `PT-0.5S`), loads as a timedelta of days and seconds,
    # which saves back as another duration: a duration is found by its text, which has no other form.
    if kind == held.kind and not below and (kind != "duration" or request["text"] == held):
        return
    declared = _declared_name(model.node_id_type)
    fits = _PYTHON_TYPE_OF_TAG[held.kind]
    advice = f"declare {field} as Any" if fits == declared or kind == held.kind else f"declare {field} as a {fits}"
    raise DrivineError(
        f"{model.type.__name__}'s NodeId {field} is declared a {declared}, but the node whose id is '{held}' holds it"
        f" as a {held.kind}{' below the microsecond' if kind == held.kind and below else ''}, which a {declared} can't"
        f" save back: {advice} to load it."
    )


def _refuses_tags(kind: Any) -> bool:
    """
    Whether a NodeId declared `kind` refuses a temporal value's tag: a type no tag loads as (`str`), or a union that
    loads the tag's text as text: one with no temporal member (`int | None`), or one with a `str` (`str | date`, as
    pydantic keeps text that is a `str` as it is). `int | date` loads the text as the date, and isn't refused. One
    declared `Any`, or a union with it, takes the text as it is given, and a union's `None` takes no tag.
    """
    if typing.get_origin(kind) in (typing.Union, types.UnionType):
        members = [it for it in typing.get_args(kind) if it is not type(None)]
        if Any in members:
            return False
        texts = any(isinstance(it, type) and issubclass(it, str) for it in members)
        return texts or all(_refuses_tags(it) for it in members)
    return kind is not Any and isinstance(kind, type) and not issubclass(kind, _TEMPORAL_IDS)


def _declared_name(kind: Any) -> str:
    """The NodeId's declared type as Python writes it: `str`, `int`, `str | None`, `str | date`."""
    if typing.get_origin(kind) in (typing.Union, types.UnionType):
        return " | ".join("None" if it is type(None) else _declared_name(it) for it in typing.get_args(kind))
    return kind.__name__ if isinstance(kind, type) else str(kind)


def empty_collection(kind: CollectionKind) -> Any:
    """What a load gives an EmptyWhenAbsent field of a node that holds no property for it, or holds null."""
    return {} if kind == "dict" else _collected([], kind)


def _instance(type_: type[BaseModel], values: dict[str, Any], validate: bool) -> Any:
    return type_.model_validate(values) if validate else type_.model_construct(**values)


def _held_bytes(value: Any, collection: CollectionKind | None) -> Any:
    """A field of bytes as store layout 2 holds it (see FieldMapping.bytes_fields), as the bytes it holds."""
    if collection == "dict" and isinstance(value, Mapping):
        return {key: _bytes(it) for key, it in cast(Mapping[str, Any], value).items()}
    if collection is not None and isinstance(value, list):
        return [_bytes(it) for it in cast(list[Any], value)]
    return _bytes(value)


def _bytes(value: Any) -> Any:
    """Bytes as store layout 2 holds them, the list of their signed values, as the bytes they are; else as it is."""
    return bytes(int(it) & 0xFF for it in cast(list[Any], value)) if isinstance(value, list) else value


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


def _constructed(items: Iterable[Any], kind: CollectionKind) -> Any:
    """
    A stored list as the set or tuple of an unvalidated fragment's field. Items nothing built, such as the lists a
    set of tuples is stored as, can't be hashed into a set, so the list is left as it is.
    """
    try:
        return _collected(items, kind)
    except TypeError:
        return items


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
