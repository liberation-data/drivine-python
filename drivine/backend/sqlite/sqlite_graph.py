from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from drivine.backend.sql.sql_graph import DEFAULT_GRAPH, SqlGraph, sql_text
from drivine.backend.sql.write_plan import WritePlan, WriteReturns
from drivine.drivine_error import DrivineError
from drivine.model import stamps

MAX_OBJECT_NAME = 63
"""SQLite takes a name of any length; this keeps one as long as the other stores' do, as drivine4j does."""

JSON_ROWS = "/* drivine:json */"
"""
What a statement whose text values are JSON opens with: Drivine's own statements over a SQLite graph give documents as
JSON text, and SQLite has no JSON type for a column to say so. drivine4j's marker, read by SqliteConnection.
"""

WRITE = "drivine_write"
DELETE = "drivine_delete"
LOWER_COPIES = "drivine_lower_copies"
SIMILARITY = "drivine_similarity"
REGEXP_COUNT = "drivine_regexp_count"
LOWER = "drivine_lower"
EPOCH = "drivine_epoch"
"""The names of the functions Drivine registers on each connection (see sqlite_functions), drivine4j's."""

Kind = Literal["TEXT", "NUMBER", "INSTANT", "DATE"]
"""How a property is read: as text, as a number, as a moment (nanoseconds since the epoch) or as a date."""


class SqliteGraph(SqlGraph):
    """
    A graph as SQLite keeps it, drivine4j's layout: the tables of a SqlGraph, with `labels`, `props` and a
    relationship's `props` as JSON text. Text and not SQLite's binary `jsonb`, so that drivine4j and Drivine for
    TypeScript read the same file as it is.

    There is no index over the labels: SQLite has none that answers whether a JSON array holds a value. A label is
    tested on the text of the array (see has_label), which a partial index of the schema grammar answers for the nodes
    of a label.

    Every statement of `ddl` is safe to run again, so the store is made by running them all where it is not all there,
    on a provider's first connection: SQLite opens a transaction for a schema statement as for any other, and two
    processes that each find the store missing make it one after the other.

    SQLite reads a table's name without regard to case, so a graph called `Contracts` is the same graph as one called
    `contracts`, as on Postgres. The tables keep the case of the name that made them.
    """

    def __init__(self, name: str = DEFAULT_GRAPH) -> None:
        super().__init__(name, "SQLite", MAX_OBJECT_NAME)

    @property
    def ddl(self) -> list[str]:
        """What makes the store, drivine4j's statements: each may run again."""
        return [
            f"CREATE TABLE IF NOT EXISTS {self.nodes} (\n"
            "    nid    INTEGER PRIMARY KEY,\n"
            "    labels TEXT NOT NULL,\n"
            "    props  TEXT NOT NULL,\n"
            "    stamp  TEXT\n"
            ")",
            f"CREATE TABLE IF NOT EXISTS {self.edges} (\n"
            "    eid   INTEGER PRIMARY KEY,\n"
            f"    src   INTEGER NOT NULL REFERENCES {self.nodes} (nid) ON DELETE CASCADE,\n"
            f"    dst   INTEGER NOT NULL REFERENCES {self.nodes} (nid) ON DELETE CASCADE,\n"
            "    type  TEXT NOT NULL,\n"
            "    props TEXT NOT NULL DEFAULT '{}'\n"
            ")",
            f"CREATE TABLE IF NOT EXISTS {self.catalog} (\n"
            "    kind        TEXT NOT NULL,\n"
            "    label       TEXT NOT NULL,\n"
            "    properties  TEXT NOT NULL,\n"
            "    name        TEXT,\n"
            "    object_name TEXT,\n"
            "    details     TEXT NOT NULL,\n"
            "    PRIMARY KEY (kind, label, properties)\n"
            ")",
            f"CREATE INDEX IF NOT EXISTS {self.name}_edge_src ON {self.edges} (src, type)",
            f"CREATE INDEX IF NOT EXISTS {self.name}_edge_dst ON {self.edges} (dst, type)",
        ]

    @property
    def made(self) -> str:
        """
        Counts what of the `ddl` makes is there: each table and index, by its name in any case. A user who may only
        read the file, or a connection with `query_only` set, is refused a `CREATE … IF NOT EXISTS` even over a table
        that is there, so the store is looked for first.
        """
        names: list[str] = []
        for it in self.ddl:
            found = re.search(r"IF NOT EXISTS (\w+)", it)
            assert found is not None
            names.append(sql_text(found.group(1).lower()))
        return f"SELECT COUNT(*) FROM sqlite_master WHERE lower(name) IN ({', '.join(names)})"

    def write_call(self, returns: WriteReturns) -> str:
        """
        The statement that carries out a plan by the write function of the connection, drivine4j's: the same text for
        every plan of the graph. The function gives back a JSON array, read as rows: a stamp as a JSON string, as every
        text value of a statement that opens with JSON_ROWS is JSON, and a count as a number.
        """
        call = (
            f"{WRITE}({sql_text(self.name)}, ${WritePlan.PLAN}, ${WritePlan.SHAPES}, ${WritePlan.MARK}, '{returns}', "
            f"${WritePlan.PATCHED_IF})"
        )
        value = "w.value" if returns == "COUNT" else "json_quote(w.value)"
        return f"{JSON_ROWS}\nSELECT {value} FROM json_each({call}) w ORDER BY w.key"


