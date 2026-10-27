from __future__ import annotations

import dataclasses
import math
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from drivine.backend.oracle.oracle_graph import holds_label
from drivine.backend.oracle.oracle_stored_functions import nulls_equal_function, nulls_equal_name
from drivine.cypher.ast.query import Query
from drivine.cypher.sql.compiled_cypher import CompiledCypher
from drivine.cypher.sql.oracle_temporal_sql import CLOCK, NOW, OracleTemporalSql
from drivine.cypher.sql.parameters import OBJECT, cypher_json, json_text, jsonable
from drivine.cypher.sql.sql_cypher_compiler import (
    CALLER,
    COUNT,
    DEGREES_TO_RADIANS,
    DOOMED,
    FOUND,
    GATHERED,
    ID,
    NESTING,
    OUT,
    PARAMS,
    PLAIN_FLOAT,
    RADIANS_TO_DEGREES,
    ROW,
    ROWS,
    SAVED,
    VALUE,
    Body,
    SqlCypherCompiler,
    Walk,
    Walked,
    indent,
    literal,
    utf8_bytes,
)
from drivine.cypher.sql.temporal_sql import TemporalSql
from drivine.cypher.sql.value import Json, Node
from drivine.drivine_error import DrivineError
from drivine.model import stamps

CURSOR = "cy_cursor"
"""The cursor a procedure returns its rows by."""

_PLAIN_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
"""A key a JSON path writes bare: what SQL would take for an identifier."""

_JSON_WORDS = ("true", "false", "null")
"""The words a JSON path reads as values, not keys."""

NULLS_EQUAL = "cy_nulls_equal"
"""
The function a query declares that compares two values that may hold a null, where the store keeps none: see
`nulls_equal`.
"""

_QUOTED = r"""'"([^"\\]|\\.)*"'"""
"""A JSON text, quoted, as an Oracle pattern: its quotes, and what is between them, an escape whole."""

_NULL_HELD = "'[[,:]null[],}]'"
"""A null in a list or a map, as an Oracle pattern over JSON's text whose texts are taken out."""


