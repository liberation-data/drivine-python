from __future__ import annotations

from typing import Any, Protocol

from drivine.dsl.condition import Condition
from drivine.dsl.scope import Scope


class ConditionWriter(Protocol):
    """Writes conditions over a class's references in a store's own language, binding their values as it goes."""

    bindings: dict[str, Any]

    def where(self, condition: Condition, scope: Scope, alias: str) -> str: ...
