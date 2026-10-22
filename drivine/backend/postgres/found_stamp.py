from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FoundStamp:
    """
    One node of what drivine4j's write function hands back, `index/found=stamp`: the index a step gave it, what it
    carried before the save (empty for a node the save made, `-` for one that was there without a stamp, or a patch's
    outcome), and the stamp it carries now.
    """

    index: str
    found: str
    stamp: str

    @staticmethod
    def of(entry: str) -> FoundStamp:
        """Reads an entry. An index holds no `/`, and what is found holds no `=`."""
        slash = entry.index("/")
        equals = entry.index("=", slash + 1)
        return FoundStamp(entry[:slash], entry[slash + 1 : equals], entry[equals + 1 :])


def stamps_by_place(entries: Iterable[str], handed: Mapping[str, tuple[int, Any]], places: int) -> list[dict[str, Any]]:
    """
    The entries the write function handed back, `{before, after}` for each of `places`, each a list of `{id, stamp}`,
    as the manager hands stamps back from them. `handed` gives each step's index its place and the id of its node. A
    node held twice is found by its first step as it was before the write, and its last says what it is now: so it is
    handed back the stamp it was left with, as on the graph engines.
    """
    found: list[tuple[dict[Any, str], dict[Any, str]]] = [({}, {}) for _ in range(places)]
    for entry in (FoundStamp.of(it) for it in entries):
        place, id_ = handed[entry.index]
        before, after = found[place]
        if id_ not in before:
            before[id_] = entry.found
        after[id_] = entry.stamp
    return [
        {
            "before": [{"id": id_, "stamp": it} for id_, it in before.items()],
            "after": [{"id": id_, "stamp": it} for id_, it in after.items()],
        }
        for before, after in found
    ]
