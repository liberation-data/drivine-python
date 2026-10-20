from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from drivine.query.query_specification import QuerySpecification


@dataclass(frozen=True)
class GraphStatement:
    """
    A statement a backend generated, and the parameters it binds.
    """

    text: str
    bindings: Mapping[str, Any] = field(default_factory=lambda: {})

    def spec(self, more: Mapping[str, Any] | None = None) -> QuerySpecification[Any]:
        return QuerySpecification[Any](self.text).bind({**self.bindings, **(more or {})})
