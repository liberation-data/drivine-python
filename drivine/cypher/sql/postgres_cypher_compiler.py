from __future__ import annotations

import hashlib
import itertools
import math
import re
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any, cast

from drivine.cypher.sql.compiled_cypher import CompiledCypher
from drivine.cypher.sql.parameters import OBJECT, cypher_json, json_text, jsonable
from drivine.cypher.sql.postgres_temporal_sql import PostgresTemporalSql
from drivine.cypher.sql.sql_cypher_compiler import (
    CALLER,
    COUNT,
    DOOMED,
    FOUND,
    GATHERED,
    ID,
    MATCHED,
    NESTING,
    OUT,
    PARAMS,
    PLAIN_FLOAT,
    RADIANS_TO_DEGREES,
    RECORD,
    ROW,
    ROWS,
    SAVED,
    VALUE,
    SqlCypherCompiler,
    Walk,
    Walked,
    indent,
    java_text,
    literal,
)
from drivine.cypher.sql.temporal_sql import TemporalSql
from drivine.cypher.sql.value import Json, Node
from drivine.model import stamps

_NUMERIC_TEXT = r"^\s*[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?\s*$"
"""A text Postgres reads as a `numeric`, spaces around it allowed."""

_EXP_UNDERFLOW = "-745.1332191019411"
"""
Below it, `exp` is nearer 0 than the least double, half of 2^-1074, and Java gives 0.0: ln(2^-1075) is
-745.13321910194120…, and this is the least double Java's `exp` gives more than 0.0 for.
"""

_SCIENTIFIC = "9.9999999999999999EEEE"
"""`to_char`'s format of a number's 17 digits and exponent: ` 1.0000000000000000e+07`."""

_PROPERTY = re.compile(r"^\(([\w.]+) -> ('(?:[^']|'')*')\)$")
"""A property as `property_of` reads it from a column: `(n1.props -> 'name')`."""


