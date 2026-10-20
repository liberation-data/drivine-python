from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from drivine.model.fragment_model import FragmentModel


@dataclass(frozen=True)
class MatchedNode:
    """A node matched by its fragment's labels, and any more, and its id."""

    model: FragmentModel
    id: Any
    labels: Sequence[str] = ()
