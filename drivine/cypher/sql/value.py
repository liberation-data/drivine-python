from __future__ import annotations

from dataclasses import dataclass

from drivine.cypher.sql.temporal import Temporal


@dataclass(frozen=True)
class Json:
    """
    A JSON value: `jsonb`, or with `ordered` `json`, whose object keys keep the order they were written in. Where the
    statement makes it of a temporal kind (a constructor, a parameter of a datetime type, temporal arithmetic), it is
    that kind's ISO text; where it makes it a list whose every item is of one, `items` is that kind, so an item taken
    from it, unwound or indexed, is of it. Where it is a map or a list the statement builds that holds a node or a
    relationship, `identity` is the same value, as JSON that compares, with each node or relationship in it as its id:
    it is compared by that, as a graph engine compares what it holds by identity, not by its properties.
    """

    sql: str
    ordered: bool = False
    temporal: Temporal | None = None
    items: Temporal | None = None
    identity: str | None = None
    item_items: Temporal | None = None
    """Where the statement makes it a list whose every item is a list of `items` of one kind, that kind: an item taken
    from it has it as its `items`."""
    temporals: bool = False
    """
    Whether the statement makes it a list or a map that holds, at any depth, a value it makes temporal: deeper than
    `items` and `item_items` tell, or beside values of other kinds, as in `[l, l]` of a list `l` of them. Of an item
    taken from such a list, of an entry read from such a map, and of a `CASE`, a `coalesce` or a `reduce` that may give
    one, that it may be or hold one. Oracle reads the items of such a list by their places (#331, #347): see
    `SqlCypherCompiler.temporal_elements`.
    """
    list: bool = False
    """Whether the statement makes it a list, and with `item_lists` a list of lists: a store may sort a list as Cypher
    does only then."""
    item_lists: bool = False
    text: bool = False
    """Whether the statement makes it a text or null (a text written, `toString`, a string function, a `+` of texts),
    and with `texts` a list whose every item is: a `+` writes such a side as its own text, not as `toString` writes a
    float."""
    texts: bool = False
    flat: bool = False
    """
    Whether it is known to hold no null in a list or a map, at any depth: a value of one kind, a property read from the
    store, which holds none, or a list or map the statement writes or is given without one. It may itself be null. Two
    such values compare as JSON; any other may hold a null, which makes Cypher's comparison of the two null where it is
    not false.
    """
    row_keys: tuple[str, ...] | None = None
    """
    Where a write reads it from the row it is making, to work out what it writes, the keys it is at in that row: a
    property of it is read from the row at those keys and the property's, as `SqlCypherCompiler.row_value` reads it.
    """
    from_list: bool = False
    """
    Whether it is read from an item of a list the query unwinds, an `UNWIND`'s or the rows a write carries on, and
    passed on as it is: it reads no node's or relationship's properties, so a property of it a `RETURN` or a `WITH`
    gives is read as `SqlCypherCompiler.item_value` reads it.
    """


@dataclass(frozen=True)
class Bool:
    """A truth value: SQL's boolean, with its null for Cypher's."""

    sql: str


@dataclass(frozen=True)
class Node:
    """A node: the columns of its row."""

    nid: str
    labels: str
    props: str
    stamp: str


@dataclass(frozen=True)
class Relationship:
    """A relationship: the columns of its row."""

    eid: str
    src: str
    dst: str
    type: str
    props: str


@dataclass(frozen=True)
class Entities:
    """
    A list of nodes, or with `relationships` of relationships: `ids`, the JSON list of their ids, in order. It is given
    as their properties, and read as the nodes or relationships themselves where it is unwound, so a write reaches
    them, and where it is compared, indexed, searched with `IN` or iterated, so they compare by their ids. Where
    `collect` gathers it, `gathered` is the JSON list of their properties, gathered beside the ids, for a store reads no
    aggregate of the query it is in from a subquery. With `flat`, it is known to hold no null: see Json.flat.
    """

    ids: str
    relationships: bool
    gathered: str | None = None
    flat: bool = False


@dataclass(frozen=True)
class Path:
    """A path: the ids of its nodes, and of its relationships, in the order the pattern names them."""

    nids: str
    eids: str


Value = Json | Bool | Node | Relationship | Entities | Path
"""
A compiled expression: the SQL that gives its value, and what kind of value it is. A Cypher value is JSON on a SQL
store, as a node's properties are: so a comparison is JSON's, and `'7'` is not `7`. A node and a relationship are their
rows' columns, so a pattern can join to them and a projection can give their properties.
"""
