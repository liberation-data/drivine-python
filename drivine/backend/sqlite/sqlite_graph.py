from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from drivine.backend.sql.sql_graph import DEFAULT_GRAPH, SqlGraph, sql_text
from drivine.backend.sql.store_layout import judge
from drivine.backend.sql.write_plan import WritePlan, WriteReturns
from drivine.backend.store_statements import store_statements
from drivine.connection.reused_ids_error import ReusedIdsError
from drivine.drivine_error import DrivineError
from drivine.model import stamps

MAX_OBJECT_NAME = 63
"""SQLite takes a name of any length; this keeps one as long as the other stores' do, as drivine4j does."""

REUSES_IDS = -1
"""What `made` gives for a store whose tables give a deleted row's id again: see ReusedIdsError."""

DATABASE_FILE = "SELECT file FROM pragma_database_list WHERE name = 'main'"
"""The file of the connection's database, read only to name it as a store is refused: empty for one in memory."""

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
EQUAL = "drivine_equal"
ADD = "drivine_add"
CANONICAL = "drivine_canonical"
CYPHER = "drivine_cypher"
UPPER = "drivine_upper"
SPLIT = "drivine_split"
REVERSE = "drivine_reverse"
UUID = "drivine_uuid"
REGEX_GROUP = "drivine_regex_group"
REGEX_REPLACE = "drivine_regexp_replace"
TRUNC = "drivine_trunc"
TO_CHAR = "drivine_to_char"
TEMPORAL = "drivine_temporal"
FLOAT_TEXT = "drivine_float_text"
SORT_KEY = "drivine_sort_key"
LIST_ORDER = "drivine_list_order"
MATH = "drivine_math"
ROUND = "drivine_round"
FAIL = "drivine_fail"
"""
The names of the functions each connection is given, drivine4j's: the shared module's from its extension (see
sqlite_extension) and drivine-python's own (see sqlite_functions).
"""

Kind = Literal["TEXT", "NUMBER", "INSTANT", "DATE", "TIME"]
"""
How a property is read: as text, as a number, as a moment (nanoseconds since the epoch), as a date, or as a time of
day (its key, see time_key).
"""

