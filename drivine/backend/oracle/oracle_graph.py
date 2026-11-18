from __future__ import annotations

import re
from collections.abc import Sequence

from drivine.backend.oracle.oracle_stored_functions import (
    OracleStoredFunctions,
    era_date_name,
    era_name,
    nulls_equal_name,
)
from drivine.backend.sql.sql_graph import DEFAULT_GRAPH, SqlGraph, sql_text
from drivine.backend.store_statements import store_statements
from drivine.model import stamps


class OracleGraph(SqlGraph):
    """
    A graph as Oracle keeps it: the tables of a SqlGraph, and the SQL property graph over the two, which view reads go
    through.
    """

    def __init__(self, name: str = DEFAULT_GRAPH) -> None:
        super().__init__(name, "Oracle", 128)

    @property
    def tables_made(self) -> str:
        """
        A query of the store's catalog whose one value is how many of the graph's table of nodes it holds, none or one:
        a graph without one is new, and its layout is not judged (see store_layout.judging).
        """
        return f'SELECT COUNT(*) AS "made" FROM user_tables WHERE table_name = {sql_text(self.nodes.upper())}'

    @property
    def ddl(self) -> list[str]:
        """What makes the store, the shared module's statements (see store_statements). Each may run again."""
        ddl = store_statements("oracle", self.name).ddl
        assert ddl is not None
        return ddl

    @property
    def nulls_equal(self) -> str:
        """
        The function a compiled Cypher statement compares two values that may hold a null by, list item by list item
        and map key by map key (see `functions`): a statement that writes is a PL/SQL block, whose queries
        call no function of its own, so the store keeps it. drivine4j and Drivine for TypeScript make it byte for byte
        alike.
        """
        return nulls_equal_name(self.name)

    @property
    def era(self) -> str:
        """
        The function a compiled Cypher statement renumbers a year that may be BC by as it reads an ISO text (see
        `CompiledCypher.calls_stored_era`), and `era_date` the one it renumbers it by as it writes one: each reads its
        text once, where the SQL it stands for read it three or four times, which Oracle took far longer to plan
        (liberation-data/drivine#572). drivine4j and Drivine for TypeScript make them byte for byte alike.
        """
        return era_name(self.name)

    @property
    def era_date(self) -> str:
        """See `era`."""
        return era_date_name(self.name)

    @property
    def functions(self) -> list[str]:
        """What makes each of the graph's functions, the shared module's: safe to run again."""
        functions = store_statements("oracle", self.name).functions
        assert functions is not None
        return functions

    @property
    def ensuring(self) -> str:
        """
        The block that makes the store when it is not there and does nothing when it is, the shared module's (see
        store_statements): the property graph is made last, so its presence says the rest is, and each of the
        `functions` is made where it is not there, not valid or of another version, so a store an earlier version made
        gains those it lacks. A user who may not make a function still has the store made: a statement then calls none
        of those it lacks (see OracleStoredFunctions). Two sessions that each find the store missing make it together:
        a statement turned away by the lock the other's holds (ORA-00054) is tried again, for up to 30 seconds.
        """
        ensuring = store_statements("oracle", self.name).ensuring
        assert ensuring is not None
        return ensuring

    def ensured(self) -> None:
        """
        Has every session asked again whether it may call the `functions` (see OracleStoredFunctions): `ensuring` has
        just run.
        """
        OracleStoredFunctions.ensured(self.name)


def json_key(name: str) -> str:
    """A JSON path step naming a key, quoted: `."metadata.source"`."""
    return '."' + _escaped(name) + '"'


_PLAIN_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def drivine4j_step(key: str) -> str:
    """
    The step of a JSON path naming `key` as drivine4j writes it: bare where SQL would take it for an identifier, else
    quoted as `json_key` quotes it, for the SQL literal it is written in.
    """
    return key if _PLAIN_KEY.fullmatch(key) and key not in ("true", "false", "null") else json_key(key)[1:]


def text_of(alias: str, property: str) -> str:
    """
    The property of the node row `alias` as text, as drivine4j's `OracleSql.property` reads it. The stamp is a column
    of its own.
    """
    if property == stamps.PROPERTY:
        return f"{alias}.stamp"
    return f"JSON_VALUE({alias}.props, '$.{drivine4j_step(property)}')"


def holding(alias: str, property: str) -> str:
    """
    The JSON the property of the node row `alias` is read from: its properties, or, for the stamp, which is a column
    of its own, a document of that column under the stamp's property, so a query reads it as any other.
    """
    if property == stamps.PROPERTY:
        return f"JSON_OBJECT({sql_text(property)} VALUE {alias}.stamp ABSENT ON NULL RETURNING JSON)"
    return f"{alias}.props"


def node_document(alias: str) -> str:
    """The map a load returns for the node row `alias`: its properties, its stamp among them, and its labels apart."""
    return (
        f"JSON_OBJECT('props' VALUE JSON_TRANSFORM({alias}.props, SET '${json_key(stamps.PROPERTY)}' = {alias}.stamp "
        f"IGNORE ON NULL), 'labels' VALUE {alias}.labels RETURNING JSON)"
    )


