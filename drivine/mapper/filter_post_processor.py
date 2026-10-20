from __future__ import annotations

from collections.abc import Callable
from typing import Any


class FilterPostProcessor:
    def __init__(self, predicate: Callable[[Any], bool]) -> None:
        self.predicate = predicate

    def apply(self, results: list[Any]) -> list[Any]:
        return [it for it in results if self.predicate(it)]
