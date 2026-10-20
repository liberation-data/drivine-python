from __future__ import annotations

from functools import cache
from typing import Any

from pydantic import TypeAdapter


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