TIME_KINDS = {"time": "4", "localtime": "5"}
"""
The tags of a time, a time with an offset and a local one, each with the digit its key opens with: the module orders a
time before a local time, as Neo4j does, as these do.
"""


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

    A node's `nid` and a relationship's `eid` are `AUTOINCREMENT` keys, so an id is never given again once its row is
    deleted, as Oracle's and Postgres's identity keys are never given again: without it SQLite gives a new row the
    greatest key plus one, which is the key of the last row deleted when it had the greatest (drivine4j#713). A file
    whose tables were made without it, by an earlier Drivine, is refused as the store is looked for (see `ensure`):
    there is no migration, as SQLite cannot change a table's key in place.
    """

    def __init__(self, name: str = DEFAULT_GRAPH) -> None:
        super().__init__(name, "SQLite", MAX_OBJECT_NAME)

    @property
    def tables_made(self) -> str:
        """
        A query of the store's catalog whose one value is how many of the graph's table of nodes it holds, none or one:
        a graph without one is new, and its layout is not judged (see store_layout.judging).
        """
        return (
            f"SELECT count(*) AS \"made\" FROM sqlite_master WHERE type = 'table' "
            f"AND lower(name) = {sql_text(self.nodes.lower())}"
        )

    @property
    def ddl(self) -> list[str]:
        """
        What makes the store when it is not there: the shared module's statements (see store_statements), so each
        library makes the same file. Each may run again; the last, the lock table's row, makes nothing of its own.
        """
        ddl = store_statements("sqlite", self.name).ddl
        assert ddl is not None
        return ddl

    @property
    def made(self) -> str:
        """
        Counts what of the `ddl` makes is there: each table and index, by its name in any case, and each statement that
        makes nothing of its own (the lock table's row) as there where the rest are, as the module counts it
        (`SqliteDdl.made`). A user who may only read the file, or a connection with `query_only` set, is refused a
        `CREATE … IF NOT EXISTS` even over a table that is there, so the store is looked for first.

        The count is REUSES_IDS where the table of nodes or of relationships is there and was made without
        `AUTOINCREMENT`, as SQLite keeps the statement that made it: one look finds both. drivine4j's text: it looks for
        `KEY AUTOINCREMENT`, so that a graph whose name holds the word is not taken for one made with it.
        """
        making: list[str] = []
        for it in self.ddl:
            found = re.search(r"IF NOT EXISTS (\w+)", it)
            if found is not None:
                making.append(sql_text(found.group(1).lower()))
        rows = len(self.ddl) - len(making)
        keyed = ", ".join(sql_text(it.lower()) for it in (self.nodes, self.edges))
        return (
            "SELECT CASE WHEN EXISTS (SELECT 1 FROM sqlite_master WHERE type = 'table' AND lower(name) IN "
            f"({keyed}) AND sql NOT LIKE '%KEY AUTOINCREMENT%') THEN {REUSES_IDS} ELSE COUNT(*)"
            f"{f' + {rows}' if rows else ''} END FROM sqlite_master WHERE lower(name) IN ({', '.join(making)})"
        )

    def ensure(self, run: Callable[[str], Any], rows: Callable[[str, Mapping[str, Any]], list[Any]]) -> None:
        """
        Makes the store, each statement of the `ddl` on its own, unless `made` finds all it makes there; `run` gives the
        first value of a statement's first row, None for none, and `rows` a statement's rows, bound. A store whose
        tables give a deleted row's id again is refused with ReusedIdsError, naming its file, and then the graph's
        layout is judged (see store_layout.judge), before anything is made: one this version doesn't read is refused
        with StoreLayoutError.
        """
        if run(self.made) == REUSES_IDS:
            raise ReusedIdsError(self.name, run(DATABASE_FILE) or None)
        judge("sqlite", self.name, self.tables_made, rows)
        # Counted again: the verdict may have dropped what an earlier layout left.
        if run(self.made) == len(self.ddl):
            return
        for it in self.ddl:
            run(it)

    booleans: tuple[str, str] = ("1", "0")
    """True and false written into a patch's query, as the driver binds them (see PlanGraph)."""

    def write_call(self, returns: WriteReturns) -> str:
        """
        The statement that carries out a plan by the write function of the connection, drivine4j's: the same text for
        every plan of the graph. The function gives back a JSON array, read as rows in order, each the SQL value
        `json_each` gives: a stamp as text, a count as a number. Drivine for TypeScript's text, character for character
        (drivine4j's reads the same rows as JSON, under JSON_ROWS).
        """
        call = (
            f"{WRITE}({sql_text(self.name)}, ${WritePlan.PLAN}, ${WritePlan.SHAPES}, ${WritePlan.MARK}, '{returns}', "
            f"${WritePlan.PATCHED_IF})"
        )
        return f"SELECT w.value AS v FROM json_each({call}) w ORDER BY w.key"


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
    return holds_label(column(alias, "labels"), label)


def holds_label(labels: str, label: str) -> str:
    """Whether the JSON array of labels `labels` holds the label: the test `has_label` makes of a node's column, which
    compiled Cypher makes too."""
    return f"instr({labels}, {sql_text(quoted(label))}) > 0"


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


def temporal_text(alias: str, property: str) -> str:
    """
    The text of the property of the row `alias` as a comparison reads a temporal value's: where store layout 2 holds it
    as its tag (`{"$t": "date", "v": "2024-01-01", "s": …}`, see value_layout), as a save writes a moment, a date, a
    time or a duration, the tag's text `v`; anything else as text_of reads it, as a moment written as text. A map is a
    tag only where it has the key `$t`, which the layout escapes in a map of the user's (`$$t`).
    """
    props, at = column(alias, "props"), f"$.{quoted(property)}"
    tag, held = sql_text(at + '."$t"'), f"{props} ->> {sql_text(at + '.v')}"
    return f"(CASE WHEN json_type({props}, {tag}) = 'text' THEN {held} ELSE {text_of(alias, property)} END)"


def time_key(alias: str, property: str, tag: str | None = None) -> str:
    """The property of the row `alias` as the key of the time it holds: see time_key_of."""
    return time_key_of(column(alias, "props"), f"$.{quoted(property)}", tag)


def time_key_of(json: str, at: str, tag: str | None = None) -> str:
    """
    The time the tag at the path `at` of the JSON `json` holds, as a key that compares and orders as Cypher compares
    and orders times: the digit of its kind (see TIME_KINDS), then the tag's sort key `s`, which the module works out
    of the time's UTC nanoseconds of the day, unwrapped at midnight, then its offset. So a time with an offset is before
    another where it is earlier in UTC, as Neo4j has it, and equal to it only at the same offset, as two of a kind are
    equal exactly where their texts are. Given the kind `tag` names, a time of the other kind is null: Cypher compares a
    time with an offset with a local one as null. Anything else is null, as a text is never a time.
    """
    tag_at, key = sql_text(at + '."$t"'), f"{json} ->> {sql_text(at + '.s')}"
    kinds = [tag] if tag is not None else list(TIME_KINDS)
    # `||` binds no tighter than `->>`, so the key is in parentheses of its own.
    branches = " ".join(f"WHEN {sql_text(it)} THEN {sql_text(TIME_KINDS[it])} || ({key})" for it in kinds)
    return f"(CASE {json} ->> {tag_at} {branches} END)"


def typed(kind: Kind, text: str) -> str:
    """
    `text`, the text of a value, read as `kind` compares: a moment as nanoseconds since the epoch (see epoch), so two
    written with different offsets compare as the moments they are, to the nanosecond; a date as SQLite writes one; a
    time's key (see time_key) as it is.
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
