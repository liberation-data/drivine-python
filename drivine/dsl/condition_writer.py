from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from drivine.dsl.condition import Condition
from drivine.dsl.scope import Scope


class ConditionWriter(Protocol):
    """Writes conditions over a class's references in a store's own language, binding their values as it goes."""

    @property
    def bindings(self) -> Mapping[str, Any]:
        """What the conditions written so far bind, by the names they give them."""
        ...

    def where(self, condition: Condition, scope: Scope, alias: str) -> str: ...
