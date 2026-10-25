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
    list: bool = False
    """Whether the statement makes it a list, and with `item_lists` a list of lists: a store may sort a list as Cypher
    does only then."""
    item_lists: bool = False


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
    aggregate of the query it is in from a subquery.
    """

    ids: str
    relationships: bool
    gathered: str | None = None


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
