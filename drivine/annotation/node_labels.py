from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class NodeLabels:
    """
    Maps a field holding strings, a list or a set, to the node's labels beyond its fragment's own. With `members` (a
    list of names, or a string Enum) the set is closed: the field reads only those labels. Without, it is open: it
    reads every label besides the fragment's own. One to a fragment.
    """

    members: tuple[str, ...] | None = None

    def __init__(self, members: Iterable[str] | type[Enum] | None = None) -> None:
        if members is None:
            names = None
        elif isinstance(members, type):
            names = tuple(str(it.value) for it in members)
        else:
            names = tuple(members)
        object.__setattr__(self, "members", names)
