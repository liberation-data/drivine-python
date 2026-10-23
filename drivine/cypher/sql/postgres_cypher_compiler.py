from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from typing import Any

from drivine.cypher.ast.relationship_pattern import Direction
from drivine.cypher.sql.compiled_cypher import CompiledCypher
from drivine.cypher.sql.parameters import OBJECT, cypher_json
from drivine.cypher.sql.postgres_temporal_sql import PostgresTemporalSql
from drivine.cypher.sql.sql_cypher_compiler import (
    COUNT,
    DOOMED,
    FOUND,
    GATHERED,
    ID,
    MATCHED,
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
    indent,
    literal,
)
from drivine.cypher.sql.temporal_sql import TemporalSql
from drivine.cypher.sql.value import Json, Node
from drivine.model import stamps

_NUMERIC_TEXT = r"^\s*[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?\s*$"
"""A text Postgres reads as a `numeric`, spaces around it allowed."""

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
        return f"CAST({literal(cypher_json(value))} AS jsonb)"

    def parameter_json(self, marker: str) -> str:
        return f"CAST({marker} AS jsonb)"

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
        """A `numeric` with a fraction, so a whole one is written `2.0`, as a graph engine writes a float, and stays one
        in arithmetic."""
        return f"(CAST({sql} AS numeric) + 0.0)"

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

    def collect(self, value: str, distinct: bool, order: str | None) -> str:
        """Distinct values are in no order: Postgres orders a `DISTINCT` aggregate by its argument alone."""
        gathered = f"DISTINCT {value}" if distinct else f"{value}{f' ORDER BY {order}' if order is not None else ''}"
        return f"COALESCE(json_agg({gathered}) FILTER (WHERE {value} IS NOT NULL), '[]'::json)"

    def extreme(self, value: str, min_: bool) -> str:
        # Postgres has no min or max of jsonb, which orders: the first of the values in order is the least.
        direction = "ASC" if min_ else "DESC"
        return (
            f"(array_agg({value} ORDER BY {self.kind_order(value)} {direction}, {value} {direction}) "
            f"FILTER (WHERE COALESCE(jsonb_typeof({value}), 'null') <> 'null'))[1]"
        )

    def add(self, a: str, b: str) -> str:
        return (
            f"CASE WHEN jsonb_typeof({a}) = 'number' AND jsonb_typeof({b}) = 'number' THEN "
            f"to_jsonb({self.number(a)} + {self.number(b)}) "
            f"WHEN jsonb_typeof({a}) = 'array' AND jsonb_typeof({b}) = 'array' THEN {a} || {b} "
            f"WHEN jsonb_typeof({a}) = 'array' THEN {a} || jsonb_build_array({b}) "
            f"WHEN jsonb_typeof({b}) = 'array' THEN jsonb_build_array({a}) || {b} "
            f"WHEN jsonb_typeof({a}) IN ('string', 'number') AND jsonb_typeof({b}) IN ('string', 'number') THEN "
            f"to_jsonb(({a} #>> '{{}}') || ({b} #>> '{{}}')) END"
        )

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
            "tostring": lambda: f"to_jsonb({json} #>> '{{}}')",
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
        e = walk.edge
        if walk.direction is Direction.OUTGOING:
            join, far = f"{e}.src = {w}.nid", f"{e}.dst"
        elif walk.direction is Direction.INCOMING:
            join, far = f"{e}.dst = {w}.nid", f"{e}.src"
        else:
            # Either way, and a relationship from a node to itself once.
            join, far = (
                f"({e}.src = {w}.nid OR {e}.dst = {w}.nid)",
                f"CASE WHEN {e}.src = {w}.nid THEN {e}.dst ELSE {e}.src END",
            )
        conditions = [
            f"{e}.eid <> ALL ({w}.eids)",
            *([f"{w}.depth < {walk.max}"] if walk.max is not None else []),
            *walk.conditions,
        ]
        # Walked backwards, each node it leaves goes first, so its nodes end with the one it started from.
        if walk.backwards:
            step = f"    SELECT {far}, {w}.depth + 1, {w}.nid || {w}.nids, {e}.eid || {w}.eids"
        else:
            step = f"    SELECT {far}, {w}.depth + 1, {w}.nids || {far}, {w}.eids || {e}.eid"
        text = "\n".join(
            [
                f"WITH RECURSIVE {w} (nid, depth, nids, eids) AS (",
                f"    SELECT {walk.from_}, 0, {self.no_ids}, {self.no_ids}",
                "    UNION ALL",
                step,
                f"    FROM {w} JOIN {self.edges} {e} ON {join}",
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
        matches no line break, as Java's does, unless the pattern says `(?s)`.
        """
        return f"({text} ~ regexp_replace({pattern}, '^(\\(\\?([a-z]+)\\))?(.*)$', '(?p\\2)^(\\3)$'))"

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
            declared += [f"    {SAVED}{depth} jsonb;", f"    {GATHERED}{depth} jsonb;"]
        text = "\n".join([*declared, "BEGIN", indent("\n".join(body)), "END"])
        digest = hashlib.sha256(text.encode()).hexdigest()
        function = f"pg_temp.drivine_cypher_{digest[:24]}"
        prelude = (
            f"CREATE OR REPLACE FUNCTION {function}({PARAMS} jsonb) RETURNS SETOF json LANGUAGE plpgsql "
            f"AS $cy$\n{text}\n$cy$"
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