class OracleCypherCompiler(SqlCypherCompiler):
    """
    Compiles a Cypher statement to SQL over a graph's tables on Oracle: see SqlCypherCompiler.

    A value is Oracle's `JSON`, which compares, groups and orders as JSON and keeps a map's keys in the order written.
    Oracle keeps a number as a number, so a stored `2.0` reads as `2`; a floating-point number the statement writes, or
    a parameter's that is a whole number, is a `BINARY_DOUBLE`, and stays one. A text Oracle reads out of JSON is a
    `VARCHAR2` of at most 4,000 bytes, and one longer is read as a CLOB where it is measured, cut, changed or searched
    (see `texts`), but for `reverse()`, which still gives null. An empty text is null, as Oracle's empty text is.
    `reduce` is a function the query declares, and does not run in a statement that writes, in the step of a `reduce`,
    or where its step reads a path.

    A list of ids is text, each id between commas (`,1,2,`): Oracle has no array, and a recursive query takes no `JSON`
    that changes from row to row. A recursive query reads no row outside it, so a walk starts from every node that
    meets what the node walked from meets of its own, and carries the node it started from.
    """

    def __init__(self, graph: str, nulls_equal_stored: bool = True) -> None:
        """
        `nulls_equal_stored` says whether two values that may hold a null are compared by the function the store keeps
        beside the graph's tables, `nulls_equal_name`: see `nulls_equal`.
        """
        super().__init__(graph)
        self._nulls_equal_stored = nulls_equal_stored
        self._stored_nulls_equal = nulls_equal_name(graph)
        """The function the store keeps that compares two values that may hold a null: see `nulls_equal`."""
        self._calls_stored_nulls_equal = False
        """Whether the statement calls `_stored_nulls_equal`: see `compile_query`."""
        self._folding = False
        """Whether the step of a `reduce` is being written, as its function's own: see `reduce`."""
        self._nulls_compared = False
        """Whether the query declares NULLS_EQUAL: see `nulls_equal`."""
        self._extended: dict[int, str] = {}
        """
        The parameters' texts that hold a float as Oracle's extended JSON writes one, by identity: see `parameter_text`.
        A map parameter `{"$numberDouble": "2.0"}` is written as the same text as the float 2.0. Each text is kept, so
        its id is no other's.
        """
        self._extended_parameters: set[str] = set()
        """The names of a procedure's parameters that hold a float as extended JSON writes one: see `property_of`."""
        self.parameter_path = self._parameter_path
        self._long = False
        """Whether the texts `texts` gives its body are CLOBs: see `scalar`, `starts_with`, `ends_with`, `split` and
        `replace_text`, which read them otherwise."""

    def compile_query(self, query: Query, parameters: Mapping[str, Any]) -> CompiledCypher:
        """As SqlCypherCompiler's, saying whether the statement calls the store's function: see `nulls_equal`."""
        compiled = super().compile_query(query, parameters)
        return dataclasses.replace(compiled, calls_stored_nulls_equal=self._calls_stored_nulls_equal)

    def literal_json(self, value: Any) -> str:
        if isinstance(value, float):
            return f"JSON_SCALAR(TO_BINARY_DOUBLE({literal(repr(value))}))"
        return f"JSON({literal(cypher_json(value))})"

    def parameter_json(self, marker: str, text: str | None) -> str:
        """
        A text that holds an extended JSON double is read as extended JSON, and only one: else a map of the parameter's
        whose key is `$numberDouble` would be read as a double.
        """
        return f"JSON({marker} EXTENDED)" if text is not None and id(text) in self._extended else f"JSON({marker})"

    def parameter_text(self, value: Any) -> str:
        """
        A float that is a whole number, `2.0`, written as Oracle's extended JSON writes a double,
        `{"$numberDouble": "2.0"}`, which `JSON(… EXTENDED)` reads as one: Oracle reads JSON's `2.0` as the number 2, a
        whole number, as it reads `2`. Not in a value that holds a map with a key as extended JSON's are,
        `{"$numberLong": "5"}`, which `JSON(… EXTENDED)` would read as the value it writes: such a value is written as
        JSON is, its floats that are whole numbers read as whole numbers. A statement that writes binds all its
        parameters as one text, and reads as extended JSON only those of them with such a double in them: see
        `property_of`. NaN and the infinities are texts: see `_finite`.
        """
        tree = _finite(jsonable(value))
        if _keyed_as_extended(tree):
            return json_text(tree)
        doubled = False

        def doubles(it: Any) -> Any:
            # A Decimal is no float: cypher_json writes a whole one as the whole number it is. A float of a subclass is
            # written as json writes its number, not as its own repr.
            nonlocal doubled
            if isinstance(it, float) and it.is_integer():
                doubled = True
                return {_DOUBLE: float.__repr__(it)}
            if isinstance(it, dict):
                return {key: doubles(item) for key, item in cast(dict[str, Any], it).items()}
            if isinstance(it, list):
                return [doubles(item) for item in cast(list[Any], it)]
            return it

        written = doubles(tree)
        text = json_text(written)
        if doubled:
            self._extended[id(text)] = text
            # A procedure's parameters are one map: those of its entries with a double in them are read as extended
            # JSON.
            if isinstance(written, dict):
                entries = cast(dict[str, Any], written)
                self._extended_parameters.update(key for key, item in entries.items() if _keyed_as_extended(item))
        return text

    def _parameter_path(self, marker: str, text: str | None, keys: Sequence[str], value: Any) -> str:
        """
        A value in a parameter that is a map, `$p.text`, read with one path from the text the parameter is bound as, and
        not from the parameter read as JSON first: Oracle crashed the session (#145) where a statement read a value from
        a parameter past 32 KB, taken as JSON, for each of two rows. Read as extended JSON where the parameter is (see
        `parameter_text`) and the value holds such a double.
        """
        extended = (
            keys[0] in self._extended_parameters
            if marker == PARAMS
            else text is not None and id(text) in self._extended
        )
        path = _path_of(keys)
        if extended and _doubled(value):
            return f"JSON(JSON_QUERY({marker}, {path} RETURNING CLOB) EXTENDED)"
        return f"JSON_QUERY({marker}, {path} RETURNING JSON)"

    def parameter_count(self, marker: str) -> str:
        return f"CAST({marker} AS NUMBER)"

    def comparable(self, value: Json) -> str:
        return value.sql

    def scalar(self, sql: str) -> str:
        """A CLOB that `texts` gives its body is null where it is empty, as a `VARCHAR2` is, and as Oracle's empty text
        is."""
        return f"JSON_SCALAR(CASE WHEN LENGTH({sql}) > 0 THEN {sql} END)" if self._long else f"JSON_SCALAR({sql})"

    def type_of(self, json: str) -> str:
        """A floating-point number is a number, as JSON has it."""
        return self.shared1(json, lambda x: f"DECODE({_kind(x)}, 'double', 'number', 'float', 'number', {_kind(x)})")

    def is_json_null(self, json: str) -> str:
        return f"NVL({_kind(json)}, 'null') = 'null'"

    def null_if_json_null(self, json: str) -> str:
        return self.shared1(json, lambda x: f"CASE WHEN {_kind(x)} <> 'null' THEN {x} END")

    def open_sort_key(self, json: str) -> list[str]:
        """
        Where the kind comes, as `kind_order` ranks them and null for null, then the value as `JSON` orders it, which is
        Cypher's order within a kind: `JSON` orders a number before a text, a truth value after both, and a map and a
        list last. A null is ranked null, so the value needs no `null_if_json_null`, which would read it again.
        """
        ranks = "'object', 1, 'array', 2, 'string', 3, 'boolean', 4, 'number', 5, 'double', 5, 'float', 5"
        rank = self.shared1(json, lambda x: f"DECODE({_kind(x)}, {ranks})")
        return [rank, json]

    def number(self, json: str) -> str:
        return self.shared1(
            json, lambda x: f"CASE WHEN {self.type_of(x)} = 'number' THEN JSON_VALUE({x}, '$' RETURNING NUMBER) END"
        )

    def integral(self, json: str) -> str:
        """A whole number, and no floating-point number, as Cypher has an integer."""
        return self.shared1(
            json,
            lambda x: (
                f"({_kind(x)} = 'number' AND JSON_VALUE({x}, '$' RETURNING NUMBER) = "
                f"TRUNC(JSON_VALUE({x}, '$' RETURNING NUMBER)))"
            ),
        )

    def text(self, json: str) -> str:
        """A `VARCHAR2`, which holds 4,000 bytes: a longer text is null. See `texts`, which reads one as a CLOB."""
        return self.shared1(json, lambda x: f"CASE WHEN {_kind(x)} = 'string' THEN JSON_VALUE({x}, '$') END")

    def _long_text(self, json: str) -> str:
        """The JSON as a CLOB, where it is a string: a text of any length, slower to read than `text`'s `VARCHAR2`."""
        return self.shared1(
            json, lambda x: f"CASE WHEN {_kind(x)} = 'string' THEN JSON_VALUE({x}, '$' RETURNING CLOB) END"
        )

    def texts(self, jsons: Sequence[str], body: Body) -> str:
        """
        The body over the texts as `text` reads them, `VARCHAR2`s, and where it gives null, over them as CLOBs (#168,
        drivine4j#298). A text past 4,000 bytes is null as a `VARCHAR2`, where Neo4j measures it, cuts it and searches
        it whole. The body gives null where a text it is given is null, and where what it gives would not fit in a
        `VARCHAR2`: see `replace_text` and `split`. A short text is not read as a CLOB, which is slower: `COALESCE`
        reads its second value only where the first is null. The body knows which it is given by `_long`.
        """

        def both(xs: list[str]) -> str:
            # Within a body given CLOBs, as CLOBs alone.
            if self._long:
                return body([self._text_of(it, clob=True) for it in xs])
            read = body([self._text_of(it, clob=False) for it in xs])
            self._long = True
            try:
                whole = body([self._text_of(it, clob=True) for it in xs])
            finally:
                self._long = False
            return f"COALESCE({read}, {whole})"

        return self.shared(jsons, both)

    def _text_of(self, json: str, clob: bool) -> str:
        """The JSON as text, where it is a string: a CLOB where `clob`, else as `text` reads it; a property by its own
        path (see `_direct`)."""
        return _direct(self._long_text(json) if clob else self.text(json), json)

    def bool_of(self, json: str) -> str:
        return self.shared1(
            json, lambda x: f"CASE WHEN {_kind(x)} = 'boolean' THEN JSON_VALUE({x}, '$' RETURNING BOOLEAN) END"
        )

    def float_(self, sql: str) -> str:
        return f"TO_BINARY_DOUBLE({sql})"

    def json_array(self, items: Sequence[str]) -> str:
        return f"JSON_ARRAY({', '.join(items)} NULL ON NULL RETURNING JSON)" if items else "JSON('[]')"

    def json_object(self, entries: Sequence[tuple[str, str]]) -> str:
        if not entries:
            return "JSON('{}')"
        listed = ", ".join(f"{literal(key)} VALUE {value}" for key, value in entries)
        return f"JSON_OBJECT({listed} NULL ON NULL RETURNING JSON)"

    def with_entries(self, json: str, entries: Sequence[tuple[str, str]]) -> str:
        if not entries:
            return json
        return f"JSON_TRANSFORM({json}, {', '.join(f'SET {_path(key)} = {value}' for key, value in entries)})"

    def property_of(self, json: str, key: str) -> str:
        """
        A procedure's parameter is read from the text its parameters are bound as, each where it is read, and one that
        holds a double as extended JSON (see `parameter_text`) from its own text, as extended JSON. Oracle crashes the
        session (ORA-07445 `ksmem_malloc`, then ORA-00600 `kghfrempty:ds`) where a statement run more than once reads a
        value past 32 KB from JSON it parsed, a `JSON` variable or `JSON(… EXTENDED)` of all the parameters, and not
        from the text.
        """
        if json == PARAMS and key in self._extended_parameters:
            return f"JSON(JSON_QUERY({PARAMS}, {_path(key)} RETURNING CLOB) EXTENDED)"
        return f"JSON_QUERY({json}, {_path(key)} RETURNING JSON)"

    def item_property(self, props: str, key: str) -> str:
        """
        A property returned or passed on by a `RETURN` or a `WITH`, read through its text: only the key is kept
        (`JSON_TRANSFORM … KEEP`), written out as extended JSON, then read back from that text as extended JSON, so a
        float that is a whole number stays a float.

        **A workaround for an Oracle server bug (#144, drivine4j#255), to be removed once Oracle fixes it.** Seen on
        Oracle 23.26.3 (gvenzl/oracle-free 23). Where a statement takes a string value past about 32 KB from a node's
        properties with `JSON_QUERY(props, '$.a' RETURNING JSON)` over two or more rows, as the compiled SQL does and
        then feeds it on (an outer `SELECT`, an `ORDER BY`, a `JSON_ARRAY`), the long value is written over the server
        process's heap and the process dies: the client sees ORA-03113 (end-of-file on communication channel), and the
        alert log shows ORA-00600 `[17114]` and `[kghfrh1]`, and ORA-07445 in `kghalo`, `kghsrch_new` or
        `kxsFreePages`, faulting at `0x78787878787878`, the bytes of the string. One row doesn't crash it, nor 30,000
        characters, nor a long list, nor a map that holds the long string. A plain-SQL repro is in
        `docs/oracle-long-json-string-crash.md`.

        It costs about 2.4 to 3.8 times the plain read on short values: 100,000 rows of `RETURN n.name` took 493 ms
        against 147 ms. Cheaper forms were tried and rejected: each crashed, read a CLOB column back as text (5.0 as 5),
        or, as a `CASE` on `$.a.type()` or on the value's length that reads the properties twice, made Oracle drop the
        row's label test and return nodes it had ruled out. Beside a variable-length walk the plain read is kept: Oracle
        has closed the session over this form in the more involved statements a walk writes. Only an item that is the
        property itself is read so: a property inside an item (`[n.a]`, `collect(n.a)`) is read as `property_of` reads
        it, and isn't worked around. Read so, a text past 32 KB still can't be sorted by, grouped by or made `DISTINCT`:
        Oracle refuses it (ORA-40180), as it does any JSON that large, and the session stays open. A write's block is
        worked around too, where it takes a text from the row it is writing: see `row_value`.

        To remove it, delete this override, so the item is read as `property_of` reads it, `JSON_QUERY … RETURNING
        JSON`, once an Oracle release no longer crashes on the repro, and the "a property past 32 KB read on two nodes"
        Oracle test still passes without it. `tests/unit/test_oracle_item_property.py`, which pins this SQL, goes
        with it.
        """
        return (
            f"JSON(JSON_QUERY(JSON_SERIALIZE(JSON_TRANSFORM({props}, KEEP {_path(key)}) RETURNING CLOB EXTENDED), "
            f"{_path(key)} RETURNING CLOB) EXTENDED)"
        )

    def row_value(self, keys: Sequence[str]) -> str:
        """
        A value that a write takes from the row it is writing, to work out what it writes (`SET n.a = t.a`,
        `CREATE (:X {a: t.a})`, `MERGE … SET n.a = t.a`, where `t` is an item of an unwound parameter): a text read with
        one path from the row written out as text, anything else with that path from the row as JSON, as before.

        **A workaround for the Oracle bug of `item_property` (#176, drivine4j#309), to be removed with it.** The block
        crashed the session where it took a text past 32 KB from the row, a `JSON` variable, with
        `JSON_QUERY(JSON_QUERY(cy_row, '$.v1' RETURNING JSON), '$.a' RETURNING JSON)`, for each of two or more rows.
        Read as `item_property` reads a property, through `JSON_TRANSFORM(cy_row, KEEP …)`, it crashed too; written out
        whole, `JSON_SERIALIZE(cy_row …)`, it didn't. A text is read from text that isn't extended JSON, which has no
        float to keep; anything else is read as JSON, as before, so a map of the row with a key as extended JSON's are
        (`{"$numberLong": "5"}`) stays a map. A list or a map that holds a long text is so read as JSON, which didn't
        crash: `SET n += t` sets such a map whole. The `CASE` on the value's type, which in a read made Oracle drop a
        label test (see `item_property`), doesn't here: it reads the row variable, one value for the whole statement,
        after the rows were matched, and a labelled pattern in a subquery of that statement keeps its test (the
        "an unwound item's text … is written only to the nodes a labelled pattern matches" test).

        To remove it, delete this override, so the value is read as `property_of` reads each key, once an Oracle
        release no longer crashes on the repro in `docs/oracle-long-json-string-crash.md`, and the "a text past 32 KB
        set from an unwound parameter" Oracle test still passes without it. The SQL pins in
        `tests/unit/test_oracle_item_property.py` go with it.
        """
        path = _path_of(keys)
        return (
            f"CASE WHEN JSON_VALUE({ROW}, {_path_of(keys, '.type()')}) = 'string' "
            f"THEN JSON(JSON_QUERY(JSON_SERIALIZE({ROW} RETURNING CLOB), {path} RETURNING CLOB)) "
            f"ELSE JSON_QUERY({ROW}, {path} RETURNING JSON) END"
        )

    def element(self, json: str, index: str) -> str:
        """Counted from the end where negative: a JSON path counts from the start."""

        def at(values: list[str]) -> str:
            list_, i = values
            number = f"JSON_VALUE({i}, '$' RETURNING NUMBER)"
            return (
                f"JSON_QUERY({list_}, '$[$i]' PASSING CASE WHEN {number} < 0 THEN "
                f"JSON_VALUE({list_}, '$.size()' RETURNING NUMBER) + {number} ELSE {number} END "
                'AS "i" RETURNING JSON)'
            )

        return self.shared([json, index], at)

    def keyed(self, json: str, key: str) -> str | None:
        """A JSON path names a key as it is written, so a key an expression gives is not read: `map[key]` is null,
        where `list[i]` reads its item."""
        return None

    def contains_all(self, props: str, map_: str) -> str:
        """A merge of the map over the properties that changes nothing: each of its entries is there, with an equal
        value."""
        return self.shared1(props, lambda p: f"JSON_EQUAL(JSON_MERGEPATCH({p}, {map_} RETURNING JSON), {p})")

    def has_label(self, labels: str, label: str) -> str:
        return f"JSON_EXISTS({labels}, {literal(f'$[*]?(@ == {_quoted(label)})')})"

    def has_labels(self, labels: str, all_: Sequence[str]) -> str:
        """
        The first label as the index over labels answers it, and the rest by the labels' text: when two tests of the
        labels are answered by that index, Oracle (23.26) finds no node the open transaction has written and not yet
        committed.
        """
        rest = [holds_label(labels, it) for it in all_[1:]]
        return " AND ".join([self.has_label(labels, all_[0]), *rest])

    def elements(self, json: str, alias: str) -> str:
        """
        A list that holds a subquery is read from a lateral view first: a subquery in `JSON_TABLE`'s argument reads no
        table beside it in the `FROM`.
        """
        items = f"COLUMNS (item_no FOR ORDINALITY, item JSON PATH '$')) {alias}"
        if "SELECT" not in json.upper():
            return f"JSON_TABLE({json}, '$[*]' {items}"
        return f"LATERAL (SELECT {json} AS list) {alias}_l CROSS JOIN JSON_TABLE({alias}_l.list, '$[*]' {items}"

    def temporal_elements(self, json: str, alias: str) -> str:
        """
        Each item read by its place, from as many rows as the list has items: Oracle 23 takes many seconds, or minutes,
        to parse a `JSON_TABLE` over a list of temporal values worked out with temporal arithmetic, item by item in a
        comprehension (liberation-data/drivine4j#331), and neither a query timeout nor a kill ends the parse.
        `JSON_QUERY` reads the same list in moments, at about ten times the cost of `JSON_TABLE` for a short list, so a
        list of other values is read by `elements`. The list is worked out once, in a lateral view of its own, and its
        places counted in another: where they are counted over the list's own view, or the list is written out again
        where it is read, Oracle reads an `EXISTS` over its items with the wrong row's columns. It reads as
        `JSON_TABLE`'s `$[*]` does: no rows for null, and one for a value that is no list.
        """
        list_, place = f"{alias}_l", f"{alias}_r"
        size = f"JSON_VALUE({list_}.list, '$.size()' RETURNING NUMBER)"
        return (
            f"LATERAL (SELECT /*+ NO_MERGE */ {json} AS list FROM DUAL) {list_} CROSS JOIN "
            f"LATERAL (SELECT {place}.n AS item_no, "
            f"JSON_QUERY({list_}.list, '$[$i]' PASSING {place}.n - 1 AS \"i\" RETURNING JSON) AS item "
            f"FROM (SELECT LEVEL n FROM DUAL CONNECT BY LEVEL <= GREATEST(NVL({size}, 0), 1)) {place} "
            f"WHERE {place}.n <= {size}) {alias}"
        )

    def aggregate_list(self, value: str, order: str | None = None) -> str:
        ordered = f" ORDER BY {order}" if order is not None else ""
        return f"COALESCE(JSON_ARRAYAGG({value}{ordered} NULL ON NULL RETURNING JSON), JSON('[]'))"

    def collect(self, value: str, order: str | None) -> str:
        """`JSON_ARRAYAGG` leaves nulls out."""
        ordered = f" ORDER BY {order}" if order is not None else ""
        return f"COALESCE(JSON_ARRAYAGG({value}{ordered} RETURNING JSON), JSON('[]'))"

    def joined_texts(self, text: str, order: str) -> str:
        """At most 4,000 bytes, as a `VARCHAR2`: the rest is left off, where it would fail the statement."""
        return f"LISTAGG({text} ON OVERFLOW TRUNCATE '' WITHOUT COUNT) WITHIN GROUP (ORDER BY {order})"

    def distinct_items(self, list_: str, temporal: bool = False) -> str:
        """
        Each item where it is first among those equal to it as `JSON` compares them, kept as it is: not by its text,
        which is at most 4,000 bytes and keeps a map's keys in the order written, and not read back from it, which would
        read a float that is a whole number, `2.0`, as the number 2.
        """
        return (
            "(SELECT COALESCE(JSON_ARRAYAGG(d.item ORDER BY d.item_no RETURNING JSON), JSON('[]')) "
            "FROM (SELECT e.item_no, e.item, ROW_NUMBER() OVER (PARTITION BY e.item ORDER BY e.item_no) AS n "
            f"FROM {self.elements_of(list_, 'e', temporal)}) d WHERE d.n = 1)"
        )

    def first_of_each(self, list_: str, identities: str) -> str:
        return (
            f"(SELECT {self.aggregate_list('v.item', 'v.item_no')} FROM {self.elements(list_, 'v')} "
            "JOIN (SELECT d.item_no FROM (SELECT e.item_no, ROW_NUMBER() OVER (PARTITION BY e.item ORDER BY e.item_no) "
            f"AS n FROM {self.elements(identities, 'e')}) d WHERE d.n = 1) f ON f.item_no = v.item_no)"
        )

    def extreme(self, value: str, min_: bool, key: str | None = None) -> str:
        """Of the values of the kind that comes first, or last, as `kind_order` orders them; by `key` first where there
        is one."""
        direction = "ASC" if min_ else "DESC"
        keyed = f", {key} {direction} NULLS LAST" if key is not None else ""
        return (
            f"{'MIN' if min_ else 'MAX'}({self.null_if_json_null(value)}) KEEP (DENSE_RANK FIRST ORDER BY "
            f"{self.kind_order(value)} {direction} NULLS LAST{keyed})"
        )

    def sum(self, json: str, distinct: str) -> str:
        """
        A float, `BINARY_DOUBLE`, where any value summed is one, a double or a number with a fraction, as in Cypher:
        `NUMBER` keeps no such kind, so a sum of floats that is whole, 0.5 + 1.0 + 0.5, would be the integer 2. Floats
        are summed as doubles, as Neo4j sums them: as `NUMBER`s, 1e300 would be dropped, and 0.1 + 0.2 would be 0.3.
        Nothing is bound under an aggregate, so each value is written out where it is read: each read is one path that
        tests the value's kind itself, a text not summed and a number with a fraction written with a point, or with a
        negative exponent, `1E-41`, as Oracle writes one of more than 40 digits, so a sum reads it three times, as
        `number` alone does. A path compares no item to another, `@ != @.floor()` failing with ORA-40597, so a fraction
        is told by how it is written.
        """

        def summed(type_: str) -> str:
            return f"sum({distinct}JSON_VALUE({json}, '$?(@.type() != \"string\")' RETURNING {type_}))"

        fraction = '@.string() has substring "." || @.string() has substring "E-"'
        float_ = (
            f'MAX(JSON_VALUE({json}, \'$?(@.type() == "double" || (@.type() == "number" && ({fraction})))\')) '
            "IS NOT NULL"
        )
        return (
            f"CASE WHEN {float_} THEN JSON_SCALAR({summed('BINARY_DOUBLE')}) "
            f"ELSE JSON_SCALAR(COALESCE({summed('NUMBER')}, 0)) END"
        )

    def arithmetic(self, a: str, b: str, operator: str) -> str:
        """In `BINARY_DOUBLE` where either is one, so a float stays a float, as in Cypher: `NUMBER` keeps no such
        kind."""
        return (
            f"CASE WHEN {_kind(a)} = 'double' OR {_kind(b)} = 'double' "
            f"THEN JSON_SCALAR(TO_BINARY_DOUBLE({self.number(a)}) {operator} TO_BINARY_DOUBLE({self.number(b)})) "
            f"ELSE JSON_SCALAR({self.number(a)} {operator} {self.number(b)}) END"
        )

    def add(self, a: str, b: str, given: tuple[Any, Any] = (None, None)) -> str:
        """A side that is a property is read from the properties by its own path: see `_direct`."""
        sql = (
            f"CASE WHEN {self.type_of(a)} = 'number' AND {self.type_of(b)} = 'number' THEN "
            f"{self.arithmetic(a, b, '+')} "
            f"WHEN {_kind(a)} = 'array' AND {_kind(b)} = 'array' THEN "
            f"JSON_QUERY(JSON_ARRAY({a}, {b} RETURNING JSON), '$[*][*]' RETURNING JSON WITH ARRAY WRAPPER) "
            f"WHEN {_kind(a)} = 'array' THEN JSON_TRANSFORM({a}, APPEND '$' = {b}) "
            f"WHEN {_kind(b)} = 'array' THEN JSON_TRANSFORM({b}, INSERT '$[0]' = {a}) "
            f"WHEN {self.type_of(a)} IN ('string', 'number') AND {self.type_of(b)} IN ('string', 'number') THEN "
            f"{self._text_sum(a, b, given)} END"
        )
        return _direct(_direct(sql, a), b)

    def _text_sum(self, a: str, b: str, given: tuple[Any, Any]) -> str:
        """
        Two texts put together, as `VARCHAR2`s where both are read so and their sum fits in one, 4,000 bytes; else as a
        CLOB (#168, drivine4j#298), where a text past 4,000 bytes was left out as null, and a longer sum failed
        (ORA-01489). Whether they fit is told from the sides' own texts, short to read: a number may be written
        otherwise (`_as_text`), though in at most _NUMBER_TEXT bytes. A text the statement gives is as long as it is,
        and not read for each row.
        """
        swapped = (given[1], given[0])

        def bytes_(json: str, it: Any) -> str:
            given_bytes = utf8_bytes(it)
            return str(given_bytes) if given_bytes is not None else f"LENGTHB(JSON_VALUE({json}, '$'))"

        fits = f"{bytes_(a, given[0])} + {bytes_(b, given[1])} <= {_SHORT_TEXT - 2 * _NUMBER_TEXT}"
        short = f"{self._summed_text(a, given, short=True)} || {self._summed_text(b, swapped, short=True)}"
        whole = f"TO_CLOB({self._summed_text(a, given, short=False)}) || {self._summed_text(b, swapped, short=False)}"
        # Two empty texts are null together, as Oracle's empty text is, and not an empty CLOB.
        empty = f"JSON_VALUE({a}, '$.length()') = 0 AND JSON_VALUE({b}, '$.length()') = 0"
        return f"CASE WHEN {fits} THEN JSON_SCALAR({short}) WHEN {empty} THEN NULL ELSE JSON_SCALAR({whole}) END"

    def _summed_text(self, json: str, given: tuple[Any, Any], short: bool) -> str:
        """
        The side of a `+` whose sides the statement gives as `given`, this side first, as text: see `summed_text`. A
        text as a `VARCHAR2` where `short`, null past 4,000 bytes, and else as a CLOB; a number's text is short.
        """
        if short:
            return self.summed_text(json, given[0], given[1], lambda it: f"JSON_VALUE({it}, '$')", self._as_text)
        return self.summed_text(
            json,
            given[0],
            given[1],
            lambda it: f"JSON_VALUE({it}, '$' RETURNING CLOB)",
            lambda it: (
                f"CASE WHEN {_kind(it)} = 'string' THEN JSON_VALUE({it}, '$' RETURNING CLOB) "
                f"ELSE TO_CLOB({self._as_text(it)}) END"
            ),
        )

    def _as_text(self, json: str) -> str:
        """
        The JSON, a text, a number or a truth value, as `toString` gives it: a float (a double, or a number with a
        fraction) as Java writes a double, from the shortest decimal Oracle writes it as, with no exponent from 10^-3 up
        to 10^7, and else as `1.0E7`, `1.2E-4`; NaN and the infinities by Java's names. A double past a `NUMBER`'s
        range, `1e300`, is written from Oracle's own text of it, its shortest digits, `1E+300`.
        """

        def text(x: str) -> str:
            written = f"JSON_VALUE({x}, '$')"
            float_ = f"{_kind(x)} = 'double' OR ({_kind(x)} = 'number' AND NOT {self.integral(x)})"
            n = f"TO_NUMBER({written} DEFAULT NULL ON CONVERSION ERROR)"
            double = f"JSON_VALUE({x}, '$' RETURNING BINARY_DOUBLE)"
            # Past a NUMBER's range, which reads 1e-200 as 0, the double's own text, with a point where it has none:
            # `1E-310`. TO_CHAR of the double writes 15 digits, so 1e-310 as 9.99999999999997E-311.
            scientific = _scientific(
                f"NVL(TO_CHAR(NULLIF({n}, 0), 'FM9.9999999999999999EEEE'), "
                f"REGEXP_REPLACE({written}, '^(-?[0-9]+)E', '\\1.0E'))"
            )
            return (
                f"CASE WHEN {_kind(x)} = 'double' AND {written} IN ('Nan', 'Inf', '-Inf') "
                f"THEN DECODE({written}, 'Nan', 'NaN', 'Inf', 'Infinity', '-Infinity') "
                f"WHEN {float_} THEN CASE WHEN {double} = 0 THEN '0.0' "
                f"WHEN ABS({n}) >= 0.001 AND ABS({n}) < 10000000 THEN TO_CHAR({n}, '{PLAIN_FLOAT}') "
                f"ELSE {scientific} END "
                f"ELSE {written} END"
            )

        return self.shared1(json, text)

    aggregates_case_subqueries = False
    """`collect(CASE WHEN … THEN [r] ELSE [m.title] END)` fails with ORA-00979, or closes the session: see
    `_cases_first`."""

    extremes_by_list_key = False
    """
    Oracle's JSON orders lists item by item already, and a grouped `KEEP (DENSE_RANK FIRST ORDER BY …)` by the list key
    drops the connection (ORA-03113), as drivine4j found.
    """

    def identity_equals(self, x: str, y: str) -> str:
        """`JSON_EQUAL`, which compares values larger than `=` does, with ORA-40180 past about 32 KB."""
        return f"JSON_EQUAL({x}, {y})"

    def item_at(self, json: str, index: int) -> str:
        return f"JSON_QUERY({json}, '$[{index}]' RETURNING JSON)"

    def list_size(self, json: str) -> str:
        return f"JSON_VALUE({json}, '$.size()' RETURNING NUMBER)"

    def holds_null(self, json: str) -> str | None:
        """
        A null as JSON's text writes one in a list or a map, its texts and keys taken out first, so a text that holds a
        null's text (`"a,null]"`) does not say so: Oracle's JSON path walks no descendants. Read as a `VARCHAR2`,
        faster than as a `CLOB`, which a value whose text is past 4,000 bytes is read as instead. It is exact, as a
        procedure's `nulls_equal` needs.
        """

        def nulls(text: str) -> str:
            return f"REGEXP_LIKE(REGEXP_REPLACE({text}, {_QUOTED}, '\"\"'), {_NULL_HELD})"

        short = f"JSON_SERIALIZE({json} RETURNING VARCHAR2(4000) NULL ON ERROR)"
        long = f"JSON_SERIALIZE({json} RETURNING CLOB)"
        return f"({short} IS NOT NULL AND {nulls(short)} OR {short} IS NULL AND {nulls(long)})"

    def nulls_equal(self, x: str, y: str, equals: Callable[[str, str], str]) -> str:
        """
        Two values written as the same JSON text are null, as one holds a null and so its pair in the other does: that
        needs no walk. The text is read as a `VARCHAR2`, as `holds_null` reads it; a value past 4,000 bytes has none,
        and `=` and `JSON_EQUAL` fail on one past about 32 KB (ORA-40180), so such a value is walked. Any other two are
        walked together by a function, as Oracle's recursive queries take no `JSON`: the store's own
        (`nulls_equal_name`), which the graph is made with, so a procedure's queries and a `reduce`'s step's call it
        too. Where the store has none (see `CompiledCypher.calls_stored_nulls_equal`), a query declares it (see
        `reduce`); a procedure's queries call no function it declares, nor do a `reduce`'s step's, so there two equal as
        JSON are null and any other two false, as before null was compared item by item: `a = [1]`, of `[null] AS a`,
        is false there, where Cypher's is null. Lists and maps written out are compared item by item without it (see
        `_itemwise`), so `[null] = [1]` is null everywhere.
        """
        if not self._nulls_equal_stored and (self.procedural or self._folding):
            return f"CASE WHEN {equals(x, y)} THEN NULL ELSE FALSE END"
        if self._nulls_equal_stored:
            self._calls_stored_nulls_equal = True
            walked = f"({self._stored_nulls_equal}({x}, {y}) = 1)"
        else:
            if not self._nulls_compared:
                self._nulls_compared = True
                self.declare(nulls_equal_function(NULLS_EQUAL))
            walked = f"({NULLS_EQUAL}({x}, {y}) = 1)"

        def text(json: str) -> str:
            return f"JSON_SERIALIZE({json} RETURNING VARCHAR2(4000) NULL ON ERROR)"

        return f"CASE WHEN {text(x)} = {text(y)} THEN NULL ELSE {walked} END"

    def id_tokens(self, ids: str, key: str) -> str:
        token = literal('{"' + key + '": \\1}')
        return f"JSON(REGEXP_REPLACE(JSON_SERIALIZE({ids} RETURNING CLOB), '(-?[0-9]+)', {token}))"

    def modulo(self, a: str, b: str) -> str:
        return f"MOD({a}, {b})"

    def starts_with(self, a: str, b: str) -> str:
        """CLOBs, which `=` does not compare (ORA-22848), by `DBMS_LOB.COMPARE`: see `texts`."""
        if self._long:
            return self.shared1(b, lambda x: f"(DBMS_LOB.COMPARE({a}, {x}, LENGTH({x}), 1, 1) = 0)")
        return self.shared1(b, lambda x: f"(SUBSTR({a}, 1, LENGTH({x})) = {x})")

    def ends_with(self, a: str, b: str) -> str:
        if self._long:

            def compared(values: list[str]) -> str:
                t, x = values
                return (
                    f"(LENGTH({t}) >= LENGTH({x}) AND "
                    f"DBMS_LOB.COMPARE({t}, {x}, LENGTH({x}), LENGTH({t}) - LENGTH({x}) + 1, 1) = 0)"
                )

            return self.shared([a, b], compared)
        return self.shared1(b, lambda x: f"(SUBSTR({a}, -LENGTH({x})) = {x})")

    def contains_text(self, a: str, b: str) -> str:
        """Of CLOBs too (see `texts`), though `INSTR` looks for no text past 32 KB (ORA-22828)."""
        return f"(INSTR({a}, {b}) > 0)"

    def function(self, name: str, json: str) -> str | None:
        """Of a property, read from the properties by its own path: see `_direct`."""
        sql = self._function_of(name, json)
        return _direct(sql, json) if sql is not None else None

    def _function_of(self, name: str, json: str) -> str | None:
        functions: dict[str, Callable[[], str]] = {
            # A text past 4,000 bytes, null as a VARCHAR2, is measured as a CLOB (#168, drivine4j#298).
            "size": lambda: (
                f"CASE {_kind(json)} WHEN 'array' THEN JSON_SCALAR(JSON_VALUE({json}, '$.size()' RETURNING NUMBER)) "
                f"WHEN 'string' THEN JSON_SCALAR(COALESCE(LENGTH(JSON_VALUE({json}, '$')), "
                f"LENGTH(JSON_VALUE({json}, '$' RETURNING CLOB)))) END"
            ),
            "tolower": lambda: self.texts([json], lambda it: self.scalar(f"LOWER({it[0]})")),
            "toupper": lambda: self.texts([json], lambda it: self.scalar(f"UPPER({it[0]})")),
            "trim": lambda: self.texts([json], lambda it: self.scalar(f"TRIM({it[0]})")),
            # A text past 4,000 bytes, which _as_text reads as null, is read as a CLOB.
            "tostring": lambda: f"COALESCE(JSON_SCALAR({self._as_text(json)}), JSON_SCALAR({self._long_text(json)}))",
            "tointeger": lambda: (
                f"JSON_SCALAR(TRUNC(TO_NUMBER(JSON_VALUE({json}, '$') DEFAULT NULL ON CONVERSION ERROR)))"
            ),
            "tofloat": lambda: (
                f"JSON_SCALAR(TO_BINARY_DOUBLE(JSON_VALUE({json}, '$') DEFAULT NULL ON CONVERSION ERROR))"
            ),
            "head": lambda: f"JSON_QUERY({json}, '$[0]' RETURNING JSON)",
            "last": lambda: f"JSON_QUERY({json}, '$[last]' RETURNING JSON)",
            "abs": lambda: (
                f"CASE WHEN {_kind(json)} = 'double' THEN JSON_SCALAR(ABS(TO_BINARY_DOUBLE({self.number(json)}))) "
                f"ELSE JSON_SCALAR(ABS({self.number(json)})) END"
            ),
            "keys": lambda: self._map_keys(json),
        }
        function = functions.get(name)
        return function() if function is not None else None

    def _map_keys(self, json: str) -> str:
        """
        A JSON path reads no map's keys, so they are read from the map's data guide: each path it lists one step from
        the top is a key, written `$.name`, or `$."…"` as JSON writes a string where the name is any other. The map is
        bound first: the data guide's query reads a column of a row outside it, and not an expression of one. Null has
        no keys, and gives null.
        """
        return self.bound([json], lambda it: f"CASE WHEN {_kind(it[0])} = 'object' THEN {_keys_of(it[0])} END")

    def page(self, skip: str | None, limit: str | None) -> list[str]:
        return [
            *([f"OFFSET {skip} ROWS"] if skip is not None else []),
            *([f"FETCH NEXT {limit} ROWS ONLY"] if limit is not None else []),
        ]

    def ids(self, id_: str) -> str:
        return f"(',' || {id_} || ',')"

    def join_ids(self, lists: Sequence[str]) -> str:
        return "(" + lists[0] + "".join(f" || SUBSTR({it}, 2)" for it in lists[1:]) + ")"

    def store_ids(self, json: str) -> str:
        def listed(ids: str) -> str:
            agg = "LISTAGG(x.id, ',') WITHIN GROUP (ORDER BY x.ord)"
            return (
                f"CASE WHEN {ids} IS NOT NULL THEN (SELECT NVL2({agg}, ',' || {agg} || ',', ',') FROM "
                f"JSON_TABLE({ids}, '$[*]' COLUMNS (ord FOR ORDINALITY, id NUMBER PATH '$')) x) END"
            )

        return self.shared1(json, listed)

    @property
    def no_ids(self) -> str:
        return "','"

    def id_in(self, id_: str, list_: str) -> str:
        return f"INSTR({list_}, ',' || {id_} || ',') > 0"

    def ids_overlap(self, a: str, b: str) -> str:
        return f"EXISTS (SELECT 1 FROM {self.id_rows(a, 'ov')} WHERE INSTR({b}, ',' || ov.id || ',') > 0)"

    def id_count(self, list_: str) -> str:
        return f"JSON_SCALAR(REGEXP_COUNT({list_}, ',') - 1)"

    def id_rows(self, list_: str, alias: str) -> str:
        return (
            f"JSON_TABLE(JSON('[' || TRIM(BOTH ',' FROM {list_}) || ']'), '$[*]' "
            f"COLUMNS (ord FOR ORDINALITY, id NUMBER PATH '$')) {alias}"
        )

    walks_read_row = False
    """Oracle's recursive queries read nothing of the row they are in."""

    def walk(self, walk: Walk) -> Walked:
        """A recursive query from each node that meets the seed, each row carrying the node it started from."""
        w = walk.step
        n = f"{walk.name}_n"
        joins, reached, distinct = self.steps(walk, f"{w}.nid")
        eids = [f"{it.edge}.eid" for it in walk.hops]
        conditions = [
            *(f"INSTR({w}.eids, ',' || {it} || ',') = 0" for it in eids),
            *distinct,
            *([f"{w}.depth < {walk.max}"] if walk.max is not None else []),
            *walk.conditions,
        ]
        seeded = f" WHERE {walk.seed(n)}" if walk.seed is not None else ""
        # Oracle adapts a step's plan as the statement runs, and a second session that runs the statement then finds
        # no rows where a walk either way goes around a cycle (writes.yaml's "Walks around a cycle"): no plan adapts.
        select = "SELECT /*+ NO_ADAPTIVE_PLAN */"
        # Walked backwards, each node it leaves goes first, so its nodes end with the one it started from.
        if walk.backwards:
            step = (
                f"    {select} {w}.src, {reached[0]}, {w}.depth + 1, ',' || {w}.nid || {w}.nids, "
                f"',' || {eids[0]} || {w}.eids"
            )
        else:
            nids = "".join(f" || {it} || ','" for it in reached)
            appended = "".join(f" || {it} || ','" for it in eids)
            step = f"    {select} {w}.src, {reached[-1]}, {w}.depth + 1, {w}.nids{nids}, {w}.eids{appended}"
        text = "\n".join(
            [
                f"WITH {w} (src, nid, depth, nids, eids) AS (",
                f"    SELECT {n}.nid, {n}.nid, 0, CAST(',' AS VARCHAR2(4000)), CAST(',' AS VARCHAR2(4000))",
                f"    FROM {self.nodes} {n}{seeded}",
                "    UNION ALL",
                step,
                f"    FROM {w}",
                *(f"    {it}" for it in joins),
                f"    WHERE {' AND '.join(conditions)}",
                # A walk may come back to a node it left, which Oracle takes for a cycle unless told what one is: a
                # step takes no relationship twice, so no row's relationships are an earlier row's.
                ") CYCLE eids SET looped TO 'Y' DEFAULT 'N'",
                f"SELECT src, nid, nids, eids FROM {w} WHERE depth >= {walk.min}",
            ]
        )
        return Walked(
            f"(\n{indent(text)}\n) {walk.name}",
            f"{walk.name}.src = {walk.from_} AND {walk.name}.nid = {walk.to}",
            f"{walk.name}.eids",
            f"{walk.name}.nids",
        )

    def node_json(self, node: Node) -> str:
        return (
            f"CASE WHEN {node.nid} IS NOT NULL THEN JSON_TRANSFORM({node.props}, "
            f"SET {_path(stamps.PROPERTY)} = {node.stamp} IGNORE ON NULL) END"
        )

    def reduce(
        self,
        name: str,
        initial: str,
        list_: str,
        step: Callable[[str, str], str],
        context: Callable[[], str],
        context_step: Callable[[str, str, str], str],
    ) -> str:
        """
        A function the query declares, `WITH FUNCTION`, that folds the list: Oracle's recursive queries read nothing of
        the row they are in. The step reads what it reads of the row from the map it is given. A procedure's queries
        call no function it declares, so a statement that writes takes no `reduce`; nor does the step of one, whose
        queries are its function's own.
        """
        if self.procedural:
            raise DrivineError(
                "reduce() does not run on Oracle in a statement that writes: its block calls no function of its own "
                "from SQL."
            )
        if self._folding:
            raise DrivineError(
                "reduce() does not run on Oracle in the step of a reduce(): its function calls no function of the "
                "query's from SQL."
            )
        function = f"cy_{name}"
        given = context()
        self._folding = True
        try:
            folded = context_step("cy_value", "cy_step.item", "cy_context")
        finally:
            self._folding = False
        self.declare(
            "\n".join(
                [
                    f"FUNCTION {function}(cy_initial JSON, cy_list JSON, cy_context JSON) RETURN JSON IS",
                    "    cy_value JSON := cy_initial;",
                    "    cy_kind VARCHAR2(20);",
                    "BEGIN",
                    "    SELECT JSON_VALUE(cy_list, '$.type()') INTO cy_kind FROM DUAL;",
                    "    IF cy_kind IS NULL OR cy_kind <> 'array' THEN RETURN NULL; END IF;",
                    "    FOR cy_step IN (SELECT s.item FROM JSON_TABLE(cy_list, '$[*]' "
                    "COLUMNS (item_no FOR ORDINALITY, item JSON PATH '$')) s ORDER BY s.item_no) LOOP",
                    indent(f"    SELECT {folded} INTO cy_value FROM DUAL;"),
                    "    END LOOP;",
                    "    RETURN cy_value;",
                    "END;",
                ]
            )
        )
        return f"{function}({initial}, {list_}, {given})"

    def range(self, from_: str, to: str, step: str) -> str:
        """As many rows as the range has numbers, from a hierarchical query of one row."""

        def ranged(values: list[str]) -> str:
            first, last, by = values
            count = f"FLOOR(({last} - {first}) / {by}) + 1"
            return (
                f"(SELECT COALESCE(JSON_ARRAYAGG({first} + (r.n - 1) * {by} ORDER BY r.n RETURNING JSON), JSON('[]')) "
                f"FROM (SELECT LEVEL n FROM DUAL CONNECT BY LEVEL <= GREATEST({count}, 1)) r WHERE r.n <= {count})"
            )

        return self.shared([from_, to, step], ranged)

    @property
    def temporal_sql(self) -> TemporalSql:
        return OracleTemporalSql(self.shared, lambda: self.procedural)

    def bound_select(self, columns: str) -> str:
        """
        Oracle merges a view of no table into the subquery that reads it, and where that subquery is bound in another
        and the row reads another property, the merged query reads the value as null: each is kept apart.
        """
        return f"SELECT /*+ NO_MERGE */ {columns}"

    def returned(self, json: str) -> str:
        """
        Encoded again: a document updated in place, as a write updates a node's properties, comes back as OSON version
        2, which python-oracledb's thin mode does not read (oracle/python-oracledb#622). drivine4j's driver reads it,
        and returns the value as it is.
        """
        return f"JSON_TRANSFORM({json}, KEEP '$')"

    def called(self, query: str) -> str:
        """
        Oracle merges a `CALL { … }` query into the row's query that joins it, and a value bound in that row from what
        the query returns is then read as null (`CALL { WITH n RETURN n.day AS d } RETURN d + duration('P1D')`).
        """
        return f"SELECT /*+ NO_MERGE */ * FROM (\n{indent(query)}\n)"

    def lateral(self, query: str, name: str, optional: bool) -> str:
        """
        An optional join is on `1 = 1`, not `ON TRUE`: in a scalar subquery, as a `COUNT { … }` is, Oracle refuses a
        join on `TRUE` followed by a `WHERE … AND …` (ORA-00907), and crashes the session on one without (ORA-07445 in
        `qcsjccqb`, ORA-03113 to the client) (#200). See docs/oracle-on-true-in-scalar-subquery-crash.md.
        """
        if optional:
            return f"LEFT JOIN LATERAL (\n{indent(query)}\n) {name} ON 1 = 1"
        return super().lateral(query, name, optional=False)

    def matches(self, text: str, pattern: str) -> str:
        """
        Flags the pattern opens with, `(?i)`, are Oracle's match parameter, Java's `s` its `n`. The whole of the text
        matches, as `=~` asks: it starts at `\\A` and ends at `\\z`, the text's start and end, which `(?m)` leaves as
        they are, and Oracle's `$` also matches before a line break that ends the text. Under `(?m)`, Oracle's own `^`
        and `$` match at a line break only where nothing in the pattern reads past it: `x$\\n^ab` matches no text.
        """

        def matched(p: str) -> str:
            flags = f"REGEXP_SUBSTR({p}, '^\\(\\?([a-z]+)\\)', 1, 1, NULL, 1)"
            return (
                f"REGEXP_LIKE({text}, '\\A(' || REGEXP_REPLACE({p}, '^\\(\\?[a-z]+\\)', '') || ')\\z', "
                f"NVL(REPLACE({flags}, 's', 'n'), 'c'))"
            )

        return self.shared1(pattern, matched)

    def split(self, text: str, separator: str) -> str:
        """
        The text as JSON writes it, with each separator as JSON writes it made the end of one string and the start of
        the next. A `VARCHAR2` is cut off past 4,000 bytes where `REPLACE` makes it longer, so a text is split as one
        only where what it is made cannot be longer, each of its bytes written as six at most (`\\u0001`), and else as
        a CLOB: see `texts`.
        """

        def serialized(x: str) -> str:
            # A separator past 4,000 bytes is a CLOB too, and written as one (ORA-40478).
            returning = " RETURNING CLOB" if self._long else ""
            return f"JSON_SERIALIZE(JSON_SCALAR({x}){returning})"

        def cut(values: list[str]) -> str:
            t, s = values
            written = serialized(t)
            separator_written = f"SUBSTR({serialized(s)}, 2, LENGTH({serialized(s)}) - 2)"
            fits = "" if self._long else f" AND LENGTHB({t}) <= {(_SHORT_TEXT - 2) // 6}"
            return (
                f"CASE WHEN {t} IS NOT NULL AND {s} IS NOT NULL{fits} THEN "
                f"JSON('[' || REPLACE({written}, {separator_written}, '\",\"') || ']') END"
            )

        return self.shared([text, separator], cut)

    def replace_text(
        self,
        text: str,
        search: str,
        replacement: str,
        search_bytes: int | None = None,
        replacement_bytes: int | None = None,
    ) -> str:
        """
        A `VARCHAR2` is cut off past 4,000 bytes where `REPLACE` makes it longer: as one only where it cannot be, as
        each `search` made `replacement` grows the text by their ratio at most, and else null, so `texts` replaces in a
        CLOB. Their lengths are worked out here where the statement gives them, and not for each row.
        """
        replaced = f"REPLACE({text}, {search}, {replacement})"
        if self._long:
            return replaced
        if search_bytes is not None and replacement_bytes is not None:
            if replacement_bytes <= search_bytes:
                return replaced
            return (
                f"CASE WHEN LENGTHB({text}) * {replacement_bytes} <= {_SHORT_TEXT * search_bytes} THEN {replaced} END"
            )
        s = str(search_bytes) if search_bytes is not None else f"LENGTHB({search})"
        r = str(replacement_bytes) if replacement_bytes is not None else f"LENGTHB({replacement})"
        return f"CASE WHEN {r} <= {s} OR LENGTHB({text}) * {r} <= {_SHORT_TEXT} * {s} THEN {replaced} END"

    def reverse_text(self, text: str) -> str:
        """Oracle has no reverse of a text that it documents: its characters, read from the last."""
        return self.shared1(
            text,
            lambda t: (
                f"(SELECT LISTAGG(SUBSTR({t}, LEVEL, 1)) WITHIN GROUP (ORDER BY LEVEL DESC) FROM DUAL "
                f"CONNECT BY LEVEL <= LENGTH({t}))"
            ),
        )

    @property
    def row_number(self) -> str:
        """`ROW_NUMBER()` takes an order of its own; `ROWNUM` counts the rows as the query gives them."""
        return "ROWNUM"

    def uuid(self) -> str:
        """Oracle's GUID, in a UUID's form: unique, though not random as a version 4 UUID is."""
        return "LOWER(REGEXP_REPLACE(RAWTOHEX(SYS_GUID()), '(.{8})(.{4})(.{4})(.{4})(.{12})', '\\1-\\2-\\3-\\4-\\5'))"

    def math(self, name: str, numbers: Sequence[str]) -> str:
        """
        Worked out as a `NUMBER`, then made the nearest double: Oracle fails where Neo4j gives NaN or an infinity,
        `LN(0)`, `SQRT(-1)`, `ASIN(2)` and `1 / TAN(0)` among them, and where a `NUMBER` overflows, past 1e126, as
        `EXP(300)` does. One too small for a `NUMBER`, below 1e-130, is 0, as `EXP(-1000)` is in Java, and `EXP(-300)`
        is not. A `BINARY_DOUBLE` would give NaN or an infinity, which JSON holds neither of.
        """
        x = numbers[0]
        results = {
            "log": lambda: f"LN({x})",
            "log10": lambda: f"LOG(10, {x})",
            "cot": lambda: f"(1 / TAN({x}))",
            # In doubles from the one nearest the cosine, as Java works it out: near 0, that loses digits a NUMBER
            # keeps.
            "haversin": lambda: f"((1 - TO_BINARY_DOUBLE(COS({x}))) / 2)",
            # In doubles, by the ratio Java multiplies by: see RADIANS_TO_DEGREES.
            "degrees": lambda: f"(TO_BINARY_DOUBLE({x}) * {RADIANS_TO_DEGREES})",
            "radians": lambda: f"(TO_BINARY_DOUBLE({x}) * {DEGREES_TO_RADIANS})",
            # Oracle overflows at ATAN2(0, 0), which Java gives as 0.0: a NUMBER has no negative zero to give π for.
            "atan2": lambda: self.shared(
                [x, numbers[1]],
                lambda it: f"CASE WHEN {it[0]} = 0 AND {it[1]} = 0 THEN 0 ELSE ATAN2({it[0]}, {it[1]}) END",
            ),
        }
        return self.float_(results[name]() if name in results else f"{name.upper()}({x})")

    def right_text(self, text: str, count: str) -> str:
        """SUBSTR from the end gives null where it is asked for more than there is."""
        return self.shared(
            [text, count],
            lambda it: (
                f"CASE WHEN {it[1]} >= LENGTH({it[0]}) THEN {it[0]} WHEN {it[1]} > 0 THEN SUBSTR({it[0]}, -{it[1]}) END"
            ),
        )

    def procedure(self, body: list[str], parameters: dict[str, Any]) -> CompiledCypher:
        """An anonymous block, which returns its rows as an implicit result."""
        declared = [
            "DECLARE",
            # Text, read as JSON where each parameter is read: see `property_of`.
            f"    {PARAMS} CLOB := ${OBJECT};",
            f"    {ROWS} JSON;",
            f"    {OUT} JSON;",
            f"    {ROW} JSON;",
            f"    {VALUE} JSON;",
            f"    {ID} NUMBER;",
            f"    {COUNT} NUMBER;",
            f"    {FOUND} BOOLEAN;",
            f"    {DOOMED} JSON;",
            f"    {CURSOR} SYS_REFCURSOR;",
            f"    {NOW} TIMESTAMP;",
        ]
        for depth in range(1, NESTING + 1):
            declared += [f"    {SAVED}{depth} JSON;", f"    {GATHERED}{depth} JSON;", f"    {CALLER}{depth} JSON;"]
        # The clock is read once, by a query: see OracleTemporalSql.now.
        clock = f"SELECT {CLOCK} INTO {NOW} FROM DUAL;"
        return CompiledCypher("\n".join([*declared, "BEGIN", indent("\n".join([clock, *body])), "END;"]), parameters)

    def return_rows(self, query: str) -> list[str]:
        return [f"OPEN {CURSOR} FOR\n{indent(query)};", f"DBMS_SQL.RETURN_RESULT({CURSOR});"]

    def raise_(self, message: str) -> str:
        return f"RAISE_APPLICATION_ERROR(-20000, {literal(message)});"

    def sign_of(self, number: str) -> str:
        """
        Oracle's `SIGN` gives 0 for a positive number under 1.0E-128, from 1.0E-130 up, which NUMBER holds and compares
        as greater than 0, and is right from 1.0E-128 up, for a negative number, 0, and null. So the number is held to
        between -1 and 1, where a thousand times it cannot overflow, and that is signed, past 1.0E-128 where it is not
        0: the number is read once, so a sign of a sign does not write it out twice at each level.
        """
        return f"SIGN(GREATEST(LEAST({number}, 1), -1) * 1000)"

    def round_places(self, number: str, precision: str) -> str:
        return self._past_its_digits(number, precision, super().round_places)

    def round_to(self, number: str, precision: str, mode: str) -> str:
        base = super().round_to
        return self._past_its_digits(number, precision, lambda n, p: base(n, p, mode))

    def _past_its_digits(self, number: str, precision: str, round_: Callable[[str, str], str]) -> str:
        """
        `round` of the SQL number to `precision` places, or the number itself where those places are past its digits:
        Oracle's NUMBER stops near 10^126, so `ROUND(n, 400)`, and the scale `10^p` a mode rounds by, overflow
        (ORA-01426) where Neo4j gives the number back. Neo4j rounds a double, of _DOUBLE_DIGITS significant digits at
        most, so rounding it to places past its last digit changes nothing; a number Oracle holds to more digits is a
        double again once rounded. Oracle works out only the branch of a CASE a row takes, so the overflowing one is
        never reached. The precision is read first, in a WHEN of its own, as a CASE tests its WHENs in order: so a null
        one gives null for 0 too, not the 0. Places below 0 or past MAX_COUNT never reach here: they fail on a number
        first, and give null for anything else. The digits are compared in decimal, not by `LOG(10, ABS(n))`, which
        fails (ORA-01428) on 0 and on the least numbers NUMBER holds, near 1.0E-130; zero is given back where
        `10^(17 - p)` underflows to it, and rounded to 0 short of that. Short of its digits, a tiny number may still
        take more than _MOST_PLACES places, as 1.0E-120 to 130 does: it is rounded moved that many places fewer to the
        right, then moved back, which is exact in decimal.
        """

        def rounded(values: list[str]) -> str:
            n, p = values
            past = f"GREATEST({p} - {_MOST_PLACES}, 0)"
            shift = f"POWER(10, {past})"
            return (
                f"CASE WHEN {p} IS NULL THEN NULL "
                f"WHEN ABS({n}) >= POWER(10, {_DOUBLE_DIGITS} - {p}) THEN {n} "
                f"ELSE {round_(f'({n} * {shift})', f'({p} - {past})')} / {shift} END"
            )

        return self.shared([number, precision], rounded)

    def failure(self, message: str, value: str) -> str:
        """A text that is no number, read as one: Oracle fails with ORA-01722, the text in its detail."""
        return f"TO_NUMBER({literal(message + ' ')} || TO_CHAR({value}))"

    def id_of(self, json: str, key: str | None) -> str:
        return f"JSON_VALUE({json}, {_path(key) if key is not None else chr(39) + '$' + chr(39)} RETURNING NUMBER)"

    def appended(self, list_: str, item: str) -> str:
        return f"JSON_TRANSFORM({list_}, APPEND '$' = {item})"

    def strip_nulls(self, json: str) -> str:
        return f"JSON_MERGEPATCH(JSON('{{}}'), {json} RETURNING JSON)"

    def merged(self, props: str, map_: str) -> str:
        return f"JSON_MERGEPATCH({props}, {map_} RETURNING JSON)"

    def without_label(self, labels: str, label: str) -> str:
        return f"JSON_TRANSFORM({labels}, REMOVE {literal(f'$[*]?(@ == {_quoted(label)})')})"


