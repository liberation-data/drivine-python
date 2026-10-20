"""
A node's stamp: two random tokens of 64 bits, `<data>:<links>`, stored under `__drivine.stamp`.

The data token speaks for the node's own properties and labels, the links token for its relationships, of every type
and in both directions. To mark a node is to replace one of its tokens. The format is drivine4j's and TypeScript's, so
a store written from any of them is checked alike.
"""

from __future__ import annotations

import secrets

PROPERTY = "__drivine.stamp"
TOKEN = 16
NEVER_STAMPED = "-"
"""What a node with no stamp is found to carry."""

_QUOTED = "`__drivine.stamp`"
_ENGINE_TOKEN = f"left(replace(randomUUID(), '-', ''), {TOKEN})"
"""A new token, made by the engine."""


def fresh() -> str:
    return f"{secrets.token_hex(8)}:{secrets.token_hex(8)}"


def data_token(stamp: str) -> str:
    return stamp.split(":")[0]


def links_token(stamp: str) -> str | None:
    index = stamp.find(":")
    return stamp[index + 1 :] if 0 <= index < len(stamp) - 1 else None


def handed_back(carried: str | None, found: str, now: str) -> str:
    """
    The stamp an object is handed back after a save. Each token is the node's new one only if what it speaks for was
    as the object's stamp said when the save began; otherwise the object keeps the token it had, and a save that
    compares it is refused until the object is loaded again.

    `found` is what the node carried before the save: '' when the save made it, '-' when it had no stamp.
    """
    if carried is None:
        # A node the save made is the object's whole. Of one that was there, the object's lists didn't come from the
        # store, so it is handed the data token alone.
        return now if found == "" else f"{data_token(now)}:"
    if carried == found:
        return now
    data = data_token(now) if data_token(carried) == data_token(found) else data_token(carried)
    links = links_token(now) if links_token(carried) == links_token(found) else links_token(carried)
    return f"{data}:{links or ''}"


def two_tokens_of(alias: str) -> str:
    """
    The node's stamp, in Cypher, if it is two tokens, as every save leaves it; null otherwise, which counts as none.
    """
    return (
        f"CASE WHEN size({alias}.{_QUOTED}) = {2 * TOKEN + 1} AND substring({alias}.{_QUOTED}, {TOKEN}, 1) = ':' "
        f"THEN {alias}.{_QUOTED} ELSE null END"
    )


def set_clause(alias: str) -> str:
    """
    The Cypher `SET` item that marks the node bound to `alias` as changed, for Cypher of your own that changes its
    properties or labels: `f"MATCH (p:Person {{id: $id}}) SET p.name = $name, {stamps.set_clause('p')}"`.
    """
    return (
        f"{alias}.{_QUOTED} = {_ENGINE_TOKEN} + ':' + coalesce(right({two_tokens_of(alias)}, {TOKEN}), {_ENGINE_TOKEN})"
    )


def links_clause(alias: str) -> str:
    """
    The Cypher `SET` item that marks the relationships of the node bound to `alias` as changed, for Cypher of your own
    that adds or removes a relationship, or changes one's properties. Use it for both ends.
    """
    return (
        f"{alias}.{_QUOTED} = coalesce(left({two_tokens_of(alias)}, {TOKEN}), {_ENGINE_TOKEN}) + ':' + {_ENGINE_TOKEN}"
    )
