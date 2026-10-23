"""
What a schema marker's inventory records, drivine4j's `SchemaVersionStore.RecordedInventory`: the key of every item an
owner applied, which of them were ensured at runtime (`SchemaManager.ensure`) rather than declared by a catalog, and
which of those wait to be recreated. A marked key is written with a prefix, in the same newline-joined `items`, so the
marker keeps its shape on every engine and drivine4j, TypeScript and Python read one another's.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

RUNTIME_PREFIX = "runtime:"
"""The prefix of an entry for an item ensured at runtime. An inventory key starts with its kind, never with this."""

STALE_RUNTIME_PREFIX = "runtime-stale:"
"""
The prefix of an entry for an item ensured at runtime whose owner was recreated before the item was ensured again: it
is recreated when next ensured.
"""


@dataclass(frozen=True)
class RecordedInventory:
    keys: tuple[str, ...]
    """Every item's inventory key, without its prefix, in the order recorded."""
    runtime: tuple[str, ...] = ()
    """The keys of the items ensured at runtime, stale ones included."""
    stale: tuple[str, ...] = ()
    """The keys of the runtime items to be recreated when next ensured."""


def read_inventory(entries: Sequence[str] | None) -> RecordedInventory | None:
    """
    What a marker's entries (its `items`, split by the backend) record; None for a marker with no inventory, which is
    not one with an empty inventory: callers must not count everything an orphan.
    """
    if entries is None:
        return None
    keys: list[str] = []
    runtime: list[str] = []
    stale: list[str] = []
    for entry in entries:
        if entry.startswith(STALE_RUNTIME_PREFIX):
            key = entry[len(STALE_RUNTIME_PREFIX) :]
            keys.append(key)
            runtime.append(key)
            stale.append(key)
        elif entry.startswith(RUNTIME_PREFIX):
            key = entry[len(RUNTIME_PREFIX) :]
            keys.append(key)
            runtime.append(key)
        else:
            keys.append(entry)
    return RecordedInventory(tuple(keys), tuple(runtime), tuple(stale))


def inventory_entries(keys: Iterable[str], runtime: Iterable[str] = (), stale: Iterable[str] = ()) -> str:
    """The marker's `items`, an entry to a line: a key in `stale` marked stale, else one in `runtime` marked runtime."""
    runtime_keys, stale_keys = set(runtime), set(stale)
    return "\n".join(
        STALE_RUNTIME_PREFIX + key if key in stale_keys else RUNTIME_PREFIX + key if key in runtime_keys else key
        for key in keys
    )