class PostgresCypherCompiler(SqlCypherCompiler):
    """
    Compiles a Cypher statement to SQL over a graph's tables on Postgres: see SqlCypherCompiler.

    A value is `jsonb`, which compares, groups and orders as JSON. A map or a list the statement builds is `json`, which
    keeps its keys in the order written, and is cast to `jsonb` where it is compared. A list of ids is an array of
    `bigint`. A statement that writes carries its rows as `jsonb`, so a map carried past a write has its keys in
    `jsonb`'s order.
    """

    def literal_json(self, value: Any) -> str:
        return f"CAST({literal(_written(jsonable(value)))} AS jsonb)"

    def parameter_json(self, marker: str, text: str | None) -> str:
        return f"CAST({marker} AS jsonb)"

    def parameter_text(self, value: Any) -> str:
        return _written(jsonable(value))

    def parameter_count(self, marker: str) -> str:
        return f"CAST({marker} AS bigint)"

    def comparable(self, value: Json) -> str:
        return f"CAST({value.sql} AS jsonb)" if value.ordered else value.sql

    def scalar(self, sql: str) -> str:
        return f"to_jsonb({sql})"

    def type_of(self, json: str) -> str:
        return f"jsonb_typeof({json})"

    def is_json_null(self, json: str) -> str:
        return f"COALESCE(jsonb_typeof({json}), 'null') = 'null'"

    def null_if_json_null(self, json: str) -> str:
        return f"NULLIF({json}, 'null'::jsonb)"

    def number(self, json: str) -> str:
        return self.shared1(
            json, lambda x: f"CASE WHEN jsonb_typeof({x}) = 'number' THEN CAST({x} #>> '{{}}' AS numeric) END"
        )

    def integral(self, json: str) -> str:
        return self.shared1(json, lambda x: f"(jsonb_typeof({x}) = 'number' AND ({x} #>> '{{}}') ~ '^-?[0-9]+$')")

    def text(self, json: str) -> str:
        return self.shared1(json, lambda x: f"CASE WHEN jsonb_typeof({x}) = 'string' THEN {x} #>> '{{}}' END")

    def bool_of(self, json: str) -> str:
        return self.shared1(json, lambda x: f"CASE WHEN jsonb_typeof({x}) = 'boolean' THEN CAST({x} AS boolean) END")

    def float_(self, sql: str) -> str:
        """
        A `numeric` with a fraction, so a whole one is written `2.0`, as a graph engine writes a float, and stays one in
        arithmetic; without the zeros its scale leaves after its digits (`trim_scale`), so `avg` of whole numbers is
        `1999.0`, not `1999.0000000000000000`.
        """
        return f"(trim_scale(CAST({sql} AS numeric)) + 0.0)"

    def arithmetic(self, a: str, b: str, operator: str) -> str:
        """
        As any store works it out, in `numeric`, and a quotient without the zeros `numeric`'s division leaves after its
        digits: `4.0 / 2` is `2.0`, not `2.0000000000000000`. Postgres works a float out exactly, so a result may differ
        from a double's in the last digits: `0.1 * 3` is `0.3`, where Cypher gives `0.30000000000000004`
        (liberation-data/drivine4j#188).
        """
        if operator == "/":
            return self.scalar(self.float_(f"({self.number(a)} / {self.number(b)})"))
        return super().arithmetic(a, b, operator)

    def json_array(self, items: Sequence[str]) -> str:
        return f"json_build_array({', '.join(items)})"

    def json_object(self, entries: Sequence[tuple[str, str]]) -> str:
        return f"json_build_object({', '.join(f'{literal(key)}, {value}' for key, value in entries)})"

    def with_entries(self, json: str, entries: Sequence[tuple[str, str]]) -> str:
        return f"({json} || CAST({self.json_object(entries)} AS jsonb))"

    def property_of(self, json: str, key: str) -> str:
        return f"({json} -> {literal(key)})"

    def element(self, json: str, index: str) -> str:
        return f"({json} -> CAST({index} #>> '{{}}' AS int))"

    def keyed(self, json: str, key: str) -> str | None:
        return f"{json} -> ({key} #>> '{{}}')"

    def contains_all(self, props: str, map_: str) -> str:
        return f"{props} @> {map_}"

    def has_label(self, labels: str, label: str) -> str:
        return f"{labels} @> {literal(cypher_json([label]))}"

    def indexed_equality(self, property: str, value: str) -> str | None:
        """The property's text, as a range index or a uniqueness constraint reads it with `->>`, compared with the
        value's."""
        match = _PROPERTY.fullmatch(property)
        return f"({match.group(1)} ->> {match.group(2)}) = ({value} #>> '{{}}')" if match else None

    def elements(self, json: str, alias: str) -> str:
        return f"jsonb_array_elements({json}) WITH ORDINALITY {alias} (item, item_no)"

    def aggregate_list(self, value: str, order: str | None = None) -> str:
        ordered = f" ORDER BY {order}" if order is not None else ""
        return f"COALESCE(json_agg({value}{ordered}), '[]'::json)"

    def collect(self, value: str, order: str | None) -> str:
        ordered = f" ORDER BY {order}" if order is not None else ""
        return f"COALESCE(json_agg({value}{ordered}) FILTER (WHERE {value} IS NOT NULL), '[]'::json)"

    def distinct_items(self, list_: str) -> str:
        return (
            "(SELECT COALESCE(json_agg(d.item ORDER BY d.first), '[]'::json) FROM (SELECT min(e.item_no) AS first, "
            f"e.item FROM {self.elements(list_, 'e')} GROUP BY e.item) d)"
        )

    def first_of_each(self, list_: str, identities: str) -> str:
        return (
            f"(SELECT {self.aggregate_list('v.item', 'v.item_no')} FROM {self.elements(list_, 'v')} "
            f"JOIN (SELECT min(e.item_no) AS first FROM {self.elements(identities, 'e')} GROUP BY e.item) f "
            "ON f.first = v.item_no)"
        )

    def extreme(self, value: str, min_: bool, key: str | None = None) -> str:
        # Postgres has no min or max of jsonb, which orders: the first of the values in order is the least.
        direction = "ASC" if min_ else "DESC"
        return (
            f"(array_agg({value} ORDER BY {self.kind_order(value)} {direction}, {key or value} {direction}) "
            f"FILTER (WHERE COALESCE(jsonb_typeof({value}), 'null') <> 'null'))[1]"
        )

    def add(self, a: str, b: str, given: tuple[Any, Any] = (None, None)) -> str:
        return (
            f"CASE WHEN jsonb_typeof({a}) = 'number' AND jsonb_typeof({b}) = 'number' THEN "
            f"to_jsonb({self.number(a)} + {self.number(b)}) "
            f"WHEN jsonb_typeof({a}) = 'array' AND jsonb_typeof({b}) = 'array' THEN {a} || {b} "
            f"WHEN jsonb_typeof({a}) = 'array' THEN {a} || jsonb_build_array({b}) "
            f"WHEN jsonb_typeof({b}) = 'array' THEN jsonb_build_array({a}) || {b} "
            f"WHEN jsonb_typeof({a}) IN ('string', 'number') AND jsonb_typeof({b}) IN ('string', 'number') THEN "
            f"to_jsonb({self._summed_text(a, given)} || {self._summed_text(b, (given[1], given[0]))}) END"
        )

    def _summed_text(self, json: str, given: tuple[Any, Any]) -> str:
        """The side of a `+` whose sides the statement gives as `given`, this side first, as text: see
        `summed_text`."""
        return self.summed_text(json, given[0], given[1], lambda it: f"({it} #>> '{{}}')", self._as_text)

    def _as_text(self, json: str) -> str:
        """
        The JSON, a text, a number or a truth value, as `toString` gives it: a float, a `numeric` with a fraction,
        without the zeros `numeric`'s scale leaves after its digits (`trim_scale`), as Java writes a double: with no
        exponent from 10^-3 up to 10^7, a point and a digit after it at least, `2.0`, and else as `1.0E7`, `1.2E-4`.
        Postgres works a float out in `numeric`, exactly, so its digits may differ from a double's in the last places:
        `0.1 * 3` is `0.3`.
        """

        def text(x: str) -> str:
            exact = f"trim_scale(CAST({x} #>> '{{}}' AS numeric))"
            return (
                f"CASE WHEN jsonb_typeof({x}) = 'number' AND NOT {self.integral(x)} THEN "
                f"{self.shared1(exact, _float_text)} ELSE {x} #>> '{{}}' END"
            )

        return self.shared1(json, text)

    def list_sort_key(self, json: str) -> str:
        """
        A list as `jsonb` that Postgres orders as Cypher orders lists, item by item: each item, from the first, a list
        of three, of where its kind comes (`item_rank`), the item, and what follows it so written, the end an empty
        list. `jsonb` orders the shorter of two lists first, and every list here but the last is of three. An item that
        is a list is followed by its own items, at any depth, then by its end, which comes before any item. Any other
        value sorts as itself, as before. Only a value the statement makes a list sorts so: the planner costs the walk
        at thousands a row, which brings in its JIT for a sort of more than a few dozen rows
        (liberation-data/drivine4j#223).
        """
        return self.shared1(
            json,
            lambda x: (
                f"CASE WHEN jsonb_typeof({x}) = 'array' THEN CAST({self._list_key(x)} AS jsonb) "
                f"ELSE {self.null_if_json_null(x)} END"
            ),
        )

    def _list_key(self, list_: str) -> str:
        """
        The list as `list_sort_key` writes it, as text: its items and the items of each that is a list, all the way
        down, by their places, each list held in it as its kind, then its items, then its end, ranked 0.
        """

        # Each list's items as an array unnested: the planner takes an array it cannot see for 10 items, and a
        # set-returning function's for 100, which it multiplies through the walk, to a cost 150 times as high.
        def items(json: str) -> str:
            return f"unnest(ARRAY(SELECT jsonb_array_elements({json})))"

        children = items("CASE WHEN jsonb_typeof(kn.item) = 'array' THEN kn.item ELSE '[]'::jsonb END")
        walked = (
            f"SELECT ARRAY[ke.item_no] AS place, ke.item FROM {items(list_)} WITH ORDINALITY ke (item, item_no) "
            f"UNION ALL SELECT kn.place || kc.item_no, kc.item FROM kn, {children} WITH ORDINALITY kc (item, item_no)"
        )
        ends = (
            "SELECT kn.place || CAST(jsonb_array_length(kn.item) + 1 AS bigint), 0, 'null' FROM kn "
            "WHERE jsonb_typeof(kn.item) = 'array'"
        )
        tokens = (
            f"SELECT kn.place, {self.item_rank('kn.item')} AS rank, "
            "CASE WHEN jsonb_typeof(kn.item) = 'array' THEN 'null' ELSE CAST(kn.item AS text) END AS item "
            f"FROM kn UNION ALL {ends}"
        )
        return (
            f"(WITH RECURSIVE kn (place, item) AS ({walked}) "
            "SELECT COALESCE(string_agg('[' || kt.rank || ', ' || kt.item || ', ', '' ORDER BY kt.place), '') "
            f"|| '[]' || repeat(']', COUNT(*)::int) FROM ({tokens}) kt)"
        )

    def id_tokens(self, ids: str, key: str) -> str:
        token = literal('{"' + key + '": \\1}')
        return f"CAST(regexp_replace(CAST({ids} AS text), '(-?[0-9]+)', {token}, 'g') AS jsonb)"

    def modulo(self, a: str, b: str) -> str:
        return f"({a} % {b})"

    def starts_with(self, a: str, b: str) -> str:
        return f"starts_with({a}, {b})"

    def ends_with(self, a: str, b: str) -> str:
        return self.shared1(b, lambda x: f"right({a}, length({x})) = {x}")

    def contains_text(self, a: str, b: str) -> str:
        return f"strpos({a}, {b}) > 0"

    def function(self, name: str, json: str) -> str | None:
        if name == "keys":
            return self.shared1(
                json,
                lambda x: (
                    f"CASE WHEN jsonb_typeof({x}) = 'object' THEN "
                    f"(SELECT COALESCE(jsonb_agg(k ORDER BY k), '[]'::jsonb) FROM jsonb_object_keys({x}) k) END"
                ),
            )
        functions: dict[str, Callable[[], str]] = {
            "size": lambda: (
                f"CASE jsonb_typeof({json}) WHEN 'array' THEN to_jsonb(jsonb_array_length({json})) "
                f"WHEN 'string' THEN to_jsonb(length({json} #>> '{{}}')) END"
            ),
            "tolower": lambda: f"to_jsonb(lower({self.text(json)}))",
            "toupper": lambda: f"to_jsonb(upper({self.text(json)}))",
            "trim": lambda: f"to_jsonb(btrim({self.text(json)}))",
            "tostring": lambda: f"to_jsonb({self._as_text(json)})",
            "tointeger": lambda: f"to_jsonb(trunc({self._numeric(json)}))",
            "tofloat": lambda: f"to_jsonb({self.float_(self._numeric(json))})",
            "head": lambda: f"({json} -> 0)",
            "last": lambda: f"({json} -> -1)",
            "abs": lambda: f"to_jsonb(abs({self.number(json)}))",
        }
        function = functions.get(name)
        return function() if function is not None else None

    def _numeric(self, json: str) -> str:
        """The JSON as a `numeric`, where it is a number or a text that writes one: null for any other text, as Cypher
        converts it."""
        return self.shared1(
            json,
            lambda x: (
                f"CASE WHEN jsonb_typeof({x}) = 'number' OR ({x} #>> '{{}}') ~ {literal(_NUMERIC_TEXT)} "
                f"THEN CAST({x} #>> '{{}}' AS numeric) END"
            ),
        )

    def page(self, skip: str | None, limit: str | None) -> list[str]:
        return [*([f"OFFSET {skip}"] if skip is not None else []), *([f"LIMIT {limit}"] if limit is not None else [])]

    def ids(self, id_: str) -> str:
        return f"ARRAY[{id_}]"

    def join_ids(self, lists: Sequence[str]) -> str:
        return "(" + " || ".join(lists) + ")"

    def store_ids(self, json: str) -> str:
        return self.shared1(
            json,
            lambda ids: (
                f"CASE WHEN {ids} IS NOT NULL THEN ARRAY(SELECT CAST(x.id AS bigint) FROM "
                f"jsonb_array_elements_text({ids}) WITH ORDINALITY x (id, ord) ORDER BY x.ord) END"
            ),
        )

    @property
    def no_ids(self) -> str:
        return "CAST(ARRAY[] AS bigint[])"

    def id_in(self, id_: str, list_: str) -> str:
        return f"{id_} = ANY ({list_})"

    def ids_overlap(self, a: str, b: str) -> str:
        return f"{a} && {b}"

    def id_count(self, list_: str) -> str:
        return f"to_jsonb(cardinality({list_}))"

    def id_rows(self, list_: str, alias: str) -> str:
        return f"unnest({list_}) WITH ORDINALITY {alias} (id, ord)"

    def walk(self, walk: Walk) -> Walked:
        """A lateral recursive query from the node walked from, which reads its row."""
        w = walk.step
        joins, reached, distinct = self.steps(walk, f"{w}.nid")
        eids = [f"{it.edge}.eid" for it in walk.hops]
        conditions = [
            *(f"{it} <> ALL ({w}.eids)" for it in eids),
            *distinct,
            *([f"{w}.depth < {walk.max}"] if walk.max is not None else []),
            *walk.conditions,
        ]
        # Walked backwards, each node it leaves goes first, so its nodes end with the one it started from.
        if walk.backwards:
            step = f"    SELECT {reached[0]}, {w}.depth + 1, {w}.nid || {w}.nids, {eids[0]} || {w}.eids"
        else:
            step = (
                f"    SELECT {reached[-1]}, {w}.depth + 1, {w}.nids || ARRAY[{', '.join(reached)}], "
                f"{w}.eids || ARRAY[{', '.join(eids)}]"
            )
        text = "\n".join(
            [
                f"WITH RECURSIVE {w} (nid, depth, nids, eids) AS (",
                f"    SELECT {walk.from_}, 0, {self.no_ids}, {self.no_ids}",
                "    UNION ALL",
                step,
                f"    FROM {w}",
                *(f"    {it}" for it in joins),
                f"    WHERE {' AND '.join(conditions)}",
                ")",
                f"SELECT nid, nids, eids FROM {w} WHERE depth >= {walk.min}",
            ]
        )
        return Walked(
            f"LATERAL (\n{indent(text)}\n) {walk.name}",
            f"{walk.name}.nid = {walk.to}",
            f"{walk.name}.eids",
            f"{walk.name}.nids",
        )

    def node_json(self, node: Node) -> str:
        return (
            f"CASE WHEN {node.nid} IS NOT NULL THEN {node.props} || "
            f"jsonb_strip_nulls(jsonb_build_object({literal(stamps.PROPERTY)}, {node.stamp})) END"
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
        """A recursive query, a row for each item taken, that reads the row it is in, as Postgres's may."""
        return self.shared1(list_, lambda items: self._fold(name, initial, items, step))

    def _fold(self, name: str, initial: str, list_: str, step: Callable[[str, str], str]) -> str:
        text = "\n".join(
            [
                f"WITH RECURSIVE {name} (i, acc) AS (",
                f"    SELECT 0, CAST({initial} AS jsonb)",
                "    UNION ALL",
                f"    SELECT {name}.i + 1, CAST({step(f'{name}.acc', f'({list_} -> {name}.i)')} AS jsonb) FROM {name} "
                f"WHERE {name}.i < jsonb_array_length({list_})",
                ")",
                f"SELECT acc FROM {name} ORDER BY i DESC LIMIT 1",
            ]
        )
        return f"CASE WHEN jsonb_typeof({list_}) = 'array' THEN (\n{indent(text)}\n) END"

    def range(self, from_: str, to: str, step: str) -> str:
        return (
            "(SELECT COALESCE(jsonb_agg(g ORDER BY o), '[]'::jsonb) FROM "
            f"generate_series(CAST({from_} AS bigint), CAST({to} AS bigint), CAST({step} AS bigint)) "
            "WITH ORDINALITY r (g, o))"
        )

    @property
    def row_number(self) -> str:
        return "ROW_NUMBER() OVER ()"

    def bound_select(self, columns: str) -> str:
        """
        Postgres pulls a subquery of no table up into the query that reads it, and writes each of its values out again
        where it is read: values bound in one another so grow exponentially as it plans, and a duration added to a date
        read from the store took the server's memory. `OFFSET 0` keeps the subquery apart.
        """
        return f"SELECT {columns} OFFSET 0"

    @property
    def temporal_sql(self) -> TemporalSql:
        return PostgresTemporalSql(self.shared)

    def matches(self, text: str, pattern: str) -> str:
        """
        Flags the pattern opens with, `(?i)`, are put before the anchors, where Postgres reads them, after `p`: a dot
        matches no line break, as Java's does, unless the pattern says `(?s)`. The anchors are `\\A` and `\\Z`, the
        text's start and end, which `(?m)` leaves as they are: it lets the pattern's own `^` and `$` match at a line
        break. Postgres keeps only the last of its `s` and `m`, so flags with both get a `w` after them, which is both.
        """
        both = f"regexp_replace({pattern}, '^\\(\\?([a-z]*(s[a-z]*m|m[a-z]*s)[a-z]*)\\)', '(?\\1w)')"
        return f"({text} ~ regexp_replace({both}, '^(\\(\\?([a-z]+)\\))?(.*)$', '(?p\\2)\\\\A(\\3)\\\\Z'))"

    def split(self, text: str, separator: str) -> str:
        """
        An empty separator cuts the text at each character, as Cypher's does: `string_to_array` does so for a null one.
        The empty text is a list of itself, where `string_to_array` gives an empty one.
        """
        return self.shared(
            [text, separator],
            lambda it: (
                f"CASE WHEN {it[1]} IS NOT NULL THEN CASE WHEN {it[0]} = '' THEN jsonb_build_array({it[0]}) "
                f"ELSE to_jsonb(string_to_array({it[0]}, NULLIF({it[1]}, ''))) END END"
            ),
        )

    def reverse_text(self, text: str) -> str:
        return f"reverse({text})"

    def uuid(self) -> str:
        return "CAST(gen_random_uuid() AS text)"

    def math(self, name: str, numbers: Sequence[str]) -> str:
        """
        Worked out as a `double precision`, as Neo4j works it out as a Java double, and written back as the text of that
        double: a cast to `numeric` would keep 15 of its digits. Postgres fails where Neo4j gives NaN or an infinity,
        `ln(0)`, `sqrt(-1)`, `asin(2)`, `exp(1000)` and `1 / tan(0)` among them, but for an exponent too small for a
        double, which Neo4j gives as 0.0, and Postgres would fail on as an underflow.
        """
        doubles = [f"CAST({it} AS double precision)" for it in numbers]
        x = doubles[0]
        results = {
            "exp": lambda: self.shared1(x, lambda d: f"CASE WHEN {d} < {_EXP_UNDERFLOW} THEN 0 ELSE exp({d}) END"),
            "log": lambda: f"ln({x})",
            "log10": lambda: f"log({x})",
            "cot": lambda: f"(1 / tan({x}))",
            "haversin": lambda: f"((1 - cos({x})) / 2)",
            # By the ratio Java multiplies by, where Postgres divides by its inverse: see RADIANS_TO_DEGREES.
            "degrees": lambda: f"({x} * CAST({RADIANS_TO_DEGREES} AS double precision))",
            "atan2": lambda: f"atan2({x}, {doubles[1] if len(doubles) > 1 else 'NULL'})",
        }
        result = results[name]() if name in results else f"{name}({x})"
        return self.float_(f"CAST({result} AS text)")

    def right_text(self, text: str, count: str) -> str:
        return f"right({text}, {count})"

    def procedure(self, body: list[str], parameters: dict[str, Any]) -> CompiledCypher:
        """
        A function of the session's own, `pg_temp`, made for the procedure, then called: Postgres runs a block that
        returns rows or takes parameters no other way. It is named for its text, so a statement run again replaces it
        with itself. It is made by a statement of its own, the prelude: psycopg sends a statement with parameters as one
        command.
        """
        declared = [
            "#variable_conflict use_column",
            "DECLARE",
            f"    {ROWS} jsonb;",
            f"    {OUT} jsonb;",
            f"    {ROW} jsonb;",
            f"    {VALUE} jsonb;",
            f"    {ID} bigint;",
            f"    {COUNT} bigint;",
            f"    {FOUND} boolean;",
            f"    {DOOMED} jsonb;",
            f"    {RECORD} record;",
            f"    {MATCHED} record;",
        ]
        for depth in range(1, NESTING + 1):
            declared += [f"    {SAVED}{depth} jsonb;", f"    {GATHERED}{depth} jsonb;", f"    {CALLER}{depth} jsonb;"]
        text = "\n".join([*declared, "BEGIN", indent("\n".join(body)), "END"])
        digest = hashlib.sha256(text.encode()).hexdigest()
        function = f"pg_temp.drivine_cypher_{digest[:24]}"
        # The body is quoted with dollars, by a tag no text in it holds: a text of `$cy$` would end it. The first of
        # `$cy$`, `$cy1$`, `$cy2$`, … as drivine4j and drivine choose it.
        quote = next(it for it in itertools.chain(["$cy$"], (f"$cy{n}$" for n in itertools.count(1))) if it not in text)
        prelude = (
            f"CREATE OR REPLACE FUNCTION {function}({PARAMS} jsonb) RETURNS SETOF json LANGUAGE plpgsql "
            f"AS {quote}\n{text}\n{quote}"
        )
        return CompiledCypher(
            f"SELECT cy.result FROM {function}(CAST(${OBJECT} AS jsonb)) AS cy (result)", parameters, prelude
        )

    def return_rows(self, query: str) -> list[str]:
        return [f"RETURN QUERY SELECT CAST(cy_q.result AS json) FROM (\n{indent(query)}\n) cy_q;"]

    def raise_(self, message: str) -> str:
        return f"RAISE EXCEPTION USING MESSAGE = {literal(message)};"

    def id_of(self, json: str, key: str | None) -> str:
        return f"CAST({json} ->> {literal(key)} AS bigint)" if key is not None else f"CAST({json} #>> '{{}}' AS bigint)"

    def appended(self, list_: str, item: str) -> str:
        return f"({list_} || jsonb_build_array({item}))"

    def strip_nulls(self, json: str) -> str:
        return f"jsonb_strip_nulls(CAST({json} AS jsonb))"

    def merged(self, props: str, map_: str) -> str:
        return f"jsonb_strip_nulls({props} || CAST({map_} AS jsonb))"

    def without_label(self, labels: str, label: str) -> str:
        return f"({labels} - {literal(label)})"


def _float_text(n: str) -> str:
    """The `numeric`, a float without trailing zeros, as Java writes a double: see PostgresCypherCompiler._as_text."""
    # `to_char` writes 17 digits and the exponent: the digits without the zeros after them, a point and one at least.
    scientific = f"to_char({n}, '{_SCIENTIFIC}')"
    mantissa = f"regexp_replace(rtrim(btrim(split_part({scientific}, 'e', 1)), '0'), '\\.$', '.0')"
    return (
        f"CASE WHEN {n} = 0 THEN '0.0' "
        f"WHEN abs({n}) >= 0.001 AND abs({n}) < 10000000 THEN to_char({n}, '{PLAIN_FLOAT}') "
        f"ELSE {mantissa} || 'E' || CAST(split_part({scientific}, 'e', 2) AS int) END"
    )


def _written(value: Any) -> str:
    """
    The value, of JSON's own kinds, as JSON text, each float in it that is a whole number written out in full with a
    fraction, `10000000000000000.0`, from its shortest decimal: as Python writes it, `1e+16`, `jsonb` reads it as the
    whole number, which is then read back as an int. NaN and the infinities are written as the texts drivine4j binds
    them as, `"NaN"`, `"Infinity"`, `"-Infinity"`: `jsonb` holds no such number.
    """
    if isinstance(value, float):
        if math.isnan(value):
            return '"NaN"'
        if math.isinf(value):
            return '"Infinity"' if value > 0 else '"-Infinity"'
        if value.is_integer():
            whole = format(Decimal(repr(value)), "f")
            return whole if "." in whole else f"{whole}.0"
        # As Java writes it, as drivine4j binds it: Postgres works a float out in exact decimal, so its digits are
        # those it computes with, 4.9E-324 and not 5e-324.
        return java_text(value)
    if isinstance(value, dict):
        entries = cast(dict[str, Any], value)
        return "{" + ",".join(f"{json_text(key)}:{_written(it)}" for key, it in entries.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_written(it) for it in cast(list[Any], value)) + "]"
    return json_text(value)
