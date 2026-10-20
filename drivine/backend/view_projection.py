from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ViewProjection:
    """
    A view's projection: CALL blocks to run, in order, after the root is matched, and the expression the RETURN gives.
    """

    expression: str
    calls: list[str] = field(default_factory=lambda: [])
