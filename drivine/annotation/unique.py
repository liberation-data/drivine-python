from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Unique:
    """
    A uniqueness constraint over the field, or, as a class argument, over the fields named. With `ignore_case`, it is
    over the lowercased copy each `CaseInsensitive` field keeps, so that it refuses `A@x.com` beside `a@x.com`: see
    CaseInsensitive.
    """

    properties: Sequence[str] = ()
    ignore_case: bool = False
