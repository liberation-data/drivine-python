from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class RangeIndex:
    """
    A range index over the field, `Annotated[str, RangeIndex()]`, or, as a class argument, over the fields named:
    `class Session(NodeFragment, labels=[...], schema=[RangeIndex(["last_activity_at", "session_id"])])`.
    """

    properties: Sequence[str] = ()
