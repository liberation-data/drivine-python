from __future__ import annotations

import importlib
import types
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from drivine.drivine_error import DrivineError
from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.model_registry import ModelRegistry
from drivine.model.relationship_fragment_model import RelationshipFragmentModel
from drivine.model.target_model import RelatedModel

RESERVED = frozenset({"instance_of", "has_any_label", "property", "predicate", "field", "any", "none", "path"})
"""What every node's references offer beside its fields: a field of one of these names is reached with `field`."""

_ORDERED = (int, float, Decimal, date, datetime, time, timedelta)
"""Types whose values a query orders, and compares by order."""

_COLLECTIONS = (list, tuple, set, frozenset, Sequence, AbstractSet)

_BUILTINS = frozenset({"int", "float", "str", "bool", "bytes"})


class StubWriter:
    """
    Writes typed references for the models some modules declare: a module that is drivine's `refs` at run time, and a
    stub beside it that a type checker reads instead. The stub declares, for each model, the references of its fields
    (`PersonRefs`), what a relationship to it reaches (`PersonNodes`, which adds `any` and `none`), and an overload of
    `refs` that gives a class's references. A field's reference follows from its type, as TypeScript's `Refs<T>`
    derives it: text compares as text, numbers and dates by order, lists hold items, and a relationship leads to the
    fields of the nodes it reaches.
    """

    def __init__(self, modules: Sequence[str], command: str) -> None:
        """`command` is how the files were written, for their header, so a reader knows how to write them again."""
        self.modules = list(modules)
        self.command = command
        self._registry = ModelRegistry()
        self._imports: dict[str, tuple[str, str]] = {}
        """Each name the stub uses, by the module and qualified name it is imported as."""
        self._models: dict[type, RelatedModel] = {}

    def write(self) -> tuple[str, str]:
        """The runtime module and the stub, as text."""
        for type_ in declared(self.modules):
            self._add(type_)
        if not self._models:
            raise DrivineError(
                f"{', '.join(self.modules)} declares no NodeFragment, GraphView or RelationshipFragment."
            )
        ordered = sorted(self._models, key=lambda it: (it.__module__, it.__qualname__))
        classes = [self._protocols(it) for it in ordered]
        loadable = [it for it in ordered if not isinstance(self._models[it], RelationshipFragmentModel)]
        # A subclass's overload first: an overload of its parent would match it too.
        overloads = sorted(loadable, key=lambda it: -len(it.__mro__))
        return self._runtime(ordered), self._stub(classes, overloads)

    def _add(self, type_: type) -> None:
        if type_ in self._models:
            return
        kind = self._registry.kind_of(type_)
        if kind == "NODE_FRAGMENT":
            self._models[type_] = self._registry.fragment(type_)
        elif kind == "GRAPH_VIEW":
            view = self._registry.view(type_)
            self._models[type_] = view
            self._add(view.root.type)
            for rel in view.relationships:
                self._add(rel.related.type)
        elif kind == "RELATIONSHIP_FRAGMENT":
            model = self._registry.relationship_fragment(type_)
            self._models[type_] = model
            if model.target is not None:
                self._add(model.target.type)

    def _protocols(self, type_: type) -> str:
        """The references of a model's fields, and what a relationship to it reaches."""
        name = _prefix(type_)
        fields = self._fields(type_, self._models[type_])
        lines = [f"class {name}Refs(NodeTests, Protocol):"]
        for field, ref in fields:
            lines.append(f"    {field}: {ref}" if ref else f'    # {field}: reached with field("{field}")')
        if not fields:
            lines.append("    pass")
        lines += [
            "",
            "",
            f"class {name}Nodes({name}Refs, Protocol):",
            f"    def any(self, conditions: Callable[[{name}Nodes], Where], /) -> Condition: ...",
            f"    def none(self, conditions: Callable[[{name}Nodes], Where], /) -> Condition: ...",
        ]
        return "\n".join(lines)

    def _fields(self, type_: type, model: RelatedModel) -> list[tuple[str, str | None]]:
        """Each field a query reaches, in the class's order, with its reference; None for a reserved name."""
        hints = get_type_hints(type_)
        refs: dict[str, str] = {}
        if isinstance(model, GraphViewModel):
            refs[model.root_field] = f"{_prefix(model.root.type)}Nodes"
            for rel in model.relationships:
                refs[rel.field] = f"{_prefix(rel.related.type)}Nodes"
        elif isinstance(model, FragmentModel):
            for field in model.graph_properties:
                refs[field] = self._value_ref(hints.get(field, Any), field in model.json_packed)
            for bag in model.property_bags:
                refs[bag.field] = "BagRefs"
            if model.node_labels is not None:
                refs[model.node_labels.field] = "ListRef[str]"
            if model.stamp_field:
                refs[model.stamp_field] = "StringRef"
        else:
            for field in model.graph_properties:
                refs[field] = self._value_ref(hints.get(field, Any), field in model.json_packed)
            if model.target is not None:
                refs[model.target_field] = f"{_prefix(model.target.type)}Nodes"
        order = list(getattr(type_, "model_fields", {}))
        fields = sorted(refs, key=lambda it: order.index(it) if it in order else len(order))
        return [(it, None if it in RESERVED else refs[it]) for it in fields]

    def _value_ref(self, hint: Any, json_packed: bool) -> str:
        """The reference of a property of the type: as TypeScript's `RefOf` derives it."""
        if json_packed:
            return "ValueRef[Any]"
        members = _without_none(hint)
        if len(members) != 1:
            if members and all(_is_text(it) for it in members):
                return "StringRef"
            if members and all(_is_ordered(it) for it in members):
                return f"ComparableRef[{' | '.join(self._type(it) for it in members)}]"
            return "ValueRef[Any]"
        value = members[0]
        if _is_text(value):
            return "StringRef"
        if value is bool:
            return "ValueRef[bool]"
        if _is_ordered(value):
            return f"ComparableRef[{self._type(value)}]"
        if isinstance(value, type) and issubclass(value, Enum):
            return f"ValueRef[{self._type(value)}]"
        origin = get_origin(value)
        if origin in _COLLECTIONS or (isinstance(value, type) and issubclass(value, _COLLECTIONS)):
            items = [it for it in get_args(value) if it is not Ellipsis]
            item = _without_none(items[0]) if len(items) == 1 else []
            if len(item) == 1 and _is_scalar(item[0]):
                return f"ListRef[{self._type(item[0])}]"
            return "ListRef[Any]"
        return "ValueRef[Any]"

    def _type(self, type_: Any) -> str:
        """How the stub names a type, imported where it isn't a builtin."""
        if get_origin(type_) is Literal:
            return f"Literal[{', '.join(repr(it) for it in get_args(type_))}]"
        if type_.__name__ in _BUILTINS and type_.__module__ == "builtins":
            return type_.__name__
        return self._name(type_)

    def _name(self, type_: type) -> str:
        top = type_.__qualname__.split(".")[0]
        name = top
        while name in self._imports and self._imports[name] != (type_.__module__, top):
            name = f"{type_.__module__.replace('.', '_')}_{top}"
        self._imports[name] = (type_.__module__, top)
        return name + type_.__qualname__[len(top) :]

    def _header(self) -> str:
        return (
            f"# Written by drivine: write it again when the models change, rather than editing it.\n#   {self.command}"
        )

    def _runtime(self, types_: list[type]) -> str:
        names = [f"{_prefix(it)}{suffix}" for it in types_ for suffix in ("Refs", "Nodes")]
        lines = [
            self._header(),
            '"""',
            f"Typed references for the models of {', '.join(self.modules)}. At run time `refs` is drivine's, and every",
            "reference a Ref; a type checker reads the stub beside this module, which types each field.",
            '"""',
            "",
            "from drivine import Ref, refs",
            "",
        ]
        lines += [f"{it} = Ref" for it in names]
        lines += ["", f"__all__ = [{', '.join(repr(it) for it in ['refs', *names])}]", ""]
        return "\n".join(lines)

    def _stub(self, classes: list[str], overloads: list[type]) -> str:
        refs = [f"@overload\ndef refs(type_: type[{self._name(it)}], /) -> {_prefix(it)}Refs: ..." for it in overloads]
        refs.append("@overload\ndef refs(type_: type[object], /) -> Any: ...")
        imports = self._import_lines()
        literal = "Literal, " if any("Literal[" in it for it in classes) else ""
        head = [
            self._header(),
            "from collections.abc import Callable",
            f"from typing import Any, {literal}Protocol, overload",
            "",
            "from drivine import (\n    BagRefs,\n    ComparableRef,\n    Condition,\n    ListRef,\n    NodeTests,\n"
            "    StringRef,\n    ValueRef,\n    Where,\n)",
            *imports,
        ]
        return "\n".join(head) + "\n\n\n" + "\n\n\n".join([*classes, *refs]) + "\n"

    def _import_lines(self) -> list[str]:
        by_module: dict[str, list[str]] = {}
        for name, (module, top) in self._imports.items():
            by_module.setdefault(module, []).append(top if name == top else f"{top} as {name}")
        lines: list[str] = []
        for module, names in sorted(by_module.items()):
            line = f"from {module} import {', '.join(sorted(names))}"
            if len(line) > 120:
                line = f"from {module} import (\n" + "".join(f"    {it},\n" for it in sorted(names)) + ")"
            lines.append(line)
        return lines


