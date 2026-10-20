from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AnyLabelTest:
    """The node carries at least one of the labels."""

    labels: tuple[str, ...]
