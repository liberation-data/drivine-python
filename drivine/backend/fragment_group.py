from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from drivine.model.fragment_model import FragmentModel


@dataclass
class FragmentGroup:
    """The fragments of one class in a batch."""

    model: FragmentModel
    objects: list[Any] = field(default_factory=lambda: [])
