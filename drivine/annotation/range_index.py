from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class RangeIndex:
    """
    A range index over the field, `Annotated[str, RangeIndex()]`, or, as a class argument, over the fields named:
    `class Session(NodeFragment, labels=[...], schema=[RangeIndex(["last_activity_at", "session_id"])])`. With
    `ignore_case`, it is over the lowercased copy each `CaseInsensitive` field keeps, so that it serves
    `eq_ignore_case`: see CaseInsensitive.
    """

    properties: Sequence[str] = ()
    ignore_case: bool = False