def _scientific(text: str) -> str:
    """`TO_CHAR`'s scientific notation, `1.E+07`, `-1.2E-04`, as Java's: `1.0E7`, `-1.2E-4`."""
    return f"REGEXP_REPLACE(REPLACE({text}, '.E', '.0E'), 'E\\+?(-?)0*([0-9])', 'E\\1\\2')"


def _finite(value: Any) -> Any:
    """
    The value, of JSON's own kinds, with NaN and the infinities as the texts drivine4j binds them as, `"NaN"`,
    `"Infinity"`, `"-Infinity"`: JSON holds no such number, and Oracle reads Python's bare `NaN` as no double it writes
    by Java's names.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, dict):
        return {key: _finite(it) for key, it in cast(dict[str, Any], value).items()}
    if isinstance(value, list):
        return [_finite(it) for it in cast(list[Any], value)]
    return value


def _keyed_as_extended(value: Any) -> bool:
    """
    Whether the value, of JSON's own kinds, holds a map with a key that starts with `$`, as extended JSON's keys do.
    """
    if isinstance(value, dict):
        entries = cast(dict[str, Any], value)
        return any(key.startswith("$") for key in entries) or any(_keyed_as_extended(it) for it in entries.values())
    if isinstance(value, list):
        return any(_keyed_as_extended(it) for it in cast(list[Any], value))
    return False


_DOUBLE_DIGITS = 17
"""How many significant digits a double holds at most, as Neo4j rounds one."""

_MOST_PLACES = 100
"""The most places `round()` rounds to on Oracle, whose scale `10^p` is well inside NUMBER's range."""

