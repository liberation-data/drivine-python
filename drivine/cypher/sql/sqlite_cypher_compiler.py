from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Sequence
from typing import Any, cast

from drivine.backend.sqlite import sqlite_graph
from drivine.backend.sqlite.sqlite_graph import holds_label, property_text, quoted
from drivine.cypher.sql import procedure_step as steps
from drivine.cypher.sql.compiled_cypher import CompiledCypher
from drivine.cypher.sql.parameters import OBJECT, jsonable
from drivine.cypher.sql.procedure_step import ProcedureStep
from drivine.cypher.sql.sql_cypher_compiler import (
    CALLER,
    COUNT,
    FOUND,
    GATHERED,
    ID,
    MATCHED,
    MOST_ARGUMENTS,
    NESTING,
    OUT,
    PARAMS,
    RECORD,
    ROW,
    ROWS,
    SAVED,
    VALUE,
    SqlCypherCompiler,
    Walk,
    Walked,
    chunked,
    indent,
    java_text,
    literal,
)
from drivine.cypher.sql.sqlite_temporal_sql import SqliteTemporalSql
from drivine.cypher.sql.temporal_sql import TemporalSql
from drivine.cypher.sql.value import Json, Node
from drivine.model import stamps

_VARIABLES = frozenset(
    {PARAMS, ROWS, OUT, ROW, VALUE, ID, COUNT, FOUND}
    | {f"{it}{depth}" for it in (SAVED, GATHERED, CALLER) for depth in range(1, NESTING + 1)}
)
"""The variables of a procedure, which a step's SQL reads as numbered parameters: see SqliteCypherCompiler.procedure."""

_RECORDS = frozenset({RECORD, MATCHED})
"""The records of a procedure's loops, whose `item` a step reads."""

_NUMERIC_TEXT = r"^\s*[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?\s*$"
"""A text SQLite reads as a number, spaces around it allowed."""

_WHITESPACE = "char(32, 9, 10, 11, 12, 13)"
"""The characters `trim` takes off a text's ends, as Cypher's does: SQLite's own takes spaces alone."""

_PROPERTY = re.compile(r"^\(([\w.]+) -> ('(?:[^']|'')*')\)$")
"""A property as `property_of` reads it from a column: `(n1.props -> '$."name"')`."""


