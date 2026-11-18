from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from drivine.query.query_specification import QuerySpecification


@dataclass(frozen=True)
class StoredFunctionCalls:
    """
    The stored functions of the Oracle graph `graph` a statement calls, as the shared module's fragments in it say
    (`Fragments.calls_stored_nulls_equal`, `calls_stored_era`, `calls_stored_temporal_arithmetic`): the connection makes
    sure the session may call them before it runs the statement (see OracleConnection).
    """

    graph: str
    nulls_equal: bool = False
    era: bool = False
    temporal_arithmetic: bool = False


@dataclass(frozen=True)
class GraphStatement:
    """
    A statement a backend generated, and the parameters it binds.
    """

    text: str
    bindings: Mapping[str, Any] = field(default_factory=lambda: {})
    calls: StoredFunctionCalls | None = field(default=None, kw_only=True)
    """The stored functions it calls, on Oracle, where it calls any."""

    def spec(self, more: Mapping[str, Any] | None = None) -> QuerySpecification[Any]:
        spec = QuerySpecification[Any](self.text).bind({**self.bindings, **(more or {})})
        # A backend's statement reads the store's own JSON, which is in the store layout.
        spec.store_layout = True
        spec.stored_functions = self.calls
        return spec