def has_labels(alias: str, labels: Sequence[str]) -> str:
    """
    A predicate that holds when the node row `alias` carries every one of the labels. A label may come from data (a
    NodeLabels filter's value, a search's partition label): it is escaped as `json_key` escapes a name, the backslash
    first, for the JSON string the path holds it in, and then the SQL literal around it; a label after the first is
    looked for by its text (`holds_label`).

    The first label is tested so the multivalue index over labels can answer it, and the rest by the labels' text,
    which that index can't: when two tests of the labels are answered by the index, Oracle (23.26) finds no node the
    open transaction has written and not yet committed (drivine#221). drivine4j's `OracleSql.hasLabels` writes the same.
    """
    return " AND ".join(
        f"JSON_EXISTS({alias}.labels, '$?(@ == \"{_escaped(it)}\")')" if i == 0 else holds_label(f"{alias}.labels", it)
        for i, it in enumerate(labels)
    )


def carries_labels(alias: str, labels: Sequence[str]) -> str:
    """
    A predicate that holds when the node row `alias` carries every one of the labels, each told by the labels' text
    (`holds_label`), so the index over labels answers none of them: the form for a node that is found some other way,
    by its id. Given the choice, Oracle reads every node of a label and tests each for the id, where the index over the
    id gives the one node. drivine4j's `OracleSql.hasLabels(scanned = false)` writes the same, its `carriesLabel` for
    each.
    """
    return " AND ".join(holds_label(f"{alias}.labels", it) for it in labels)


def holds_label(labels: str, label: str) -> str:
    """
    A predicate that holds when the JSON array of labels `labels` holds `label`, told by its text. `JSON_SERIALIZE`
    writes the array compactly, `["A","B"]`; with its brackets made commas and `,"` put after it, `,"A","B","`, each
    label is `,"label","`, and the label is looked for as that. The trims take one bracket at each end, since a label's
    quote stands beside each, and no labels at all leave `,,"`, which holds no label. Matched there, the needle is a
    whole label. Its first quote follows a comma, so isn't escaped: it opens a label or closes one. If it opens one, the
    label's text runs to the first unescaped quote, which is the one that closes the needle's label. If it closes one, a
    comma and an opening quote follow, which the needle has only when the label is `,`, and then the quote after that
    closes a label `,`. The label is written as `JSON_SERIALIZE` writes it (`serialized_label`), so one holding a
    control character is found, and one holding `","` is not taken for two. drivine4j's `OracleSql.holdsLabel` writes
    the same.
    """
    needle = sql_text(f',{serialized_label(label)},"')
    return f"INSTR(',' || LTRIM(RTRIM(JSON_SERIALIZE({labels}), ']'), '[') || ',\"', {needle}) > 0"


_SHORT_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}
"""The characters `JSON_SERIALIZE` writes with a short escape."""


def serialized_label(text: str) -> str:
    """
    `text` as Oracle's `JSON_SERIALIZE` writes a string, checked on Oracle Free 23 for every character: `"` and `\\`
    escaped; backspace, tab, newline, form feed and carriage return as `\\b`, `\\t`, `\\n`, `\\f` and `\\r`; every
    other character below U+0020, and U+007F, as `\\u00XX` in upper case; U+2028 and U+2029 as `\\u2028` and `\\u2029`;
    and every other character as it is.
    """
    escaped = "".join(
        _SHORT_ESCAPES.get(c) or (f"\\u{ord(c):04X}" if ord(c) < 0x20 or ord(c) in (0x7F, 0x2028, 0x2029) else c)
        for c in text
    )
    return f'"{escaped}"'


def labelled_value(alias: str, label: str, property: str) -> str:
    """
    A property of the node row `alias` as a range index or a uniqueness constraint over the label holds it: as text,
    for the label's nodes alone, and null for every other node, for a value that isn't a scalar, and for one longer
    than 4,000 bytes. A predicate over this expression implies the label.
    """
    return (
        f"CASE WHEN {has_labels(alias, [label])} THEN JSON_VALUE({alias}.props, '${json_key(property)}' "
        "RETURNING VARCHAR2(4000) NULL ON ERROR) END"
    )


def _escaped(text: str) -> str:
    """
    Text as JSON writes it inside a string, in an SQL literal: backslashes and quotes escaped, then single quotes
    doubled. A label or a key is held so in a JSON path's string and in the labels' text alike.
    """
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("'", "''")


def related_subquery(*hints: str) -> str:
    """
    The SELECT of a subquery over related nodes, with the hint every one carries and any more given. Oracle 23
    coalesces two such subqueries ORed together into one, and rewrites the second's label test as a multivalue index
    lookup that, evaluated as a filter, misses nodes with more than one label: of two roots, one related to each
    subclass, `instance_of(A) | instance_of(B)` found one. NO_COALESCE_SQ prevents it.
    """
    return f"SELECT /*+ {' '.join(['NO_COALESCE_SQ', *hints])} */ 1"