_DOUBLE = "$numberDouble"
"""The key of a double in Oracle's extended JSON."""

_SHORT_TEXT = 4000
"""The most bytes a `VARCHAR2` holds in SQL, as a database is made by default (`MAX_STRING_SIZE = STANDARD`)."""

_NUMBER_TEXT = 32
"""The most bytes `_as_text` writes a number in: Java's text of a double, `-2.2250738585072014E-308`, and some to
spare."""

_PROPERTY_READ = re.compile(r"JSON_QUERY\((.+), ('\$(?:[^']|'')*') RETURNING JSON\)", re.DOTALL)
"""A property as `property_of` reads it, its properties and its path, a literal whose quotes are doubled: see
`_property_read`."""


def _property_read(json: str) -> tuple[str, str] | None:
    """The properties and the path of the JSON where it is a property as `property_of` reads it,
    `JSON_QUERY(props, '$.a' RETURNING JSON)`."""
    match = _PROPERTY_READ.fullmatch(json)
    return (match[1], match[2]) if match is not None and _balanced(match[1]) else None


def _balanced(sql: str) -> bool:
    """Whether the SQL's parentheses outside its literals close each other: so it is the one argument, not two
    calls."""
    depth = 0
    quoted = False
    for c in sql:
        if c == "'":
            quoted = not quoted
        elif quoted:
            continue
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not quoted