def declared(modules: Sequence[str]) -> list[type]:
    """
    The models each module declares, in its own order: a class it imports from elsewhere is that module's, and is
    reached, if at all, through a model that leads to it.
    """
    found: list[type] = []
    for name in modules:
        module = importlib.import_module(name)
        for value in list(vars(module).values()):
            if isinstance(value, type) and value.__module__ == name and ModelRegistry.kind_of(value) is not None:
                found.append(value)
    return found


def _prefix(type_: type) -> str:
    """What a model's protocols are named after: its class's name."""
    return type_.__qualname__.replace(".", "")


def _without_none(hint: Any) -> list[Any]:
    """The types of a hint, a union's members, but None."""
    if get_origin(hint) in (Union, types.UnionType):
        return [it for it in get_args(hint) if it is not type(None)]
    return [] if hint is type(None) else [hint]


def _is_text(type_: Any) -> bool:
    if get_origin(type_) is Literal:
        return all(isinstance(it, str) for it in get_args(type_))
    return type_ is str


def _is_ordered(type_: Any) -> bool:
    return isinstance(type_, type) and issubclass(type_, _ORDERED) and type_ is not bool


def _is_scalar(type_: Any) -> bool:
    return (
        _is_text(type_) or type_ is bool or _is_ordered(type_) or (isinstance(type_, type) and issubclass(type_, Enum))
    )
