from __future__ import annotations

from typing import Any, Protocol


class ResultPostProcessor(Protocol):
    def apply(self, results: list[Any]) -> list[Any]: ...