class SqliteCypherCompiler(SqlCypherCompiler):
    """
    Compiles a Cypher statement to SQL over a graph's tables on SQLite: see SqlCypherCompiler. A statement it compiles
    is read with its text values as JSON: the connection gives it `/* drivine:json */` to open with. drivine4j's
    SqliteCypherCompiler is the reference: the two compile a statement alike.

    - **A value** is JSON text, which SQLite's JSON functions read: SQLite has no JSON type, and a value that passes
      through a subquery is text again, so a value put into a list or a map is read as JSON there with `json(…)`.
    - **What Cypher does with values** that SQLite's JSON does not, functions Drivine registers on each connection do
      (see sqlite_functions): values compare as Cypher compares them, `1` equal to `1.0` and a map to a map of the same
      entries in any order, and lists and texts are added. A value orders by the SQL value `->>` reads of it.
    - **Temporal values** are worked out by SqliteTemporalSql.
    - **A lateral query** (an `OPTIONAL MATCH`, a `CALL { … }`, a shortest path, a walk) is not one SQLite joins: its
      rows are gathered as a JSON list of maps in a subquery, which may read the row it is in, and that list is joined
      with `json_each`, whose argument may read the row too. Each column is read back out of the map.
    - **A list of ids** is text, each id between commas (`,1,2,`), as on Oracle.
    - **A relationship of either direction** is joined with an OR across its two directions, which SQLite probes by
      index, not read both ways round as on Postgres and Oracle (`undirected_both_ways`).

    A statement that writes is a procedure, as on the other stores, which a function Drivine registers carries out:
    see `procedure`.
    """

    undirected_both_ways = False
    """
    A relationship of either direction keeps its OR across the two directions here, which SQLite plans as a
    `MULTI-INDEX OR`, probing the index on each end. Read both ways round, as on Postgres and Oracle (#390), SQLite
    builds an index over all of it for each statement: at 20,000 nodes a walk from 20 of them took 59 ms, against
    9.5 ms with the OR, and a single step 66 ms against 30 (liberation-data/drivine4j#574).
    """

    def literal_json(self, value: Any) -> str:
        """A float the statement writes as Java writes a double, `1.0E9`, as drivine4j's JSON writes it."""
        return literal(_java_json(jsonable(value)))

    def parameter_text(self, value: Any) -> str:
        """NaN and the infinities as the texts drivine4j binds them as, `"NaN"`, `"Infinity"`, `"-Infinity"`: JSON holds
        no such number."""
        return _parameter_json(jsonable(value))

    def parameter_json(self, marker: str, text: str | None) -> str:
        return marker

    def parameter_count(self, marker: str) -> str:
        return f"CAST({marker} AS INTEGER)"

    def comparable(self, value: Json) -> str:
        """A map or a list the statement builds with its maps' keys in order, so two of the same entries group and are
        kept once as one."""
        return f"{sqlite_graph.CANONICAL}({value.sql})" if value.ordered else value.sql

    def scalar(self, sql: str) -> str:
        """Null as SQL's null, where `json_quote` writes it as JSON's."""
        return f"NULLIF(json_quote({sql}), 'null')"

    def failure(self, message: str, value: str) -> str:
        """SQLite has no failing expression of its own: Drivine's function fails with the text."""
        return f"{sqlite_graph.FAIL}({literal(f'{message} ')} || {value})"

    def type_of(self, json: str) -> str:
        return (
            f"(CASE json_type({json}) WHEN 'integer' THEN 'number' WHEN 'real' THEN 'number' WHEN 'text' THEN 'string' "
            "WHEN 'true' THEN 'boolean' WHEN 'false' THEN 'boolean' WHEN 'array' THEN 'array' "
            "WHEN 'object' THEN 'object' "
            "WHEN 'null' THEN 'null' END)"
        )

    def is_json_null(self, json: str) -> str:
        return f"COALESCE(json_type({json}), 'null') = 'null'"

    def null_if_json_null(self, json: str) -> str:
        return f"NULLIF({json}, 'null')"

    def number(self, json: str) -> str:
        return self.shared1(json, lambda x: f"CASE WHEN json_type({x}) IN ('integer', 'real') THEN {x} ->> '$' END")

    def integral(self, json: str) -> str:
        return f"(json_type({json}) = 'integer')"

    def text(self, json: str) -> str:
        return self.shared1(json, lambda x: f"CASE WHEN json_type({x}) = 'text' THEN {x} ->> '$' END")

    def bool_of(self, json: str) -> str:
        return f"(CASE json_type({json}) WHEN 'true' THEN 1 WHEN 'false' THEN 0 END)"

    def bool_json(self, sql: str) -> str:
        return f"(CASE ({sql}) WHEN 1 THEN 'true' WHEN 0 THEN 'false' END)"

    def float_(self, sql: str) -> str:
        return f"({sql} + 0.0)"

    def json_array(self, items: Sequence[str]) -> str:
        """
        SQLite takes no more than 100 arguments to a function (drivine4j#332): past them, the first 100 items are a
        `json_array`, and the rest are appended to it in parts, each by a `json_insert` of as many as it takes.
        """
        listed = f"json_array({', '.join(f'json({it})' for it in items[:MOST_ARGUMENTS])})"
        for part in chunked(items[MOST_ARGUMENTS:], (MOST_ARGUMENTS - 1) // 2):
            listed = f"json_insert({listed}, {', '.join(f'{chr(39)}$[#]{chr(39)}, json({it})' for it in part)})"
        return listed

    def json_object(self, entries: Sequence[tuple[str, str]]) -> str:
        """Past 50 entries, as for `json_array`, the rest are set over the first 50 by `with_entries`."""
        first = ", ".join(f"{literal(key)}, json({value})" for key, value in entries[: MOST_ARGUMENTS // 2])
        return self.with_entries(f"json_object({first})", entries[MOST_ARGUMENTS // 2 :])

    def with_entries(self, json: str, entries: Sequence[tuple[str, str]]) -> str:
        """In parts, as for `json_array`: a `json_set` of as many entries as it takes, each set over the last."""
        for part in chunked(entries, (MOST_ARGUMENTS - 1) // 2):
            json = f"json_set({json}, {', '.join(f'{_path(key)}, json({value})' for key, value in part)})"
        return json

    def property_of(self, json: str, key: str) -> str:
        return f"({json} -> {_path(key)})"

    def element(self, json: str, index: str) -> str:
        """
        Counted from the end where negative, as the path `$[#-N]` reads it: `-> -N` is refused before SQLite 3.47. The
        index is read once, and a null one gives a null path, which reads null.
        """
        return f"({json} -> ('$[' || replace(CAST({index} ->> '$' AS INTEGER), '-', '#-') || ']'))"

    def keyed(self, json: str, key: str) -> str | None:
        return f"({json} -> ('$.' || json_quote({key} ->> '$')))"

    def contains_all(self, props: str, map_: str) -> str:
        return f"{sqlite_graph.CONTAINS}({props}, {map_})"

    def has_label(self, labels: str, label: str) -> str:
        """The test a partial index over the label's nodes is made with: see sqlite_graph.has_label."""
        return holds_label(labels, label)

    def indexed_equality(self, property: str, value: str) -> str | None:
        """
        The property's text, as a range index or a uniqueness constraint reads it (see sqlite_graph.property_text),
        compared with the value's: a string's text, or a truth value's JSON.
        """
        read = _PROPERTY.match(property)
        if read is None:
            return None
        text = property_text(read[1], read[2])
        return f"{text} = (CASE json_type({value}) WHEN 'text' THEN {value} ->> '$' ELSE {value} END)"

    def key_value(self, json: str) -> str:
        """SQLite compares an `INTEGER PRIMARY KEY` with a float by its value, so `5.0` reads the row whose key is
        `5`."""
        return self.shared1(json, lambda x: f"(CASE WHEN json_type({x}) IN ('integer', 'real') THEN {x} ->> '$' END)")

    def elements(self, json: str, alias: str) -> str:
        return f"json_each({json}) AS {alias}"

    def item(self, alias: str) -> str:
        """`json_each` gives a string, a number or a truth value as the SQL value it is, and a list or a map as JSON."""
        return (
            f"(CASE {alias}.type WHEN 'true' THEN 'true' WHEN 'false' THEN 'false' WHEN 'null' THEN 'null' "
            f"WHEN 'array' THEN {alias}.value WHEN 'object' THEN {alias}.value ELSE json_quote({alias}.value) END)"
        )

    def item_no(self, alias: str) -> str:
        return f"({alias}.key + 1)"

    def item_at(self, json: str, index: int) -> str:
        return f"({json} -> '$[{index if index >= 0 else f'#{index}'}]')"

    def list_size(self, json: str) -> str:
        size = self.function("size", json)
        assert size is not None
        return self.number(size)

    def json_equals(self, x: str, y: str) -> str:
        return f"{sqlite_graph.EQUAL}({x}, {y})"

    def identity_equals(self, x: str, y: str) -> str:
        return self.json_equals(x, y)

    def holds_null(self, json: str) -> str | None:
        """`drivine_equal` compares a value that holds a null as Cypher does, null where no item or entry differs."""
        return None

    def nulls_equal(self, x: str, y: str, equals: Callable[[str, str], str]) -> str:
        return equals(x, y)

    def given_equals(self, x: str, y: str, given: Any) -> str:
        """A text, a number or a truth value the statement gives is compared without the function that compares any
        two values."""
        if isinstance(given, str):
            return self.shared(
                [x, y], lambda v: f"(json_type({v[0]}) = 'text' AND ({v[0]} ->> '$') = ({v[1]} ->> '$'))"
            )
        if isinstance(given, bool):
            return f"(json_type({x}) = json_type({y}))"
        # NaN and the infinities are bound as the texts drivine4j binds them as, and compared as any value is.
        if isinstance(given, int) or (isinstance(given, float) and math.isfinite(given)):
            return self.shared(
                [x, y],
                lambda v: f"(json_type({v[0]}) IN ('integer', 'real') AND ({v[0]} ->> '$') = ({v[1]} ->> '$'))",
            )
        return self.json_equals(x, y)

    def ordered(self, x: str, operator: str, y: str) -> str:
        return f"(({x} ->> '$') {operator} ({y} ->> '$'))"

    def list_order(self, x: str, y: str) -> str:
        """By `drivine_list_order`, which walks the two as Cypher does."""
        return f"{sqlite_graph.LIST_ORDER}({x}, {y})"

    def sort_key(self, json: str) -> str:
        """A list as `drivine_sort_key` writes it, which orders as Cypher orders lists; any other value as the SQL
        value `->>` reads."""
        return self.shared1(
            json,
            lambda x: f"CASE WHEN json_type({x}) = 'array' THEN {sqlite_graph.SORT_KEY}({x}) ELSE ({x} ->> '$') END",
        )

    def open_sort_key(self, json: str) -> list[str]:
        """
        Where the kind comes, then the SQL value `->>` reads, which orders a number before a text, and a truth value, 1
        or 0, among the numbers. The first term is a text: the kind's rank, a list's followed by itself as
        `drivine_sort_key` writes it, so a list is ordered by it; and null for null. The value is read once more, not
        twice, as `sort_key` reads it, and each term is a value JSON holds, as a `CALL { … }` carries it out.
        """
        rank = self.shared1(
            json,
            lambda x: (
                f"CASE json_type({x}) WHEN 'object' THEN '1' WHEN 'array' THEN '2' || {sqlite_graph.SORT_KEY}({x}) "
                "WHEN 'text' THEN '3' WHEN 'true' THEN '4' WHEN 'false' THEN '4' WHEN 'integer' THEN '5' "
                "WHEN 'real' THEN '5' END"
            ),
        )
        return [rank, self.shared1(json, lambda x: f"({x} ->> '$')")]

    def order_term(self, term: str, descending: bool) -> str:
        """SQLite puts null first when ascending, and Cypher last."""
        return f"{term} DESC NULLS FIRST" if descending else f"{term} NULLS LAST"

    def whole_quotient(self, a: str, b: str) -> str:
        """SQLite divides one whole number by another to a whole number, toward zero."""
        return f"({a} / {b})"

    def aggregate_list(self, value: str, order: str | None = None) -> str:
        return f"json_group_array(json({value}){f' ORDER BY {order}' if order else ''})"

    def collect(self, value: str, order: str | None) -> str:
        return (
            f"json_group_array(json({value}){f' ORDER BY {order}' if order else ''}) FILTER (WHERE {value} IS NOT NULL)"
        )

    def joined_texts(self, text: str, order: str) -> str:
        return f"group_concat({text}, '' ORDER BY {order})"

    def distinct_items(self, list_: str, temporal: bool = False) -> str:
        return (
            f"(SELECT json_group_array(json(d.item) ORDER BY d.first) FROM (SELECT min({self.item_no('e')}) AS first, "
            f"{self.item('e')} AS item FROM {self.elements(list_, 'e')} "
            f"GROUP BY {sqlite_graph.CANONICAL}({self.item('e')})) d)"
        )

    def first_of_each(self, list_: str, identities: str) -> str:
        """Each item read at its place: `json_each` is no table a join can look a place up in, so a join reads it
        whole for each."""
        return (
            f"(SELECT json_group_array(json({list_} -> ('$[' || f.first || ']')) ORDER BY f.first) "
            f"FROM (SELECT min(e.key) AS first FROM {self.elements(identities, 'e')} "
            f"GROUP BY {sqlite_graph.CANONICAL}({self.item('e')})) f)"
        )

    def extreme(self, value: str, min_: bool, key: str | None = None) -> str:
        """The first of the values in order, the nulls left out: SQLite's `min` and `max` order no JSON."""
        direction = "ASC" if min_ else "DESC"
        by = key if key is not None else f"({value} ->> '$')"
        return (
            f"(json_group_array(json({value}) ORDER BY {self.kind_order(value)} {direction}, {by} {direction}) "
            f"FILTER (WHERE NOT {self.is_json_null(value)}) -> 0)"
        )

    def id_tokens(self, ids: str, key: str) -> str:
        return self.shared1(
            ids,
            lambda x: (
                f"CASE WHEN {x} IS NOT NULL THEN (SELECT json_group_array(CASE WHEN t.type = 'null' THEN json('null') "
                f"ELSE json_object({literal(key)}, t.value) END ORDER BY t.key) FROM json_each({x}) t) END"
            ),
        )

    def add(self, a: str, b: str, given: tuple[Any, Any] = (None, None)) -> str:
        return f"{sqlite_graph.ADD}({a}, {b})"

    def modulo(self, a: str, b: str) -> str:
        """`%` of two whole numbers, and of any other two `mod`, which keeps the fraction: SQLite's `%` takes whole
        numbers alone."""
        return self.shared(
            [a, b],
            lambda v: (
                f"CASE WHEN typeof({v[0]}) = 'integer' AND typeof({v[1]}) = 'integer' THEN {v[0]} % {v[1]} "
                f"ELSE mod({v[0]}, {v[1]}) END"
            ),
        )

    def starts_with(self, a: str, b: str) -> str:
        return self.shared(
            [a, b],
            lambda v: (
                f"CASE WHEN {v[0]} IS NOT NULL AND {v[1]} IS NOT NULL "
                f"THEN substr({v[0]}, 1, length({v[1]})) = {v[1]} END"
            ),
        )

    def ends_with(self, a: str, b: str) -> str:
        return self.shared(
            [a, b],
            lambda v: (
                f"CASE WHEN {v[0]} IS NOT NULL AND {v[1]} IS NOT NULL "
                f"THEN ({v[1]} = '' OR substr({v[0]}, -length({v[1]})) = {v[1]}) END"
            ),
        )

    def contains_text(self, a: str, b: str) -> str:
        return f"(instr({a}, {b}) > 0)"

    def function(self, name: str, json: str) -> str | None:
        def keys(x: str) -> str:
            return (
                f"CASE WHEN json_type({x}) = 'object' THEN "
                f"(SELECT json_group_array(k.key ORDER BY k.key) FROM json_each({x}) k) END"
            )

        def size(x: str) -> str:
            return (
                f"CASE json_type({x}) WHEN 'array' THEN json_quote(json_array_length({x})) "
                f"WHEN 'text' THEN json_quote(length({x} ->> '$')) END"
            )

        def to_string(x: str) -> str:
            # A string is itself; anything else is the text of its JSON.
            return (
                f"CASE json_type({x}) WHEN 'text' THEN {x} WHEN 'null' THEN NULL "
                f"WHEN 'real' THEN json_quote({sqlite_graph.FLOAT_TEXT}({x} ->> '$')) ELSE json_quote('' || {x}) END"
            )

        def to_integer(x: str) -> str:
            return (
                f"CASE json_type({x}) WHEN 'integer' THEN {x} "
                f"WHEN 'real' THEN json_quote(CAST({x} ->> '$' AS INTEGER)) "
                f"WHEN 'text' THEN CASE WHEN trim({x} ->> '$') REGEXP '^[+-]?[0-9]+$' "
                f"THEN json_quote(CAST(trim({x} ->> '$') AS INTEGER)) "
                f"WHEN ({x} ->> '$') REGEXP {literal(_NUMERIC_TEXT)} "
                f"THEN json_quote(CAST(CAST(trim({x} ->> '$') AS REAL) AS INTEGER)) END END"
            )

        def to_float(x: str) -> str:
            return (
                f"CASE json_type({x}) WHEN 'integer' THEN json_quote(CAST({x} ->> '$' AS REAL)) WHEN 'real' THEN {x} "
                f"WHEN 'text' THEN CASE WHEN ({x} ->> '$') REGEXP {literal(_NUMERIC_TEXT)} "
                f"THEN json_quote(CAST(trim({x} ->> '$') AS REAL)) END END"
            )

        functions: dict[str, Callable[[], str]] = {
            "keys": lambda: self.shared1(json, keys),
            "size": lambda: self.shared1(json, size),
            "tolower": lambda: self.scalar(f"{sqlite_graph.LOWER}({self.text(json)})"),
            "toupper": lambda: self.scalar(f"{sqlite_graph.UPPER}({self.text(json)})"),
            "trim": lambda: self.scalar(f"trim({self.text(json)}, {_WHITESPACE})"),
            "tostring": lambda: self.shared1(json, to_string),
            "tointeger": lambda: self.shared1(json, to_integer),
            "tofloat": lambda: self.shared1(json, to_float),
            "head": lambda: f"({json} -> 0)",
            "last": lambda: self.item_at(json, -1),
            "abs": lambda: self.scalar(f"abs({self.number(json)})"),
        }
        function = functions.get(name)
        return function() if function is not None else None

    def page(self, skip: str | None, limit: str | None) -> list[str]:
        if skip is None and limit is None:
            return []
        return [f"LIMIT {limit if limit is not None else '-1'}{f' OFFSET {skip}' if skip is not None else ''}"]

    def ids(self, id_: str) -> str:
        return f"(',' || {id_} || ',')"

    def join_ids(self, lists: Sequence[str]) -> str:
        return f"({lists[0]}{''.join(f' || substr({it}, 2)' for it in lists[1:])})"

    def store_ids(self, json: str) -> str:
        return self.shared1(
            json,
            lambda ids: (
                f"CASE WHEN {ids} IS NOT NULL THEN (SELECT ',' || COALESCE(group_concat(x.value, ',' ORDER BY x.key) "
                f"|| ',', '') FROM json_each({ids}) x) END"
            ),
        )

    @property
    def no_ids(self) -> str:
        return "','"

    def id_in(self, id_: str, list_: str) -> str:
        return f"instr({list_}, ',' || {id_} || ',') > 0"

    def ids_overlap(self, a: str, b: str) -> str:
        return f"EXISTS (SELECT 1 FROM {self.id_rows(a, 'ov')} WHERE instr({b}, ',' || {self.row_id('ov')} || ',') > 0)"

    def id_count(self, list_: str) -> str:
        return self.shared1(list_, lambda it: f"json_quote(length({it}) - length(replace({it}, ',', '')) - 1)")

    def id_rows(self, list_: str, alias: str) -> str:
        return f"json_each('[' || trim({list_}, ',') || ']') AS {alias}"

    def row_id(self, alias: str) -> str:
        return f"{alias}.value"

    def row_ord(self, alias: str) -> str:
        return f"({alias}.key + 1)"

    def joined(self, source: str) -> str:
        """
        `JOIN … ON TRUE`, which SQLite's planner may reorder: SQLite reads its sources in the order written across a
        `CROSS JOIN`, so a step `(n)-[r]->(m)` would read every pair of nodes before a relationship (#391). A
        `json_each` that reads an earlier source is still read after it.
        """
        return f"JOIN {source} ON TRUE"

    def lateral(self, query: str, name: str, optional: bool, columns: Sequence[tuple[str, bool]]) -> str:
        """
        A JSON list of maps, a map for each row of the query, gathered in a subquery that may read the row it is in,
        and joined as `json_each` gives its items: SQLite joins no lateral query.
        """
        rows = self._gathered(query, name, columns)
        return f"LEFT JOIN {rows} ON TRUE" if optional else self.joined(rows)

    def _gathered(self, query: str, name: str, columns: Sequence[tuple[str, bool]]) -> str:
        """The rows of the query, of the columns, as `json_each` gives the JSON list of maps they are gathered in,
        named `name`: see `lateral`."""
        entries = ", ".join(
            f"{literal(column)}, {f'json(q.{column})' if json else f'q.{column}'}" for column, json in columns
        )
        return f"json_each((SELECT json_group_array(json_object({entries})) FROM (\n{indent(query)}\n) q)) AS {name}"

    def lateral_column(self, name: str, column: str, json: bool) -> str:
        """JSON's null, which a column of SQL's null is in the map, as SQL's null again: a list of nodes that is null
        is not the list `[null]`."""
        return f"NULLIF({name}.value -> '$.{column}', 'null')" if json else f"({name}.value ->> '$.{column}')"

    def walk(self, walk: Walk) -> Walked:
        """
        A recursive query from the node walked from, which reads its row, its rows gathered as `lateral` gathers a
        query's: each the node a walk reached, and the ids of the nodes and relationships it took to get there.
        """
        w = walk.step
        joins, reached, distinct = self.steps(walk, f"{w}.nid")
        eids = [f"{it.edge}.eid" for it in walk.hops]
        conditions = [
            *(f"instr({w}.eids, ',' || {it} || ',') = 0" for it in eids),
            *distinct,
            *([f"{w}.depth < {walk.max}"] if walk.max is not None else []),
            *walk.conditions,
        ]
        # Walked backwards, each node it leaves goes first, so its nodes end with the one it started from.
        if walk.backwards:
            step = f"    SELECT {reached[0]}, {w}.depth + 1, ',' || {w}.nid || {w}.nids, ',' || {eids[0]} || {w}.eids"
        else:
            nids = "".join(f" || {it} || ','" for it in reached)
            eid_list = "".join(f" || {it} || ','" for it in eids)
            step = f"    SELECT {reached[-1]}, {w}.depth + 1, {w}.nids{nids}, {w}.eids{eid_list}"
        text = "\n".join(
            [
                f"WITH RECURSIVE {w} (nid, depth, nids, eids) AS (",
                f"    SELECT {walk.from_}, 0, ',', ','",
                "    UNION ALL",
                step,
                f"    FROM {w}",
                *(f"    {it}" for it in joins),
                f"    WHERE {' AND '.join(conditions)}",
                ")",
                f"SELECT nid, nids, eids FROM {w} WHERE depth >= {walk.min}",
            ]
        )
        name = walk.name
        rows = self._gathered(text, name, [("nid", False), ("nids", False), ("eids", False)])
        return Walked(
            rows,
            f"{self.lateral_column(name, 'nid', False)} = {walk.to}",
            self.lateral_column(name, "eids", False),
            self.lateral_column(name, "nids", False),
        )

    def node_json(self, node: Node) -> str:
        return (
            f"CASE WHEN {node.nid} IS NOT NULL THEN CASE WHEN {node.stamp} IS NULL THEN {node.props} "
            f"ELSE json_set({node.props}, {_path(stamps.PROPERTY)}, {node.stamp}) END END"
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
        """A recursive query, a row for each item taken, that reads the row it is in, as SQLite's may."""

        def folded(items: str) -> str:
            text = "\n".join(
                [
                    f"WITH RECURSIVE {name} (i, acc) AS (",
                    f"    SELECT 0, {initial}",
                    "    UNION ALL",
                    f"    SELECT {name}.i + 1, {step(f'{name}.acc', f'({items} -> {name}.i)')} FROM {name} "
                    f"WHERE {name}.i < json_array_length({items})",
                    ")",
                    f"SELECT acc FROM {name} ORDER BY i DESC LIMIT 1",
                ]
            )
            return f"CASE WHEN json_type({items}) = 'array' THEN (\n{indent(text)}\n) END"

        return self.shared1(list_, folded)

    def range(self, from_: str, to: str, step: str) -> str:
        """A recursive query, a row for each number: SQLite's `generate_series` is an extension its build leaves out."""

        def ranged(values: list[str]) -> str:
            first, last, by = values

            def within(g: str) -> str:
                return f"CASE WHEN {by} > 0 THEN {g} <= CAST({last} AS INTEGER) ELSE {g} >= CAST({last} AS INTEGER) END"

            text = "\n".join(
                [
                    "WITH RECURSIVE r (g, o) AS (",
                    f"    SELECT CAST({first} AS INTEGER), 1",
                    "    UNION ALL",
                    f"    SELECT r.g + CAST({by} AS INTEGER), r.o + 1 FROM r "
                    f"WHERE {within(f'r.g + CAST({by} AS INTEGER)')}",
                    ")",
                    f"SELECT json_group_array(r.g ORDER BY r.o) FROM r WHERE {within('r.g')}",
                ]
            )
            # A step of 0 would never end, and Cypher refuses it.
            return f"CASE WHEN CAST({by} AS INTEGER) <> 0 THEN (\n{indent(text)}\n) END"

        return self.shared([from_, to, step], ranged)

    @property
    def row_number(self) -> str:
        return "ROW_NUMBER() OVER ()"

    def uuid(self) -> str:
        return f"{sqlite_graph.UUID}()"

    def once(self, query: str) -> str:
        """Materialized, as SQLite otherwise flattens the query into the one that reads it, and makes a `randomUUID()`
        again for each read."""
        return f"WITH once AS MATERIALIZED (\n{indent(query)}\n)\nSELECT * FROM once"

    def math(self, name: str, numbers: Sequence[str]) -> str:
        """By Java's `Math`, as Neo4j works these out: see `drivine_math`. SQLite's own give null where Neo4j gives NaN
        or negative infinity."""
        return f"{sqlite_graph.MATH}({literal(name)}, {', '.join(numbers)})"

    def round_to(self, number: str, precision: str, mode: str) -> str:
        """In decimal, as Neo4j rounds: see `drivine_round`. SQLite's numbers are binary, and 2.345 is below 2.345
        there."""
        return f"{sqlite_graph.ROUND}({number}, {precision}, {literal(mode)})"

    def round_places(self, number: str, precision: str) -> str:
        """Half up, as Neo4j rounds by two arguments, by Drivine's function as a mode rounds: SQLite's `ROUND` keeps no
        more than 15 places of a fraction, so 1.0E-100 to 120 places would be 0."""
        return self.round_to(number, precision, "HALF_UP")

    def round_whole(self, number: str) -> str:
        """Half toward +infinity, by Drivine's function as a mode rounds: half up above 0, half down below."""
        return self.shared1(
            number,
            lambda n: (
                f"CASE WHEN {n} < 0 THEN {self.round_to(n, '0', 'HALF_DOWN')} "
                f"ELSE {self.round_to(n, '0', 'HALF_UP')} END"
            ),
        )

    def right_text(self, text: str, count: str) -> str:
        return self.shared(
            [text, count],
            lambda v: (
                f"CASE WHEN {v[0]} IS NULL OR {v[1]} IS NULL THEN NULL WHEN {v[1]} >= length({v[0]}) THEN {v[0]} "
                f"WHEN {v[1]} > 0 THEN substr({v[0]}, -{v[1]}) ELSE '' END"
            ),
        )

    @property
    def temporal_sql(self) -> TemporalSql:
        """One for the compiler, so a statement reads one clock: see SqliteTemporalSql.now."""
        if self._temporal_sql is None:
            self._temporal_sql = SqliteTemporalSql(self.shared)
        return self._temporal_sql

    _temporal_sql: TemporalSql | None = None

    def matches(self, text: str, pattern: str) -> str:
        """
        The whole of the text matches, as `=~` asks: `REGEXP` finds a match anywhere. It ends at `\\z`, the text's end,
        where Java's `$` would match before a line break that ends the text too.
        """
        return f"({text} REGEXP ('^(?:' || {pattern} || ')\\z'))"

    def split(self, text: str, separator: str) -> str:
        return f"{sqlite_graph.SPLIT}({text}, {separator})"

    def reverse_text(self, text: str) -> str:
        return f"{sqlite_graph.REVERSE}({text})"

    def procedure(self, program: Sequence[ProcedureStep], parameters: dict[str, Any]) -> CompiledCypher:
        """
        The procedure's steps carried out one by one by the function Drivine registers on each connection, under a
        savepoint of its own, so the statement is applied whole or not at all (see `drivine_cypher`): SQLite has no
        procedural language. The steps are given as JSON, each with its SQL, in which each variable of the procedure it
        reads is a numbered parameter, `?1`, and the variables bound to them in turn. The rows it returns are JSON's,
        in a list.
        """
        program_json = json.dumps({"steps": [_step(it) for it in program]}, separators=(",", ":"), ensure_ascii=False)
        c = "cy"
        return CompiledCypher(
            f"SELECT {self.item(c)} AS result FROM json_each({sqlite_graph.CYPHER}({literal(program_json)}, "
            f"${OBJECT})) AS {c} ORDER BY {c}.key",
            parameters,
        )

    def id_of(self, json: str, key: str | None) -> str:
        return f"CAST({json} ->> {_path(key) if key is not None else chr(39) + '$' + chr(39)} AS INTEGER)"

    def appended(self, list_: str, item: str) -> str:
        return f"json_insert({list_}, '$[#]', json({item}))"

    def strip_nulls(self, json: str) -> str:
        """A merge patch onto an empty map leaves out each entry whose value is null."""
        return f"json_patch('{{}}', {json})"

    def merged(self, props: str, map_: str) -> str:
        return f"json_patch({props}, {map_})"

    def without_label(self, labels: str, label: str) -> str:
        return (
            f"(SELECT json_group_array(l.value ORDER BY l.key) FROM json_each({labels}) l "
            f"WHERE l.value <> {literal(label)})"
        )


def _path(key: str) -> str:
    """The path of the key of a map, as SQLite's JSON functions read one: a key of any characters, in quotes."""
    return literal(f"$.{quoted(key)}")


def _java_json(value: Any) -> str:
    """A value of JSON's own kinds as compact JSON text, each float in it written as Java writes a double, as
    drivine4j's JSON writes it."""
    if isinstance(value, float):
        return java_text(value) if math.isfinite(value) else json.dumps(java_text(value))
    if isinstance(value, dict):
        entries = cast(dict[str, Any], value)
        return (
            "{"
            + ",".join(f"{json.dumps(key, ensure_ascii=False)}:{_java_json(it)}" for key, it in entries.items())
            + "}"
        )
    if isinstance(value, list):
        return "[" + ",".join(_java_json(it) for it in cast(list[Any], value)) + "]"
    return json.dumps(value, ensure_ascii=False)


def _parameter_json(value: Any) -> str:
    """A value of JSON's own kinds as compact JSON text, as a parameter is bound: see `parameter_text`."""
    if isinstance(value, float) and not math.isfinite(value):
        return json.dumps(java_text(value))
    if isinstance(value, dict):
        entries = cast(dict[str, Any], value)
        return (
            "{"
            + ",".join(f"{json.dumps(key, ensure_ascii=False)}:{_parameter_json(it)}" for key, it in entries.items())
            + "}"
        )
    if isinstance(value, list):
        return "[" + ",".join(_parameter_json(it) for it in cast(list[Any], value)) + "]"
    return json.dumps(value, ensure_ascii=False)


def _step(step: ProcedureStep) -> dict[str, Any]:
    """The step as `drivine_cypher` reads it."""
    if isinstance(step, steps.Select):
        return {**_step_sql("select", f"SELECT {step.expression}{step.from_}"), "variable": step.variable}
    if isinstance(step, steps.Let):
        return {**_step_sql("select", f"SELECT {step.expression}"), "variable": step.variable}
    if isinstance(step, steps.Loop):
        return {
            **_step_sql("loop", step.query),
            "record": f"{step.record}.item",
            "body": [_step(it) for it in step.body],
        }
    if isinstance(step, steps.If):
        return {
            **_step_sql("if", f"SELECT CASE WHEN {step.condition} THEN 1 ELSE 0 END"),
            "body": [_step(it) for it in step.body],
        }
    if isinstance(step, steps.Raise):
        return {"op": "raise", "message": step.message}
    if isinstance(step, steps.Execute):
        return {**_step_sql("execute", step.statement), "variable": step.into}
    return _step_sql("return", step.query)


def _is_word(c: str) -> bool:
    """Whether the character is one of a name: a letter, a digit or `_`."""
    return c.isalnum() or c == "_"


def _step_sql(op: str, sql: str) -> dict[str, Any]:
    """
    The step `op` of the SQL, each variable it reads a numbered parameter, and the variables, in the parameters'
    order. A literal or a quoted name is read past whole: a text may hold what reads as a variable.
    """
    variables: list[str] = []
    written: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c in "'\"":
            end = i + 1
            while end < n:
                if sql[end] != c:
                    end += 1
                elif end + 1 < n and sql[end + 1] == c:
                    end += 2
                else:
                    break
            written.append(sql[i : min(end + 1, n)])
            i = end + 1
        elif (c.isalpha() or c == "_") and (i == 0 or not (_is_word(sql[i - 1]) or sql[i - 1] == ".")):
            end = i
            while end < n and _is_word(sql[end]):
                end += 1
            name = sql[i:end]
            if name in _RECORDS and sql.startswith(".item", end) and (end + 5 >= n or not _is_word(sql[end + 5])):
                name += ".item"
                end += 5
            if name in _VARIABLES or (name.endswith(".item") and name[:-5] in _RECORDS):
                if name not in variables:
                    variables.append(name)
                written.append(f"?{variables.index(name) + 1}")
            else:
                written.append(name)
            i = end
        else:
            written.append(c)
            i += 1
    return {"op": op, "sql": "".join(written), "variables": variables}
