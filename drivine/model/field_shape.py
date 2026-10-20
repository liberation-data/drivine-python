from __future__ import annotations

import types
import typing
from collections import abc
from dataclasses import dataclass
from typing import Any, Literal

CollectionKind = Literal["list", "set", "tuple", "dict"]

_COLLECTIONS: dict[Any, CollectionKind] = {
    list: "list",
    tuple: "tuple",
    set: "set",
    frozenset: "set",
    dict: "dict",
    abc.Sequence: "list",
    abc.MutableSequence: "list",
    abc.Set: "set",
    abc.MutableSet: "set",
    abc.Mapping: "dict",
    abc.MutableMapping: "dict",
}


@dataclass(frozen=True)
class FieldShape:
    """
    What a field's annotation says about it: whether it may be None, whether it is a collection and of what kind,
    and the type of the item it holds.
    """

    optional: bool
    collection: CollectionKind | None
    item: Any

    @classmethod
    def of(cls, annotation: Any) -> FieldShape:
        optional = False
        if typing.get_origin(annotation) in (typing.Union, types.UnionType):
            args = typing.get_args(annotation)
            rest = [it for it in args if it is not type(None)]
            optional = len(rest) < len(args)
            if len(rest) == 1:
                annotation = rest[0]
        if typing.get_origin(annotation) is typing.Annotated:
            annotation = typing.get_args(annotation)[0]
        origin = typing.get_origin(annotation)
        collection = _COLLECTIONS.get(origin) or _COLLECTIONS.get(annotation)
        if collection is None:
            return cls(optional, None, annotation)
        args = typing.get_args(annotation)
        return cls(optional, collection, args[0] if args and collection != "dict" else Any)
