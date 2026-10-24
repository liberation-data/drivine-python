"""
The model file, `.drivine/model.json`: a project's fragments, relationship fragments and views, and how each is
stored, in the format in drivine-cypher-spec's `model/`, which drivine4j and Drivine (TypeScript) write too, and Drivine
Studio reads. The stubs step writes it beside the project's `pyproject.toml`, from the models ModelRegistry builds of
the same classes a load reads.
"""

from __future__ import annotations

import importlib.metadata
import json
import tomllib
import types
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal, Union, cast, get_args, get_origin

from pydantic import AwareDatetime, FutureDate, FutureDatetime, NaiveDatetime, PastDate, PastDatetime

from drivine.drivine_error import DrivineError
from drivine.model.field_mapping import FieldMapping
from drivine.model.field_shape import FieldShape
from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.model_registry import ModelRegistry
from drivine.model.relationship_fragment_model import RelationshipFragmentModel
from drivine.stubs.stub_writer import declared

FORMAT = "drivine-model/1"

COMMENT = (
    "Drivine model file: this module's fragments, relationship fragments and views, written by the build. "
    "Drivine Studio reads it to browse the model and query the data. Keep it in source control: the build rewrites it "
    "when the model changes, and CI checks that it is current. "
    "Format: https://github.com/liberation-data/drivine-cypher-spec/tree/main/model"
)
"""Word for word what the format asks every library to write."""

FILE = Path(".drivine") / "model.json"
"""Where the file goes, from the project's directory."""

Entry = dict[str, Any]

_SCALARS: tuple[tuple[type, str], ...] = (
    # bool before int, which it extends.
    (bool, "boolean"),
    (int, "int"),
    (float, "float"),
    (Decimal, "float"),
    (str, "string"),
    (uuid.UUID, "uuid"),
    # datetime before date, which it extends.
    (datetime, "datetime"),
    (date, "date"),
    (time, "localTime"),
    (timedelta, "duration"),
)

_PYDANTIC: dict[Any, str] = {
    # Pydantic's checked dates and times, which are not subclasses of the type they check.
    AwareDatetime: "datetime",
    NaiveDatetime: "localDatetime",
    PastDatetime: "datetime",
    FutureDatetime: "datetime",
    PastDate: "date",
    FutureDate: "date",
}

_INDEXES = {"UNIQUE": "unique", "RANGE": "range", "FULLTEXT": "fullText"}


@dataclass
class ReadModel:
    """What some modules declare, as the model file's entries, and what could not be read."""

    fragments: list[Entry] = field(default_factory=lambda: [])
    relationship_fragments: list[Entry] = field(default_factory=lambda: [])
    views: list[Entry] = field(default_factory=lambda: [])
    problems: list[str] = field(default_factory=lambda: [])


