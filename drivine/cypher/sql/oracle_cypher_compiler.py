from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from typing import Any, cast

from drivine.backend.oracle.oracle_graph import holds_label
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
    SqlCypherCompiler,
    Walk,
    Walked,
    indent,
    literal,
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
"""The function a query declares that compares two values that may hold a null: see `nulls_equal`."""

_QUOTED = r"""'"([^"\\]|\\.)*"'"""
"""A JSON text, quoted, as an Oracle pattern: its quotes, and what is between them, an escape whole."""

_NULL_HELD = "'[[,:]null[],}]'"
"""A null in a list or a map, as an Oracle pattern over JSON's text whose texts are taken out."""

_NULLS_EQUAL_BODY = (
    f"FUNCTION {NULLS_EQUAL}(cy_x JSON, cy_y JSON) RETURN NUMBER IS",
    "    FUNCTION eq(a JSON_ELEMENT_T, b JSON_ELEMENT_T) RETURN NUMBER IS",
    "        r NUMBER := 1;",
    "        e NUMBER;",
    "        la JSON_ARRAY_T;",
    "        lb JSON_ARRAY_T;",
    "        oa JSON_OBJECT_T;",
    "        ob JSON_OBJECT_T;",
    "        ks JSON_KEY_LIST;",
    "        too_long EXCEPTION;",
    "        PRAGMA EXCEPTION_INIT(too_long, -40478);",
    "    BEGIN",
    "        IF a.is_Null OR b.is_Null THEN RETURN NULL; END IF;",
    "        IF a.is_Array AND b.is_Array THEN",
    "            la := TREAT(a AS JSON_ARRAY_T);",
    "            lb := TREAT(b AS JSON_ARRAY_T);",
    "            IF la.get_Size <> lb.get_Size THEN RETURN 0; END IF;",
    "            FOR i IN 0 .. la.get_Size - 1 LOOP",
    "                e := eq(la.get(i), lb.get(i));",
    "                IF e = 0 THEN RETURN 0; END IF;",
    "                IF e IS NULL THEN r := NULL; END IF;",
    "            END LOOP;",
    "            RETURN r;",
    "        END IF;",
    "        IF a.is_Object AND b.is_Object THEN",
    "            oa := TREAT(a AS JSON_OBJECT_T);",
    "            ob := TREAT(b AS JSON_OBJECT_T);",
    "            IF oa.get_Size <> ob.get_Size THEN RETURN 0; END IF;",
    "            ks := oa.get_Keys;",
    "            FOR i IN 1 .. ks.COUNT LOOP",
    "                IF NOT ob.has(ks(i)) THEN RETURN 0; END IF;",
    "                e := eq(oa.get(ks(i)), ob.get(ks(i)));",
    "                IF e = 0 THEN RETURN 0; END IF;",
    "                IF e IS NULL THEN r := NULL; END IF;",
    "            END LOOP;",
    "            RETURN r;",
    "        END IF;",
    "        IF a.is_Number AND b.is_Number THEN RETURN CASE WHEN a.to_Number = b.to_Number THEN 1 ELSE 0 END; END IF;",
    # As VARCHAR2s, three times as fast as CLOBs; as CLOBs where one is past 32 KB, which to_String raises on
    # (ORA-40478).
    "        IF a.is_String AND b.is_String THEN",
    "            BEGIN",
    "                RETURN CASE WHEN a.to_String = b.to_String THEN 1 ELSE 0 END;",
    "            EXCEPTION WHEN too_long THEN",
    "                RETURN CASE WHEN DBMS_LOB.COMPARE(a.to_Clob, b.to_Clob) = 0 THEN 1 ELSE 0 END;",
    "            END;",
    "        END IF;",
    "        IF a.is_Boolean AND b.is_Boolean THEN",
    "            RETURN CASE WHEN a.to_Boolean = b.to_Boolean THEN 1 ELSE 0 END;",
    "        END IF;",
    "        RETURN 0;",
    "    END;",
    "BEGIN",
    "    RETURN eq(JSON_ELEMENT_T.parse(JSON_SERIALIZE(cy_x RETURNING CLOB)), "
    "JSON_ELEMENT_T.parse(JSON_SERIALIZE(cy_y RETURNING CLOB)));",
    "END;",
)
"""
NULLS_EQUAL, which walks two values together, item by item and key by key, as Cypher compares them: 0 where a pair
differs, else null where a pair holds a null, else 1.
"""


