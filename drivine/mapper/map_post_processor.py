from __future__ import annotations

from collections.abc import Callable
from typing import Any


class MapPostProcessor:
    def __init__(self, mapper: Callable[[Any], Any]) -> None:
        self.mapper = mapper

    def apply(self, results: list[Any]) -> list[Any]:
        return [self.mapper(it) for it in results]