def _direct(sql: str, json: str) -> str:
    """
    The SQL with each value it reads out of the JSON with `JSON_VALUE(json, '$…')` read from the properties by the
    property's own path instead, where the JSON is a property as `property_of` reads it, `JSON_QUERY(props, '$.a'
    RETURNING JSON)`. Oracle closed the session (ORA-03113) on statements, one row each, that took a text past 32 KB out
    of a node's properties so and read it as a CLOB or put it together with another, and a later statement in the
    session lost its rows: the memory corruption of #144 (drivine4j#255). A value used whole, as a list a sum joins,
    still takes the property out. drivine4j measured it at about a quarter more on short texts: 100,000 rows of
    `size(n.name)` took 208 ms against 162 ms.
    """
    read = _property_read(json)
    if read is None:
        return sql
    props, path = read
    return sql.replace(f"JSON_VALUE({json}, '$", f"JSON_VALUE({props}, {path[:-1]}")


def _kind(json: str) -> str:
    """The kind of the JSON as Oracle names it, a floating-point number by its own name."""
    return f"JSON_VALUE({json}, '$.type()')"


def _quoted(text: str) -> str:
    """A string as JSON writes it, for a JSON path."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _path(key: str) -> str:
    """The path of the property, as a SQL literal: a key quoted when it must be, so a key with a dot in it is one
    key."""
    return literal(f"$.{_step(key)}")


def _step(key: str) -> str:
    """The key, as a step of a JSON path: quoted when it must be, so a key with a dot in it is one key."""
    if not key:
        raise DrivineError("A property name is empty: an empty name is no property a statement can read.")
    return key if _PLAIN_KEY.fullmatch(key) and key not in _JSON_WORDS else _quoted(key)


def _path_of(keys: Sequence[str], suffix: str = "") -> str:
    """
    The path of the property the keys lead to, each of a map in the one before, as a SQL literal: `'$.p.text'`, or
    with `suffix` after it, `'$.p.text.type()'`.
    """
    return literal("$" + "".join(f".{_step(key)}" for key in keys) + suffix)


def _doubled(value: Any) -> bool:
    """Whether the value, of JSON's own kinds, holds a float that is a whole number: see `parameter_text`."""
    if isinstance(value, float):
        return value.is_integer()
    if isinstance(value, dict):
        return any(_doubled(it) for it in cast(dict[str, Any], value).values())
    if isinstance(value, list):
        return any(_doubled(it) for it in cast(list[Any], value))
    return False


def _keys_of(json: str) -> str:
    key = "CASE WHEN SUBSTR(d.p, 3, 1) = '\"' THEN JSON_VALUE(JSON(SUBSTR(d.p, 3)), '$') ELSE SUBSTR(d.p, 3) END"
    guide = (
        f"JSON_TABLE((SELECT JSON_DATAGUIDE({json}, 2) FROM DUAL), '$[*]' COLUMNS "
        "(p VARCHAR2(4000) PATH '$.\"o:path\"')) d"
    )
    top_level = 'REGEXP_LIKE(d.p, \'^\\$\\.(\\w+|"([^"\\\\]|\\\\.)*")$\')'
    return (
        f"(SELECT COALESCE(JSON_ARRAYAGG(g.k ORDER BY g.k RETURNING JSON), JSON('[]')) FROM "
        f"(SELECT {key} AS k FROM {guide} WHERE {top_level}) g)"
    )