class ModelReader:
    """
    Reads the models some modules declare, and those they lead to, as the model file describes them. A class
    ModelRegistry refuses is not loadable, so it is left out, and its refusal named.
    """

    def __init__(self, modules: Sequence[str], registry: ModelRegistry | None = None) -> None:
        self.modules = list(modules)
        self._registry = registry or ModelRegistry()
        self._seen: set[type] = set()
        self._read = ReadModel()

    def read(self) -> ReadModel:
        """The entries of every model the modules declare or lead to, each kind sorted by id, as the format asks."""
        for type_ in declared(self.modules):
            self._add(type_)
        # A current entry refers only to current ones, so a subtype the registry refused, and named, is not listed.
        ids = {it["id"] for it in self._read.fragments}
        for entry in self._read.fragments:
            if "subtypes" in entry:
                entry["subtypes"] = [it for it in entry["subtypes"] if it in ids]
                if not entry["subtypes"]:
                    del entry["subtypes"]
        for entries in (self._read.fragments, self._read.relationship_fragments, self._read.views):
            entries.sort(key=lambda it: it["id"])
        return self._read

    def _add(self, type_: type) -> None:
        if type_ in self._seen:
            return
        self._seen.add(type_)
        kind = self._registry.kind_of(type_)
        try:
            if kind == "NODE_FRAGMENT":
                self._read.fragments.append(self._fragment(self._registry.fragment(type_)))
                for subtype in self._registry.subtypes(type_):
                    self._add(subtype)
            elif kind == "RELATIONSHIP_FRAGMENT":
                model = self._registry.relationship_fragment(type_)
                self._read.relationship_fragments.append(self._relationship_fragment(model))
                self._add(model.target.type)  # pyright: ignore[reportOptionalMemberAccess]
            elif kind == "GRAPH_VIEW":
                view = self._registry.view(type_)
                self._read.views.append(self._view(view))
                self._add(view.root.type)
                for rel in view.relationships:
                    self._add(rel.related.type)
                for path in view.paths:
                    self._add(path.target.type)
        except DrivineError as error:
            self._read.problems.append(f"{model_id(type_)}: {error}")

    def _fragment(self, model: FragmentModel) -> Entry:
        type_ = model.type
        hints = _annotations(type_)
        # A schema item over one property is that property's own; over several, the fragment's.
        single = {
            it.field or _field_of(model, it.properties[0]): it.kind for it in model.schema if len(it.properties) == 1
        }
        subtypes = sorted(model_id(it) for it in self._registry.subtypes(type_))
        return _entry(
            id=model_id(type_),
            name=type_.__name__,
            labels=list(model.labels),
            nodeId=model.node_id_field,
            stamp=True if model.stamp_field else None,
            subtypes=subtypes or None,
            # A subtype may be registered in code, with register_subtype, which no file can see.
            subtypesComplete=False,
            labelSet=_entry(
                field=model.node_labels.field,
                values=list(model.node_labels.members) if model.node_labels.members is not None else None,
            )
            if model.node_labels
            else None,
            bags=[
                _entry(
                    field=it.field,
                    prefix=it.namespace,
                    flat=True if it.flat else None,
                    type=_typed(_dict_value(hints.get(it.field)))[0],
                )
                for it in sorted(model.property_bags, key=lambda bag: bag.field)
            ]
            or None,
            properties=self._properties(model, hints, single),
            indexes=[
                _entry(kind=_INDEXES[it.kind], properties=[_field_of(model, name) for name in it.properties])
                for it in model.schema
                if len(it.properties) > 1
            ]
            or None,
        )

    def _properties(self, mapping: FieldMapping, hints: dict[str, Any], single: dict[str, str]) -> list[Entry]:
        vectors = getattr(mapping, "schema", [])
        properties: list[Entry] = []
        for name in sorted(mapping.graph_properties):
            annotation = hints.get(name, Any)
            kind, of, values = _typed(annotation)
            packed = name in mapping.json_packed
            vector = next((it for it in vectors if it.kind == "VECTOR" and it.field == name), None)
            index = single.get(name)
            properties.append(
                _entry(
                    name=name,
                    stored=mapping.graph_properties[name],
                    # A packed value the format has no type for is JSON text, whatever it is in the code.
                    type="json" if packed and kind == "any" else kind,
                    of=of,
                    values=values,
                    nullable=FieldShape.of(annotation).optional or annotation is Any,
                    whenAbsent="default"
                    if name in mapping.null_as_default
                    else "empty"
                    if name in mapping.empty_when_absent
                    else None,
                    packed="json" if packed else None,
                    index=_INDEXES.get(index) if index else None,
                    vector=_entry(similarity=vector.similarity, dimensions=vector.dimensions) if vector else None,
                    source=_source(annotation),
                )
            )
        return properties

    def _relationship_fragment(self, model: RelationshipFragmentModel) -> Entry:
        return _entry(
            id=model_id(model.type),
            name=model.type.__name__,
            target=_entry(field=model.target_field, fragment=model_id(model.target.type)),  # pyright: ignore[reportOptionalMemberAccess]
            properties=self._properties(model, _annotations(model.type), {}),
        )

    def _view(self, view: GraphViewModel) -> Entry:
        relationships: list[Entry] = []
        for rel in view.relationships:
            relationships.append(
                _entry(
                    field=rel.field,
                    type=rel.type,
                    direction=str(rel.direction),
                    target=model_id(rel.related.type),
                    many=rel.is_collection,
                    required=rel.required,
                    maxDepth=rel.max_depth if rel.max_depth > 1 else None,
                    sortedBy=_entry(property=rel.sorted_by.path, ascending=rel.sorted_by.ascending)
                    if rel.sorted_by
                    else None,
                    readOnly=True if rel.read_only else None,
                )
            )
        for path in view.paths:
            relationships.append(
                _entry(
                    field=path.field,
                    target=model_id(path.target.type),
                    many=path.is_collection,
                    required=path.required,
                    # A path is never written.
                    readOnly=True,
                    path=[
                        _entry(
                            type=hop.type,
                            direction=str(hop.direction),
                            label=hop.label or None,
                            orderBy=hop.order_by or None,
                            ascending=hop.ascending if hop.order_by else None,
                        )
                        for hop in path.hops
                    ],
                )
            )
        return _entry(
            id=model_id(view.type),
            name=view.type.__name__,
            root=_entry(field=view.root_field, fragment=model_id(view.root.type)),
            relationships=sorted(relationships, key=lambda it: it["field"]),
            aggregates=[
                _entry(
                    field=it.field,
                    function=str(it.function),
                    type=it.type,
                    direction=str(it.direction),
                    property=it.property or None,
                )
                for it in sorted(view.aggregates, key=lambda aggregate: aggregate.field)
            ]
            or None,
        )


