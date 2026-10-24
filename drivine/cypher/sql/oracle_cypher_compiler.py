from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

from drivine.cypher.sql.compiled_cypher import CompiledCypher
from drivine.cypher.sql.oracle_temporal_sql import OracleTemporalSql
from drivine.cypher.sql.parameters import OBJECT, cypher_json
from drivine.cypher.sql.sql_cypher_compiler import (
    COUNT,
    DOOMED,
    FOUND,
    GATHERED,
    ID,
    NESTING,
    OUT,
    PARAMS,
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


class OracleCypherCompiler(SqlCypherCompiler):
    """
    Compiles a Cypher statement to SQL over a graph's tables on Oracle: see SqlCypherCompiler.

    A value is Oracle's `JSON`, which compares, groups and orders as JSON and keeps a map's keys in the order written.
    Oracle keeps a number as a number, so a stored `2.0` reads as `2`; a floating-point number the statement writes is
    a `BINARY_DOUBLE`, and stays one. A text Oracle reads out of JSON is at most 4,000 bytes, and an empty one is null,
    as Oracle's empty text is. `reduce` is a function the query declares, and does not run in a statement that writes,
    in the step of a `reduce`, or where its step reads a path.

    A list of ids is text, each id between commas (`,1,2,`): Oracle has no array, and a recursive query takes no `JSON`
    that changes from row to row. A recursive query reads no row outside it, so a walk starts from every node that
    meets what the node walked from meets of its own, and carries the node it started from.
    """

    def __init__(self, graph: str) -> None:
        super().__init__(graph)
        self._folding = False
        """Whether the step of a `reduce` is being written, as its function's own: see `reduce`."""

    def literal_json(self, value: Any) -> str:
        if isinstance(value, float):
            return f"JSON_SCALAR(TO_BINARY_DOUBLE({literal(repr(value))}))"
        return f"JSON({literal(cypher_json(value))})"

    def parameter_json(self, marker: str) -> str:
        return f"JSON({marker})"

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
        return f"JSON_QUERY({json}, {_path(key)} RETURNING JSON)"

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
        rest = [f"INSTR(JSON_SERIALIZE({labels}), {literal(_quoted(it))}) > 0" for it in all_[1:]]
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

    def collect(self, value: str, distinct: bool, order: str | None) -> str:
        """
        `JSON_ARRAYAGG` leaves nulls out, and takes no `DISTINCT`: distinct values are gathered as their text, which
        `LISTAGG` keeps once each.
        """
        if distinct:
            return (
                f"JSON('[' || LISTAGG(DISTINCT JSON_SERIALIZE({value}), ',') WITHIN GROUP "
                f"(ORDER BY JSON_SERIALIZE({value})) || ']')"
            )
        ordered = f" ORDER BY {order}" if order is not None else ""
        return f"COALESCE(JSON_ARRAYAGG({value}{ordered} RETURNING JSON), JSON('[]'))"

    def extreme(self, value: str, min_: bool) -> str:
        """Of the values of the kind that comes first, or last, as `kind_order` orders them."""
        return (
            f"{'MIN' if min_ else 'MAX'}({self.null_if_json_null(value)}) KEEP (DENSE_RANK FIRST ORDER BY "
            f"{self.kind_order(value)} {'ASC' if min_ else 'DESC'} NULLS LAST)"
        )

    def arithmetic(self, a: str, b: str, operator: str) -> str:
        """In `BINARY_DOUBLE` where either is one, so a float stays a float, as in Cypher: `NUMBER` keeps no such
        kind."""
        return (
            f"CASE WHEN {_kind(a)} = 'double' OR {_kind(b)} = 'double' "
            f"THEN JSON_SCALAR(TO_BINARY_DOUBLE({self.number(a)}) {operator} TO_BINARY_DOUBLE({self.number(b)})) "
            f"ELSE JSON_SCALAR({self.number(a)} {operator} {self.number(b)}) END"
        )

    def add(self, a: str, b: str) -> str:
        return (
            f"CASE WHEN {self.type_of(a)} = 'number' AND {self.type_of(b)} = 'number' THEN "
            f"{self.arithmetic(a, b, '+')} "
            f"WHEN {_kind(a)} = 'array' AND {_kind(b)} = 'array' THEN "
            f"JSON_QUERY(JSON_ARRAY({a}, {b} RETURNING JSON), '$[*][*]' RETURNING JSON WITH ARRAY WRAPPER) "
            f"WHEN {_kind(a)} = 'array' THEN JSON_TRANSFORM({a}, APPEND '$' = {b}) "
            f"WHEN {_kind(b)} = 'array' THEN JSON_TRANSFORM({b}, INSERT '$[0]' = {a}) "
            f"WHEN {self.type_of(a)} IN ('string', 'number') AND {self.type_of(b)} IN ('string', 'number') THEN "
            f"JSON_SCALAR(JSON_VALUE({a}, '$') || JSON_VALUE({b}, '$')) END"
        )

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
            "tostring": lambda: f"JSON_SCALAR(JSON_VALUE({json}, '$'))",
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
        return OracleTemporalSql(self.shared)

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
        """Flags the pattern opens with, `(?i)`, are Oracle's match parameter, Java's `s` its `n`."""

        def matched(p: str) -> str:
            flags = f"REGEXP_SUBSTR({p}, '^\\(\\?([a-z]+)\\)', 1, 1, NULL, 1)"
            return (
                f"REGEXP_LIKE({text}, '^(' || REGEXP_REPLACE({p}, '^\\(\\?[a-z]+\\)', '') || ')$', "
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
            f"    {PARAMS} JSON := JSON(${OBJECT});",
            f"    {ROWS} JSON;",
            f"    {OUT} JSON;",
            f"    {ROW} JSON;",
            f"    {VALUE} JSON;",
            f"    {ID} NUMBER;",
            f"    {COUNT} NUMBER;",
            f"    {FOUND} BOOLEAN;",
            f"    {DOOMED} JSON;",
            f"    {CURSOR} SYS_REFCURSOR;",
        ]
        for depth in range(1, NESTING + 1):
            declared += [f"    {SAVED}{depth} JSON;", f"    {GATHERED}{depth} JSON;"]
        return CompiledCypher("\n".join([*declared, "BEGIN", indent("\n".join(body)), "END;"]), parameters)

    def return_rows(self, query: str) -> list[str]:
        return [f"OPEN {CURSOR} FOR\n{indent(query)};", f"DBMS_SQL.RETURN_RESULT({CURSOR});"]

    def raise_(self, message: str) -> str:
        return f"RAISE_APPLICATION_ERROR(-20000, {literal(message)});"

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


def _kind(json: str) -> str:
    """The kind of the JSON as Oracle names it, a floating-point number by its own name."""
    return f"JSON_VALUE({json}, '$.type()')"


def _quoted(text: str) -> str:
    """A string as JSON writes it, for a JSON path."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _path(key: str) -> str:
    """The path of the property, as a SQL literal: a key quoted when it must be, so a key with a dot in it is one
    key."""
    if not key:
        raise DrivineError("A property name is empty: an empty name is no property a statement can read.")
    step = key if _PLAIN_KEY.fullmatch(key) and key not in _JSON_WORDS else _quoted(key)
    return literal(f"$.{step}")


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
