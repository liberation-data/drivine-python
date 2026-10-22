from __future__ import annotations

from dataclasses import dataclass


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
