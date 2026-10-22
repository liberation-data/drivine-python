from __future__ import annotations

from collections.abc import Sequence

from drivine.backend.postgres.postgres_store_text import (
    GRAPH_PLACEHOLDER,
    POSTGRES_STORE_ENSURING,
    POSTGRES_STORE_VERSION,
)
from drivine.backend.sql.sql_graph import DEFAULT_GRAPH, SqlGraph, sql_text
from drivine.drivine_error import DrivineError
from drivine.model import stamps

MAX_OBJECT_NAME = 63
"""Postgres names are at most 63 characters: past that, it cuts them short without a word."""


class PostgresGraph(SqlGraph):
    """
    A graph as Postgres keeps it, drivine4j's store: the tables of a SqlGraph, with `labels` and `props` as `jsonb`.
    The labels are a JSON array under a GIN index that answers whether a node carries a label
    (`labels @> '["Person"]'`), so a node carries as many as it likes: a label is data, not a table.

    Names are written bare, so Postgres folds them to lower case: a graph called `Contracts` keeps its nodes in
    `contracts_node`, and is the same graph as one called `contracts`.
    """

    VERSION = POSTGRES_STORE_VERSION
    """What the node table's comment says of a store this version made."""

    def __init__(self, name: str = DEFAULT_GRAPH) -> None:
        super().__init__(name, "Postgres", MAX_OBJECT_NAME)

    @property
    def ensuring(self) -> str:
        """
        The block that makes the store when it is not there, and does nothing when it is: drivine4j's, byte for byte
        (see postgres_store_text), so a graph is one store whichever library made it. The node table's comment, written
        last, says which version made the store: one this version made is left alone, and over one an earlier version
        made the rest is made again, which keeps its rows and has its functions replaced. The block takes a lock of its
        own for the graph first, so two processes that each find the store missing make it one after the other;
        Postgres runs a block's schema statements in its transaction, so the store is made whole or not at all.
        """
        return POSTGRES_STORE_ENSURING.replace(GRAPH_PLACEHOLDER, self.name)


def _quoted(text: str) -> str:
    """A string as JSON writes it."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def json_key(key: str) -> str:
    """
    The key of a JSON object, as a literal: refused when it is empty, which names no property. A key is written in, not
    bound, so a statement's text is the same for every value and an index over the property matches it.
    """
    if not key:
        raise DrivineError("A property name is empty: an empty name is no property a statement can read.")
    return sql_text(key)


def has_labels(alias: str, labels: Sequence[str]) -> str:
    """
    A predicate that holds when the node row `alias` carries every one of the labels: a test for each, the form the GIN
    index over labels answers, and the predicate of a partial index over the nodes of a label, so one over any of them
    is seen to hold the nodes asked for. A label may come from data, so it is escaped for JSON and for SQL.
    """
    return " AND ".join(f"{_column(alias, 'labels')} @> {sql_text(f'[{_quoted(it)}]')}" for it in labels)


def json_value(alias: str, property: str) -> str:
    """The JSON value of the property of the node or relationship row `alias`."""
    return f"({_column(alias, 'props')} -> {json_key(property)})"


def text_of(alias: str, property: str) -> str:
    """
    The property of the row `alias` as text, as `->>` reads it: a stored `""` is `""`, and a number or a boolean its
    JSON text. The stamp is a column of its own.
    """
    if property == stamps.PROPERTY:
        return _column(alias, "stamp")
    return f"({_column(alias, 'props')} ->> {json_key(property)})"


def number_of(alias: str, property: str) -> str:
    """
    The property of the row `alias` as a number where one is stored, and null for anything else, as the graph engines
    compare a text with a number as unequal: `'7'` is not `7`.
    """
    return (
        f"CASE WHEN jsonb_typeof({json_value(alias, property)}) = 'number' "
        f"THEN ({_column(alias, 'props')} ->> {json_key(property)})::numeric END"
    )


def has_property(alias: str, property: str) -> str:
    """Whether the row `alias` has the property at all, null or not."""
    if property == stamps.PROPERTY:
        return f"{alias}.stamp IS NOT NULL"
    return f"{json_value(alias, property)} IS NOT NULL"


def node_document(alias: str) -> str:
    """The map a load returns for the node row `alias`: its properties, its stamp among them, and its labels apart."""
    return (
        f"jsonb_build_object('props', {alias}.props || jsonb_strip_nulls(jsonb_build_object("
        f"{sql_text(stamps.PROPERTY)}, {alias}.stamp)), 'labels', {alias}.labels)"
    )


def _column(alias: str, name: str) -> str:
    """The row's column; the column of the table itself, as an index names it, for an empty alias."""
    return f"{alias}.{name}" if alias else name
