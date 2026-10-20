from __future__ import annotations

from collections.abc import Callable
from functools import cache
from typing import Any, Protocol

from pydantic import TypeAdapter


class ResultPostProcessor(Protocol):
    def apply(self, results: list[Any]) -> list[Any]: ...


class MapPostProcessor:
    def __init__(self, mapper: Callable[[Any], Any]) -> None:
        self.mapper = mapper

    def apply(self, results: list[Any]) -> list[Any]:
        return [self.mapper(it) for it in results]


class FilterPostProcessor:
    def __init__(self, predicate: Callable[[Any], bool]) -> None:
        self.predicate = predicate

    def apply(self, results: list[Any]) -> list[Any]:
        return [it for it in results if self.predicate(it)]


class TransformPostProcessor:
    """
    Validates each result as a type, with a Pydantic TypeAdapter, made once for each type.
    """

    def __init__(self, type_: Any) -> None:
        self.type = type_
        self.adapter = _list_adapter(type_)

    def apply(self, results: list[Any]) -> list[Any]:
        return self.adapter.validate_python(results)


@cache
def _list_adapter(type_: Any) -> TypeAdapter[list[Any]]:
    return TypeAdapter(list[type_])  # pyright: ignore[reportInvalidTypeForm]
