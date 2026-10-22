from __future__ import annotations

import re
from collections.abc import Sequence

from drivine.drivine_error import DrivineError

DEFAULT_GRAPH = "drivine"
"""The graph a manager on a SQL store works on when none is named."""

_LONGEST_ADDED = len("_v__") + 8 + 2
"""What the longest name a graph gives an object adds to the graph's own: `_v__`, a hash and the `_t` beside it."""


class SqlGraph:
    """
    A graph as a SQL store keeps it, drivine4j's layout, so a store written from the JVM, from Node and from Python is
    one: a table of nodes, each with its labels (a JSON array), its properties (a JSON object keyed by stored name) and
    its stamp; a table of relationships, from `src` to `dst`, with a type and JSON properties; and a catalog of what
    Drivine made of indexes and constraints. A node's relationships go with it: the relationship table cascades.

    What a store makes beside the tables is named by `object_name`, within the longest name the store allows.
    """

    def __init__(self, name: str, engine: str, max_object_name: int) -> None:
        """`engine` is the store's name, for what is said of a name it can't take; `max_object_name` its longest."""
        longest = max_object_name - _LONGEST_ADDED
        if not re.match(rf"^[A-Za-z][A-Za-z0-9_]{{0,{longest - 1}}}$", name):
            raise DrivineError(
                f"'{name}' can't name a graph on {engine}: it names tables, so it is letters, digits and underscores, "
                f"beginning with a letter, at most {longest}."
            )
        self.name = name
        self._max_object_name = max_object_name
        self.nodes = f"{name}_node"
        self.edges = f"{name}_edge"
        self.catalog = f"{name}_schema"
        """What Drivine made of indexes and constraints."""

    def vectors(self, label: str, property: str) -> str:
        """The table that holds the embeddings a vector index on the label's property is over."""
        return self.object_name("v", [label, property])

    def texts(self, label: str, properties: Sequence[str]) -> str:
        """The table that holds the text a full-text index on the label's properties is over."""
        return self.object_name("t", [label, *properties])

    def object_name(self, kind: str, parts: Sequence[str]) -> str:
        """
        A name for an object of `kind` made for `parts`, as drivine4j names it, so the JVM, Node and Python make one:
        the graph's name, the kind, what of the parts reads as a name, and a hash of the parts (Java's String.hashCode),
        leaving room for the `_t` or `_i` an object made beside it adds within the longest name the store allows.
        """
        hashed = format(_java_hash("\u0000".join(parts)) & 0xFFFFFFFF, "x")
        fixed = len(f"{self.name}_{kind}__{hashed}") + 2
        room = min(max(self._max_object_name - fixed, 0), 40)
        readable = "".join(re.sub(r"[^A-Za-z0-9]", "", it) for it in parts)[:room]
        return f"{self.name}_{kind}_{readable}_{hashed}"


def sql_text(text: str) -> str:
    """A string as an SQL literal."""
    return "'" + text.replace("'", "''") + "'"


def _java_hash(text: str) -> int:
    """Java's String.hashCode, over UTF-16 code units, as a 32-bit int."""
    units = text.encode("utf-16-le")
    hashed = 0
    for i in range(0, len(units), 2):
        hashed = (31 * hashed + int.from_bytes(units[i : i + 2], "little")) & 0xFFFFFFFF
    return hashed - (1 << 32) if hashed >= 1 << 31 else hashed