class OracleCypherCompiler(SqlCypherCompiler):
    """
    Compiles a Cypher statement to SQL over a graph's tables on Oracle: see SqlCypherCompiler.

    A value is Oracle's `JSON`, which compares, groups and orders as JSON and keeps a map's keys in the order written.
    Oracle keeps a number as a number, so a stored `2.0` reads as `2`; a floating-point number the statement writes, or
    a parameter's that is a whole number, is a `BINARY_DOUBLE`, and stays one. A text Oracle reads out of JSON is at
    most 4,000 bytes, and an empty one is null, as Oracle's empty text is. `reduce` is a function the query declares,
    and does not run in a statement that writes, in the step of a `reduce`, or where its step reads a path.

    A list of ids is text, each id between commas (`,1,2,`): Oracle has no array, and a recursive query takes no `JSON`
    that changes from row to row. A recursive query reads no row outside it, so a walk starts from every node that
    meets what the node walked from meets of its own, and carries the node it started from.
    """

    def __init__(self, graph: str) -> None:
        super().__init__(graph)
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
        return f"JSON_SCALAR({sql})"

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
        return self.shared1(json, lambda x: f"CASE WHEN {_kind(x)} = 'string' THEN JSON_VALUE({x}, '$') END")

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
        Oracle refuses it (ORA-40180), as it does any JSON that large, and the session stays open.

        To remove it, delete this override, so the item is read as `property_of` reads it, `JSON_QUERY … RETURNING
        JSON`, once an Oracle release no longer crashes on the repro, and the "a property past 32 KB read on two nodes"
        Oracle test still passes without it. `tests/unit/test_oracle_item_property.py`, which pins this SQL, goes
        with it.
        """
        return (
            f"JSON(JSON_QUERY(JSON_SERIALIZE(JSON_TRANSFORM({props}, KEEP {_path(key)}) RETURNING CLOB EXTENDED), "
            f"{_path(key)} RETURNING CLOB) EXTENDED)"
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

    def aggregate_list(self, value: str, order: str | None = None) -> str:
        ordered = f" ORDER BY {order}" if order is not None else ""
        return f"COALESCE(JSON_ARRAYAGG({value}{ordered} NULL ON NULL RETURNING JSON), JSON('[]'))"

    def collect(self, value: str, order: str | None) -> str:
        """`JSON_ARRAYAGG` leaves nulls out."""
        ordered = f" ORDER BY {order}" if order is not None else ""
        return f"COALESCE(JSON_ARRAYAGG({value}{ordered} RETURNING JSON), JSON('[]'))"

    def distinct_items(self, list_: str) -> str:
        """
        Each item where it is first among those equal to it as `JSON` compares them, kept as it is: not by its text,
        which is at most 4,000 bytes and keeps a map's keys in the order written, and not read back from it, which would
        read a float that is a whole number, `2.0`, as the number 2.
        """
        return (
            "(SELECT COALESCE(JSON_ARRAYAGG(d.item ORDER BY d.item_no RETURNING JSON), JSON('[]')) "
            "FROM (SELECT e.item_no, e.item, ROW_NUMBER() OVER (PARTITION BY e.item ORDER BY e.item_no) AS n "
            f"FROM {self.elements(list_, 'e')}) d WHERE d.n = 1)"
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
        return (
            f"CASE WHEN {self.type_of(a)} = 'number' AND {self.type_of(b)} = 'number' THEN "
            f"{self.arithmetic(a, b, '+')} "
            f"WHEN {_kind(a)} = 'array' AND {_kind(b)} = 'array' THEN "
            f"JSON_QUERY(JSON_ARRAY({a}, {b} RETURNING JSON), '$[*][*]' RETURNING JSON WITH ARRAY WRAPPER) "
            f"WHEN {_kind(a)} = 'array' THEN JSON_TRANSFORM({a}, APPEND '$' = {b}) "
            f"WHEN {_kind(b)} = 'array' THEN JSON_TRANSFORM({b}, INSERT '$[0]' = {a}) "
            f"WHEN {self.type_of(a)} IN ('string', 'number') AND {self.type_of(b)} IN ('string', 'number') THEN "
            f"JSON_SCALAR({self._summed_text(a, given)} || {self._summed_text(b, (given[1], given[0]))}) END"
        )

    def _summed_text(self, json: str, given: tuple[Any, Any]) -> str:
        """The side of a `+` whose sides the statement gives as `given`, this side first, as text: see
        `summed_text`."""
        return self.summed_text(json, given[0], given[1], lambda it: f"JSON_VALUE({it}, '$')", self._as_text)

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
        By a function the query declares (see `reduce`), which walks the two together: Oracle's recursive queries take
        no `JSON`. A procedure's queries call no function it declares, nor do a `reduce`'s step's: there, two values
        equal as JSON are null, as each holds a null, and any other two false, as before null was compared item by
        item: `a = [1]`, of `[null] AS a`, is false there, where Cypher's is null. Lists and maps written out are
        compared item by item without it (see `_itemwise`), so `[null] = [1]` is null there.
        """
        if self.procedural or self._folding:
            return f"CASE WHEN {equals(x, y)} THEN NULL ELSE FALSE END"
        if not self._nulls_compared:
            self._nulls_compared = True
            self.declare("\n".join(_NULLS_EQUAL_BODY))
        return f"({NULLS_EQUAL}({x}, {y}) = 1)"

    def id_tokens(self, ids: str, key: str) -> str:
        token = literal('{"' + key + '": \\1}')
        return f"JSON(REGEXP_REPLACE(JSON_SERIALIZE({ids} RETURNING CLOB), '(-?[0-9]+)', {token}))"

    def modulo(self, a: str, b: str) -> str:
        return f"MOD({a}, {b})"

    def starts_with(self, a: str, b: str) -> str:
        return self.shared1(b, lambda x: f"(SUBSTR({a}, 1, LENGTH({x})) = {x})")

    def ends_with(self, a: str, b: str) -> str:
        return self.shared1(b, lambda x: f"(SUBSTR({a}, -LENGTH({x})) = {x})")

    def contains_text(self, a: str, b: str) -> str:
        return f"(INSTR({a}, {b}) > 0)"

    def function(self, name: str, json: str) -> str | None:
        functions: dict[str, Callable[[], str]] = {
            "size": lambda: (
                f"CASE {_kind(json)} WHEN 'array' THEN JSON_SCALAR(JSON_VALUE({json}, '$.size()' RETURNING NUMBER)) "
                f"WHEN 'string' THEN JSON_SCALAR(LENGTH(JSON_VALUE({json}, '$'))) END"
            ),
            "tolower": lambda: f"JSON_SCALAR(LOWER({self.text(json)}))",
            "toupper": lambda: f"JSON_SCALAR(UPPER({self.text(json)}))",
            "trim": lambda: f"JSON_SCALAR(TRIM({self.text(json)}))",
            "tostring": lambda: f"JSON_SCALAR({self._as_text(json)})",
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
        """The text as JSON writes it, with each separator as JSON writes it made the end of one string and the start
        of the next."""

        def cut(values: list[str]) -> str:
            t, s = values
            written = f"JSON_SERIALIZE(JSON_SCALAR({t}))"
            separator_written = (
                f"SUBSTR(JSON_SERIALIZE(JSON_SCALAR({s})), 2, LENGTH(JSON_SERIALIZE(JSON_SCALAR({s}))) - 2)"
            )
            return (
                f"CASE WHEN {t} IS NOT NULL AND {s} IS NOT NULL THEN "
                f"JSON('[' || REPLACE({written}, {separator_written}, '\",\"') || ']') END"
            )

        return self.shared([text, separator], cut)

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


def _path_of(keys: Sequence[str]) -> str:
    """The path of the property the keys lead to, each of a map in the one before, as a SQL literal: `'$.p.text'`."""
    return literal("$" + "".join(f".{_step(key)}" for key in keys))


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