def model_id(type_: type) -> str:
    """A class's id in the file: its module and qualified name, as `sample.fragments.person.Person`."""
    return f"{type_.__module__}.{type_.__qualname__}"


def _entry(**keys: Any) -> Entry:
    """An entry, in the order its keys are given, without those that have nothing to say."""
    return {key: value for key, value in keys.items() if value is not None}


def _annotations(type_: type) -> dict[str, Any]:
    """Each field's annotation, as Pydantic resolved it."""
    return {name: info.annotation for name, info in getattr(type_, "model_fields", {}).items()}


def _field_of(mapping: FieldMapping, stored: str) -> str:
    """The field a stored property is of, as the format names a property by its field."""
    return mapping.fields_by_property.get(stored, stored)


def _dict_value(annotation: Any) -> Any:
    """The type of a dict's values, or Any for a field that is not a dict or does not say."""
    shape = FieldShape.of(annotation) if annotation is not None else None
    args = get_args(_unwrap(annotation)) if shape and shape.collection == "dict" else ()
    return args[1] if len(args) == 2 else Any


def _unwrap(annotation: Any) -> Any:
    """The annotation without None, and without Annotated's metadata."""
    if get_origin(annotation) in (Union, types.UnionType):
        rest = [it for it in get_args(annotation) if it is not type(None)]
        if len(rest) == 1:
            annotation = rest[0]
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def _typed(annotation: Any) -> tuple[str, str | None, list[str] | None]:
    """The format's type of a field's annotation, the type of what it holds, and an enum's values."""
    shape = FieldShape.of(annotation)
    if shape.collection == "dict":
        return "map", _typed(_dict_value(annotation))[0], None
    if shape.collection is not None:
        return "list", _typed(shape.item)[0], None
    item = _unwrap(shape.item)
    if get_origin(item) is Literal:
        literals: tuple[Any, ...] = get_args(item)
        if all(isinstance(it, str) for it in literals):
            return "enum", None, sorted(str(it) for it in literals)
        kinds: set[type] = {type(it) for it in literals}
        return _typed(type(literals[0]))[0] if len(kinds) == 1 else "any", None, None
    if isinstance(item, type) and issubclass(item, Enum):
        members = [it.value for it in item]
        if all(isinstance(it, str) for it in members):
            return "enum", None, members
        return ("int", None, None) if all(isinstance(it, int) for it in members) else ("any", None, None)
    if isinstance(item, type) and item in _PYDANTIC:
        return _PYDANTIC[item], None, None
    if isinstance(item, type):
        for scalar, name in _SCALARS:
            if issubclass(item, scalar):
                return name, None, None
    return "any", None, None


def _source(annotation: Any) -> str:
    """The type as Python writes it, without None, which `nullable` says."""
    item = _unwrap(annotation)
    if isinstance(item, type) and not get_args(item):
        return item.__qualname__
    return str(item).replace("typing.", "")


# The file


