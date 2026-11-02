"""
The lowercased copy a node keeps of each `CaseInsensitive` property, so that a case-insensitive lookup is a plain
comparison an index can serve. The copy of `email` is stored under `__drivine.lower.email`, beside the stamp, and is the
database's own lowercasing of the property: `toLower` on the graph engines, `LOWER` on Oracle, `lower` on Postgres. A
lookup lowercases what it is given the same way, so the copy and the value compared with it are lowercased alike, and
drivine4j, TypeScript's Drivine and drivine-python store and match the same thing.

Every Drivine write of the node sets the copy from the property as stored once the write is done, and removes it where
the property has no value. The copy is no part of whether a save changed the node: a save that leaves the property as it
was gives the node no new stamp, whether or not the copy was already there. The layout is drivine4j's.
"""

from __future__ import annotations

from collections.abc import Sequence

from drivine.backend.cypher import quote_name

PREFIX = "__drivine.lower."
"""The prefix of the property a copy is stored under."""


def of(property: str) -> str:
    """The property the copy of `property`, a stored name, is stored under."""
    return f"{PREFIX}{property}"


def set_clause(alias: str, property: str) -> str:
    """
    The `SET` item that writes the copy of `property` on the node bound to `alias`, from what the node holds: for
    Cypher of your own that writes a `CaseInsensitive` property, after it has written it. Write it in a `SET` of its
    own, after the one that writes the property, as an engine may read every value of one `SET` before it writes any:
    `f"MATCH (a:Account {{id: $id}}) SET a.email = $email SET {lower_copies.set_clause('a', 'email')}"`.
    """
    return f"{alias}.{quote_name(of(property))} = toLower({alias}.{quote_name(property)})"


def clauses(alias: str, properties: Sequence[str]) -> list[str]:
    """
    The `SET` clause that writes the copy of each of `properties` on `alias`, as a list of none or one: what a save of a
    fragment with `CaseInsensitive` fields adds after it has written them.
    """
    return [f"SET {', '.join(set_clause(alias, it) for it in properties)}"] if properties else []