def quoted(text: str) -> str:
    """A string as JSON writes it."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def path(key: str) -> str:
    """
    The path of the property `key` of a JSON object, as a literal, `'$."name"'`, so a name with a dot in it is one
    property and not a path into another. Refused when it is empty, which names no property.
    """
    if not key:
        raise DrivineError("A property name is empty: an empty name is no property a statement can read.")
    return sql_text(f"$.{quoted(key)}")


def column(alias: str, name: str) -> str:
    """The row's column; the column of the table itself, as an index names it, for an empty alias."""
    return f"{alias}.{name}" if alias else name


def has_label(alias: str, label: str) -> str:
    """
    Whether the node row `alias` carries the label: a test of the text of its labels for the label as JSON writes it,
    quotes and all. A label is written whole, so it is found whole; and the test is deterministic, so it is the
    predicate of a partial index over the label's nodes, which a statement that makes it is answered by.
    """
    return f"instr({column(alias, 'labels')}, {sql_text(quoted(label))}) > 0"


def has_labels(alias: str, labels: Sequence[str]) -> str:
    """Whether the node row `alias` carries every one of the labels, each a test of its own; `1 = 1` for none."""
    return " AND ".join(has_label(alias, it) for it in labels) if labels else "1 = 1"


def label_array(label: str) -> str:
    """The labels of a node that carries the label alone, as a literal of the JSON array they are stored as."""
    return sql_text(f"[{quoted(label)}]")


def json_value(alias: str, property: str) -> str:
    """The JSON of the property of the node or relationship row `alias`, as text: `'"Ada"'` for a string."""
    return f"({column(alias, 'props')} -> {path(property)})"


def json_type(alias: str, property: str) -> str:
    """The JSON type of the property, as SQLite names it (`'text'`, `'integer'`, `'array'`…); null where it has none."""
    return f"json_type({column(alias, 'props')}, {path(property)})"


def property_text(props: str, at: str) -> str:
    """
    The value at the path `at` of the JSON `props` as text, as Postgres's `->>` reads it: a string as itself, anything
    else as its JSON (`'7'`, `'1.50'`, `'true'`), and null as SQL's null. SQLite's own `->>` gives 7 as an integer and
    `true` as 1. A range index or a uniqueness constraint is made over it.
    """
    return (
        f"(CASE json_type({props}, {at}) WHEN 'text' THEN {props} ->> {at} WHEN 'null' THEN NULL "
        f"ELSE {props} -> {at} END)"
    )


def text_of(alias: str, property: str) -> str:
    """
    The property of the row `alias` as text (see property_text). The stamp is a column of its own. For an empty alias,
    the table's own column, as an index over the property names it.
    """
    if property == stamps.PROPERTY:
        return column(alias, "stamp")
    return property_text(column(alias, "props"), path(property))


def number_of(alias: str, property: str) -> str:
    """
    The property of the row `alias` as a number where one is stored, and null for anything else, as the graph engines
    compare a text with a number as unequal: `'7'` is not `7`.
    """
    return (
        f"(CASE WHEN {json_type(alias, property)} IN ('integer', 'real') "
        f"THEN {column(alias, 'props')} ->> {path(property)} END)"
    )


def typed(kind: Kind, text: str) -> str:
    """
    `text`, the text of a value, read as `kind` compares: a moment as nanoseconds since the epoch (see epoch), so two
    written with different offsets compare as the moments they are, to the nanosecond; a date as SQLite writes one.
    """
    if kind == "INSTANT":
        return epoch(text)
    if kind == "DATE":
        return f"date({text})"
    return text


def epoch(text: str) -> str:
    """The moment `text` names, as nanoseconds since the epoch: see sqlite_functions."""
    return f"{EPOCH}({text})"


def has_property(alias: str, property: str) -> str:
    """Whether the row `alias` has the property at all, null or not."""
    if property == stamps.PROPERTY:
        return f"{alias}.stamp IS NOT NULL"
    return f"{json_type(alias, property)} IS NOT NULL"


def node_document(alias: str) -> str:
    """
    The map a load returns for the node row `alias`: its properties, its stamp among them, and its labels apart. A
    merge patch leaves out a key whose value is null, so a node with no stamp has none.
    """
    return (
        f"json_object('props', json_patch({alias}.props, json_object({sql_text(stamps.PROPERTY)}, {alias}.stamp)), "
        f"'labels', json({alias}.labels))"
    )


def text(parameter: str) -> str:
    """A bound value, read as text, as a property is read."""
    return f"CAST({parameter} AS TEXT)"