def previous(file: Path, warn: Callable[[str], None]) -> Entry | None:
    """
    The model file a project had, or None when there is none, or it is not one this library can read, which `warn` is
    told of: a file of another format is written over, as one the project never had.
    """
    if not file.is_file():
        return None
    try:
        model: Any = json.loads(file.read_text())
    except ValueError:
        model = None
    if not isinstance(model, dict) or cast(Entry, model).get("format") != FORMAT:
        warn(f"Drivine model file: {file} is not a {FORMAT} file, so nothing in it is kept.")
        return None
    return cast(Entry, model)


def with_removed(current: list[Entry], before: Any) -> list[Entry]:
    """
    The current entries, and the previous file's entries the code no longer declares, marked removed, sorted by id.
    An entry that comes back into the code is written from the code again, and is no longer removed.
    """
    ids = {it["id"] for it in current}
    entries: list[Any] = cast(list[Any], before) if isinstance(before, list) else []
    kept = [
        _removed(cast(Entry, it))
        for it in entries
        if isinstance(it, dict) and isinstance(cast(Entry, it).get("id"), str) and cast(Entry, it)["id"] not in ids
    ]
    return sorted([*current, *kept], key=lambda it: it["id"])


def _removed(entry: Entry) -> Entry:
    """The entry marked removed, the mark after its name, where the format lists it."""
    out: Entry = {}
    for key, value in entry.items():
        if key == "removed":
            continue
        out[key] = value
        if key == "name":
            out["removed"] = True
    out.setdefault("removed", True)
    return out


def model(module: str, read: ReadModel, before: Entry | None) -> Entry:
    """The whole file: the envelope, and the entries, with those the code no longer declares marked removed."""
    return {
        "$comment": COMMENT,
        "format": FORMAT,
        "generator": {"library": "drivine-python", "version": _version(), "language": "python"},
        "module": module,
        "fragments": with_removed(read.fragments, (before or {}).get("fragments")),
        "relationshipFragments": with_removed(read.relationship_fragments, (before or {}).get("relationshipFragments")),
        "views": with_removed(read.views, (before or {}).get("views")),
    }


def text(value: Entry) -> str:
    """The file's text, the same way every time: two spaces of indentation, keys in order, a newline at the end."""
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _version() -> str:
    try:
        return importlib.metadata.version("drivine")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


@dataclass(frozen=True)
class Project:
    """Where a project's model file goes, what it is called, and whether it wants one."""

    directory: Path
    name: str
    enabled: bool

    @property
    def file(self) -> Path:
        """The model file, `.drivine/model.json` in the project's directory."""
        return self.directory / FILE

    @classmethod
    def of(cls, start: Path) -> Project:
        """The project of the nearest pyproject.toml at or above `start`."""
        for directory in [start, *start.parents]:
            pyproject = directory / "pyproject.toml"
            if pyproject.is_file():
                config = tomllib.loads(pyproject.read_text())
                name = config.get("project", {}).get("name") or config.get("tool", {}).get("poetry", {}).get("name")
                enabled = config.get("tool", {}).get("drivine", {}).get("model", True) is not False
                return cls(directory, name or directory.name, enabled)
        raise DrivineError(f"There is no pyproject.toml at or above {start}, beside which the model file goes.")


def write_model_file(
    modules: Sequence[str], project: Project, check: bool, warn: Callable[[str], None]
) -> Literal["written", "current", "stale", "off", "empty"]:
    """
    Writes the project's model file from what the modules declare, or with `check` writes nothing and says whether it
    is current. A file is written only when it changes; one the project had is kept, its entries marked removed, when
    the modules declare nothing any more.
    """
    if not project.enabled:
        return "off"
    read = ModelReader(modules).read()
    for problem in read.problems:
        warn(f"Drivine model file: {problem}")
    before = previous(project.file, warn)
    if before is None and not (read.fragments or read.relationship_fragments or read.views):
        return "empty"
    content = text(model(project.name, read, before))
    current = project.file.is_file() and project.file.read_text() == content
    if check or current:
        return "current" if current else "stale"
    project.file.parent.mkdir(parents=True, exist_ok=True)
    project.file.write_text(content)
    return "written"
