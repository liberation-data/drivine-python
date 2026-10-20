from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Scored(Generic[T]):
    """A search hit and its score: a similarity, higher nearer, on one scale on every engine."""

    value: T
    score: float
