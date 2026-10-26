from __future__ import annotations

import dataclasses
import functools
import logging
import math
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from functools import cached_property
from typing import Any, NoReturn, TypeVar, cast

from drivine.backend.sql.sql_graph import sql_text as literal
from drivine.cypher.ast.clause import (
    Call,
    Clause,
    Create,
    Delete,
    Foreach,
    Match,
    Merge,
    Remove,
    Return,
    SetClause,
    Unwind,
    With,
)
from drivine.cypher.ast.expression import (
    AllProperties,
    Binary,
    BinaryOperator,
    Case,
    CountStar,
    Entry,
    Exists,
    Expression,
    Function,
    HasLabels,
    Index,
    ListComprehension,
    ListLiteral,
    Literal,
    MapLiteral,
    MapProjection,
    Parameter,
    PatternComprehension,
    PatternPredicate,
    Property,
    Quantifier,
    QuantifierKind,
    Reduce,
    Selected,
    Slice,
    Subquery,
    SubqueryKind,
    Unary,
    UnaryOperator,
    Variable,
    VariableElement,
)
from drivine.cypher.ast.node_pattern import NodePattern
from drivine.cypher.ast.pattern import Pattern, Shortest
from drivine.cypher.ast.projection import Projection, ProjectionItem
from drivine.cypher.ast.query import Query
from drivine.cypher.ast.relationship_pattern import Direction, RelationshipPattern
from drivine.cypher.ast.remove_item import RemoveItem, RemoveProperty
from drivine.cypher.ast.set_item import SetAdd, SetItem, SetProperty, SetReplace
from drivine.cypher.cypher_reader import read_cypher
from drivine.cypher.sql.compiled_cypher import CompiledCypher
from drivine.cypher.sql.parameters import Parameters, cypher_json, jsonable
from drivine.cypher.sql.temporal import Temporal
from drivine.cypher.sql.temporal_sql import TemporalSql
from drivine.cypher.sql.temporals import FIELD_NAMES, NoNumber, Temporals, canonical
from drivine.cypher.sql.value import Bool, Entities, Json, Node, Path, Relationship, Value
from drivine.drivine_error import DrivineError
from drivine.model import stamps

logger = logging.getLogger("drivine.cypher")

T = TypeVar("T")

Scope = Mapping[str, Value]
"""The variables an expression sees, and what each is."""

Body = Callable[[list[str]], str]
"""SQL written over values, each as `shared` names it."""

# ----- The variables of a procedure -----

PARAMS = "cy_params"
"""The parameters, a JSON map."""

ROWS = "cy_rows"
"""The rows, a JSON list of maps, each variable a row holds at its key."""

OUT = "cy_out"
"""The rows a write leaves, as it makes them."""

ROW = "cy_row"
"""The row a write is making, a JSON map."""

VALUE = "cy_value"
"""A JSON value a write has worked out."""

ID = "cy_id"
"""The id of the node or the relationship a write has made."""

COUNT = "cy_count"
"""A count."""

FOUND = "cy_found"
"""Whether a `MERGE` found its pattern."""

SAVED = "cy_saved"
"""
The rows a `FOREACH`, or a `CALL { … }` that writes, is written over: what it puts back once its clauses are written,
where it returns nothing. Followed by its depth, from 1 to NESTING.
"""

GATHERED = "cy_gathered"
"""
The rows a `CALL { … }` that writes and returns gives, or the rows and items a `FOREACH` writes for, each one alone,
followed by its depth, from 1 to NESTING.
"""

CALLER = "cy_caller"
"""
The row a `CALL { … }` that writes and returns is written for, followed by its depth, from 1 to NESTING: each row it
gives is that row with what it returns, whatever the call bound to the row's names inside it.
"""

DOOMED = "cy_doomed"
"""The nodes a `DELETE` deletes once its rows are done, a JSON list of ids."""

RECORD = "cy_record"
"""The record of a write's loop over the rows: its `item` is a row."""

MATCHED = "cy_matched"
"""The record of a `MERGE`'s loop over what it found: its `item` is a row."""

NESTING = 3
"""How deep `FOREACH`es and writing `CALL`s nest."""

PENDING = "__with__"
"""Stands for the name of the query a `WITH` is read from, before it is named."""

SHORTEST_FROM = " shortest from"
SHORTEST_TO = " shortest to"
"""The names of a shortest path's ends where its pattern gives none, which no statement can write unquoted."""

SHARED_ABOVE = 200
"""How long a value `shared` binds may be written where it is read, in characters."""

_TEMPORAL_KINDS = ("date", "localtime", "time", "localdatetime", "datetime")

TEMPORAL_NOW = frozenset(f"{kind}.{it}" for kind in _TEMPORAL_KINDS for it in ("statement", "transaction", "realtime"))
"""The functions of a temporal kind that give the moment the statement runs."""

TEMPORAL_TRUNCATE = frozenset(f"{it}.truncate" for it in _TEMPORAL_KINDS)
"""The functions of a temporal kind that truncate a value to a unit."""

INSTANT_MAP_FIELDS = frozenset(
    {"year", "month", "day", "hour", "minute", "second", "millisecond", "microsecond", "nanosecond", "timezone"}
)
"""The fields a map makes a moment or a time of day of."""

DURATION_MAP_FIELDS = frozenset(
    {
        "years", "quarters", "months", "weeks", "days", "hours", "minutes", "seconds", "milliseconds", "microseconds",
        "nanoseconds",
    }
)  # fmt: skip
"""The fields a map makes a duration of."""

AGGREGATES = frozenset({"count", "collect", "sum", "avg", "min", "max"})

DISTINCT_ITEMS = "#distinct items"
"""The function that keeps each item of a list once, where it is first: see `_aggregates_first`. No statement can name
it."""

PLAIN_FLOAT = "FM9999990.0999999999999999999999"
"""
The format a float from 10^-3 up to 10^7 is written in by `TO_CHAR`, as Java writes a double: no exponent, a digit
before the point and one after it at least, and as many more as its shortest decimal has.
"""

MATH_FUNCTIONS = frozenset(
    {"exp", "log", "log10", "sqrt", "sin", "cos", "tan", "cot", "asin", "acos", "atan"}
    | {"degrees", "radians", "haversin"}
)
"""openCypher's functions of one number that `math` works out, as Neo4j names them, lowered."""

TEXT_FUNCTIONS = frozenset({"tostring", "toupper", "tolower", "trim"})
"""The functions of one value a store writes (`function`) whose result is a text, or null: see Json.text."""


class MadeText:
    """A side of a `+` the statement makes a text, or null, though it does not give it: see Json.text."""


MADE_TEXT = MadeText()
"""The one MadeText: what `add` is given for such a side."""

ROUNDING_MODES = ("CEILING", "FLOOR", "UP", "DOWN", "HALF_EVEN", "HALF_UP", "HALF_DOWN", "UNNECESSARY")
"""The modes `round()` takes, as Java's `RoundingMode` names them."""

NEGATIVE_PRECISION = "round() takes a precision of 0 or more, as in Neo4j."
"""What `round()` is refused with, as Neo4j refuses it, a precision below 0."""

RADIANS_TO_DEGREES = "57.29577951308232"
DEGREES_TO_RADIANS = "0.017453292519943295"
"""
The doubles Java's `Math.toDegrees` and `Math.toRadians` multiply by, as Neo4j's `degrees` and `radians` do: a store
that works out 180 / π otherwise, or divides, can differ in the last digit, as at `degrees(0.1)`.
"""

_COMPARED = {
    BinaryOperator.EQUALS: "=",
    BinaryOperator.NOT_EQUALS: "<>",
    BinaryOperator.LESS_THAN: "<",
    BinaryOperator.GREATER_THAN: ">",
    BinaryOperator.LESS_THAN_OR_EQUAL: "<=",
    BinaryOperator.GREATER_THAN_OR_EQUAL: ">=",
}


@dataclass(frozen=True)
class Hop:
    """A relationship a walk's step takes, the row `edge`, in `direction` from the node the step is at."""

    edge: str
    direction: Direction


@dataclass(frozen=True)
class Walk:
    """
    A walk from the node whose id is `from_` to the one whose is `to`, as rows named `name`, to a depth from `min` to
    `max` (any, if None). Each step takes the `hops` in turn, from the node the step is at, and meets `conditions`: one
    hop for a variable-length relationship, a hop for each relationship of a quantified path pattern's. A node the
    conditions read is the row `nodes` names, at its place among the hops' nodes: the node the step is at first, then
    the far node of each hop. With `backwards` a walk of one hop is walked from the pattern's later node. `seed`, where
    there is one, is the condition the node walked from meets of its own, over a node row.
    """

    from_: str
    to: str
    name: str
    step: str
    hops: tuple[Hop, ...]
    nodes: tuple[str | None, ...]
    min: int
    max: int | None
    conditions: tuple[str, ...]
    backwards: bool
    seed: Callable[[str], str] | None

    def __post_init__(self) -> None:
        if len(self.nodes) != len(self.hops) + 1:
            raise ValueError("A walk's step has one node more than it has hops.")
        if self.backwards and len(self.hops) != 1:
            raise ValueError("A walk of more than one hop a step is walked forwards.")


@dataclass(frozen=True)
class Walked:
    """
    What a walk is: its rows' `source`, the `condition` that joins them to the pattern, the list of the ids of the
    relationships each row took, `eids`, and the list of the ids of the nodes it reached after the pattern's earlier
    node, `nids`, both in the pattern's order.
    """

    source: str
    condition: str
    eids: str
    nids: str


class _Kind(Enum):
    """What a variable a row holds is."""

    NODE = "NODE"
    RELATIONSHIP = "RELATIONSHIP"
    VALUE = "VALUE"
    NODES = "NODES"
    RELATIONSHIPS = "RELATIONSHIPS"
    PATH = "PATH"


class _Select:
    """The rows a part of a statement reads: its sources and joins, its conditions, and the variables it binds."""

    def __init__(self, outer: Scope) -> None:
        self._outer = outer
        self.sources: list[str] = []
        self.where: list[str] = []
        self.bound: dict[str, Value] = {}
        self.order: list[tuple[str, bool]] = []
        """The order its rows are read in, where it has one: each term, and whether it descends."""
        self.walks = False
        """Whether a variable-length walk is among its sources, or among those of a `WITH` it reads: a term it sorts by
        is then bound in no subquery (see `_project`)."""

    @property
    def scope(self) -> dict[str, Value]:
        """What an expression of this part sees: the variables bound around it, then its own."""
        return {**self._outer, **self.bound}

    def bind(self, variable: str, value: Value) -> None:
        self.bound[variable] = value

    def source(self, text: str) -> None:
        self.sources.append(f"FROM {text}" if not self.sources else f"CROSS JOIN {text}")

    def join(self, text: str) -> None:
        if not self.sources:
            self.sources.append("FROM (SELECT 1) one")
        self.sources.append(text)

    def from_(self) -> list[str]:
        return self.sources

    def where_clause(self) -> str | None:
        return "WHERE " + "\n  AND ".join(self.where) if self.where else None

    def size(self) -> int:
        """How much this part reads: it reads more when a clause adds to it."""
        return len(self.sources) + len(self.where) + len(self.bound)


@dataclass(frozen=True)
class _Hop:
    """
    The relationships one part of a pattern matched: one, its id `eids`, or with `many` those a variable-length
    relationship or a quantified path pattern walked, a list of ids. Of the types it may have; `nodes` is the list of
    the ids of the nodes it reaches after the one it starts from.
    """

    eids: str
    many: bool
    types: tuple[str, ...]
    nodes: str


@dataclass(frozen=True)
class _Projected:
    """
    What a `WITH` or a `RETURN` makes of the rows it reads: the SQL of its query, what each item's name is bound to
    among its columns, and the order of its rows, each term a column of the query and whether it descends.
    """

    sql: str
    scope: dict[str, Value]
    order: list[tuple[str, bool]]


class SqlCypherCompiler(ABC):
    """
    Compiles a Cypher statement to SQL over a graph's tables, `<graph>_node` and `<graph>_edge`, so a statement written
    for a graph engine runs on a SQL store and gives the same rows. What is the same on each store is here; a store's
    own SQL, for JSON, lists, walks and paging, is its subclass's. drivine4j's SqlCypherCompiler, ported.

    - A `MATCH` is a join of node and relationship rows, its patterns' labels, types, directions and properties its
      conditions. Two relationships of one `MATCH` are two relationships, as in Cypher.
    - A variable-length relationship, `*min..max`, is a recursive walk from the end of it that is pinned down, which
      takes no relationship twice. A variable on one is the list of its relationships.
    - A path, `p = (…)`, is the ids of its nodes and relationships, read by `nodes`, `relationships` and `length`. It is
      not returned whole: each graph engine's driver gives a path as a value of its own.
    - An `OPTIONAL MATCH` is a lateral join that keeps the row with nulls where nothing is found.
    - A `WITH` is a query of its own, which the rest of the statement reads from.
    - A projection that aggregates is grouped by what it holds outside its aggregates, as Cypher groups it.
    - A value is JSON, so a comparison is JSON's and `'7'` is not `7`, and a missing property is null and meets no
      comparison. A map the statement builds keeps its keys in the order written, as a graph engine gives them.
    - The statement returns one value a row, as Drivine's statements are written to: the one item a `RETURN` has, or a
      list of its items when it has more, as Drivine gives a row of several columns. A node is its properties, as
      Drivine gives a node.

    A statement that writes (`CREATE`, `MERGE`, `SET`, `REMOVE`, `DELETE`) is a procedure, the store's own, which runs
    as one statement and so is applied whole or not at all. Its rows are carried from clause to clause as a JSON list, a
    node or a relationship in a row as its id, and each write is made for one row after another, as Cypher makes it: a
    `MERGE` finds what an earlier row made. A read after a write sees what it wrote. Its parameters are bound as one
    object (see parameters.OBJECT).

    A compiler compiles one statement: make one for each.
    """

    aggregates_case_subqueries = True
    """Whether the store aggregates a `CASE` whose result holds a subquery: see `_cases_first`."""

    extremes_by_list_key = True
    """
    Whether `min` and `max` of a list the statement makes order it by `list_sort_key`, item by item, as an `ORDER BY`
    sorts it: where the store's own JSON order puts the shorter list first.
    """

    def __init__(self, graph: str) -> None:
        self.nodes = f"{graph}_node"
        self.edges = f"{graph}_edge"
        self.procedural = False
        """Whether the statement is a procedure, which declares no function of its own a query of it may call."""
        self._functions: list[str] = []
        """The functions a query of the statement calls, which it declares before it: see `declare`."""
        self._shares = 0
        """How many values `shared` has bound, to name each subquery it binds one in."""
        self._identities = True
        """
        Whether a value's identity is carried out of a query as a column of its own (see Json.identity): not where the
        queries of a `UNION` in `CALL { … }` would give different columns, which compare by their properties instead.
        """
        self._sharing = True
        """Whether `shared` binds a long value: not in an expression that aggregates, whose aggregate a subquery would
        take from its group, nor in a term a projection beside a variable-length walk sorts by that is none of its items
        (see `_project`)."""
        self._begin(Parameters({}, self.parameter_json, self.parameter_count, text=self.parameter_text))

    def declare(self, function: str) -> None:
        """Declares the function, a function's whole text, for the statement to call: a query's own, before it."""
        self._functions.append(function)

    def _with_functions(self, compiled: CompiledCypher) -> CompiledCypher:
        """The compiled statement with the functions it calls declared before it."""
        if not self._functions:
            return compiled
        return dataclasses.replace(compiled, sql="WITH\n" + "\n".join(self._functions) + "\n" + compiled.sql)

    def compile(self, cypher: str, parameters: Mapping[str, Any] | None = None) -> CompiledCypher:
        """The statement, with its parameters' values, as SQL."""
        return self.compile_query(read_cypher(cypher), parameters or {})

    def compile_query(self, query: Query, parameters: Mapping[str, Any]) -> CompiledCypher:
        """The statement, read already, with its parameters' values, as SQL: a query, or a procedure where it
        writes."""
        if query.unions:
            return self._with_functions(self._union(query, parameters))
        if not any(writes(it) for it in query.clauses):
            self._begin(
                Parameters(
                    parameters,
                    self.parameter_json,
                    self.parameter_count,
                    text=self.parameter_text,
                    as_path=self.parameter_path,
                )
            )
            return self._with_functions(CompiledCypher(self._query(query), self._parameters.values))
        self.procedural = True
        # A procedure takes its parameters as one object, and reads each from it.
        self._begin(
            Parameters(
                parameters,
                lambda name, _: self.null_if_json_null(self.property_of(PARAMS, name)),
                lambda it: self.number(self.property_of(PARAMS, it)),
                in_object=True,
                text=self.parameter_text,
                as_path=self._in_params(self.parameter_path),
            )
        )
        # The parameters' text is written before the procedure's body is: on Oracle, writing it is what says whether the
        # body reads it as extended JSON.
        values = self._parameters.values
        body = self._procedure(query)
        return self.procedure(body, values)

    def shared(self, values: Sequence[str], body: Body) -> str:
        """
        The body over the values, each read by name where one is long: bound once in a subquery, so a body that reads a
        value more than once (to test its kind, then to read it) does not write it out again, and operations nested in
        one another do not grow the statement exponentially. Short values are written where they are read. Oracle
        groups by no subquery, so a projection that would group by a value so bound works it out first.
        """
        if not self._sharing or all(len(it) <= SHARED_ABOVE for it in values):
            return body(list(values))
        return self.bound(values, body)

    def shared1(self, value: str, body: Callable[[str], str]) -> str:
        """`shared`, of one value."""
        return self.shared([value], lambda it: body(it[0]))

    def bound(self, values: Sequence[str], body: Body) -> str:
        """The body over the values, each bound in a subquery and read by name whatever its length: see `shared`."""
        self._shares += 1
        alias = f"sh{self._shares}"
        columns = ", ".join(f"{value} AS v{i + 1}" for i, value in enumerate(values))
        read = body([f"{alias}.v{i + 1}" for i in range(len(values))])
        return f"(SELECT {read} FROM ({self.bound_select(columns)}) {alias})"

    def bound_select(self, columns: str) -> str:
        """
        The subquery `bound` binds its values in, of the columns: kept apart from the query that reads it, where a store
        would merge the two, so the planner does not write each value out again where it is read.
        """
        return f"SELECT {columns}"

    def returned(self, json: str) -> str:
        """The JSON a statement returns, as the store's driver reads it back."""
        return json

    def called(self, query: str) -> str:
        """
        The query of a `CALL { … }`, as the row that reads what it returns joins it: kept apart from that row's query,
        where a store would merge the two and read a value bound in the row wrongly.
        """
        return query

    def once(self, query: str) -> str:
        """
        The query that makes a value anew for each row, `randomUUID()`, kept apart from the query that reads it, where
        a store would merge the two and make the value again for each read: a `WITH`'s, and a projection's sorted by
        it.
        """
        return self.called(query)

    def _union(self, query: Query, parameters: Mapping[str, Any]) -> CompiledCypher:
        """
        A `UNION`: each query's rows, in the order of the queries and each query's own order, as a graph engine gives
        them; without `ALL`, each row once, where it is first given.
        """
        parts = [query.clauses, *(it.clauses for it in query.unions)]
        if any(writes(clause) for part in parts for clause in part):
            unsupported("A write in a UNION")
        if len({it.all for it in query.unions}) > 1:
            raise DrivineError("UNION and UNION ALL are not mixed in one statement, as in Cypher.")
        names = [[item.name for item in _returned(part).projection.items] for part in parts]
        if any(it != names[0] for it in names):
            listed = " and ".join("(" + ", ".join(it) + ")" for it in names)
            raise DrivineError(f"Each query of a UNION returns the same names, as in Cypher: {listed}.")
        self._begin(
            Parameters(
                parameters,
                self.parameter_json,
                self.parameter_count,
                text=self.parameter_text,
                as_path=self.parameter_path,
            )
        )
        rows = "\nUNION ALL\n".join(
            f"SELECT {i + 1} AS part, {self.row_number} AS n, q.result "
            f"FROM (\n{indent(self._query(Query(clauses)))}\n) q"
            for i, clauses in enumerate(parts)
        )
        if query.unions[0].all:
            sql = f"SELECT u.result FROM (\n{indent(rows)}\n) u\nORDER BY u.part, u.n"
        else:
            # A row is given once, where it is first given: its rows are compared as JSON compares them.
            key = self.comparable(Json("u.result", ordered=True))
            grouped = (
                f"SELECT {key} AS result, MIN(u.part * 1000000000.0 + u.n) AS k "
                f"FROM (\n{indent(rows)}\n) u\nGROUP BY {key}"
            )
            sql = f"SELECT d.result FROM (\n{indent(grouped)}\n) d\nORDER BY d.k"
        return CompiledCypher(sql, self._parameters.values)

    # ----- A store's SQL -----

    @abstractmethod
    def literal_json(self, value: Any) -> str:
        """A value the statement writes, as JSON."""

    @abstractmethod
    def parameter_json(self, marker: str, text: str | None) -> str:
        """The parameter bound as the marker, the JSON text `text`, as JSON."""

    def parameter_text(self, value: Any) -> str:
        """The JSON text a parameter's value is bound as."""
        return cypher_json(value)

    parameter_path: Callable[[str, str | None, Sequence[str], Any], str] | None = None
    """
    Where a store reads a value in a parameter that is a map, `$p.text`, straight from the text bound as a marker: given
    the marker, the JSON text bound at it (in a procedure, PARAMS and None, the parameter's name first among the keys),
    the keys, and the value at them, of JSON's own kinds, the value as JSON. None where the store reads the parameter as
    JSON, then each key of it.
    """

    @staticmethod
    def _in_params(
        read: Callable[[str, str | None, Sequence[str], Any], str] | None,
    ) -> Callable[[str, str | None, Sequence[str], Any], str] | None:
        """`parameter_path` in a procedure, which reads each parameter from PARAMS: the parameter's name is its first
        key."""
        if read is None:
            return None
        return lambda name, _, keys, value: read(PARAMS, None, [name, *keys], value)

    @abstractmethod
    def parameter_count(self, marker: str) -> str:
        """The parameter bound as the marker, a whole number, as one."""

    @abstractmethod
    def comparable(self, value: Json) -> str:
        """A JSON value as what compares, groups and orders as JSON."""

    @abstractmethod
    def scalar(self, sql: str) -> str:
        """A SQL value, a number, a text or a truth value, as JSON."""

    @abstractmethod
    def type_of(self, json: str) -> str:
        """The kind of the JSON: `number`, `string`, `boolean`, `array`, `object` or `null`, as text."""

    @abstractmethod
    def is_json_null(self, json: str) -> str:
        """Whether the JSON is null, or JSON's null: no value, as on a graph engine."""

    @abstractmethod
    def null_if_json_null(self, json: str) -> str:
        """The JSON, or null where it is JSON's null."""

    @abstractmethod
    def number(self, json: str) -> str:
        """The JSON as a number, where it is one."""

    @abstractmethod
    def integral(self, json: str) -> str:
        """Whether the JSON is a whole number."""

    @abstractmethod
    def text(self, json: str) -> str:
        """The JSON as text, where it is a string."""

    @abstractmethod
    def bool_of(self, json: str) -> str:
        """The JSON as a condition, where it is `true` or `false`."""

    @abstractmethod
    def float_(self, sql: str) -> str:
        """The SQL number as a floating-point number."""

    @abstractmethod
    def json_array(self, items: Sequence[str]) -> str:
        """A list of the items, JSON each, nulls kept."""

    @abstractmethod
    def json_object(self, entries: Sequence[tuple[str, str]]) -> str:
        """A map of the entries, JSON each, its keys in the order given."""

    @abstractmethod
    def with_entries(self, json: str, entries: Sequence[tuple[str, str]]) -> str:
        """The map with the entries set over it."""

    @abstractmethod
    def property_of(self, json: str, key: str) -> str:
        """The value of the key in the JSON map; null where there is none."""

    @abstractmethod
    def element(self, json: str, index: str) -> str:
        """The item of the JSON list at the JSON index, counted from the end where it is negative."""

    @abstractmethod
    def keyed(self, json: str, key: str) -> str | None:
        """
        The value of the JSON map at the key the JSON `key` names; None where the store reads no key an expression
        names, where such a key reads as null.
        """

    @abstractmethod
    def contains_all(self, props: str, map_: str) -> str:
        """Whether the JSON map `props` holds every entry of the JSON map `map_`."""

    @abstractmethod
    def has_label(self, labels: str, label: str) -> str:
        """Whether the labels, a JSON list, hold the label."""

    def has_labels(self, labels: str, all_: Sequence[str]) -> str:
        """Whether the labels, a JSON list, hold each of `all_`."""
        return " AND ".join(self.has_label(labels, it) for it in all_)

    def indexed_equality(self, property: str, value: str) -> str | None:
        """
        A test an index answers, to stand beside the comparison of the property, read as `property_of` gives it, with
        the value, a text or a truth value; None where there is none.
        """
        return None

    @abstractmethod
    def elements(self, json: str, alias: str) -> str:
        """The items of the JSON list as rows named `alias`, with columns `item` and `item_no`, counted from 1."""

    @abstractmethod
    def aggregate_list(self, value: str, order: str | None = None) -> str:
        """The values of a group, or of a query's rows ordered by `order`, as a JSON list: empty where there are
        none."""

    @abstractmethod
    def collect(self, value: str, order: str | None) -> str:
        """
        `collect`: the values of a group that are not null, as a JSON list, in the order of the rows where they have
        one.
        """

    @abstractmethod
    def distinct_items(self, list_: str) -> str:
        """
        The items of the JSON list once each, where each is first: what `collect(DISTINCT …)` keeps of what a `WITH`
        before it collected, for a store dedupes no aggregate in the query that makes it.
        """

    @abstractmethod
    def first_of_each(self, list_: str, identities: str) -> str:
        """
        The items of the JSON list whose item in `identities`, a list as long, is first among those equal to it, as
        `distinct_items` finds them: what `collect(DISTINCT …)` keeps of what holds a node or a relationship, by its
        identity. Worked out in one pass, where comparing each identity with those before it took seconds over a few
        thousand.
        """

    @abstractmethod
    def extreme(self, value: str, min_: bool, key: str | None = None) -> str:
        """
        `min` or `max` of the JSON values of a group: of values of several kinds, as `kind_order` orders them; of one
        kind, as JSON orders them, or by `key` where there is one, what orders them.
        """

    def kind_order(self, json: str) -> str:
        """
        Where the JSON's kind comes among the others, as Cypher orders values of several kinds: a map, a list, a text, a
        truth value, then a number. JSON orders them otherwise on each store.
        """
        return self.shared1(
            json,
            lambda x: (
                f"CASE {self.type_of(x)} WHEN 'object' THEN 1 WHEN 'array' THEN 2 WHEN 'string' THEN 3 "
                "WHEN 'boolean' THEN 4 WHEN 'number' THEN 5 END"
            ),
        )

    @abstractmethod
    def id_tokens(self, ids: str, key: str) -> str:
        """
        The JSON list of ids with each id the map `{key: id}`, a node's or a relationship's identity (see
        `_identity_of`), and null where the list is: by its text, so it reads no aggregate in a subquery, as Oracle
        reads none.
        """

    def item_rank(self, json: str) -> str:
        """Where the kind of the JSON, an item of a list, comes among the others as Cypher orders them: as
        `kind_order`, null last."""
        return f"COALESCE({self.kind_order(json)}, 6)"

    def identity_equals(self, x: str, y: str) -> str:
        """
        Whether the identities, neither of them null, are equal: see `_identity_of`. A list of a few thousand
        relationships' tokens is larger than Oracle's `=` compares, so Oracle compares them otherwise.
        """
        return f"{x} = {y}"

    def list_sort_key(self, json: str) -> str:
        """What an order sorts the JSON by where the statement makes it a list (Json.list): as any value, where the
        store orders lists as Cypher does."""
        return self.null_if_json_null(json)

    @abstractmethod
    def add(self, a: str, b: str, given: tuple[Any, Any] = (None, None)) -> str:
        """
        `a + b` of the JSON values: numbers added, lists joined, texts put together. `given` is what the statement gives
        each as, where it does, or MADE_TEXT where it makes it a text without giving it: see `summed_text`.
        """

    def summed_text(
        self, json: str, given: Any, other: Any, plain: Callable[[str], str], written: Callable[[str], str]
    ) -> str:
        """
        The text the JSON, a side of a `+` that may put a number and a text together, is written as there: by
        `written`, as `toString` writes it, a float as Java writes a double. Where the statement gives it a number,
        `given`, it is that number's text, worked out here; and where it gives the other side, `other`, a number, or
        this side a text, it is its own text, `plain`, for a side is put together with a number only where it is a
        text. So is a side the statement makes a text (MADE_TEXT), as `'x' + toString(i)`. So only a side the
        statement gives or makes neither way, which may be a float, is written as `toString` writes it, which is long:
        `c.name + 1` compiles as it did before floats were so written. NaN and the infinities count as given neither
        way: Postgres binds them as texts, so a number may be added to one as text.
        """
        if _is_given_number(given):
            return literal(java_text(given))
        if isinstance(given, str) or given is MADE_TEXT or _is_given_number(other):
            return plain(json)
        return written(json)

    def arithmetic(self, a: str, b: str, operator: str) -> str:
        """`a op b` of the JSON numbers, as JSON: the operator is `+`, `-`, `*` or `/`."""
        return self.scalar(f"({self.number(a)} {operator} {self.number(b)})")

    @abstractmethod
    def modulo(self, a: str, b: str) -> str:
        """The remainder of the SQL numbers."""

    @abstractmethod
    def starts_with(self, a: str, b: str) -> str:
        """Whether the text `a` starts with the text `b`."""

    @abstractmethod
    def ends_with(self, a: str, b: str) -> str:
        """Whether the text `a` ends with the text `b`."""

    @abstractmethod
    def matches(self, text: str, pattern: str) -> str:
        """Whether the whole of the text matches the regular expression, as `=~` matches it."""

    @abstractmethod
    def split(self, text: str, separator: str) -> str:
        """`split`: the SQL text cut at each separator, a SQL text, as a JSON list of texts."""

    @abstractmethod
    def reverse_text(self, text: str) -> str:
        """The SQL text, its characters backwards."""

    @abstractmethod
    def contains_text(self, a: str, b: str) -> str:
        """Whether the text `a` holds the text `b`."""

    @abstractmethod
    def function(self, name: str, json: str) -> str | None:
        """A function of one argument whose SQL is the store's own: `size`, `toLower`, `head`…; None when there is
        none."""

    @abstractmethod
    def page(self, skip: str | None, limit: str | None) -> list[str]:
        """The `OFFSET` and `LIMIT` of a query, each a SQL whole number or None."""

    @abstractmethod
    def ids(self, id_: str) -> str:
        """The list of ids holding only the id."""

    @abstractmethod
    def join_ids(self, lists: Sequence[str]) -> str:
        """The lists of ids, one after another."""

    @abstractmethod
    def store_ids(self, json: str) -> str:
        """The JSON list of ids as the store's list of ids, in its order: null where the JSON is."""

    @property
    @abstractmethod
    def no_ids(self) -> str:
        """The list of no ids."""

    @abstractmethod
    def id_in(self, id_: str, list_: str) -> str:
        """Whether the id is among the list of ids."""

    @abstractmethod
    def ids_overlap(self, a: str, b: str) -> str:
        """Whether two lists of ids share an id."""

    @abstractmethod
    def id_count(self, list_: str) -> str:
        """How many ids the list holds, as JSON."""

    @abstractmethod
    def id_rows(self, list_: str, alias: str) -> str:
        """The ids of the list as rows named `alias`, with columns `id` and `ord`, in its order."""

    walks_read_row = True
    """
    Whether a walk reads the row it is in: a condition of a quantified path pattern may then read a variable bound
    outside it.
    """

    @abstractmethod
    def walk(self, walk: Walk) -> Walked:
        """A walk of a variable-length relationship or a quantified path pattern: see Walk."""

    def leaves(self, edge: str, direction: Direction, at: str) -> tuple[str, str]:
        """
        The condition that the relationship row `edge` leaves the node whose id is `at` in `direction`, and the id of
        the node it reaches.
        """
        if direction is Direction.OUTGOING:
            return f"{edge}.src = {at}", f"{edge}.dst"
        if direction is Direction.INCOMING:
            return f"{edge}.dst = {at}", f"{edge}.src"
        # Either way, and a relationship from a node to itself once.
        return (
            f"({edge}.src = {at} OR {edge}.dst = {at})",
            f"CASE WHEN {edge}.src = {at} THEN {edge}.dst ELSE {edge}.src END",
        )

    def steps(self, walk: Walk, at: str) -> tuple[list[str], list[str], list[str]]:
        """
        The joins of a walk's step from the node whose id is `at`: each hop's relationship, and each node a condition
        reads. Gives them, the ids of the nodes the hops reach, in turn, and that no two relationships of the step are
        one.
        """
        joins: list[str] = []
        reached: list[str] = []
        first = walk.nodes[0]
        if first is not None:
            joins.append(f"JOIN {self.nodes} {first} ON {first}.nid = {at}")
        for i, hop in enumerate(walk.hops):
            join, far = self.leaves(hop.edge, hop.direction, reached[-1] if reached else at)
            joins.append(f"JOIN {self.edges} {hop.edge} ON {join}")
            reached.append(far)
            node = walk.nodes[i + 1]
            if node is not None:
                joins.append(f"JOIN {self.nodes} {node} ON {node}.nid = {far}")
        distinct = [f"{a.edge}.eid <> {b.edge}.eid" for i, a in enumerate(walk.hops) for b in walk.hops[i + 1 :]]
        return joins, reached, distinct

    @abstractmethod
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
        `reduce`: the JSON list folded from `initial`, the JSON each step gives of the value so far and the item, as
        `step` writes it, as JSON; null where the list is. `name` names what the fold reads from. Where the step is
        worked out where it cannot read the row, it is `context_step`, which reads what the step reads of the row, and
        the statement's parameters, from the JSON map `context` gives: asked for only there, for a value such as a path
        is not JSON.
        """

    @property
    @abstractmethod
    def row_number(self) -> str:
        """The number of a row of a query, counted from 1 in the order the query gives its rows."""

    @abstractmethod
    def uuid(self) -> str:
        """`randomUUID()`: a new UUID, as SQL text in its usual form."""

    @abstractmethod
    def math(self, name: str, numbers: Sequence[str]) -> str:
        """
        One of openCypher's functions of numbers, `name`, one of MATH_FUNCTIONS or `atan2`, of `numbers`, the store's
        SQL numbers, as a SQL float: null where one is null. Neo4j works these out as Java's doubles, and gives NaN or
        an infinity for some (`log(0)`, `sqrt(-1)`, `exp(1000)`, `cot(0)`), which JSON holds neither of: there, the
        store fails.
        """

    def round_to(self, number: str, precision: str, mode: str) -> str:
        """
        `round(value, precision, mode)`: the SQL number rounded to `precision` decimal places by `mode`, one of
        ROUNDING_MODES but UNNECESSARY, as a SQL number. Worked out in decimal, as Neo4j works it out with Java's
        `BigDecimal`, so 2.345 is halfway between 2.34 and 2.35: a store whose numbers are decimal (`numeric`, `NUMBER`)
        takes this, one whose numbers are binary its own.
        """

        def rounded(values: list[str]) -> str:
            n, p = values
            scale = f"POWER(CAST(10 AS NUMERIC), {p})"
            scaled = f"(ABS({n}) * {scale})"
            if mode == "CEILING":
                return f"(CEIL({n} * {scale}) / {scale})"
            if mode == "FLOOR":
                return f"(FLOOR({n} * {scale}) / {scale})"
            # Rounded as a magnitude, then given the number's sign: UP, DOWN and the HALF_ modes round toward or away
            # from 0.
            magnitude = {
                "UP": f"CEIL({scaled})",
                "DOWN": f"FLOOR({scaled})",
                "HALF_UP": f"FLOOR({scaled} + 0.5)",
                "HALF_DOWN": f"CEIL({scaled} - 0.5)",
                "HALF_EVEN": (
                    f"CASE WHEN {scaled} - FLOOR({scaled}) = 0.5 THEN FLOOR({scaled}) + MOD(FLOOR({scaled}), 2) "
                    f"ELSE FLOOR({scaled} + 0.5) END"
                ),
            }[mode]
            return f"(SIGN({n}) * {magnitude} / {scale})"

        return self.shared([number, precision], rounded)

    @abstractmethod
    def right_text(self, text: str, count: str) -> str:
        """`right`: the last characters of the SQL text, or all of it where it has fewer."""

    @abstractmethod
    def range(self, from_: str, to: str, step: str) -> str:
        """`range`: the whole numbers from `from_` to `to`, `step` apart, SQL numbers each, as a JSON list."""

    @property
    @abstractmethod
    def temporal_sql(self) -> TemporalSql:
        """The store's SQL for temporal values, which `temporals` works openCypher's out of."""

    @cached_property
    def temporals(self) -> Temporals:
        """openCypher's temporal values: see Temporals."""
        return Temporals(self.temporal_sql, self.shared)

    # ----- A store's procedure, for a statement that writes -----

    @abstractmethod
    def procedure(self, body: list[str], parameters: dict[str, Any]) -> CompiledCypher:
        """
        The statement that runs the procedure whose statements are `body`, each ending in `;`, and gives the rows it
        returns, of one column (see `return_rows`), with the parameters it binds. The procedure declares the variables
        this module names (PARAMS, ROWS …) and sets PARAMS from the object bound at parameters.OBJECT.
        """

    @abstractmethod
    def return_rows(self, query: str) -> list[str]:
        """The statements that return the rows of the query, whose one column is `result`."""

    @abstractmethod
    def raise_(self, message: str) -> str:
        """The statement that fails the procedure with the message."""

    @abstractmethod
    def failure(self, message: str, value: str) -> str:
        """
        A whole number that fails the statement when it is worked out, with the message and the SQL value it names:
        what a compiled statement does where Neo4j fails on a value it reads. It reads the value, so no store works it
        out before a row that needs it, as one may a failing expression of constants alone.
        """

    @abstractmethod
    def id_of(self, json: str, key: str | None) -> str:
        """The id at the key of the JSON map, or with no key the id the JSON is, as the store's id."""

    @abstractmethod
    def appended(self, list_: str, item: str) -> str:
        """The JSON list with the JSON item after its items."""

    @abstractmethod
    def strip_nulls(self, json: str) -> str:
        """The JSON map without the entries whose values are null."""

    @abstractmethod
    def merged(self, props: str, map_: str) -> str:
        """The JSON map `props` with the entries of `map_` set over it, an entry whose value is null removing its
        key."""

    @abstractmethod
    def without_label(self, labels: str, label: str) -> str:
        """The labels, a JSON list, without the label."""

    @abstractmethod
    def node_json(self, node: Node) -> str:
        """A node as JSON: its properties, its stamp among them, as Drivine gives a node; null where there is no
        node."""

    # ----- One statement's compilation: its parameters, and the names it gives its rows and columns -----

    def _begin(self, parameters: Parameters) -> None:
        self._parameters = parameters
        self._counter = 0
        self._body: list[str] = []
        """The procedure's statements, in order."""
        self._depth = 0
        """How deep in loops and conditions the next statement is, for its indent."""
        self._kinds: dict[str, _Kind] = {}
        """The variables a row holds, and what each is: what ROWS and ROW carry from clause to clause."""
        self._carried: dict[str, Json] = {}
        """The value each variable of _Kind.VALUE was when a row was carried: it is read again of the kinds that value
        was of."""
        self._keys: dict[str, str] = {}
        """The key each variable is held at in a row: a name of the compiler's, so any variable's name will do."""
        self._unread: tuple[_Select, int] | None = None
        """The rows the last write left, as read from ROWS, and how much was read of them then: nothing more is there to
        carry."""
        self._given_parameters: dict[str, Value] | None = None
        """The parameters an expression reads from a map it is given, where it reads none bound: see Reduce."""
        self._nesting = 0
        """How deep in `FOREACH`es and writing `CALL`s the write being made is: each keeps the rows it puts back in a
        variable of its own depth."""
        self._row_order: str | None = None
        """
        The order of the rows the projection being compiled reads, which `collect` gathers them in, as Cypher gathers
        them: a group's rows are not read in the order of the query they come from.
        """
        self._stepping = False
        """Whether a quantified path pattern's conditions are being compiled: what they hold is in its walk's step."""

    def _alias(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def _query(self, query: Query) -> str:
        select = _Select({})
        for clause in query.clauses:
            if isinstance(clause, Return):
                return self._project(select, clause.projection, final=True).sql
            select = self._read(select, clause)
        raise DrivineError("A Cypher statement ends with RETURN.")

    # ----- Clauses -----

    def _read(self, select: _Select, clause: Clause, outer: Scope | None = None) -> _Select:
        """
        A reading clause over the rows the select reads, and the rows the clauses after it read: a `WITH` is a query of
        its own. `outer` is what a subquery reads from the row it is in.
        """
        if isinstance(clause, Match):
            self._match(select, clause)
            return select
        if isinstance(clause, Unwind):
            self._unwind(select, clause)
            return select
        if isinstance(clause, With):
            return self._with(select, clause, outer or {})
        if isinstance(clause, Call):
            self._call(select, clause)
            return select
        raise DrivineError(f"{type(clause).__name__} reads nothing")

    def _with(self, select: _Select, with_: With, outer: Scope | None = None, apart: bool = False) -> _Select:
        """A `WITH`: a query of its own, which the rest of the statement reads from, as it reads `outer`."""
        projected = self._project(select, with_.projection, final=False)
        name = self._alias("w")
        next_ = _Select(outer or {})
        next_.walks = select.walks
        # A value made anew for each row is one value however often the clauses after read it: see `once`. Kept apart
        # from the query that reads it, where that query must not have it merged in: see `_cases_first`.
        anew = any(contains(it.expression, is_anew) for it in with_.projection.items)
        sql = self.once(projected.sql) if anew else projected.sql
        next_.source(f"(\n{indent(self.called(sql) if apart else sql)}\n) {name}")
        for variable, value in projected.scope.items():
            next_.bind(variable, _columns_of(value, name))
        for column, descending in projected.order:
            next_.order.append((f"{name}.{column}", descending))
        if with_.where is not None:
            next_.where.append(self.bool(self._expression(with_.where, next_.scope)))
        return next_

    def _match(self, select: _Select, match: Match) -> None:
        # A shortest path is the shortest of the paths that meet the WHERE, as Cypher finds it: the WHERE is met where
        # they are found.
        shortest_where = match.where if match.patterns[-1].shortest is not None else None
        where = match.where if shortest_where is None else None
        if not match.optional:
            self._patterns(select, match.patterns, shortest_where)
            if where is not None:
                select.where.append(self.bool(self._expression(where, select.scope)))
            return
        # What is found is read by a lateral query, joined to keep the row when it finds nothing.
        inner = _Select(select.scope)
        self._patterns(inner, match.patterns, shortest_where)
        if where is not None:
            inner.where.append(self.bool(self._expression(where, inner.scope)))
        scope = select.scope
        found = {variable: value for variable, value in inner.bound.items() if variable not in scope}
        if not found and not inner.sources:
            return
        name = self._alias("o")
        # Each variable found under a name of its own: a variable may be called what SQL will not take.
        named = [(variable, value, self._alias("v")) for variable, value in found.items()]
        columns = [it for _, value, column in named for it in _columns(value, column, self._identities)] or ["1 AS one"]
        text = [f"SELECT {', '.join(columns)}", *inner.from_(), *_present(inner.where_clause())]
        select.join(f"LEFT JOIN LATERAL (\n{indent(chr(10).join(text))}\n) {name} ON TRUE")
        for variable, value, column in named:
            select.bind(variable, _columns_of(value, name, column, self._identities))

    def _correlated(self, query: Query, scope: Scope, what: str) -> str:
        """
        The query a subquery is, for the row it is in, whose variables `scope` holds: its rows, or with no `RETURN`, a
        row for each way its reading clauses are met.
        """
        if any(writes(it) for it in [*query.clauses, *(c for u in query.unions for c in u.clauses)]):
            unsupported(f"A write in {what}")
        order = self._row_order
        try:
            if query.unions:
                # Each query's rows as one value each, as a statement's are; without ALL, each once.
                distinct = not query.unions[0].all
                parts: list[str] = []
                for clauses in [query.clauses, *(it.clauses for it in query.unions)]:
                    select = _Select(scope)
                    for clause in clauses[:-1]:
                        select = self._read(select, clause, scope)
                    sql = self._project(select, _returned(clauses).projection, final=True).sql
                    result = self.comparable(Json("q.result", ordered=True)) if distinct else "q.result"
                    parts.append(f"SELECT {result} AS result FROM (\n{indent(sql)}\n) q")
                return ("\nUNION\n" if distinct else "\nUNION ALL\n").join(parts)
            select = _Select(scope)
            for clause in query.clauses:
                if isinstance(clause, Return):
                    return self._project(select, clause.projection, final=False).sql
                select = self._read(select, clause, scope)
            return "\n".join(["SELECT 1", *self._from(select), *_present(select.where_clause())])
        finally:
            self._row_order = order

    def _call(self, select: _Select, call: Call) -> None:
        """`CALL { … }`: a lateral query for each row, what it returns bound as variables of the row."""
        if not isinstance(call.query.clauses[-1], Return):
            unsupported("A CALL { … } that returns nothing")
        if any(writes(it) for it in call.query.clauses):
            unsupported("A write in CALL { … }")
        order = self._row_order
        if not call.query.unions:
            projected = self._called_part(select, call.query.clauses, None, distinct=False)
        else:
            projected = self._called_union(select, call.query)
        self._row_order = order
        _redeclared(projected.scope, select.bound)
        name = self._alias("c")
        text = f"(\n{indent(self.called(projected.sql))}\n) {name}"
        select.join(f"LEFT JOIN LATERAL {text} ON TRUE" if call.optional else f"CROSS JOIN LATERAL {text}")
        for variable, value in projected.scope.items():
            select.bind(variable, _columns_of(value, name))
        for column, descending in projected.order:
            select.order.append((f"{name}.{column}", descending))

    def _called_part(
        self, select: _Select, clauses: Sequence[Clause], names: list[str] | None, distinct: bool
    ) -> _Projected:
        """
        One query of a `CALL { … }`, for the row the select reads: its returned items in the order `names` lists them,
        where a `UNION` names them so, and kept distinct with `distinct`.
        """
        inner = _Select(select.scope)
        for clause in clauses[:-1]:
            inner = self._read(inner, clause, select.scope)
        projection = _returned(clauses).projection
        if names is not None:
            items = tuple(next(it for it in projection.items if it.name == name) for name in names)
        else:
            items = projection.items
        return self._project(
            inner, dataclasses.replace(projection, items=items, distinct=distinct or projection.distinct), final=False
        )

    def _called_union(self, select: _Select, query: Query) -> _Projected:
        """A `UNION` in a `CALL { … }`: each query's rows, the same items of the same kinds, matched by name as Cypher
        matches them."""
        parts = [query.clauses, *(it.clauses for it in query.unions)]
        if len({it.all for it in query.unions}) > 1:
            raise DrivineError("UNION and UNION ALL are not mixed in one statement, as in Cypher.")
        returns = [_returned(it) for it in parts]
        if any(it.projection.order_by for it in returns):
            unsupported("An ORDER BY in a query of a UNION in CALL { … }")
        names = [it.name for it in returns[0].projection.items]
        if any({item.name for item in it.projection.items} != set(names) for it in returns):
            raise DrivineError("Each query of a UNION returns the same names, as in Cypher.")
        distinct = not query.unions[0].all
        projected = [self._called_part(select, it, names, distinct) for it in parts]
        kinds = {tuple(type(value) for value in it.scope.values()) for it in projected}
        if len(kinds) > 1:
            unsupported("A UNION in CALL { … } whose queries return values of different kinds")
        # A map or a list that holds a node carries a column more, of its identity. Where one query's value has it and
        # another's has none, as `collect([r])` beside `[]`, the queries are compiled again without identities, so their
        # columns match, and what they return compares by its properties, as it did before identities.
        held = {
            tuple(isinstance(value, Json) and value.identity is not None for value in it.scope.values())
            for it in projected
        }
        if len(held) > 1:
            was = self._identities
            self._identities = False
            try:
                projected = [self._called_part(select, it, names, distinct) for it in parts]
            finally:
                self._identities = was
        sql = ("\nUNION\n" if distinct else "\nUNION ALL\n").join(
            f"SELECT * FROM (\n{indent(it.sql)}\n) {self._alias('q')}" for it in projected
        )
        # A value is of a temporal kind where each query makes it of that one, and a text, or a list of texts, where
        # each makes it one.
        scope: dict[str, Value] = {}
        for variable, value in projected[0].scope.items():
            if not isinstance(value, Json):
                scope[variable] = value
                continue
            all_ = [it.scope[variable] for it in projected]
            temporals = {it.temporal if isinstance(it, Json) else None for it in all_}
            items = {it.items if isinstance(it, Json) else None for it in all_}
            scope[variable] = dataclasses.replace(
                value,
                temporal=next(iter(temporals)) if len(temporals) == 1 else None,
                items=next(iter(items)) if len(items) == 1 else None,
                text=all(_is_text(it) for it in all_),
                texts=all(_holds_texts(it) for it in all_),
            )
        return _Projected(sql, scope, [])

    def _unwind(self, select: _Select, unwind: Unwind) -> None:
        listed = self._expression(unwind.list, select.scope)
        name = self._alias("u")
        # A list of nodes or relationships unwinds to them, as they are now, so a write reaches them; a null item is a
        # null one.
        if isinstance(listed, Entities):
            select.source(self.elements(listed.ids, name))
            row = self._alias("e" if listed.relationships else "n")
            if listed.relationships:
                select.join(f"LEFT JOIN {self.edges} {row} ON {row}.eid = {self.id_of(f'{name}.item', None)}")
                select.bind(unwind.variable, _relationship(row))
            else:
                select.join(f"LEFT JOIN {self.nodes} {row} ON {row}.nid = {self.id_of(f'{name}.item', None)}")
                select.bind(unwind.variable, _node(row))
            select.order.append((f"{name}.item_no", False))
            return

        # A value that is no list is one row, and null no rows, as in Cypher.
        def items_of(x: str) -> str:
            alone = self.comparable(Json(self.json_array([x]), ordered=True))
            return f"CASE WHEN {self.type_of(x)} = 'array' THEN {x} WHEN NOT {self.is_json_null(x)} THEN {alone} END"

        items = self.shared1(self.comparable(self.json(listed)), items_of)
        select.source(self.elements(items, name))
        # An item of a list of texts, or of a text, is a text.
        select.bind(
            unwind.variable,
            Json(
                f"{name}.item",
                temporal=_items_of(listed),
                list=_holds_lists(listed),
                text=_holds_texts(listed) or _is_text(listed),
            ),
        )
        select.order.append((f"{name}.item_no", False))

    def _distinct(self, a: _Hop, b: _Hop) -> str:
        """That no relationship of the hop `a` is one of `b`'s."""
        if not a.many and not b.many:
            return f"{a.eids} <> {b.eids}"
        if not a.many:
            return f"NOT ({self.id_in(a.eids, b.eids)})"
        if not b.many:
            return f"NOT ({self.id_in(b.eids, a.eids)})"
        return f"NOT ({self.ids_overlap(a.eids, b.eids)})"

    def _patterns(self, select: _Select, patterns: Sequence[Pattern], shortest_where: Expression | None = None) -> None:
        """
        The patterns of one `MATCH` matched over the rows the select reads: their relationships are distinct from one
        another. `shortest_where` is met by the paths of the last, a shortest path, see `_shortest`.
        """
        hops: list[_Hop] = []
        for i, pattern in enumerate(patterns):
            self._pattern(select, pattern, hops, shortest_where if i == len(patterns) - 1 else None)
        for i in range(len(hops)):
            for j in range(i + 1, len(hops)):
                a, b = hops[i], hops[j]
                # Relationships of types no relationship has both of cannot be one.
                if not a.types or not b.types or set(a.types) & set(b.types):
                    select.where.append(self._distinct(a, b))

    def _pattern(
        self, select: _Select, pattern: Pattern, hops: list[_Hop], shortest_where: Expression | None = None
    ) -> None:
        if pattern.shortest is not None:
            self._shortest(select, pattern, shortest_where)
            return
        # A node bound before the pattern, or given properties, is where a walk starts best.
        before = select.scope
        pinned = [
            node.properties is not None or (node.variable is not None and node.variable in before)
            for node in pattern.nodes
        ]
        bound: list[Node] = []
        for node in pattern.nodes:
            existing = select.scope.get(node.variable) if node.variable is not None else None
            if isinstance(existing, Node):
                value = existing
            elif existing is None:
                name = self._alias("n")
                select.source(f"{self.nodes} {name}")
                value = _node(name)
                if node.variable is not None:
                    select.bind(node.variable, value)
            else:
                raise DrivineError(f"{node.variable} is not a node, and is used as one in a pattern.")
            if node.labels:
                select.where.append(self.has_labels(value.labels, node.labels))
            if node.properties is not None:
                select.where.append(self._properties_match(value.props, node.properties, select.scope))
            bound.append(value)
        for i, relationship in enumerate(pattern.relationships):
            if relationship.group is not None:
                hops.append(
                    self._quantified(
                        select, relationship, bound[i].nid, bound[i + 1].nid, self._seed(pattern.nodes[i], before)
                    )
                )
                continue
            if relationship.range is not None:
                # Walked from its far node when only that one is pinned: the same relationships, read backwards.
                if pinned[i + 1] and not pinned[i]:
                    hops.append(
                        self._walk(
                            select,
                            _reversed(relationship),
                            bound[i + 1].nid,
                            bound[i].nid,
                            True,
                            self._seed(pattern.nodes[i + 1], before),
                        )
                    )
                else:
                    hops.append(
                        self._walk(
                            select,
                            relationship,
                            bound[i].nid,
                            bound[i + 1].nid,
                            False,
                            self._seed(pattern.nodes[i], before),
                        )
                    )
                continue
            if relationship.variable is not None and relationship.variable in select.scope:
                unsupported("A relationship variable bound before its pattern")
            name = self._alias("e")
            select.source(f"{self.edges} {name}")
            value = _relationship(name)
            if relationship.variable is not None:
                select.bind(relationship.variable, value)
            from_ = bound[i].nid
            to = bound[i + 1].nid
            if relationship.direction is Direction.OUTGOING:
                select.where.append(f"{value.src} = {from_} AND {value.dst} = {to}")
            elif relationship.direction is Direction.INCOMING:
                select.where.append(f"{value.dst} = {from_} AND {value.src} = {to}")
            else:
                # Either way, and a relationship from a node to itself once.
                select.where.append(
                    f"(({value.src} = {from_} AND {value.dst} = {to}) "
                    f"OR ({value.dst} = {from_} AND {value.src} = {to}))"
                )
            typed = self._types(value.type, relationship.types)
            if typed is not None:
                select.where.append(typed)
            if relationship.properties is not None:
                select.where.append(self._properties_match(value.props, relationship.properties, select.scope))
            hops.append(_Hop(value.eid, False, relationship.types, self.ids(to)))
        if pattern.variable is not None:
            mine = hops[len(hops) - len(pattern.relationships) :] if pattern.relationships else []
            nids = self.join_ids([self.ids(bound[0].nid), *(it.nodes for it in mine)])
            listed = [it.eids if it.many else self.ids(it.eids) for it in mine]
            eids = self.join_ids(listed) if listed else self.no_ids
            select.bind(pattern.variable, Path(nids, eids))

    def _shortest(self, select: _Select, pattern: Pattern, where: Expression | None) -> None:
        """
        `shortestPath(…)` or `allShortestPaths(…)`: the pattern's paths for the row, found in a lateral query, of which
        the shortest between each two nodes it joins is kept, or every one of the least length. Its relationship is a
        walk, so a path of it takes no relationship twice, as Cypher's shortest path takes none. The paths are those
        that meet `where`, the `WHERE` of its `MATCH`, as Cypher's shortest path is the least of those.
        """
        relationship = pattern.relationships[0] if len(pattern.relationships) == 1 else None
        if relationship is None or relationship.range is None or relationship.group is not None:
            unsupported("A shortest path of anything but one variable-length relationship")
        inner = _Select(select.scope)
        hops: list[_Hop] = []
        # Its ends named, to find the shortest between each two of them where they were not bound before it.
        first, last = pattern.nodes[0], pattern.nodes[-1]
        ends = [first.variable or SHORTEST_FROM, last.variable or SHORTEST_TO]
        named = Pattern(
            pattern.variable,
            (dataclasses.replace(first, variable=ends[0]), dataclasses.replace(last, variable=ends[1])),
            pattern.relationships,
        )
        self._pattern(inner, named, hops)
        if where is not None:
            inner.where.append(self.bool(self._expression(where, inner.scope)))
        scope = select.scope
        found = [
            (variable, value, self._alias("v"))
            for variable, value in inner.bound.items()
            if variable not in scope and variable not in (SHORTEST_FROM, SHORTEST_TO)
        ]
        length = self.number(self.id_count(_single(hops).eids))
        inner_scope = inner.scope
        from_, to = (_as_node(inner_scope[it]).nid for it in ends)
        columns = [
            *(it for _, value, column in found for it in _columns(value, column, self._identities)),
            f"{length} AS len",
            f"{from_} AS from_nid",
            f"{to} AS to_nid",
        ]
        paths = "\n".join([f"SELECT {', '.join(columns)}", *inner.from_(), *_present(inner.where_clause())])
        name = self._alias("sp")
        s = self._alias("s")
        if pattern.shortest is Shortest.ONE:
            ranked = (
                f"SELECT {s}.*, ROW_NUMBER() OVER (PARTITION BY {s}.from_nid, {s}.to_nid ORDER BY {s}.len) AS k "
                f"FROM (\n{indent(paths)}\n) {s}"
            )
            text = f"SELECT * FROM (\n{indent(ranked)}\n) {self._alias('s')}\nWHERE k = 1"
        else:
            # Every path as short as the shortest.
            least = (
                f"SELECT {s}.*, MIN({s}.len) OVER (PARTITION BY {s}.from_nid, {s}.to_nid) AS least "
                f"FROM (\n{indent(paths)}\n) {s}"
            )
            text = f"SELECT * FROM (\n{indent(least)}\n) {self._alias('s')}\nWHERE len = least"
        select.join(f"CROSS JOIN LATERAL (\n{indent(text)}\n) {name}")
        for variable, value, column in found:
            select.bind(variable, _columns_of(value, name, column, self._identities))

    def _types(self, type_: str, types: Sequence[str]) -> str | None:
        """That the relationship type is one of the types; None when any will do."""
        if not types:
            return None
        if len(types) == 1:
            return f"{type_} = {literal(types[0])}"
        return f"{type_} IN ({', '.join(literal(it) for it in types)})"

    def _seed(self, node: NodePattern, before: Scope) -> Callable[[str], str] | None:
        """
        What the node meets of its own, before a walk from it, over a node row: its labels, and its properties where
        their values do not depend on a row. None for a node bound before its pattern, which is found where it was
        bound, or with nothing of its own to meet.
        """
        if node.variable is not None and node.variable in before:
            return None
        properties = node.properties
        if properties is not None and contains(properties, lambda e: isinstance(e, (Variable, Property))):
            properties = None
        if not node.labels and properties is None:
            return None

        def seed(row: str) -> str:
            tests: list[str] = []
            if node.labels:
                tests.append(self.has_labels(f"{row}.labels", node.labels))
            if properties is not None:
                tests.append(self._properties_match(f"{row}.props", properties, before))
            return " AND ".join(tests)

        return seed

    def _walk(
        self,
        select: _Select,
        relationship: RelationshipPattern,
        from_: str,
        to: str,
        backwards: bool,
        seed: Callable[[str], str] | None,
    ) -> _Hop:
        """
        A variable-length relationship, `-[:T*min..max]->`, from the node `from_` to the node `to`: a walk from `from_`
        that takes a relationship no more than once, as Cypher takes it, to a depth from `min` (1 if left out) to `max`
        (any if left out), its rows each way it reaches `to`. Walked backwards, from the pattern's later node, it keeps
        its nodes and relationships in the pattern's order.
        """
        range_ = relationship.range
        assert range_ is not None
        edge = self._alias("e")
        conditions = [
            *_present(self._types(f"{edge}.type", relationship.types)),
            *(
                [self._properties_match(f"{edge}.props", relationship.properties, select.scope)]
                if relationship.properties is not None
                else []
            ),
        ]
        walked = self._walked(
            Walk(
                from_,
                to,
                self._alias("p"),
                self._alias("w"),
                (Hop(edge, relationship.direction),),
                (None, None),
                range_.min if range_.min is not None else 1,
                range_.max,
                tuple(conditions),
                backwards,
                seed,
            )
        )
        select.source(walked.source)
        select.walks = True
        select.where.append(walked.condition)
        # A variable on it is the list of its relationships.
        if relationship.variable is not None:
            select.bind(relationship.variable, self._relationship_list(walked.eids))
        return _Hop(walked.eids, True, relationship.types, walked.nids)

    def _quantified(
        self, select: _Select, relationship: RelationshipPattern, from_: str, to: str, seed: Callable[[str], str] | None
    ) -> _Hop:
        """
        A quantified path pattern, `((a)-[r:R]->(b) WHERE …){min,max}`, from the node `from_` to the node `to`: a walk
        whose each step is a repetition of the group's pattern, that takes a relationship no more than once, as a walk
        of a variable-length relationship does. Each variable of the group is bound to the list of what it matched in
        each repetition, in turn.
        """
        group = relationship.group
        range_ = relationship.range
        assert group is not None and range_ is not None
        inner = group.pattern
        rows = [self._alias("n") for _ in inner.nodes]
        links = [self._alias("e") for _ in inner.relationships]
        scope: list[tuple[str, Value]] = [
            *(
                (node.variable, _node(row))
                for node, row in zip(inner.nodes, rows, strict=True)
                if node.variable is not None
            ),
            *(
                (part.variable, _relationship(edge))
                for part, edge in zip(inner.relationships, links, strict=True)
                if part.variable is not None
            ),
        ]
        outside = select.scope
        named: dict[str, int] = {}
        for variable, _ in scope:
            named[variable] = named.get(variable, 0) + 1
        for variable, times in named.items():
            if times > 1:
                unsupported("A variable named twice in a quantified path pattern")
            if outside.get(variable) is not None:
                raise DrivineError(f"{variable} is bound outside the quantified path pattern that names it.")
        tests = [
            *(it.properties for it in inner.nodes if it.properties is not None),
            *(it.properties for it in inner.relationships if it.properties is not None),
            *_present(group.where),
        ]
        read = _union_of(variables_in, tests)
        # A subquery reads every variable, for variables_in names none of a subquery's: as _evaluate takes it.
        subquery = any(holds_subquery(it) for it in tests)
        if not self.walks_read_row and any(outside.get(it) is not None for it in read):
            unsupported("On Oracle, a quantified path pattern that reads a variable from outside it")
        if not self.walks_read_row and subquery and outside:
            unsupported("On Oracle, a subquery in a quantified path pattern that may read a variable from outside it")
        within = {**outside, **dict(scope)}
        stepping = self._stepping
        self._stepping = True
        try:
            conditions = [
                *(
                    test
                    for node, row in zip(inner.nodes, rows, strict=True)
                    for test in _present(
                        self.has_labels(f"{row}.labels", node.labels) if node.labels else None,
                        self._properties_match(f"{row}.props", node.properties, within)
                        if node.properties is not None
                        else None,
                    )
                ),
                *(
                    test
                    for part, edge in zip(inner.relationships, links, strict=True)
                    for test in _present(
                        self._types(f"{edge}.type", part.types),
                        self._properties_match(f"{edge}.props", part.properties, within)
                        if part.properties is not None
                        else None,
                    )
                ),
                *([self.bool(self._expression(group.where, within))] if group.where is not None else []),
            ]
        finally:
            self._stepping = stepping
        # A node is joined to the step where a condition reads it.
        joined = tuple(
            row
            if (subquery and node.variable is not None)
            or node.labels
            or node.properties is not None
            or node.variable in read
            else None
            for node, row in zip(inner.nodes, rows, strict=True)
        )
        walked = self._walked(
            Walk(
                from_,
                to,
                self._alias("p"),
                self._alias("w"),
                tuple(Hop(edge, part.direction) for part, edge in zip(inner.relationships, links, strict=True)),
                joined,
                range_.min if range_.min is not None else 1,
                range_.max,
                tuple(conditions),
                False,
                seed,
            )
        )
        select.source(walked.source)
        select.walks = True
        select.where.append(walked.condition)
        # The step's nodes and relationships, k of each a repetition, in turn: each variable is every k-th of them.
        k = len(inner.relationships)
        nids = self.join_ids([self.ids(from_), walked.nids])
        for i, node in enumerate(inner.nodes):
            if node.variable is not None:
                select.bind(node.variable, Entities(self._every(nids, i, k, k - i), relationships=False))
        for i, part in enumerate(inner.relationships):
            if part.variable is not None:
                select.bind(part.variable, Entities(self._every(walked.eids, i, k, 0), relationships=True))
        typed = all(it.types for it in inner.relationships)
        types = tuple(dict.fromkeys(t for it in inner.relationships for t in it.types)) if typed else ()
        return _Hop(walked.eids, True, types, walked.nids)

    def _every(self, list_: str, first: int, stride: int, leave: int) -> str:
        """
        The ids of the store's list from the one after the first `first`, every `stride`-th, short of the last `leave`,
        as a JSON list in its order.
        """
        u = self._alias("u")
        v = self._alias("v")
        listed = self.comparable(Json(self.aggregate_list(self.scalar(f"{v}.id"), f"{v}.ord"), ordered=True))
        counted = f"SELECT {u}.id, {u}.ord, COUNT(*) OVER () AS cnt FROM {self.id_rows(list_, u)}"
        return (
            f"(SELECT {listed} FROM ({counted}) {v} "
            f"WHERE {v}.ord > {first} AND MOD({v}.ord - {first + 1}, {stride}) = 0 AND {v}.ord <= {v}.cnt - {leave})"
        )

    def _walked(self, walk: Walk) -> Walked:
        """
        `walk`'s rows. On Oracle, no walk is in another's step, for its recursive query holds no `WITH` of its own.
        """
        if not self.walks_read_row and self._stepping:
            unsupported(
                "On Oracle, a variable-length relationship or quantified path pattern in a subquery of a quantified "
                "path pattern"
            )
        return self.walk(walk)

    def _node_list(self, nids: str) -> Entities:
        """The nodes whose ids are the store's list, in its order: null where the list is."""
        return Entities(self._id_list(nids), relationships=False)

    def _relationship_list(self, eids: str) -> Entities:
        """The relationships whose ids are the store's list, in its order: null where the list is."""
        return Entities(self._id_list(eids), relationships=True)

    def _id_list(self, list_: str) -> str:
        """The store's list of ids as a JSON list of them, in its order: null where the list is."""
        u = self._alias("u")
        listed = self.comparable(Json(self.aggregate_list(self.scalar(f"{u}.id"), f"{u}.ord"), ordered=True))
        return f"CASE WHEN {list_} IS NOT NULL THEN (SELECT {listed} FROM {self.id_rows(list_, u)}) END"

    def _entities_json(self, entities: Entities) -> str:
        """The nodes or relationships the list holds, as a JSON list of their properties in its order: null where the
        list is."""
        if entities.gathered is not None:
            return entities.gathered

        def listed(ids: str) -> str:
            u = self._alias("u")
            row = self._alias("e" if entities.relationships else "n")
            if entities.relationships:
                table, value, id_ = self.edges, self.json(_relationship(row)).sql, "eid"
            else:
                table, value, id_ = self.nodes, self.json(_node(row)).sql, "nid"
            return (
                f"CASE WHEN {ids} IS NOT NULL THEN (SELECT {self.aggregate_list(value, f'{u}.item_no')} "
                f"FROM {self.elements(ids, u)} LEFT JOIN {table} {row} "
                f"ON {row}.{id_} = {self.id_of(f'{u}.item', None)}) END"
            )

        return self.shared1(entities.ids, listed)

    def _properties_match(self, props: str, properties: Expression, scope: Scope) -> str:
        """A pattern's properties: each key equal to its value, or every key of a parameter's map."""
        if isinstance(properties, MapLiteral):
            return " AND ".join(
                self._equality(Json(self.property_of(props, key)), self._expression(value, scope), value)
                for key, value in properties.entries
            )
        return self.contains_all(props, self.comparable(self.json(self._expression(properties, scope))))

    # ----- A statement that writes -----

    def _procedure(self, query: Query) -> list[str]:
        """The statements of the procedure the query is: its reads, a loop over its rows for each write, and what it
        returns."""
        select = _Select({})
        for clause in _creates(query.clauses):
            if isinstance(clause, Return):
                for statement in self.return_rows(self._project(select, clause.projection, final=True).sql):
                    self._emit(statement)
            else:
                select = self._next(select, clause)
        return self._body

    def _next(self, select: _Select, clause: Clause) -> _Select:
        """The clause over the rows the select reads, written for them where it writes: the rows the clauses after it
        read."""
        if not writes(clause):
            return self._read(select, clause)
        self._carry(select)
        if isinstance(clause, Foreach):
            self._foreach(clause)
        elif isinstance(clause, Call):
            self._call_writing(clause)
        else:
            self._write(clause)
        return self._rows()

    def _emit(self, statement: str) -> None:
        prefix = "    " * self._depth
        self._body.append("\n".join(prefix + line for line in statement.split("\n")) if prefix else statement)

    def _assign(self, variable: str, sql: str) -> None:
        self._emit(f"SELECT {sql} INTO {variable};")

    def _key(self, variable: str) -> str:
        if variable not in self._keys:
            self._keys[variable] = f"v{len(self._keys) + 1}"
        return self._keys[variable]

    def _carry(self, select: _Select) -> None:
        """The rows the select reads, each a JSON map of the variables it binds, into ROWS."""
        if self._unread is not None and self._unread[0] is select and self._unread[1] == select.size():
            return
        self._kinds.clear()
        self._carried.clear()
        entries: list[tuple[str, str]] = []
        for variable, value in select.scope.items():
            if isinstance(value, Json):
                self._carried[variable] = value
            if isinstance(value, Node):
                self._kinds[variable] = _Kind.NODE
            elif isinstance(value, Relationship):
                self._kinds[variable] = _Kind.RELATIONSHIP
            elif isinstance(value, (Json, Bool)):
                self._kinds[variable] = _Kind.VALUE
            elif isinstance(value, Entities):
                self._kinds[variable] = _Kind.RELATIONSHIPS if value.relationships else _Kind.NODES
            else:
                self._kinds[variable] = _Kind.PATH
            entries.append((self._key(variable), self._held(value)))
        listed = self.comparable(Json(self.aggregate_list(self.json_object(entries), _order_of(select)), ordered=True))
        self._emit(
            "\n".join([f"SELECT {listed} INTO {ROWS}", *self._from(select), *_present(select.where_clause())]) + ";"
        )

    def _held(self, value: Value) -> str:
        """A value as a row holds it: a node or a relationship as its id, anything else as JSON."""
        if isinstance(value, Node):
            return self.scalar(value.nid)
        if isinstance(value, Relationship):
            return self.scalar(value.eid)
        if isinstance(value, Entities):
            return value.ids
        if isinstance(value, Path):
            # A path as the JSON lists of its nodes' and relationships' ids.
            return self.comparable(
                Json(
                    self.json_object([("n", self._id_list(value.nids)), ("e", self._id_list(value.eids))]), ordered=True
                )
            )
        return self.comparable(self.json(value))

    def _from(self, select: _Select) -> list[str]:
        return select.from_() or ["FROM (SELECT 1) one"]

    def _rows(self) -> _Select:
        """The rows in ROWS, as a query reads them."""
        select = _Select({})
        r = self._alias("r")
        select.source(self.elements(ROWS, r))
        select.order.append((f"{r}.item_no", False))
        self._bind_row(select, f"{r}.item")
        self._unread = (select, select.size())
        return select

    def _row(self, needed: set[str] | None) -> _Select:
        """
        The row in ROW alone, as a statement of a write reads it, with the variables `needed` bound: a row may hold
        many, and each node or relationship bound is a join.
        """
        select = _Select({})
        self._bind_row(select, ROW, needed)
        return select

    def _bind_row(self, select: _Select, row: str, needed: set[str] | None = None) -> None:
        """Binds into the select each variable the row holds, of `needed` where given: a node or a relationship is read
        from its table, as it is now."""
        for variable, kind in self._kinds.items():
            if needed is not None and variable not in needed:
                continue
            key = self._key(variable)
            if kind is _Kind.NODE:
                n = self._alias("n")
                select.join(f"LEFT JOIN {self.nodes} {n} ON {n}.nid = {self.id_of(row, key)}")
                select.bind(variable, _node(n))
            elif kind is _Kind.RELATIONSHIP:
                e = self._alias("e")
                select.join(f"LEFT JOIN {self.edges} {e} ON {e}.eid = {self.id_of(row, key)}")
                select.bind(variable, _relationship(e))
            elif kind is _Kind.VALUE:
                carried = self._carried.get(variable)
                select.bind(
                    variable,
                    Json(
                        self.property_of(row, key),
                        temporal=carried.temporal if carried else None,
                        items=carried.items if carried else None,
                        list=carried.list if carried else False,
                        item_lists=carried.item_lists if carried else False,
                        text=carried.text if carried else False,
                        texts=carried.texts if carried else False,
                    ),
                )
            elif kind in (_Kind.NODES, _Kind.RELATIONSHIPS):
                # A list or a path that is null is held as JSON's null, and read as SQL's.
                select.bind(
                    variable,
                    Entities(self.null_if_json_null(self.property_of(row, key)), kind is _Kind.RELATIONSHIPS),
                )
            else:
                held = self.property_of(row, key)
                select.bind(
                    variable,
                    Path(
                        self.store_ids(self.null_if_json_null(self.property_of(held, "n"))),
                        self.store_ids(self.null_if_json_null(self.property_of(held, "e"))),
                    ),
                )

    def _evaluate(self, variable: str, expressions: Sequence[Expression | None], value: Callable[[Scope], str]) -> None:
        """
        The value, worked out over the row in ROW, into the variable: `expressions` are what it reads. A subquery among
        them, in a comprehension's, a quantifier's or a reduce's expression too, reads every variable of the row, for
        `variables_in` names none of a subquery's.
        """
        read = [it for it in expressions if it is not None]
        if any(holds_subquery(it) for it in read):
            needed = None
        else:
            needed = {name for it in read for name in variables_in(it)}
        select = self._row(needed)
        self._emit("\n".join([f"SELECT {value(select.scope)} INTO {variable}", *self._from(select)]) + ";")

    def _write(self, clause: Clause) -> None:
        """One write: made for each row of ROWS in turn, and the rows it leaves put back there."""
        self._assign(OUT, self.literal_json([]))
        deleting = isinstance(clause, Delete) and not clause.detach
        if deleting:
            self._assign(DOOMED, self.literal_json([]))
        r = self._alias("r")
        self._emit(f"FOR {RECORD} IN (SELECT {r}.item FROM {self.elements(ROWS, r)} ORDER BY {r}.item_no) LOOP")
        self._depth += 1
        self._emit(f"{ROW} := {RECORD}.item;")
        if isinstance(clause, Create):
            for pattern in clause.patterns:
                self._create(pattern, merging=False)
            self._keep()
        elif isinstance(clause, Merge):
            self._merge(clause)
        elif isinstance(clause, SetClause):
            for item in clause.items:
                self._set(item)
            self._keep()
        elif isinstance(clause, Remove):
            for item in clause.items:
                self._remove(item)
            self._keep()
        elif isinstance(clause, Delete):
            self._delete(clause)
            self._keep()
        else:
            raise DrivineError(f"{type(clause).__name__} reads")
        self._depth -= 1
        self._emit("END LOOP;")
        self._emit(f"{ROWS} := {OUT};")
        if deleting:
            # A node is deleted when the clause has deleted the relationships it deletes: one it does not delete must
            # not be there.
            d = self._alias("d")
            doomed = f"SELECT {self.id_of(f'{d}.item', None)} FROM {self.elements(DOOMED, d)}"
            self._emit(f"SELECT count(*) INTO {COUNT} FROM {self.edges} WHERE src IN ({doomed}) OR dst IN ({doomed});")
            self._emit(f"IF {COUNT} > 0 THEN")
            self._emit(
                "    "
                + self.raise_("A node is deleted that still has relationships: delete them first, or DETACH DELETE it.")
            )
            self._emit("END IF;")
            self._emit(f"DELETE FROM {self.nodes} WHERE nid IN ({doomed});")

    def _call_writing(self, call: Call) -> None:
        """
        A `CALL { … }` that writes, over the rows in ROWS: its clauses written for each row alone, in turn, as a graph
        engine writes them, so what one row writes is there for the next to read. One that returns nothing leaves the
        rows as it found them; one that returns adds what it returns to each row it gives, the row's own variables as
        they were before the call, whatever it bound to their names inside it.
        """
        if call.optional:
            unsupported("An OPTIONAL CALL { … } that writes")
        if call.query.unions:
            unsupported("A UNION in CALL { … }")
        last = call.query.clauses[-1]
        returned = last if isinstance(last, Return) else None
        if returned is not None and any(contains(it.expression, is_aggregate) for it in returned.projection.items):
            unsupported("An aggregate in the RETURN of a CALL { … } that writes")
        if self._nesting == NESTING:
            unsupported(f"A FOREACH or CALL {{ … }} that writes nested more than {NESTING} deep")
        if returned is not None:
            _redeclared([it.name for it in returned.projection.items], self._kinds)
        self._nesting += 1
        kept = f"{SAVED}{self._nesting}"
        gathered = f"{GATHERED}{self._nesting}"
        caller = f"{CALLER}{self._nesting}"
        before = dict(self._kinds)
        carried_before = dict(self._carried)
        returned_kinds: dict[str, _Kind] = {}
        returned_carried: dict[str, Json] = {}
        self._emit(f"{kept} := {ROWS};")
        if returned is not None:
            self._assign(gathered, self.literal_json([]))

        def each() -> None:
            if returned is not None:
                self._emit(f"{caller} := {RECORD}.item;")
            select = self._rows()
            for clause in _imported(call.query.clauses):
                if isinstance(clause, Return):
                    select = self._with(select, With(clause.projection, None))
                else:
                    select = self._next(select, clause)
            if returned is not None:
                # The rows this row gives, after those of the rows before it: each the row the call was written for,
                # read from before the call, with what it returns. A name of the row it returns is the row's.
                self._carry(select)
                gave = {key: kind for key, kind in self._kinds.items() if key not in before}
                returned_kinds.update(gave)
                returned_carried.update({key: value for key, value in self._carried.items() if key in gave})
                g, a, b = self._alias("g"), self._alias("g"), self._alias("g")
                row = self.comparable(
                    Json(
                        self.json_object(
                            [
                                *((self._key(it), self.property_of(caller, self._key(it))) for it in before),
                                *((self._key(it), self.property_of(f"{b}.item", self._key(it))) for it in gave),
                            ]
                        ),
                        ordered=True,
                    )
                )
                both = (
                    f"SELECT 1 AS part, {a}.item_no, {a}.item FROM {self.elements(gathered, a)} UNION ALL "
                    f"SELECT 2 AS part, {b}.item_no, {row} AS item FROM {self.elements(ROWS, b)}"
                )
                listed = self.comparable(Json(self.aggregate_list(f"{g}.item", f"{g}.part, {g}.item_no"), ordered=True))
                self._emit(f"SELECT {listed} INTO {gathered} FROM ({both}) {g};")

        self._each_row(kept, each)
        if returned is None:
            self._emit(f"{ROWS} := {kept};")
            self._restore(before, carried_before)
        else:
            self._emit(f"{ROWS} := {gathered};")
            self._restore({**before, **returned_kinds}, {**carried_before, **returned_carried})
        self._unread = None
        self._nesting -= 1

    def _each_row(self, rows: str, body: Callable[[], None]) -> None:
        """
        Writes the body for each row of the rows, a variable of the procedure, alone in ROWS in turn, as a graph engine
        writes a `FOREACH` or a `CALL { … }` for each row: what one row writes is there for the next to read.
        """
        r = self._alias("r")
        self._emit(f"FOR {RECORD} IN (SELECT {r}.item FROM {self.elements(rows, r)} ORDER BY {r}.item_no) LOOP")
        self._depth += 1
        self._assign(ROWS, self.appended(self.literal_json([]), f"{RECORD}.item"))
        self._unread = None
        body()
        self._depth -= 1
        self._emit("END LOOP;")

    def _foreach(self, foreach: Foreach) -> None:
        """
        A `FOREACH` over the rows in ROWS: its list unwound over them, its clauses written for each row and item alone,
        in turn, then the rows put back as they were, for a `FOREACH` leaves them as it found them.
        """
        if self._nesting == NESTING:
            unsupported(f"A FOREACH or CALL {{ … }} that writes nested more than {NESTING} deep")
        if any(isinstance(it, Call) for it in foreach.clauses):
            raise DrivineError("A CALL { … } is not written in a FOREACH, as in Cypher.")
        self._nesting += 1
        saved = f"{SAVED}{self._nesting}"
        items = f"{GATHERED}{self._nesting}"
        before = dict(self._kinds)
        carried_before = dict(self._carried)
        self._emit(f"{saved} := {ROWS};")
        unwound = self._rows()
        self._unwind(unwound, Unwind(foreach.list, foreach.variable))
        self._carry(unwound)
        self._emit(f"{items} := {ROWS};")

        def each() -> None:
            select = self._rows()
            for clause in _creates(foreach.clauses):
                select = self._next(select, clause)

        self._each_row(items, each)
        self._emit(f"{ROWS} := {saved};")
        self._restore(before, carried_before)
        self._unread = None
        self._nesting -= 1

    def _restore(self, before: dict[str, _Kind], carried_before: dict[str, Json]) -> None:
        """
        The rows' variables put back as they were, `before` of each its kind and `carried_before` its value, where a
        `FOREACH` or a `CALL { … }` leaves the rows as it found them: a variable it bound again within is not of its
        kind.
        """
        self._kinds.clear()
        self._kinds.update(before)
        self._carried.clear()
        self._carried.update(carried_before)

    def _keep(self) -> None:
        """The row in ROW kept, as a row of what the write leaves."""
        self._assign(OUT, self.appended(OUT, ROW))

    def _create(self, pattern: Pattern, merging: bool) -> None:
        """
        Makes the pattern for the row in ROW: each node not bound before it, and each relationship, with the row given
        the ids of those it names. With `merging`, an undirected relationship is made from the pattern's earlier node,
        as a `MERGE` makes it.
        """
        if pattern.variable is not None:
            unsupported("A path variable in CREATE or MERGE")
        if pattern.shortest is not None:
            unsupported("A shortest path in CREATE")
        ids: list[str] = []
        for node in pattern.nodes:
            kind = self._kinds.get(node.variable) if node.variable is not None else None
            if kind is _Kind.NODE and node.variable is not None:
                if node.labels or node.properties is not None:
                    raise DrivineError(
                        f"{node.variable} is bound already, so a CREATE or MERGE makes no node of it with labels or "
                        "properties."
                    )
                ids.append(self.id_of(ROW, self._key(node.variable)))
            elif kind is None:
                properties = node.properties
                self._evaluate(VALUE, [properties], functools.partial(self._written, properties))
                self._emit(
                    f"INSERT INTO {self.nodes} (labels, props, stamp) VALUES ({self.literal_json(list(node.labels))}, "
                    f"{self.strip_nulls(self._unstamped(VALUE))}, {self._stamp_of(VALUE)}) RETURNING nid INTO {ID};"
                )
                ids.append(self._made(node.variable, _Kind.NODE))
            else:
                raise DrivineError(f"{node.variable} is not a node, and is used as one in a pattern.")
        for i, relationship in enumerate(pattern.relationships):
            if relationship.group is not None:
                unsupported("A quantified path pattern in CREATE or MERGE")
            if relationship.range is not None:
                unsupported("A variable-length relationship in CREATE or MERGE")
            if len(relationship.types) != 1:
                raise DrivineError("A relationship that CREATE or MERGE makes has one type.")
            type_ = relationship.types[0]
            if relationship.variable is not None and relationship.variable in self._kinds:
                unsupported("A relationship variable bound before its pattern")
            if relationship.direction is Direction.OUTGOING:
                src, dst = ids[i], ids[i + 1]
            elif relationship.direction is Direction.INCOMING:
                src, dst = ids[i + 1], ids[i]
            elif merging:
                src, dst = ids[i], ids[i + 1]
            else:
                raise DrivineError("A relationship that CREATE makes has a direction.")
            properties = relationship.properties
            self._evaluate(VALUE, [properties], functools.partial(self._written, properties))
            self._emit(
                f"INSERT INTO {self.edges} (src, dst, type, props) VALUES ({src}, {dst}, {literal(type_)}, "
                f"{self.strip_nulls(VALUE)}) RETURNING eid INTO {ID};"
            )
            self._made(relationship.variable, _Kind.RELATIONSHIP)

    def _written(self, properties: Expression | None, scope: Scope) -> str:
        """The properties a pattern writes, as JSON: none where it gives none."""
        if properties is None:
            return self.literal_json({})
        return self.comparable(self.json(self._expression(properties, scope)))

    def _made(self, variable: str | None, kind: _Kind) -> str:
        """The id in ID of what was made, held in the row at the variable's key, or at a key of its own for one of no
        variable."""
        if variable is not None:
            key = self._key(variable)
        else:
            self._counter += 1
            key = f"a{self._counter}"
        self._assign(ROW, self.with_entries(ROW, [(key, self.scalar(ID))]))
        if variable is not None:
            self._kinds[variable] = kind
        return self.id_of(ROW, key)

    def _merge(self, merge: Merge) -> None:
        """
        A `MERGE` for the row in ROW: a row for each way its pattern is found, with `ON MATCH SET` written to it, or
        where it is not found, the pattern made, with `ON CREATE SET`.
        """
        if merge.pattern.variable is not None:
            unsupported("A path variable in CREATE or MERGE")
        select = self._row(set(variables_in_pattern(merge.pattern)))
        self._patterns(select, [merge.pattern])
        found = {variable: value for variable, value in select.bound.items() if variable not in self._kinds}
        before = dict(self._kinds)
        held = self.with_entries(ROW, [(self._key(variable), self._held(value)) for variable, value in found.items()])
        match = [f"SELECT {held} AS item", *self._from(select), *_present(select.where_clause())]
        self._emit(f"{FOUND} := FALSE;")
        self._emit(f"FOR {MATCHED} IN (\n{indent(chr(10).join(match))}\n) LOOP")
        self._depth += 1
        self._emit(f"{FOUND} := TRUE;")
        self._emit(f"{ROW} := {MATCHED}.item;")
        for variable, value in found.items():
            self._kinds[variable] = _Kind.NODE if isinstance(value, Node) else _Kind.RELATIONSHIP
        for item in merge.on_match:
            self._set(item)
        self._keep()
        self._depth -= 1
        self._emit("END LOOP;")
        self._kinds.clear()
        self._kinds.update(before)
        self._emit(f"IF NOT {FOUND} THEN")
        self._depth += 1
        self._create(merge.pattern, merging=True)
        for item in merge.on_create:
            self._set(item)
        self._keep()
        self._depth -= 1
        self._emit("END IF;")

    def _target(self, variable: str, writing: str) -> tuple[str, str, str]:
        """The table the variable's node or relationship is a row of, its key column, and its id in ROW."""
        kind = self._kinds.get(variable)
        if kind is _Kind.NODE:
            return self.nodes, "nid", self.id_of(ROW, self._key(variable))
        if kind is _Kind.RELATIONSHIP:
            return self.edges, "eid", self.id_of(ROW, self._key(variable))
        if kind is None:
            raise DrivineError(
                f"{variable} is not defined: a variable is bound by a pattern, a WITH or an UNWIND before it is used."
            )
        raise DrivineError(f"{variable} is not a node or a relationship, and {writing} writes to one.")

    def _set(self, item: SetItem) -> None:
        if isinstance(item, SetProperty):
            table, id_, row = self._target(item.variable, "SET")
            self._evaluate(
                VALUE, [item.value], lambda scope: self.comparable(self.json(self._expression(item.value, scope)))
            )
            if table == self.nodes and item.key == stamps.PROPERTY:
                self._emit(f"UPDATE {self.nodes} SET stamp = {self.text(VALUE)} WHERE nid = {row};")
            else:
                merged = self.merged("props", self.json_object([(item.key, VALUE)]))
                self._emit(f"UPDATE {table} SET props = {merged} WHERE {id_} = {row};")
        elif isinstance(item, SetReplace):
            table, id_, row = self._target(item.variable, "SET")
            self._evaluate(VALUE, [item.value], lambda scope: self._properties(self._expression(item.value, scope)))
            if table == self.nodes:
                props = self.strip_nulls(self._unstamped(self._or_empty(VALUE)))
                self._emit(
                    f"UPDATE {self.nodes} SET props = {props}, stamp = {self._stamp_of(VALUE)} WHERE nid = {row};"
                )
            else:
                self._emit(f"UPDATE {table} SET props = {self.strip_nulls(self._or_empty(VALUE))} WHERE {id_} = {row};")
        elif isinstance(item, SetAdd):
            table, id_, row = self._target(item.variable, "SET")
            self._evaluate(VALUE, [item.value], lambda scope: self._properties(self._expression(item.value, scope)))
            if table == self.nodes:
                # The stamp is kept where the map has no entry for it, and removed where its entry is null.
                stamp = (
                    f"CASE WHEN {self.property_of(VALUE, stamps.PROPERTY)} IS NULL THEN stamp "
                    f"ELSE {self._stamp_of(VALUE)} END"
                )
                props = self.merged("props", self._unstamped(self._or_empty(VALUE)))
                self._emit(f"UPDATE {self.nodes} SET props = {props}, stamp = {stamp} WHERE nid = {row};")
            else:
                self._emit(
                    f"UPDATE {table} SET props = {self.merged('props', self._or_empty(VALUE))} WHERE {id_} = {row};"
                )
        else:
            table, id_, row = self._target(item.variable, "SET")
            if table != self.nodes:
                raise DrivineError(f"{item.variable} is not a node, and a label is set on one.")
            for label in item.labels:
                labels = self.appended("labels", self.literal_json(label))
                self._emit(
                    f"UPDATE {self.nodes} SET labels = {labels} "
                    f"WHERE {id_} = {row} AND NOT {self.has_label('labels', label)};"
                )

    def _remove(self, item: RemoveItem) -> None:
        if isinstance(item, RemoveProperty):
            table, id_, row = self._target(item.variable, "REMOVE")
            if table == self.nodes and item.key == stamps.PROPERTY:
                self._emit(f"UPDATE {self.nodes} SET stamp = NULL WHERE nid = {row};")
            else:
                merged = self.merged("props", self.json_object([(item.key, self.literal_json(None))]))
                self._emit(f"UPDATE {table} SET props = {merged} WHERE {id_} = {row};")
        else:
            table, id_, row = self._target(item.variable, "REMOVE")
            if table != self.nodes:
                raise DrivineError(f"{item.variable} is not a node, and a label is removed from one.")
            for label in item.labels:
                self._emit(
                    f"UPDATE {self.nodes} SET labels = {self.without_label('labels', label)} WHERE {id_} = {row};"
                )

    def _properties(self, value: Value) -> str:
        """What `SET n = …` and `SET n += …` write: a node's properties, its stamp among them, a relationship's, or a
        map."""
        if isinstance(value, Relationship):
            return value.props
        return self.comparable(self.json(value))

    def _or_empty(self, json: str) -> str:
        """The JSON, or an empty map where it is null."""
        return f"COALESCE({self.null_if_json_null(json)}, {self.literal_json({})})"

    def _unstamped(self, json: str) -> str:
        """The JSON map with its entry for the stamp null: a node's stamp is a column of its row, and no property of
        its map."""
        return self.with_entries(json, [(stamps.PROPERTY, self.literal_json(None))])

    def _stamp_of(self, json: str) -> str:
        """The stamp the JSON map gives a node, as the text its column holds."""
        return self.text(self.property_of(json, stamps.PROPERTY))

    def _delete(self, delete: Delete) -> None:
        """
        A `DELETE` for the row in ROW: its relationships first, then its nodes, each with its relationships where it is
        `DETACH`; without, a node is gathered in DOOMED and deleted once the rows are done.
        """
        targets: list[tuple[str, tuple[str, str, str]]] = []
        for expression in delete.expressions:
            if not isinstance(expression, Variable):
                unsupported("A DELETE of anything but a variable")
            targets.append((expression.name, self._target(expression.name, "DELETE")))
        for _, (table, _, row) in targets:
            if table == self.edges:
                self._emit(f"DELETE FROM {self.edges} WHERE eid = {row};")
        for _, (table, _, row) in targets:
            if table == self.nodes:
                if delete.detach:
                    self._emit(f"DELETE FROM {self.nodes} WHERE nid = {row};")
                else:
                    self._assign(DOOMED, self.appended(DOOMED, self.scalar(row)))

    # ----- Projection -----

    def _project(self, select: _Select, projection: Projection, final: bool) -> _Projected:
        """
        The query a `WITH` or a `RETURN` makes of the select: its items as columns, grouped where it aggregates, then
        ordered, skipped and limited. Rows it does not order, group or keep distinct stay in the order the select reads
        them, as a graph engine keeps them.
        """
        # As in Neo4j, a star gives the variables in the order of their names, before the items named after it.
        starred = [ProjectionItem(Variable(it), it) for it in sorted(select.bound)] if projection.star else []
        items = [*starred, *projection.items]
        if not items:
            raise DrivineError("A projection has at least one item.")
        # Before _aggregates_first, which names each variable it groups by as an item of its own.
        _require_explicit_grouping(items)
        cased = self._cases_first(select, projection, items)
        if cased is not None:
            return self._project(cased[0], cased[1], final)
        grouped = self._aggregates_first(select, projection, items)
        if grouped is not None:
            return self._project(grouped[0], grouped[1], final)
        held = self._held_first(select, projection, items)
        if held is not None:
            return self._project(held[0], held[1], final)
        if final and len(items) > 1:
            logger.warning(
                "The statement returns %s items (%s). Prefer returning a single map or value, such as "
                "RETURN { key1: val1, key2: val2 }: on Oracle and Postgres it is returned as a list of its items, as "
                "Drivine gives a row of several columns.",
                len(items),
                ", ".join(it.name for it in items),
            )
        self._row_order = _order_of(select)
        compiled = [
            (
                item.name,
                self._aggregating(item.expression, functools.partial(self._expression, item.expression, select.scope)),
            )
            for item in items
        ]
        aggregating = any(contains(it.expression, is_aggregate) for it in items)
        # Where rows are compared (DISTINCT, GROUP BY), JSON is compared as what compares.
        compared = projection.distinct or aggregating
        columns: list[tuple[str, Value]] = []
        for i, (_, value) in enumerate(compiled):
            if compared and isinstance(value, Json) and value.ordered:
                value = dataclasses.replace(value, sql=self.comparable(value), ordered=False)
            columns.append((f"c{i + 1}", value))
        select_list = [it for column, value in columns for it in _columns(value, column, self._identities)]
        # An order is written over the items' names, and where nothing is grouped or kept distinct, the variables too.
        name = self._alias("p")
        # Where rows are compared, an order term of the items that is no item is read over their columns, once
        # compared: a group is keyed by an item, and by no part of one.
        item_columns: dict[str, Value] = {
            item.name: _columns_of(value, name, column, self._identities)
            for item, (column, value) in zip(items, columns, strict=True)
        }
        after: dict[str, str] = {}
        # A term worked out in the query is sorted by in the query that reads it: the value carried out as a column,
        # and what sorts it (JSON's null as SQL's) worked out of that column. Oracle (23.26), sorting by a value that
        # reads the row more than once beside a variable-length walk, closes the session.
        sorted_: dict[str, Callable[[str], str]] = {}
        # An item made anew for each row, randomUUID(), is sorted by of its column, as it is given, not made again: see
        # `once`.
        anew = {it.name for it in items if contains(it.expression, is_anew)}
        sorts_anew = False

        def carried(column: str, value: Callable[[], Value], aggregates: Expression) -> None:
            carried_value, sorts_it = self._aggregating(aggregates, lambda: self._sort_term(value()))
            select_list.append(f"{carried_value} AS {column}")
            sorted_[column] = sorts_it

        sorts: list[tuple[str, bool]] = []
        for i, sort in enumerate(projection.order_by):
            column = f"s{i + 1}"
            # An order term that is an item, as written, is that item: so it may be ordered by where rows are compared.
            item = next((j for j, it in enumerate(items) if it.expression == sort.expression), -1)
            if contains(sort.expression, lambda it: isinstance(it, Variable) and it.name in anew) and all(
                it in item_columns for it in variables_in(sort.expression)
            ):
                after[column] = self._sortable(self._expression(sort.expression, item_columns))
                sorts_anew = True
            elif item >= 0:
                carried(column, lambda item=item: compiled[item][1], sort.expression)
            elif compared and not contains(sort.expression, _aggregates):
                after[column] = self._sortable(self._expression(sort.expression, item_columns))
            else:
                # One that aggregates reads the rows it aggregates, as an item does; so does one that reads an aggregate
                # _aggregates_first worked out first, which no item is. Beside a variable-length walk it binds no value
                # in a subquery: carried out of a WITH's query, one so bound closes Oracle's (23.26) session
                # (DPY-4011), as `WITH b ORDER BY toString(coalesce(b.name, b.title))` did, its argument long enough to
                # be bound. Elsewhere it does, so operations nested in it compile to a statement that grows with them,
                # not exponentially.
                scope = {**select.scope, **dict(compiled)}
                term = sort.expression
                carry = functools.partial(
                    carried, column, lambda term=term, scope=scope: self._expression(term, scope), term
                )
                if select.walks:
                    self._unshared(carry)
                else:
                    carry()
            sorts.append((column, sort.descending))
        if not sorts and not compared:
            for i, (term, descending) in enumerate(select.order):
                order_name = f"o{i + 1}"
                select_list.append(f"{term} AS {order_name}")
                sorts.append((order_name, descending))
        group_by: list[str] = []
        if aggregating:
            scope = select.scope
            for item, (_, value) in zip(items, columns, strict=True):
                if not contains(item.expression, is_aggregate):
                    group_by += _group_keys(value)
                else:
                    for variable in variables_outside_aggregates(item.expression):
                        if variable in scope:
                            group_by += _group_keys(scope[variable])
            group_by = list(dict.fromkeys(group_by))
        listed = ",\n       ".join(select_list)
        inner_lines = [f"SELECT {'DISTINCT ' if projection.distinct else ''}{listed}"]
        inner_lines += select.from_()
        inner_lines += _present(select.where_clause())
        if group_by:
            inner_lines.append(f"GROUP BY {', '.join(group_by)}")
        inner = "\n".join(inner_lines)

        # What sorts a term in the query that reads this one: worked out there, of the column carried out, or that
        # column.
        def sorted_by(column: str) -> str:
            if column in after:
                return after[column]
            if column in sorted_:
                return sorted_[column](f"{name}.{column}")
            return f"{name}.{column}"

        if final:
            values = [self.json(_columns_of(value, name, column, self._identities)) for column, value in columns]
            result = values[0].sql if len(values) == 1 else self.json_array([it.sql for it in values])
            outer_columns = f"{self.returned(result)} AS result"
        else:
            outer = [
                f"{name}.{it.rsplit(' AS ', 1)[-1]}"
                for column, value in columns
                for it in _columns(value, column, self._identities)
            ]
            outer += [f"{sorted_by(column)} AS {column}" for column, _ in sorts]
            outer_columns = ", ".join(outer)
        source = self.once(inner) if sorts_anew else inner
        outer_lines = [f"SELECT {outer_columns}", f"FROM (\n{indent(source)}\n) {name}"]
        if sorts:
            terms = ", ".join(f"{sorted_by(column)}{' DESC' if descending else ''}" for column, descending in sorts)
            outer_lines.append(f"ORDER BY {terms}")
        outer_lines += self.page(
            self._count(projection.skip) if projection.skip is not None else None,
            self._count(projection.limit) if projection.limit is not None else None,
        )
        # What each item is bound to in the query that reads this one: its columns, which that query names.
        scope_out: dict[str, Value] = {
            item.name: _columns_of(value, PENDING, column, self._identities)
            for item, (column, value) in zip(items, columns, strict=True)
        }
        return _Projected("\n".join(outer_lines), scope_out, sorts)

    def _cases_first(
        self, select: _Select, projection: Projection, items: list[ProjectionItem]
    ) -> tuple[_Select, Projection] | None:
        """
        On a store that aggregates no `CASE` holding a subquery (`aggregates_case_subqueries`), the rows the select
        reads with each aggregate's argument that is such a `CASE` worked out first, in a `WITH *` of its own, and the
        projection with the aggregate of that argument read from there: Oracle (23.26) fails
        `collect(CASE WHEN … THEN [r] ELSE [m.title] END)` with ORA-00979, or closes the session (ORA-03113). None where
        there is none.
        """
        if self.aggregates_case_subqueries:
            return None
        arguments: dict[Expression, None] = {}
        # Those of a collect whose list is read, not only counted by size(): see below.
        gathered: dict[Expression, None] = {}
        for term in [*(it.expression for it in items), *(it.expression for it in projection.order_by)]:
            sized: list[Expression] = []

            def found(e: Expression, sized: list[Expression] = sized) -> bool:
                if isinstance(e, Function) and e.name.lower() == "size":
                    sized.extend(e.arguments)
                if is_aggregate(e) and isinstance(e, Function):
                    for argument in e.arguments:
                        if not contains(argument, lambda it: isinstance(it, Case)):
                            continue
                        sql = self._unshared(
                            lambda argument=argument: self.json(self._expression(argument, select.scope)).sql
                        )
                        if "SELECT" not in sql.upper():
                            continue
                        arguments[argument] = None
                        if e.name.lower() == "collect" and not any(it is e for it in sized):
                            gathered[argument] = None
                return False

            contains(term, found)
        if not arguments:
            return None
        # Gathered by collect, a list of nodes or relationships beside a value of another kind, or the null of a CASE
        # with no ELSE, closes Oracle's (23.26) session (ORA-03113) wherever the CASE is worked out: refused, as Oracle
        # gave an error of its own for it (ORA-00979) before. Counted, by count() or the size() of the collect, it is
        # read.
        for argument in gathered:

            def refused(e: Expression) -> bool:
                if isinstance(e, Case):
                    results = [it[1] for it in e.alternatives] + _present(e.otherwise)
                    kinds = [
                        isinstance(self._unshared(lambda it=it: self._expression(it, select.scope)), Entities)
                        for it in results
                    ] + ([False] if e.otherwise is None else [])
                    if any(kinds) and not all(kinds):
                        unsupported(
                            "On Oracle, an aggregate of a CASE that gives a list of nodes or relationships beside a "
                            "value of another kind"
                        )
                return False

            contains(argument, refused)
        # Named as no variable is, unless quoted so.
        names: dict[Expression, str] = {}
        for argument in arguments:
            self._counter += 1
            names[argument] = f"#case{self._counter}"
        with_ = Projection(
            False, True, tuple(ProjectionItem(argument, name) for argument, name in names.items()), (), None, None
        )

        def cased(expression: Expression) -> Expression:
            return replaced(expression, lambda it: Variable(names[it]) if it in names else None)

        kept = dataclasses.replace(
            projection,
            star=False,
            items=tuple(dataclasses.replace(it, expression=cased(it.expression)) for it in items),
            order_by=tuple(dataclasses.replace(it, expression=cased(it.expression)) for it in projection.order_by),
        )
        # Kept apart, or Oracle merges it back into the query that aggregates.
        return self._with(select, With(with_, None), apart=True), kept

    def _aggregates_first(
        self, select: _Select, projection: Projection, items: list[ProjectionItem]
    ) -> tuple[_Select, Projection] | None:
        """
        Where a projection holds `collect(DISTINCT …)`, or reads an aggregate item by item (a comprehension, a
        quantifier or a reduce over `collect(…)`, `IN collect(…)`, a slice of it…), the rows the select reads grouped
        first, in a `WITH` of their own: each item that does not aggregate a key of its own; each variable an
        aggregating item names beside its aggregates, in a body, a pattern or a subquery it holds too, and each key an
        aggregating order term names; and each aggregate, the items' and the order terms', `collect(DISTINCT …)`
        collected without its `DISTINCT`. Then the projection, over those, keeps each item of such a collection once,
        where it is first, as a graph engine does: by its id for a node or a relationship, not its properties. An order
        term, or a part of one, that is an item is read by the item's name. A store dedupes no aggregate in the query
        that makes it, and Oracle's `LISTAGG`, which does, gives at most 4,000 bytes; and Oracle reads no aggregate of
        the query it is in from a subquery, which reading a list item by item is. None where the projection does
        neither.
        """
        terms = [*(it.expression for it in items), *(it.expression for it in projection.order_by)]
        if not any(contains(it, _collects_distinct) or contains(it, _iterates_aggregate) for it in terms):
            return None
        # Named as no variable is, unless quoted so.
        keys: dict[Expression, str] = {}
        for item in items:
            if not contains(item.expression, is_aggregate):
                self._counter += 1
                keys[item.expression] = f"#group{self._counter}"
        aggregates: dict[Expression, str] = {}

        def found(expression: Expression) -> bool:
            if is_aggregate(expression) and expression not in aggregates:
                self._counter += 1
                aggregates[expression] = f"#aggregate{self._counter}"
            return False

        for term in terms:
            contains(term, found)
        keyed = [it.name for it in keys if isinstance(it, Variable)]

        # A comprehension's, a quantifier's or a reduce's body is kept as it is, so a variable it names outside its own
        # is kept too, and so is a pattern's or a subquery's. A subquery may name any variable: each the projection
        # groups by is kept.
        def named(expression: Expression) -> list[str]:
            if is_aggregate(expression):
                return []
            own: list[str] = []
            if isinstance(expression, ListComprehension):
                own = [
                    it
                    for it in _ordered(*(named(e) for e in _present(expression.where, expression.projection)))
                    if it != expression.variable
                ]
            elif isinstance(expression, Quantifier):
                own = [
                    it for it in _ordered(*(named(e) for e in _present(expression.where))) if it != expression.variable
                ]
            elif isinstance(expression, Reduce):
                own = [
                    it for it in named(expression.expression) if it not in (expression.accumulator, expression.variable)
                ]
            elif isinstance(expression, (PatternComprehension, PatternPredicate, Exists)):
                own = variables_in(expression)
            elif isinstance(expression, Subquery):
                own = keyed
            return _ordered(variables_outside_aggregates(expression), own, *(named(it) for it in children(expression)))

        # An order term that aggregates may name a key too, by the variable the projection groups by.
        scope = select.scope
        beside = [
            it
            for it in _ordered(
                *(named(item.expression) for item in items if contains(item.expression, is_aggregate)),
                *(
                    [name for name in named(sort.expression) if name in keyed]
                    for sort in projection.order_by
                    if contains(sort.expression, is_aggregate)
                ),
            )
            if it in scope
        ]
        grouped = Projection(
            distinct=False,
            star=False,
            items=(
                *(ProjectionItem(expression, name) for expression, name in keys.items()),
                *(ProjectionItem(Variable(it), it) for it in beside),
                *(
                    ProjectionItem(
                        dataclasses.replace(aggregate, distinct=False)
                        if isinstance(aggregate, Function) and _collects_distinct(aggregate)
                        else aggregate,
                        name,
                    )
                    for aggregate, name in aggregates.items()
                ),
            ),
            order_by=(),
            skip=None,
            limit=None,
        )

        def kept(term: Expression) -> Expression | None:
            if term in keys:
                return Variable(keys[term])
            if term in aggregates:
                name = aggregates[term]
                return (
                    Function(DISTINCT_ITEMS, False, (Variable(name),)) if _collects_distinct(term) else Variable(name)
                )
            return None

        # An order term, or a part of one, that is an item is read by the item's name, as over a DISTINCT projection it
        # can only be; a variable an item is named after is that item, not the variable of that name before it.
        item_names = {it.expression: it.name for it in items}
        aliases = {it.name for it in items}

        def sorted_by(term: Expression) -> Expression | None:
            if isinstance(term, Variable) and term.name in aliases:
                return term
            return Variable(item_names[term]) if term in item_names else kept(term)

        keeping = dataclasses.replace(
            projection,
            star=False,
            items=tuple(dataclasses.replace(it, expression=replaced(it.expression, kept)) for it in items),
            order_by=tuple(
                dataclasses.replace(sort, expression=replaced(sort.expression, sorted_by))
                for sort in projection.order_by
            ),
        )
        return self._with(select, With(grouped, None)), keeping

    def _held_first(
        self, select: _Select, projection: Projection, items: list[ProjectionItem]
    ) -> tuple[_Select, Projection] | None:
        """
        Where a projection that aggregates would group by an item whose SQL holds a subquery, which Oracle groups by no
        subquery, or a parameter, each mention of which the driver binds as a marker of its own, so the item in the
        select list is not the one grouped by, the rows the select reads with each such item worked out first, in a
        `WITH *` of its own, and the projection with those items read from there, as variables a group is keyed by;
        None where there is none.
        """
        if not any(contains(it.expression, is_aggregate) for it in items):
            return None
        held = [
            item
            for item in items
            if not contains(item.expression, is_aggregate)
            and (
                contains(item.expression, lambda it: isinstance(it, Parameter))
                or any("SELECT" in key.upper() for key in _group_keys(self._expression(item.expression, select.scope)))
            )
        ]
        if not held:
            return None
        # Named as no variable is, unless quoted so.
        names: dict[Expression, str] = {}
        for item in held:
            self._counter += 1
            names[item.expression] = f"#key{self._counter}"
        with_ = Projection(
            False, True, tuple(ProjectionItem(it.expression, names[it.expression]) for it in held), (), None, None
        )

        def keyed(expression: Expression) -> Expression:
            return Variable(names[expression]) if expression in names else expression

        keyed_projection = dataclasses.replace(
            projection,
            star=False,
            items=tuple(dataclasses.replace(it, expression=keyed(it.expression)) for it in items),
            order_by=tuple(dataclasses.replace(it, expression=keyed(it.expression)) for it in projection.order_by),
        )
        return self._with(select, With(with_, None)), keyed_projection

    def _aggregating(self, expression: Expression, work: Callable[[], T]) -> T:
        """The work, with no value bound in a subquery where the expression aggregates: see `_sharing`."""
        return self._unshared(work) if contains(expression, is_aggregate) else work()

    def _unshared(self, work: Callable[[], T]) -> T:
        """The work, with no value bound in a subquery: see `_sharing`."""
        was = self._sharing
        self._sharing = False
        try:
            return work()
        finally:
            self._sharing = was

    def _count(self, expression: Expression) -> str:
        """A `SKIP` or `LIMIT`: a whole number, or a parameter bound to one."""
        if isinstance(expression, Literal):
            value = expression.value
            if isinstance(value, int) and not isinstance(value, bool):
                return str(value)
            raise DrivineError(f"SKIP and LIMIT take a whole number, not {_written(value)}.")
        if isinstance(expression, Parameter):
            return self._parameters.count(expression.name)
        unsupported("SKIP or LIMIT of an expression")

    # ----- Expressions -----

    def _expression(self, expression: Expression, scope: Scope) -> Value:
        if isinstance(expression, Literal):
            return Json(self.literal_json(expression.value), text=isinstance(expression.value, str))
        if isinstance(expression, Parameter):
            if self._given_parameters is not None and expression.name in self._given_parameters:
                return self._given_parameters[expression.name]
            return Json(
                self._parameters.json(expression.name),
                temporal=Temporal.of_value(self._parameters.value(expression.name)),
            )
        if isinstance(expression, Variable):
            if expression.name not in scope:
                raise DrivineError(
                    f"{expression.name} is not defined: a variable is bound by a pattern, a WITH or an UNWIND before "
                    "it is used."
                )
            return scope[expression.name]
        if isinstance(expression, Property):
            in_parameter = self._in_parameter(expression)
            if in_parameter is not None:
                return Json(in_parameter)
            return self._property(self._expression(expression.subject, scope), expression.key)
        if isinstance(expression, HasLabels):
            subject = self._expression(expression.subject, scope)
            if isinstance(subject, Node):
                return Bool(f"({self.has_labels(subject.labels, expression.labels)})")
            raise DrivineError("A label is tested of a node.")
        if isinstance(expression, ListLiteral):
            return self._list(expression, scope)
        if isinstance(expression, MapLiteral):
            entries = [(key, self._expression(value, scope)) for key, value in expression.entries]
            keys = [key for key, _ in entries]
            return Json(
                self.json_object([(key, self.json(value).sql) for key, value in entries]),
                ordered=True,
                identity=self._identity_holding(
                    [value for _, value in entries], lambda ids: self.json_object(list(zip(keys, ids, strict=True)))
                ),
            )
        if isinstance(expression, MapProjection):
            return self._map_projection(expression, scope)
        if isinstance(expression, Binary):
            return self._binary(expression, scope)
        if isinstance(expression, Unary):
            return self._unary(expression, scope)
        if isinstance(expression, Index):
            return self._index(expression, scope)
        if isinstance(expression, Slice):
            return self._slice(expression, scope)
        if isinstance(expression, Function):
            return self._function(expression, scope)
        if isinstance(expression, CountStar):
            return Json(self.scalar("count(*)"))
        if isinstance(expression, Case):
            return self._case(expression, scope)
        if isinstance(expression, ListComprehension):
            return self._list_comprehension(expression, scope)
        if isinstance(expression, Reduce):
            return self._reduce(expression, scope)
        if isinstance(expression, PatternComprehension):
            return self._pattern_comprehension(expression, scope)
        if isinstance(expression, PatternPredicate):
            return self._exists([expression.pattern], None, scope)
        if isinstance(expression, Exists):
            return self._exists(expression.patterns, expression.where, scope)
        if isinstance(expression, Subquery):
            if expression.kind is SubqueryKind.EXISTS:
                return Bool(f"EXISTS (\n{indent(self._correlated(expression.query, scope, 'EXISTS { … }'))}\n)")
            counted = self._correlated(expression.query, scope, "COUNT { … }")
            return Json(self.scalar(f"(SELECT count(*) FROM (\n{indent(counted)}\n) {self._alias('q')})"))
        return self._quantifier(expression, scope)

    def _list(self, expression: ListLiteral, scope: Scope) -> Value:
        items = [self._expression(it, scope) for it in expression.items]
        # A null written among nodes or relationships is an item of a list of them, as a node that is null is.
        entities = [item for written, item in zip(expression.items, items, strict=True) if written != Literal(None)]

        def ids(relationships: bool) -> Entities:
            listed: list[str] = []
            for item in items:
                if isinstance(item, Node):
                    listed.append(self.scalar(item.nid))
                elif isinstance(item, Relationship):
                    listed.append(self.scalar(item.eid))
                else:
                    listed.append(self.json(item).sql)
            return Entities(self.comparable(Json(self.json_array(listed), ordered=True)), relationships)

        if entities and all(isinstance(it, Node) for it in entities):
            return ids(relationships=False)
        if entities and all(isinstance(it, Relationship) for it in entities):
            return ids(relationships=True)
        return Json(
            self.json_array([self.json(it).sql for it in items]),
            ordered=True,
            items=_kind_of(list(zip(expression.items, items, strict=True))),
            identity=self._identity_holding(items, self.json_array),
            list=True,
            item_lists=bool(entities) and all(_is_list(it) for it in entities),
            texts=all(_is_text(it) for it in items),
        )

    def _known(self, index: Expression) -> Any:
        """An index written, or bound to a parameter, is known: a text is the property of that name, a number an
        item."""
        if isinstance(index, Literal):
            return index.value
        if isinstance(index, Parameter):
            return self._parameters.value(index.name)
        if isinstance(index, Unary):
            # `-1` is a negated number.
            operand = index.operand
            if index.operator is UnaryOperator.NEGATE and isinstance(operand, Literal) and _is_number(operand.value):
                return operand.value
        return None

    def _in_parameter(self, value: Property | Index) -> str | None:
        """
        A value in a parameter that is a map, `$p.text`, `$p.a.b` or `$p['a'].b`, each key a property or an index known
        as a text, read at once from the text the parameter is bound as, where the store reads it so (see
        `Parameters.json_at`); None otherwise. Oracle crashed the session where a statement took a parameter past 32 KB
        as JSON, then the value from it, for each of two rows (#145).
        """
        keys: list[str] = []
        subject: Expression = value
        while True:
            if isinstance(subject, Property):
                keys.insert(0, subject.key)
            elif isinstance(subject, Index):
                known = self._known(subject.index)
                if not isinstance(known, str):
                    break
                keys.insert(0, known)
            else:
                break
            subject = subject.subject
        if not isinstance(subject, Parameter) or not keys:
            return None
        if self._given_parameters is not None and subject.name in self._given_parameters:
            return None
        return self._parameters.json_at(subject.name, keys)

    def _index(self, expression: Index, scope: Scope) -> Value:
        index = expression.index
        listed: list[Value] = []

        def subject_value() -> Value:
            if not listed:
                listed.append(self._expression(expression.subject, scope))
            return listed[0]

        def subject() -> str:
            return self.comparable(self.json(subject_value()))

        at = self.comparable(self.json(self._expression(index, scope)))
        known = self._known(index)
        if isinstance(known, str):
            in_parameter = self._in_parameter(expression)
            if in_parameter is not None:
                return Json(in_parameter)
            return self._property(self._expression(expression.subject, scope), known)
        # An item of a list of nodes or relationships is the node or the relationship, so it is compared by its id; of
        # one collect gathers in this query, its properties, for a store reads no aggregate from a subquery. A pattern
        # comprehension gathers its properties too, but in a subquery of its own, so its item is read by id.
        entities = subject_value()
        if isinstance(entities, Entities) and not contains(expression.subject, is_aggregate):
            return self._entity_at(entities, at)
        # An item of a list that holds a node or a relationship holds it by identity too: see Json.identity.
        if _is_number(known):
            element = self.element(subject(), at)
            return Json(
                element,
                temporal=_items_of(subject_value()),
                identity=self._identity_taken(subject_value(), element, True, lambda it: self.element(it, at)),
                list=_holds_lists(subject_value()),
                text=_holds_texts(subject_value()),
            )

        def item(list_: str) -> str:
            def read(values: list[str]) -> str:
                map_, key = values
                # A store that reads no key an expression gives reads a list's item alone.
                keyed = self.keyed(map_, key)
                by_key = f" WHEN 'string' THEN {keyed}" if keyed is not None else ""
                return f"CASE {self.type_of(key)} WHEN 'number' THEN {self.element(map_, key)}{by_key} END"

            return self.shared([list_, at], read)

        taken = item(subject())
        return Json(
            taken,
            temporal=_items_of(subject_value()),
            identity=self._identity_taken(subject_value(), taken, True, item),
            list=_holds_lists(subject_value()),
            text=_holds_texts(subject_value()),
        )

    def _entity_at(self, entities: Entities, at: str) -> Value:
        """
        The node or the relationship at `at` in the list: its id read from the list, and its columns from its row, so
        it is compared by its id as a node bound by a pattern is. Null where the list has no such item.
        """
        id_ = self.id_of(self.element(entities.ids, at), None)
        row = self._alias("e" if entities.relationships else "n")
        table, key = (self.edges, "eid") if entities.relationships else (self.nodes, "nid")

        def column(name: str) -> str:
            return f"(SELECT {row}.{name} FROM {table} {row} WHERE {row}.{key} = {id_})"

        if entities.relationships:
            return Relationship(column("eid"), column("src"), column("dst"), column("type"), column("props"))
        return Node(column("nid"), column("labels"), column("props"), column("stamp"))

    def _entity_items(self, ids: str, relationships: bool, name: str) -> tuple[str, Value]:
        """
        The rows of the list of ids, as a `FROM` names them under `name`, each joined to the row of its node or, of
        `relationships`, its relationship; and the item each row is, that node or relationship.
        """
        row = self._alias("e" if relationships else "n")
        if relationships:
            return (
                f"FROM {self.elements(ids, name)} LEFT JOIN {self.edges} {row} "
                f"ON {row}.eid = {self.id_of(f'{name}.item', None)}",
                _relationship(row),
            )
        return (
            f"FROM {self.elements(ids, name)} LEFT JOIN {self.nodes} {row} "
            f"ON {row}.nid = {self.id_of(f'{name}.item', None)}",
            _node(row),
        )

    def _entity_comprehension(self, expression: ListComprehension, listed: Entities, scope: Scope) -> Value:
        """
        A list comprehension over a list of nodes or relationships: each item is the node or the relationship, and what
        it keeps of them, with no projection or one of nodes or relationships, is a list of them.
        """
        name = self._alias("x")
        kinds: list[Value] = []

        def comprehended(ids: str) -> str:
            from_, item = self._entity_items(ids, listed.relationships, name)
            inner = {**scope, expression.variable: item}
            projected = self._expression(expression.projection, inner) if expression.projection is not None else item
            kinds.append(projected)
            filter_ = (
                f" WHERE {self.bool(self._expression(expression.where, inner))}" if expression.where is not None else ""
            )
            if isinstance(projected, Node):
                value = self.scalar(projected.nid)
            elif isinstance(projected, Relationship):
                value = self.scalar(projected.eid)
            else:
                value = self.json(projected).sql
            # Of null, null, as in Cypher, and not an empty list.
            return (
                f"CASE WHEN {ids} IS NOT NULL THEN "
                f"(SELECT {self.aggregate_list(value, f'{name}.item_no')} {from_}{filter_}) END"
            )

        list_ = self.shared1(listed.ids, comprehended)
        kind = kinds[0]
        if isinstance(kind, Node):
            return Entities(self.comparable(Json(list_, ordered=True)), False)
        if isinstance(kind, Relationship):
            return Entities(self.comparable(Json(list_, ordered=True)), True)
        return Json(
            list_,
            ordered=True,
            items=kind.temporal if isinstance(kind, Json) else None,
            list=True,
            item_lists=_is_list(kind),
        )

    def _list_comprehension(self, expression: ListComprehension, scope: Scope) -> Value:
        listed = self._expression(expression.list, scope)
        if isinstance(listed, Entities):
            return self._entity_comprehension(expression, listed, scope)
        list_ = self.comparable(self.json(listed))
        name = self._alias("x")
        inner = {**scope, expression.variable: Json(f"{name}.item", temporal=_items_of(listed))}
        projected = self._expression(expression.projection, inner) if expression.projection is not None else None
        projection = self.json(projected).sql if projected is not None else f"{name}.item"
        filter_ = (
            f" WHERE {self.bool(self._expression(expression.where, inner))}" if expression.where is not None else ""
        )

        # Of null, null, as in Cypher, and not an empty list.
        def comprehended(items: str) -> str:
            return (
                f"CASE WHEN NOT {self.is_json_null(items)} THEN "
                f"(SELECT {self.aggregate_list(projection, f'{name}.item_no')} "
                f"FROM {self.elements(items, name)}{filter_}) END"
            )

        if projected is not None:
            kind = projected.temporal if isinstance(projected, Json) else None
        else:
            kind = _items_of(listed)
        item_lists = _is_list(projected) if projected is not None else _holds_lists(listed)
        return Json(self.shared1(list_, comprehended), ordered=True, items=kind, list=True, item_lists=item_lists)

    def _reduce(self, expression: Reduce, scope: Scope) -> Value:
        initial = self.comparable(self.json(self._expression(expression.initial, scope)))
        listed = self._expression(expression.list, scope)
        list_ = self.comparable(self.json(listed))
        own = {expression.accumulator, expression.variable}
        # What the step reads of the row and of the parameters, for a store that works it out where it reads neither.
        read = sorted(it for it in set(variables_in(expression.expression)) - own if it in scope)
        given = sorted(parameters_in(expression.expression))

        def context() -> str:
            # A path is no JSON a function may be given.
            if any(isinstance(scope[it], Path) for it in read):
                raise DrivineError(
                    "A path read in the step of reduce() does not run on Oracle: its function is given the row as JSON."
                )
            entries = [(it, self.json(scope[it]).sql) for it in read] + [
                (f"${it}", self._parameters.json(it)) for it in given
            ]
            return self.comparable(Json(self.json_object(entries), ordered=True))

        def step(accumulator: str, item: str) -> str:
            inner = {
                **scope,
                expression.accumulator: Json(accumulator),
                expression.variable: Json(item, temporal=_items_of(listed)),
            }
            return self.comparable(self.json(self._expression(expression.expression, inner)))

        def context_step(accumulator: str, item: str, map_: str) -> str:
            # A value read from the map is of the kind the statement made it.
            inner: dict[str, Value] = {}
            for it in read:
                value = scope[it]
                was_json = value if isinstance(value, Json) else None
                inner[it] = Json(
                    self.property_of(map_, it),
                    temporal=was_json.temporal if was_json else None,
                    items=was_json.items if was_json else None,
                )
            inner[expression.accumulator] = Json(accumulator)
            inner[expression.variable] = Json(item, temporal=_items_of(listed))
            was = self._given_parameters
            self._given_parameters = {
                it: Json(self.property_of(map_, f"${it}"), temporal=Temporal.of_value(self._parameters.value(it)))
                for it in given
            }
            try:
                return self.comparable(self.json(self._expression(expression.expression, inner)))
            finally:
                self._given_parameters = was

        return Json(self.reduce(self._alias("f"), initial, list_, step, context, context_step))

    def _pattern_comprehension(self, expression: PatternComprehension, scope: Scope) -> Value:
        inner = _Select(scope)
        self._patterns(inner, [expression.pattern])
        if expression.where is not None:
            inner.where.append(self.bool(self._expression(expression.where, inner.scope)))
        projected = self._expression(expression.projection, inner.scope)

        def gathered(value: str) -> str:
            return (
                "("
                + " ".join([f"SELECT {self.aggregate_list(value)}", *inner.from_(), *_present(inner.where_clause())])
                + ")"
            )

        # A comprehension of nodes or relationships is a list of them, so a write to an item of it reaches them.
        if isinstance(projected, Node):
            ids = self.comparable(Json(gathered(self.scalar(projected.nid)), ordered=True))
            return Entities(ids, False, gathered(self.json(projected).sql))
        if isinstance(projected, Relationship):
            ids = self.comparable(Json(gathered(self.scalar(projected.eid)), ordered=True))
            return Entities(ids, True, gathered(self.json(projected).sql))
        kind = projected.temporal if isinstance(projected, Json) else None
        return Json(
            gathered(self.json(projected).sql), ordered=True, items=kind, list=True, item_lists=_is_list(projected)
        )

    def _quantifier(self, expression: Quantifier, scope: Scope) -> Value:
        listed = self._expression(expression.list, scope)
        name = self._alias("q")

        def quantified(from_: str, item: Value) -> str:
            inner = {**scope, expression.variable: item}
            condition = self.bool(self._expression(expression.where, inner)) if expression.where is not None else "TRUE"
            if expression.kind is QuantifierKind.ANY:
                return f"EXISTS (SELECT 1 {from_} WHERE {condition})"
            if expression.kind is QuantifierKind.ALL:
                return f"NOT EXISTS (SELECT 1 {from_} WHERE NOT ({condition}))"
            if expression.kind is QuantifierKind.NONE:
                return f"NOT EXISTS (SELECT 1 {from_} WHERE {condition})"
            return f"(SELECT count(*) {from_} WHERE {condition}) = 1"

        # Over a list of nodes or relationships, each item is the node or the relationship, read from its table.
        if isinstance(listed, Entities):
            entities = listed
            return Bool(
                self.shared1(
                    entities.ids, lambda ids: quantified(*self._entity_items(ids, entities.relationships, name))
                )
            )
        item = Json(f"{name}.item", temporal=_items_of(listed))
        return Bool(quantified(f"FROM {self.elements(self.comparable(self.json(listed)), name)}", item))

    def _slice(self, slice_: Slice, scope: Scope) -> Value:
        """
        `list[from..to]`: the items from the one at `from` up to the one before `to`, each counted from the end where it
        is negative, a bound left out the list's end; null where the list or a bound given is. A slice of nodes or
        relationships is a list of them.
        """
        listed = self._expression(slice_.subject, scope)
        bounds = [self.comparable(self.json(self._expression(it, scope))) for it in _present(slice_.from_, slice_.to)]
        list_ = listed.ids if isinstance(listed, Entities) else self.comparable(self.json(listed))

        def sliced(subject: str) -> str:
            def body(values: list[str]) -> str:
                x = values[0]
                rest = iter(values[1:])
                from_ = next(rest) if slice_.from_ is not None else None
                to = next(rest) if slice_.to is not None else None
                e = self._alias("e")
                v = self._alias("v")

                # A bound counted from the end where it is negative, as an index is: past either end, the end it is
                # past.
                def bound(json: str) -> str:
                    n = self.number(json)
                    return f"CASE WHEN {n} < 0 THEN {v}.cnt + {n} ELSE {n} END"

                within = [
                    *([f"{v}.item_no > {bound(from_)}"] if from_ is not None else []),
                    *([f"{v}.item_no <= {bound(to)}"] if to is not None else []),
                ]
                rows = (
                    f"SELECT {e}.item AS item, {e}.item_no AS item_no, COUNT(*) OVER () AS cnt "
                    f"FROM {self.elements(x, e)}"
                )
                gathered = self.comparable(Json(self.aggregate_list(f"{v}.item", f"{v}.item_no"), ordered=True))
                where = f" WHERE {' AND '.join(within)}" if within else ""
                query = f"SELECT {gathered} FROM ({rows}) {v}{where}"
                present = " AND ".join(
                    [f"{self.type_of(x)} = 'array'", *(f"NOT {self.is_json_null(it)}" for it in _present(from_, to))]
                )
                return f"CASE WHEN {present} THEN ({query}) END"

            return self.shared([subject, *bounds], body)

        # A slice of a list that holds a node or a relationship holds it by identity too: see Json.identity.
        if isinstance(listed, Entities):
            return Entities(sliced(list_), listed.relationships)
        taken = sliced(list_)
        return Json(
            taken,
            items=_items_of(listed),
            identity=self._identity_taken(listed, taken, False, sliced),
            list=True,
            item_lists=_holds_lists(listed),
            texts=_holds_texts(listed),
        )

    def _exists(self, patterns: Sequence[Pattern], where: Expression | None, scope: Scope) -> Bool:
        inner = _Select(scope)
        self._patterns(inner, patterns)
        if where is not None:
            inner.where.append(self.bool(self._expression(where, inner.scope)))
        return Bool("EXISTS (" + " ".join(["SELECT 1", *inner.from_(), *_present(inner.where_clause())]) + ")")

    def _property(self, subject: Value, key: str) -> Value:
        if isinstance(subject, Entities):
            raise DrivineError("A list has no properties.")
        if isinstance(subject, Node):
            return Json(self.scalar(subject.stamp) if key == stamps.PROPERTY else self.property_of(subject.props, key))
        if isinstance(subject, Relationship):
            return Json(self.property_of(subject.props, key))
        if isinstance(subject, Json):
            field_ = self._temporal_field(subject, key)
            if field_ is not None:
                return field_
            taken = self.property_of(self.comparable(subject), key)
            return Json(
                taken, identity=self._identity_taken(subject, taken, True, lambda it: self.property_of(it, key))
            )
        if isinstance(subject, Bool):
            raise DrivineError("A truth value has no properties.")
        raise DrivineError("A path has no properties.")

    def _temporal_field(self, subject: Json, key: str) -> Value | None:
        """
        A field of a temporal value, `d.year` or `dur.days`: of the kind the statement makes it, or of the kind a text's
        shape tells, where a map's is its entry of that name; None for a name no temporal value has.
        """
        if key not in FIELD_NAMES:
            return None
        kind = subject.temporal
        if kind is not None:
            found = self.temporals.field(self.text(self.comparable(subject)), kind, key)
            if found is None:
                return Json(self.literal_json(None))
            return Json(self.scalar(found.sql))

        def read(x: str) -> str:
            kinds = tuple(it for it in Temporal if self.temporals.field("NULL", it, key) is not None)
            by_kind = self.temporals.by_kind(
                self.text(x), lambda k, t: self.scalar(_field_sql(self.temporals.field(t, k, key))), kinds
            )
            return f"CASE WHEN {self.type_of(x)} = 'string' THEN {by_kind} ELSE {self.property_of(x, key)} END"

        return Json(self.shared1(self.comparable(subject), read))

    def _temporal_binary(self, operator: BinaryOperator, left: Value, right: Value) -> Value | None:
        """
        Temporal arithmetic and comparison, where the statement makes either side of a temporal kind: a moment and a
        duration added or taken away, two durations added, a duration scaled, two moments compared as the moments they
        are. None where neither side is of a temporal kind, or the operator is no such one.
        """
        l_ = left.temporal if isinstance(left, Json) else None
        r = right.temporal if isinstance(right, Json) else None
        if l_ is None and r is None:
            return None
        json_a, json_b = self.comparable(self.json(left)), self.comparable(self.json(right))
        a = self.text(json_a)
        b = self.text(json_b)
        t = self.temporals

        def of(text: str, kind: Temporal | None) -> Json:
            return Json(self.scalar(text), temporal=kind)

        duration = Temporal.DURATION
        if operator in (BinaryOperator.ADD, BinaryOperator.SUBTRACT):
            sign = 1 if operator is BinaryOperator.ADD else -1

            # A side read from the store: the temporal of the two texts, null where neither is of a kind it reads; then,
            # in an addition, the two added as any two values are, as Cypher adds a temporal value to a list or to a
            # text.
            def read(kind: Temporal | None, temporal: Callable[[str, str], str]) -> Json:
                def both(values: list[str]) -> str:
                    x, y = values
                    value = self.scalar(self.shared([self.text(x), self.text(y)], lambda it: temporal(it[0], it[1])))
                    return f"COALESCE({value}, {self.add(x, y)})" if sign > 0 else value

                return Json(self.shared([json_a, json_b], both), temporal=kind)

            if l_ is duration and r is duration:
                return of(t.sum(a, b, sign), duration)
            if l_ is not None and l_.instant and r is duration:
                return of(t.add(a, l_, b, sign), l_)
            if l_ is duration and r is not None and r.instant and sign > 0:
                return of(t.add(b, r, a, 1), r)
            # A value read from the store, its kind told by its text, and a duration the statement gives.
            if l_ is None and r is duration:
                return read(
                    None,
                    lambda x, y: (
                        f"CASE WHEN {t.is_kind(x, duration)} THEN {t.sum(x, y, sign)} "
                        f"ELSE {t.add_to_any(x, y, sign)} END"
                    ),
                )
            if l_ is duration and r is None and sign > 0:
                return read(
                    None,
                    lambda x, y: (
                        f"CASE WHEN {t.is_kind(y, duration)} THEN {t.sum(x, y, 1)} ELSE {t.add_to_any(y, x, 1)} END"
                    ),
                )
            # A duration read from the store, taken from a duration or added to a moment the statement gives. A sum is
            # of no kind, for what is read may be a list or a text, which the moment is added to as any value is.
            if l_ is duration and r is None:
                return read(duration, lambda x, y: f"CASE WHEN {t.is_kind(y, duration)} THEN {t.sum(x, y, sign)} END")
            if l_ is not None and l_.instant and r is None:
                instant = l_
                return read(
                    instant if sign < 0 else None,
                    lambda x, y: f"CASE WHEN {t.is_kind(y, duration)} THEN {t.add(x, instant, y, sign)} END",
                )
            if l_ is None and r is not None and r.instant and sign > 0:
                instant = r
                return read(None, lambda x, y: f"CASE WHEN {t.is_kind(x, duration)} THEN {t.add(y, instant, x, 1)} END")
            return None
        if operator is BinaryOperator.MULTIPLY:
            if l_ is duration:
                return of(t.times(a, self.number(self.comparable(self.json(right)))), duration)
            if r is duration:
                return of(t.times(b, self.number(self.comparable(self.json(left)))), duration)
            return None
        if operator is BinaryOperator.DIVIDE:
            if l_ is duration:
                return of(t.times(a, f"(1.0 / {self.number(self.comparable(self.json(right)))})"), duration)
            return None
        if operator in _COMPARED:
            kind = l_ if l_ is not None else r
            assert kind is not None
            return Bool(t.compare(a, b, kind, _COMPARED[operator]))
        return None

    def _temporal(self, kind: Temporal, function: Function, arguments: list[Value], scope: Scope) -> Value:
        """A temporal value of the kind made by its function of the function's arguments: the moment, a text, or a map
        of its fields."""

        def of(text: str) -> Json:
            return Json(self.scalar(text), temporal=kind)

        if not arguments:
            if kind is Temporal.DURATION:
                raise DrivineError("duration() takes a text or a map of its fields.")
            return of(self.temporals.now(kind))
        if len(arguments) != 1:
            raise DrivineError(f"{function.name}() takes a text or a map of its fields.")
        written = function.arguments[0]
        # A text the statement writes is worked out here, as the store would work it out.
        if isinstance(written, Literal) and isinstance(written.value, str):
            text: str = written.value
            try:
                worked = canonical(text, kind)
            except NoNumber as error:
                raise DrivineError(f"'{text}' is no {function.name}.") from error
            except ValueError as error:
                raise DrivineError(f"'{text}' is no {function.name}: {error}") from error
            if worked is None:
                raise DrivineError(f"'{text}' is no {function.name}.")
            return Json(self.literal_json(worked), temporal=kind)
        if isinstance(written, MapLiteral):
            fields = {key: self._expression(value, scope) for key, value in written.entries}
            known = DURATION_MAP_FIELDS if kind is Temporal.DURATION else INSTANT_MAP_FIELDS
            unknown = next((it for it in fields if it not in known), None)
            if unknown is not None:
                unsupported(f"{function.name}() of a map with {unknown}")
            numbers = {
                key: self.number(self.comparable(self.json(value)))
                for key, value in fields.items()
                if key != "timezone"
            }
            if kind is Temporal.DURATION:
                return of(self.temporals.duration(numbers))
            timezone = fields.get("timezone")
            zone = self.text(self.comparable(self.json(timezone))) if timezone is not None else None
            return of(self.temporals.of(numbers, zone, kind))
        given = arguments[0]
        if not isinstance(given, Json):
            raise DrivineError(f"{function.name}() takes a text or a map of its fields.")
        text = self.text(self.comparable(given))
        if kind is Temporal.DURATION:
            return of(self.temporals.canonical_duration(text))
        if given.temporal is not None and given.temporal.instant:
            return of(self.temporals.convert(text, given.temporal, kind))
        return of(self.temporals.convert_any(text, kind))

    def _map_projection(self, projection: MapProjection, scope: Scope) -> Value:
        if projection.variable not in scope:
            raise DrivineError(f"{projection.variable} is not defined.")
        subject = scope[projection.variable]
        values: list[tuple[str, Value]] = []
        for element in projection.elements:
            if isinstance(element, Selected):
                values.append((element.key, self._property(subject, element.key)))
            elif isinstance(element, Entry):
                values.append((element.key, self._expression(element.value, scope)))
            elif isinstance(element, VariableElement):
                if element.name not in scope:
                    raise DrivineError(f"{element.name} is not defined.")
                values.append((element.name, scope[element.name]))
        entries = [(key, self.json(value).sql) for key, value in values]

        # A projection of null is null, as in Cypher, and not a map of nulls.
        def present(map_: str) -> str:
            return f"CASE WHEN NOT ({self._is_null(subject)}) THEN {map_} END"

        if not any(isinstance(it, AllProperties) for it in projection.elements):
            keys = [key for key, _ in values]
            identity = self._identity_holding(
                [value for _, value in values],
                lambda ids: present(self.json_object(list(zip(keys, ids, strict=True)))),
            )
            return Json(present(self.json_object(entries)), ordered=True, identity=identity)
        # Every property, then the entries over them.
        return Json(present(self.with_entries(self.comparable(self.json(subject)), entries)))

    def _compared(self, left: Value, right: Value, binary: Binary) -> tuple[Value, Value]:
        """
        The sides of the binary, as compared: where one holds a node or a relationship by identity and the other may
        hold one without it (an item taken from a list, a collect, a variable), both by their properties, as before
        identities: only a value the statement writes out or is given, all the way down (a literal, a parameter, a map,
        a list or a map projection of such, a `CASE` that gives one), holds none where it carries no identity, so
        `[{x: r}][0] = {x: r}` is true, and `{x: r} = {x: {}}` false.
        """

        # Written all the way down: `{a: ms[0]}` and `n {.*, x: r}` hold what may be a node without its identity.
        def written(expression: Expression) -> bool:
            if isinstance(expression, (Literal, Parameter)):
                return True
            if isinstance(expression, MapLiteral):
                return all(written(value) for _, value in expression.entries)
            if isinstance(expression, ListLiteral):
                return all(written(it) for it in expression.items)
            if isinstance(expression, MapProjection):
                return all(
                    isinstance(it, Selected) or (isinstance(it, Entry) and written(it.value))
                    for it in expression.elements
                )
            if isinstance(expression, Case):
                return all(
                    written(it) for it in [*(it[1] for it in expression.alternatives), *_present(expression.otherwise)]
                )
            return False

        def held(value: Value) -> bool:
            return isinstance(value, Json) and value.identity is not None

        def plain(value: Value) -> Value:
            return dataclasses.replace(value, identity=None) if isinstance(value, Json) else value

        if held(left) and isinstance(right, Json) and not held(right) and not written(binary.right):
            return plain(left), right
        if held(right) and isinstance(left, Json) and not held(left) and not written(binary.left):
            return left, plain(right)
        return left, right

    def _binary(self, binary: Binary, scope: Scope) -> Value:
        operator = binary.operator
        if operator in (BinaryOperator.AND, BinaryOperator.OR, BinaryOperator.XOR):
            left = self.bool(self._expression(binary.left, scope))
            right = self.bool(self._expression(binary.right, scope))
            if operator is BinaryOperator.AND:
                return Bool(f"({left} AND {right})")
            if operator is BinaryOperator.OR:
                return Bool(f"({left} OR {right})")
            # Each side whole: a condition may be several, and `a = b <> c` is no SQL.
            return Bool(f"(({left}) <> ({right}))")
        left_value, right_value = self._compared(
            self._expression(binary.left, scope), self._expression(binary.right, scope), binary
        )
        temporal = self._temporal_binary(operator, left_value, right_value)
        if temporal is not None:
            return temporal
        a = self.comparable(self.json(left_value))
        b = self.comparable(self.json(right_value))
        if operator is BinaryOperator.EQUALS:
            return Bool(self._equality(left_value, right_value, binary.right))
        if operator is BinaryOperator.NOT_EQUALS:
            return Bool(f"NOT ({self._equality(left_value, right_value, binary.right)})")
        if operator in _COMPARED:
            # Values of two kinds are not ordered, as in Cypher: the comparison is null.
            sql = _COMPARED[operator]

            def ordered(values: list[str]) -> str:
                x, y = values
                return (
                    f"CASE WHEN {self.type_of(x)} = {self.type_of(y)} "
                    f"AND {self.type_of(x)} IN ('number', 'string', 'boolean') "
                    f"THEN {x} {sql} {y} END"
                )

            return Bool(self.shared([a, b], ordered))
        if operator is BinaryOperator.ADD:
            # Two lists of nodes, or of relationships, are together a list of them.
            if (
                isinstance(left_value, Entities)
                and isinstance(right_value, Entities)
                and left_value.relationships == right_value.relationships
            ):
                gathered = (
                    self.shared([a, b], lambda it: self.add(it[0], it[1]))
                    if left_value.gathered is not None or right_value.gathered is not None
                    else None
                )
                ids = self.shared([left_value.ids, right_value.ids], lambda it: self.add(it[0], it[1]))
                return Entities(ids, left_value.relationships, gathered)
            # Two lists of items of one kind are, together, a list of that kind. A side the statement makes a text, not
            # given, is written as its own text; a text put together with a text, or a number given, is a text.
            items = _items_of(left_value)
            given_left, given_right = self._given(binary.left), self._given(binary.right)
            text_left = _is_text(left_value) or isinstance(given_left, str)
            text_right = _is_text(right_value) or isinstance(given_right, str)
            given = (
                MADE_TEXT if given_left is None and text_left else given_left,
                MADE_TEXT if given_right is None and text_right else given_right,
            )
            return Json(
                self.shared([a, b], lambda it: self.add(it[0], it[1], given)),
                items=items if items == _items_of(right_value) else None,
                list=_is_list(left_value) or _is_list(right_value),
                item_lists=_holds_lists(left_value) and (_holds_lists(right_value) or not _is_list(right_value)),
                text=(text_left and (text_right or _is_given_number(given_right)))
                or (text_right and _is_given_number(given_left)),
                texts=(_holds_texts(left_value) and (_holds_texts(right_value) or text_right))
                or (text_left and _holds_texts(right_value)),
            )
        if operator is BinaryOperator.SUBTRACT:
            return Json(self.shared([a, b], lambda it: self.arithmetic(it[0], it[1], "-")))
        if operator is BinaryOperator.MULTIPLY:
            return Json(self.shared([a, b], lambda it: self.arithmetic(it[0], it[1], "*")))
        if operator is BinaryOperator.DIVIDE:
            # A whole number divided by one is a whole number, as in Cypher.
            def divided(values: list[str]) -> str:
                x, y = values
                return (
                    f"CASE WHEN {self.integral(x)} AND {self.integral(y)} THEN "
                    f"{self.scalar(f'trunc({self.number(x)} / {self.number(y)})')} "
                    f"ELSE {self.arithmetic(x, y, '/')} END"
                )

            return Json(self.shared([a, b], divided))
        if operator is BinaryOperator.MODULO:
            return Json(self.scalar(self.modulo(self.number(a), self.number(b))))
        if operator is BinaryOperator.POWER:
            return Json(self.scalar(self.float_(f"power({self.number(a)}, {self.number(b)})")))
        if operator is BinaryOperator.IN:
            i = self._alias("i")
            # A node or a relationship is in a list of them where its id is.
            if isinstance(right_value, Entities):
                id_ = None
                if isinstance(left_value, Node) and not right_value.relationships:
                    id_ = self.scalar(left_value.nid)
                elif isinstance(left_value, Relationship) and right_value.relationships:
                    id_ = self.scalar(left_value.eid)
                if id_ is not None:
                    return Bool(f"EXISTS (SELECT 1 FROM {self.elements(right_value.ids, i)} WHERE {i}.item = {id_})")
            # A map or a list that holds a node or a relationship is in a list where an item holds the same, by
            # identity.
            if (isinstance(left_value, Json) and left_value.identity is not None) or (
                isinstance(right_value, Json) and right_value.identity is not None
            ):
                found = self.identity_equals(f"{i}.item", self._identified(left_value))
                return Bool(f"EXISTS (SELECT 1 FROM {self.elements(self._identified(right_value), i)} WHERE {found})")
            return Bool(f"EXISTS (SELECT 1 FROM {self.elements(b, i)} WHERE {i}.item = {a})")
        if operator is BinaryOperator.STARTS_WITH:
            return Bool(self.shared([self.text(a), self.text(b)], lambda it: self.starts_with(it[0], it[1])))
        if operator is BinaryOperator.MATCHES:
            return Bool(self.matches(self.text(a), self.text(b)))
        if operator is BinaryOperator.ENDS_WITH:
            return Bool(self.shared([self.text(a), self.text(b)], lambda it: self.ends_with(it[0], it[1])))
        return Bool(self.contains_text(self.text(a), self.text(b)))

    def _equality(self, left: Value, right: Value, written: Expression | None = None) -> str:
        """
        Whether two values are equal: as JSON, so `'7'` is not `7` and `1` is `1.0`. A node is equal to a node by its
        row. Where a property is compared with a value written, a text or a truth value the statement is given, a test
        an index answers stands beside it, where the store has one: such a value's text is equal where its JSON is, and
        a number's is not (`1` and `1.0`).
        """
        temporal = self._temporal_binary(BinaryOperator.EQUALS, left, right)
        if isinstance(temporal, Bool):
            return temporal.sql
        if isinstance(left, Node) and isinstance(right, Node):
            return f"{left.nid} = {right.nid}"
        if isinstance(left, Relationship) and isinstance(right, Relationship):
            return f"{left.eid} = {right.eid}"
        # A map or a list that holds a node or a relationship is equal where what it holds is, by identity.
        if (isinstance(left, Json) and left.identity is not None) or (
            isinstance(right, Json) and right.identity is not None
        ):
            return self.shared(
                [self._identified(left), self._identified(right)],
                lambda it: (
                    f"CASE WHEN NOT {self.is_json_null(it[0])} AND NOT {self.is_json_null(it[1])} "
                    f"THEN {self.identity_equals(it[0], it[1])} END"
                ),
            )
        # Two lists of nodes, or of relationships, are equal where their ids are, item by item.
        if isinstance(left, Entities) and isinstance(right, Entities) and left.relationships == right.relationships:
            a, b = left.ids, right.ids
        else:
            a = self.comparable(self.json(left))
            b = self.comparable(self.json(right))
        given = self._given(written) if written is not None else None
        indexed = self.indexed_equality(a, b) if isinstance(given, (str, bool)) else None
        # Null is equal to nothing, itself included: the comparison is null, as in Cypher. A value the statement is
        # given that is not null needs no test of its own, and a property that is not there is SQL's null.
        if given is not None:
            equal = f"{a} = {b}"
        else:
            equal = self.shared(
                [a, b],
                lambda it: (
                    f"CASE WHEN NOT {self.is_json_null(it[0])} AND NOT {self.is_json_null(it[1])} "
                    f"THEN {it[0]} = {it[1]} END"
                ),
            )
        return f"({indexed} AND {equal})" if indexed is not None else equal

    def _identity_of(self, value: Value) -> str | None:
        """
        The value as JSON that compares by identity: a node or a relationship as its id, under a key no property of the
        graph's has (`" node"`, `" relationship"`), a list of them as the list of their ids, and a map or a list that
        holds one as its Json.identity. None where the value holds none.
        """
        if isinstance(value, Node):
            token = self.comparable(Json(self.json_object([(" node", self.scalar(value.nid))]), ordered=True))
            return f"CASE WHEN {value.nid} IS NOT NULL THEN {token} END"
        if isinstance(value, Relationship):
            token = self.comparable(Json(self.json_object([(" relationship", self.scalar(value.eid))]), ordered=True))
            return f"CASE WHEN {value.eid} IS NOT NULL THEN {token} END"
        # Each id as the node or relationship it is, so a list of them is the same as one that holds them among other
        # values.
        if isinstance(value, Entities):
            return self.id_tokens(value.ids, " relationship" if value.relationships else " node")
        if isinstance(value, Json):
            return value.identity
        return None

    def _identity_taken(self, list_: Value, taken: str, item: bool, take: Callable[[str], str]) -> str | None:
        """
        What `take` takes of the list's identity, where it holds a node or a relationship by one: an item, a key's
        value, a slice; `taken` is what it takes of the value. Taken from a node or a relationship, a property holds
        neither, so its identity is `taken` itself. `item` says whether it takes one value, not a slice.
        """
        if not isinstance(list_, Json) or list_.identity is None:
            return None
        if not item:
            return take(list_.identity)

        def body(values: list[str]) -> str:
            i, t = values
            # A map's key, not a list's items' keys, as Oracle's lax path reads them.
            held = f"COALESCE({self.property_of(i, ' node')}, {self.property_of(i, ' relationship')})"
            return f"CASE WHEN {self.type_of(i)} = 'object' AND {held} IS NOT NULL THEN {t} ELSE {take(i)} END"

        return self.shared([list_.identity, taken], body)

    def _identified(self, value: Value) -> str:
        """The value as JSON that compares by identity where it holds a node or a relationship, and as itself where it
        does not: see `_identity_of`."""
        identity = self._identity_of(value)
        return identity if identity is not None else self.comparable(self.json(value))

    def _identity_holding(self, values: Sequence[Value], build: Callable[[list[str]], str]) -> str | None:
        """
        The identity of a map or a list of the values, built by `build` of each value's: see Json.identity. None where
        none of them holds a node or a relationship.
        """
        identities = [self._identity_of(it) for it in values]
        if all(it is None for it in identities):
            return None
        listed = [it if it is not None else self.json(value).sql for it, value in zip(identities, values, strict=True)]
        return self.comparable(Json(build(listed), ordered=True))

    def _given(self, expression: Expression) -> Any:
        """The value the expression is where the statement gives it: written, or bound to a parameter or a parameter's
        key; None where it is not given, or is null."""
        if isinstance(expression, Literal):
            return expression.value
        if isinstance(expression, Parameter):
            return self._parameters.value(expression.name)
        if isinstance(expression, Property) and isinstance(expression.subject, Parameter):
            value = self._parameters.value(expression.subject.name)
            return cast(Mapping[str, Any], value).get(expression.key) if isinstance(value, Mapping) else None
        return None

    def _unary(self, unary: Unary, scope: Scope) -> Value:
        operand = self._expression(unary.operand, scope)
        if unary.operator is UnaryOperator.NOT:
            return Bool(f"(NOT ({self.bool(operand)}))")
        if unary.operator is UnaryOperator.NEGATE:
            # Taken from 0, so a floating-point number stays one where the store keeps the kind apart.
            return Json(
                self.shared1(
                    self.comparable(self.json(operand)), lambda x: self.arithmetic(self.literal_json(0), x, "-")
                )
            )
        if unary.operator is UnaryOperator.IS_NULL:
            return Bool(f"({self._is_null(operand)})")
        return Bool(f"(NOT {self._is_null(operand)})")

    def _is_null(self, value: Value) -> str:
        if isinstance(value, Entities):
            return f"{value.ids} IS NULL"
        if isinstance(value, Node):
            return f"{value.nid} IS NULL"
        if isinstance(value, Relationship):
            return f"{value.eid} IS NULL"
        if isinstance(value, Bool):
            return f"({value.sql}) IS NULL"
        if isinstance(value, Path):
            return f"{value.nids} IS NULL"
        # A property stored as JSON's null is no property, as on a graph engine.
        return self.is_json_null(self.comparable(value))

    def _case(self, case: Case, scope: Scope) -> Value:
        subject = self._expression(case.subject, scope) if case.subject is not None else None
        written = [it[1] for it in case.alternatives] + ([case.otherwise] if case.otherwise is not None else [])
        results = [(it, self._expression(it, scope)) for it in written]
        conditions: list[str] = []
        for key, _ in case.alternatives:
            if subject is not None:
                conditions.append(self._equality(subject, self._expression(key, scope)))
            else:
                conditions.append(self.bool(self._expression(key, scope)))

        def written_as(value: Callable[[int, Value], str]) -> str:
            alternatives = " ".join(
                f"WHEN {condition} THEN {value(i, result)}"
                for i, (condition, (_, result)) in enumerate(zip(conditions, results, strict=False))
            )
            last = len(results) - 1
            otherwise = f" ELSE {value(last, results[last][1])}" if case.otherwise is not None else ""
            return f"CASE {alternatives}{otherwise} END"

        # Where a result holds a node or a relationship, the CASE holds it by identity too.
        holding = any(self._identity_of(result) is not None for _, result in results)
        identity = written_as(lambda _, it: self._identified(it)) if holding else None
        # On Oracle a list of nodes or relationships, read from their rows in a subquery, beside a result that reads the
        # row, as `CASE WHEN … THEN [r] ELSE [m.title] END`, closes the session (ORA-03113): its results are worked out
        # in a subquery first. Not a CASE that reads an aggregate, which the subquery would aggregate again, over its
        # one row.
        if (
            self.aggregates_case_subqueries
            or not any(isinstance(result, Entities) for _, result in results)
            or contains(case, is_aggregate)
        ):
            sql = written_as(lambda _, it: self.comparable(self.json(it)))
        else:
            values = [self.comparable(self.json(result)) for _, result in results]
            sql = self.bound(values, lambda bound: written_as(lambda i, _: bound[i]))
        return Json(
            sql,
            temporal=_kind_of(results),
            identity=identity,
            list=all(_is_list(result) for _, result in results),
            item_lists=all(_holds_lists(result) for _, result in results),
            text=all(_is_text(result) for _, result in results),
            texts=all(_holds_texts(result) for _, result in results),
        )

    def _function(self, function: Function, scope: Scope) -> Value:
        arguments = [self._expression(it, scope) for it in function.arguments]

        def one() -> Value:
            if len(arguments) != 1:
                raise DrivineError(f"{function.name}() takes one argument.")
            return arguments[0]

        distinct = "DISTINCT " if function.distinct else ""
        name = function.name.lower()
        if name == "properties":
            subject = one()
            return self.json(subject) if isinstance(subject, (Node, Relationship)) else subject
        if name == "labels":
            subject = one()
            if isinstance(subject, Node):
                return Json(subject.labels)
            raise DrivineError("labels() takes a node.")
        if name == "type":
            subject = one()
            if isinstance(subject, Relationship):
                return Json(self.scalar(subject.type), text=True)
            raise DrivineError("type() takes a relationship.")
        if name in ("id", "elementid"):
            subject = one()
            if isinstance(subject, Node):
                return Json(self.scalar(subject.nid))
            if isinstance(subject, Relationship):
                return Json(self.scalar(subject.eid))
            raise DrivineError(f"{function.name}() takes a node or a relationship.")
        if name == "length":
            subject = one()
            if isinstance(subject, Path):
                return Json(self.id_count(subject.eids))
            raise DrivineError("length() takes a path.")
        if name == "nodes":
            subject = one()
            if isinstance(subject, Path):
                return self._node_list(subject.nids)
            raise DrivineError("nodes() takes a path.")
        if name == "relationships":
            subject = one()
            if isinstance(subject, Path):
                return self._relationship_list(subject.eids)
            raise DrivineError("relationships() takes a path.")
        if name == "coalesce":
            coalesced = ", ".join(self.null_if_json_null(self.comparable(self.json(it))) for it in arguments)
            return Json(
                f"COALESCE({coalesced})",
                temporal=_kind_of(list(zip(function.arguments, arguments, strict=True))),
                text=all(_is_text(it) for it in arguments),
                texts=all(_holds_texts(it) for it in arguments),
            )
        if name == "count":
            return Json(self.scalar(f"count({distinct}{self._countable(one())})"))
        if name == "collect":
            # collect(DISTINCT …) is collected in a WITH of its own, then kept once each: see _aggregates_first.
            if function.distinct:
                raise AssertionError("collect(DISTINCT …) is gathered by _aggregates_first")
            gathered = one()
            # Nodes or relationships are gathered as their ids, so a list of them is still of them.
            if isinstance(gathered, Node):
                ids = self._gathered_ids(f"CASE WHEN {gathered.nid} IS NOT NULL THEN {self.scalar(gathered.nid)} END")
                return Entities(ids, False, self._collected(gathered))
            if isinstance(gathered, Relationship):
                ids = self._gathered_ids(f"CASE WHEN {gathered.eid} IS NOT NULL THEN {self.scalar(gathered.eid)} END")
                return Entities(ids, True, self._collected(gathered))
            kind = gathered.temporal if isinstance(gathered, Json) else None
            # What holds a node or a relationship, a map or a list of them, is gathered beside its identity too.
            identity = self._identity_of(gathered)
            return Json(
                self._collected(gathered),
                ordered=True,
                items=kind,
                list=True,
                item_lists=_is_list(gathered),
                texts=_is_text(gathered),
                identity=(
                    self.comparable(Json(self.collect(identity, self._row_order), ordered=True))
                    if identity is not None
                    else None
                ),
            )
        if name == DISTINCT_ITEMS:
            listed = one()
            # Nodes or relationships once each by their ids, not their properties.
            if isinstance(listed, Entities):
                return Entities(
                    self.comparable(Json(self.distinct_items(listed.ids), ordered=True)), listed.relationships
                )
            if isinstance(listed, Json):
                # What holds a node or a relationship once each by its identity, not its properties.
                if listed.identity is not None:
                    return Json(
                        self.first_of_each(self.comparable(listed), listed.identity),
                        ordered=True,
                        items=listed.items,
                        identity=self.comparable(Json(self.distinct_items(listed.identity), ordered=True)),
                        list=True,
                        item_lists=listed.item_lists,
                        texts=listed.texts,
                    )
                return Json(
                    self.distinct_items(self.comparable(listed)),
                    ordered=True,
                    items=listed.items,
                    list=True,
                    item_lists=listed.item_lists,
                    texts=listed.texts,
                )
            return Json(self.distinct_items(self.comparable(self.json(listed))), ordered=True)
        if name == "sum":
            return Json(self.scalar(f"COALESCE(sum({distinct}{self.number(self.comparable(self.json(one())))}), 0)"))
        if name == "avg":
            return Json(self.scalar(self.float_(f"avg({distinct}{self.number(self.comparable(self.json(one())))})")))
        if name in ("min", "max"):
            # Zoned values as the moments they are, whatever their offsets; of a temporal kind, still of it. A list the
            # statement makes item by item, as an ORDER BY sorts it: a store's JSON may put the shorter list first.
            value = self.json(one())
            temporal = value.temporal
            if temporal and temporal.zoned:
                key: str | None = self.temporals.moment(self.text(self.comparable(value)), temporal)
            elif self.extremes_by_list_key and _is_list(one()):
                key = self.list_sort_key(self.comparable(value))
            else:
                key = None
            return Json(
                self.extreme(self.comparable(value), min_=name == "min", key=key),
                temporal=temporal,
                text=_is_text(value),
            )
        if name == "range":
            if len(arguments) not in (2, 3):
                raise DrivineError("range() takes a start, an end and a step.")
            # Each argument read as a number first, as drivine4j reads them, so a statement's SQL is the same as its.
            numbers = [self.number(self.comparable(self.json(it))) for it in arguments]
            from_, to = numbers[:2]
            step = self.number(self.comparable(self.json(arguments[2]))) if len(arguments) == 3 else "1"
            return Json(self.range(from_, to, step), list=True)
        if name == "split":
            if len(arguments) != 2:
                raise DrivineError("split() takes a text and a separator.")
            text, separator = (self.text(self.comparable(self.json(it))) for it in arguments)
            return Json(self.split(text, separator), list=True, texts=True)
        if name == "reverse":
            subject = one()

            def reversed_(x: str) -> str:
                e = self._alias("e")
                listed = self.comparable(Json(self.aggregate_list(f"{e}.item", f"{e}.item_no DESC"), ordered=True))
                return (
                    f"CASE {self.type_of(x)} WHEN 'string' THEN {self.scalar(self.reverse_text(self.text(x)))} "
                    f"WHEN 'array' THEN (SELECT {listed} FROM {self.elements(x, e)}) END"
                )

            return Json(
                self.shared1(self.comparable(self.json(subject)), reversed_),
                items=_items_of(subject),
                list=_is_list(subject),
                item_lists=_holds_lists(subject),
                text=_is_text(subject),
                texts=_holds_texts(subject),
            )
        if name == "tail":
            subject = one()

            def tail(x: str) -> str:
                e = self._alias("e")
                listed = self.comparable(Json(self.aggregate_list(f"{e}.item", f"{e}.item_no"), ordered=True))
                return (
                    f"CASE WHEN {self.type_of(x)} = 'array' THEN "
                    f"(SELECT {listed} FROM {self.elements(x, e)} WHERE {e}.item_no > 1) END"
                )

            return Json(
                self.shared1(self.comparable(self.json(subject)), tail),
                items=_items_of(subject),
                list=_is_list(subject),
                item_lists=_holds_lists(subject),
                texts=_holds_texts(subject),
            )
        if name == "toboolean":
            # A text is read as Cypher reads it, its case and the spaces around it aside; a number is true but for 0.
            def boolean(x: str) -> str:
                true, false = self.literal_json(True), self.literal_json(False)
                return (
                    f"CASE WHEN {self.type_of(x)} = 'boolean' THEN {x} "
                    f"WHEN {self.type_of(x)} = 'number' THEN "
                    f"CASE WHEN {self.number(x)} = 0 THEN {false} ELSE {true} END "
                    f"WHEN LOWER(TRIM({self.text(x)})) = 'true' THEN {true} "
                    f"WHEN LOWER(TRIM({self.text(x)})) = 'false' THEN {false} END"
                )

            return Json(self.shared1(self.comparable(self.json(one())), boolean))
        if name == "isempty":

            def empty(x: str) -> str:
                size = self.function("size", x)
                assert size is not None
                return self.scalar(
                    f"CASE WHEN {self.type_of(x)} = 'object' THEN {x} = {self.literal_json({})} "
                    f"WHEN {self.type_of(x)} IN ('array', 'string') THEN {self.number(size)} = 0 END"
                )

            return Json(self.shared1(self.comparable(self.json(one())), empty))
        if name == "round":
            # A float, as Cypher gives these; round as Java rounds, half up. The precision is read as a whole number
            # toward zero, as Neo4j reads it: 1.7 places is 1.
            if len(arguments) == 3:
                mode = self._rounding_mode(function.arguments[2])
                if mode is None:
                    return Json(self.literal_json(None))
                return Json(
                    self.scalar(
                        self.float_(
                            self._rounded(
                                arguments[0],
                                arguments[1],
                                function.arguments[1],
                                lambda number, places: self.round_to(number, places, mode),
                            )
                        )
                    )
                )
            if len(arguments) == 2:
                return Json(
                    self.scalar(
                        self.float_(
                            self._rounded(
                                arguments[0],
                                arguments[1],
                                function.arguments[1],
                                lambda number, places: f"ROUND({number}, {places})",
                            )
                        )
                    )
                )
            return Json(self.scalar(self.float_(f"FLOOR({self.number(self.comparable(self.json(one())))} + 0.5)")))
        if name in ("floor", "ceil"):
            return Json(self.scalar(self.float_(f"{name.upper()}({self.number(self.comparable(self.json(one())))})")))
        if name in MATH_FUNCTIONS:
            return Json(self.scalar(self.math(name, [self.number(self.comparable(self.json(one())))])))
        if name == "atan2":
            if len(arguments) != 2:
                raise DrivineError("atan2() takes two numbers.")
            return Json(self.scalar(self.math(name, [self.number(self.comparable(self.json(it))) for it in arguments])))
        if name in ("e", "pi"):
            # Java's constants, which Neo4j gives, written as the floats they are.
            if arguments:
                raise DrivineError(f"{function.name}() takes no argument.")
            return Json(self.literal_json(math.e if name == "e" else math.pi))
        if name == "sign":
            return Json(self.scalar(f"SIGN({self.number(self.comparable(self.json(one())))})"))
        if name in ("ltrim", "rtrim"):
            return Json(self.scalar(f"{name.upper()}({self.text(self.comparable(self.json(one())))})"), text=True)
        if name == "randomuuid":
            if arguments:
                raise DrivineError("randomUUID() takes no argument.")
            return Json(self.scalar(self.uuid()))
        if name == "substring":
            # A position is counted from 0, as Cypher counts it, and SQL from 1.
            if len(arguments) not in (2, 3):
                raise DrivineError("substring() takes a text, a start and a length.")
            text, start = self._text_and_count(arguments)
            length = f", {self._whole(arguments[2])}" if len(arguments) == 3 else ""
            return Json(self.scalar(f"SUBSTR({text}, {start} + 1{length})"), text=True)
        if name in ("left", "right"):
            if len(arguments) != 2:
                raise DrivineError(f"{function.name}() takes a text and a length.")
            text, count = self._text_and_count(arguments)
            return Json(
                self.scalar(f"SUBSTR({text}, 1, {count})" if name == "left" else self.right_text(text, count)),
                text=True,
            )
        if name == "replace":
            if len(arguments) != 3:
                raise DrivineError("replace() takes a text, what to replace, and what with.")
            text, search, replacement = (self.text(self.comparable(self.json(it))) for it in arguments)
            return Json(self.scalar(f"REPLACE({text}, {search}, {replacement})"), text=True)
        kind = Temporal.of_function(name)
        if kind is not None:
            return self._temporal(kind, function, arguments, scope)
        if name == "timestamp":
            if arguments:
                raise DrivineError("timestamp() takes no argument.")
            now = self.temporals.now(Temporal.DATE_TIME)
            return Json(self.scalar(_field_sql(self.temporals.field(now, Temporal.DATE_TIME, "epochMillis"))))
        if name in TEMPORAL_NOW:
            # The moment the statement runs, however it is asked for: one statement is one moment here.
            now_kind = _temporal_of(name.split(".")[0])
            return Json(self.scalar(self.temporals.now(now_kind)), temporal=now_kind)
        if name in TEMPORAL_TRUNCATE:
            return self._truncate(function, arguments, _temporal_of(name.split(".")[0]))
        if name in ("duration.between", "duration.inmonths", "duration.indays", "duration.inseconds"):
            return self._between(function, arguments, name)
        # A function the store has no SQL for is named before its arguments are counted.
        if self.function(name, "NULL") is None:
            unsupported(f"The function {function.name}()")
        argument = self.comparable(self.json(one()))

        def applied(list_: str) -> str:
            return self.shared1(list_, lambda x: self.function(name, x) or "")

        result = applied(argument)
        # An item taken from a list is of the kind its items are.
        if name in ("head", "last"):
            return Json(
                result,
                temporal=_items_of(one()),
                identity=self._identity_taken(one(), result, True, applied),
                list=_holds_lists(one()),
                text=_holds_texts(one()),
            )
        return Json(result, text=name in TEXT_FUNCTIONS)

    def _truncate(self, function: Function, arguments: list[Value], to: Temporal) -> Value:
        """`date.truncate(unit, value)` and its like: the value cut short to the unit, as a value of `to`."""
        if len(arguments) != 2:
            raise DrivineError(f"{function.name}() takes a unit and a value.")
        written = function.arguments[0]
        if not isinstance(written, Literal) or not isinstance(written.value, str):
            unsupported("A truncation to a unit an expression gives")
        unit: str = written.value
        given = arguments[1]
        if not isinstance(given, Json):
            raise DrivineError(f"{function.name}() takes a unit and a value.")

        def cut(text: str, from_: Temporal) -> str:
            truncated = self.temporals.truncate(unit, text, from_, to)
            if truncated is None:
                raise DrivineError(
                    f"{function.name}() truncates to year, quarter, month, week, day, hour, minute, second or "
                    f"millisecond, not {unit}."
                )
            return truncated

        text = self.text(self.comparable(given))
        if given.temporal is not None and given.temporal.instant:
            truncated = cut(text, given.temporal)
        else:
            truncated = self.temporals.by_kind(text, lambda from_, t: cut(t, from_))
        return Json(self.scalar(truncated), temporal=to)

    def _between(self, function: Function, arguments: list[Value], name: str) -> Value:
        """`duration.between(a, b)` and `duration.inMonths`, `inDays` and `inSeconds`: the duration from one value to
        the other."""
        if len(arguments) != 2:
            raise DrivineError(f"{function.name}() takes two temporal values.")
        unit = {"duration.inmonths": "months", "duration.indays": "days", "duration.inseconds": "seconds"}.get(name)
        from_, to = arguments
        if not isinstance(from_, Json) or not isinstance(to, Json):
            raise DrivineError(f"{function.name}() takes two temporal values.")
        a, b = self.text(self.comparable(from_)), self.text(self.comparable(to))
        # A value whose kind the statement does not make is taken to be of the other's.
        from_kind = from_.temporal if from_.temporal is not None and from_.temporal.instant else None
        to_kind = to.temporal if to.temporal is not None and to.temporal.instant else None
        kind = from_kind or to_kind
        if kind is not None:
            between = self.temporals.between(a, b, kind, unit, to_kind or kind)
        else:
            between = self.shared1(
                b, lambda y: self.temporals.by_kind(a, lambda k, x: self.temporals.between(x, y, k, unit))
            )
        return Json(self.scalar(between), temporal=Temporal.DURATION)

    def _rounding_mode(self, mode: Expression) -> str | None:
        """
        The mode a `round()` names, as Java's `BigDecimal` names it, as Neo4j takes it: None where it is null, as the
        rounding then is. Written, or a parameter's value: the SQL is worked out for the one mode.
        """
        if not isinstance(mode, (Literal, Parameter)):
            unsupported("A rounding mode an expression gives")
        # As it is bound: an Enum is its value.
        name = jsonable(self._given(mode))
        if name is None:
            return None
        if name not in ROUNDING_MODES:
            raise DrivineError(
                "round() rounds by CEILING, FLOOR, UP, DOWN, HALF_EVEN, HALF_UP, HALF_DOWN or UNNECESSARY, not "
                f"{_written(name)}."
            )
        # It fails where rounding would change the number, which SQL has no way to say in a query.
        if name == "UNNECESSARY":
            unsupported("round() by UNNECESSARY")
        return str(name)

    def _text_and_count(self, arguments: list[Value]) -> tuple[str, str]:
        """A function's text, its first argument, and a whole number, its second."""
        return self.text(self.comparable(self.json(arguments[0]))), self._whole(arguments[1])

    def _whole(self, value: Value) -> str:
        """A value as a SQL whole number."""
        return f"CAST({self.number(self.comparable(self.json(value)))} AS INTEGER)"

    def _truncated_whole(self, value: Value) -> str:
        """A value as a SQL whole number cut toward zero, as Neo4j reads `round()`'s precision."""
        return f"CAST(TRUNC({self.number(self.comparable(self.json(value)))}) AS INTEGER)"

    def _rounded(self, value: Value, precision: Value, written: Expression, body: Callable[[str, str], str]) -> str:
        """
        `round()` of the value, by the body over it as a SQL number and the precision, the value written in the
        statement as `written`, as a whole number toward zero: -0.5 is 0 places. One below 0 is refused, as Neo4j
        refuses it: when compiling, where it is written, negated or not, or a parameter's value; and else when the row
        is read, by the store's `failure`, but for a null value, which Neo4j rounds to null whatever the precision.
        """
        number = self.number(self.comparable(self.json(value)))
        negated = isinstance(written, Unary) and written.operator is UnaryOperator.NEGATE
        given = jsonable(self._given(written.operand if isinstance(written, Unary) and negated else written))
        if _is_number(given):
            # At -1 or less, as below 0 toward zero: math.trunc fails on NaN or an infinity.
            if (-given if negated else given) <= -1:
                raise DrivineError(NEGATIVE_PRECISION)
            return body(number, self._truncated_whole(precision))
        return self.shared(
            [number, f"TRUNC({self.number(self.comparable(self.json(precision)))})"],
            lambda it: body(
                it[0],
                f"CASE WHEN {it[1]} < 0 AND {it[0]} IS NOT NULL THEN {self.failure(NEGATIVE_PRECISION, it[1])} "
                f"ELSE CAST({it[1]} AS INTEGER) END",
            ),
        )

    def _collected(self, value: Value) -> str:
        """The values of a group, gathered as `collect` gathers them, as JSON."""
        return self.collect(self._collectable(value), self._row_order)

    def _gathered_ids(self, id_: str) -> str:
        """The ids of a group, gathered as `collect` gathers them, as JSON that compares."""
        return self.comparable(Json(self.collect(id_, self._row_order), ordered=True))

    def _countable(self, value: Value) -> str:
        """What `count` counts of a value: a node by its row, anything else by its value, nulls not counted."""
        if isinstance(value, Node):
            return value.nid
        if isinstance(value, Relationship):
            return value.eid
        if isinstance(value, Path):
            return value.nids
        if isinstance(value, Entities):
            return value.ids
        # What holds a node or a relationship by its identity, so `count(DISTINCT {x: r})` counts each relationship.
        return self.null_if_json_null(self._identified(value))

    def _collectable(self, value: Value) -> str:
        """
        What `collect` gathers of a value: nulls are left out, as Cypher's `collect` leaves them out, JSON's null among
        them, as a list's item or a map's entry may be.
        """
        if isinstance(value, Node):
            return f"CASE WHEN {value.nid} IS NOT NULL THEN {self.json(value).sql} END"
        if isinstance(value, Json) and not value.ordered:
            return self.null_if_json_null(value.sql)
        return self.json(value).sql

    # ----- Conversions -----

    def json(self, value: Value) -> Json:
        """A value as JSON, for a projection or a comparison. A node is its properties, its stamp among them, as
        Drivine gives a node."""
        if isinstance(value, Json):
            return value
        if isinstance(value, Bool):
            return Json(self.scalar(value.sql))
        if isinstance(value, Node):
            return Json(self.node_json(value))
        if isinstance(value, Relationship):
            return Json(f"CASE WHEN {value.eid} IS NOT NULL THEN {value.props} END")
        if isinstance(value, Entities):
            return Json(self._entities_json(value), ordered=True, list=True)
        # Each graph engine's driver gives a path as a value of its own, so a path is given by its parts.
        raise DrivineError(
            "A path is not a value drivine gives the same on each store: use nodes(p), relationships(p) and length(p)."
        )

    def bool(self, value: Value) -> str:
        """A value as a condition: a boolean as itself, JSON's `true` and `false` as theirs, anything else as null."""
        if isinstance(value, Bool):
            return value.sql
        if isinstance(value, Json):
            return self.bool_of(self.comparable(value))
        raise DrivineError("A node, a relationship or a path is not a condition.")

    def _sortable(self, value: Value) -> str:
        """
        What an order sorts a value by: JSON's order for a value, a node by its properties. JSON's null, as a map or a
        list the statement builds holds it, is SQL's null, which comes last, as a graph engine orders null.
        """
        carried, sorts = self._sort_term(value)
        return sorts(carried)

    def _sort_term(self, value: Value) -> tuple[str, Callable[[str], str]]:
        """What `_sortable` sorts the value by, in two: the value as a column carries it, and what sorts it, of that
        column."""
        if isinstance(value, Bool):
            return value.sql, lambda it: it
        # A zoned value the statement makes sorts as the moment it is, whatever its offset.
        temporal = value.temporal if isinstance(value, Json) else None
        if isinstance(value, Json) and temporal is not None and temporal.zoned:
            return self.comparable(value), lambda it: self.temporals.moment(self.text(it), temporal)
        # A list the statement makes sorts as Cypher sorts lists, item by item; any other value as the store sorts its
        # JSON.
        if _is_list(value):
            return self.comparable(self.json(value)), self.list_sort_key
        return self.comparable(self.json(value)), self.null_if_json_null


# ----- What a statement holds -----


def indent(text: str) -> str:
    return "\n".join("    " + line for line in text.split("\n"))


def unsupported(construct: str) -> NoReturn:
    raise DrivineError(f"{construct} is Cypher, and is not compiled to SQL yet.")


def writes(clause: Clause) -> bool:
    """Whether the clause writes, which makes its statement a procedure."""
    if isinstance(clause, (Create, Merge, SetClause, Remove, Delete, Foreach)):
        return True
    if isinstance(clause, Call):
        return any(writes(it) for it in [*clause.query.clauses, *(c for u in clause.query.unions for c in u.clauses)])
    return False


def is_aggregate(expression: Expression) -> bool:
    return isinstance(expression, CountStar) or (
        isinstance(expression, Function) and expression.name.lower() in AGGREGATES
    )


def is_anew(expression: Expression) -> bool:
    """Whether the expression is a call of `randomUUID()`, the one function compiled here that gives a value anew each
    time it is called."""
    return isinstance(expression, Function) and expression.name.lower() == "randomuuid"


def _redeclared(returned: Iterable[str], bound: Iterable[str]) -> None:
    """
    Fails where a `CALL` returns or yields a variable of `returned` that is bound around it, in `bound`, as Neo4j fails:
    those of the query part it is in, and not those around a `CALL { … }` that it is in and that does not import them.
    In a statement that writes, each variable the row holds, which a `CALL { … }` there sees whether it imports it or
    not.
    """
    around = set(bound)
    for it in returned:
        if it in around:
            raise DrivineError(
                f"The variable {it} is bound already, so a CALL does not return or yield it, as in Cypher: name it "
                "with AS."
            )


def contains(expression: Expression, test: Callable[[Expression], bool]) -> bool:
    """Whether the expression holds an expression the test holds of, outside a nested query of its own."""
    return test(expression) or any(contains(it, test) for it in children(expression))


def variables_outside_aggregates(expression: Expression) -> list[str]:
    """
    The variables the expression names outside its aggregates, each once, in the order it names them: what an
    aggregating projection is grouped by.
    """
    if is_aggregate(expression):
        return []
    if isinstance(expression, Variable):
        return [expression.name]
    found = [expression.variable] if isinstance(expression, MapProjection) else []
    found += [it for child in children(expression) for it in variables_outside_aggregates(child)]
    return list(dict.fromkeys(found))


def variables_in(expression: Expression) -> list[str]:
    """
    Every variable the expression names, its nested queries' and comprehensions' among them, each once, in the order it
    names them: a name a comprehension binds of its own may be among them too, which costs no more than a variable read
    that is not needed.
    """
    if isinstance(expression, Variable):
        return [expression.name]
    if isinstance(expression, MapProjection):
        named = [it.name for it in expression.elements if isinstance(it, VariableElement)]
        return _ordered([expression.variable], named, _ordered_of(variables_in, children(expression)))
    if isinstance(expression, ListComprehension):
        return _ordered_of(variables_in, _present(expression.list, expression.where, expression.projection))
    if isinstance(expression, Quantifier):
        return _ordered_of(variables_in, _present(expression.list, expression.where))
    if isinstance(expression, Reduce):
        return _ordered_of(variables_in, [expression.initial, expression.list, expression.expression])
    if isinstance(expression, PatternComprehension):
        return _ordered(
            variables_in_pattern(expression.pattern),
            _ordered_of(variables_in, _present(expression.where, expression.projection)),
        )
    if isinstance(expression, PatternPredicate):
        return variables_in_pattern(expression.pattern)
    if isinstance(expression, Exists):
        return _ordered(
            *(variables_in_pattern(it) for it in expression.patterns),
            variables_in(expression.where) if expression.where is not None else [],
        )
    return _ordered_of(variables_in, children(expression))


def parameters_in(expression: Expression) -> set[str]:
    """Every parameter the expression names, in any expression it holds."""
    if isinstance(expression, Parameter):
        return {expression.name}
    if isinstance(expression, ListComprehension):
        return _union_of(parameters_in, _present(expression.list, expression.where, expression.projection))
    if isinstance(expression, Quantifier):
        return _union_of(parameters_in, _present(expression.list, expression.where))
    if isinstance(expression, Reduce):
        return _union_of(parameters_in, [expression.initial, expression.list, expression.expression])
    if isinstance(expression, PatternComprehension):
        return _union_of(parameters_in, _present(expression.where, expression.projection))
    return _union_of(parameters_in, children(expression))


def variables_in_pattern(pattern: Pattern) -> list[str]:
    """Every variable the pattern names, of its parts or in their properties, each once, in the order it names them."""
    found: list[list[str]] = []
    for node in pattern.nodes:
        found += [_present(node.variable), variables_in(node.properties) if node.properties is not None else []]
    for relationship in pattern.relationships:
        found += [
            _present(relationship.variable),
            variables_in(relationship.properties) if relationship.properties is not None else [],
        ]
        group = relationship.group
        if group is not None:
            found += [variables_in_pattern(group.pattern), variables_in(group.where) if group.where is not None else []]
    return _ordered(*found)


def holds_subquery(expression: Expression) -> bool:
    """
    Whether the expression holds a subquery anywhere: in a comprehension's, a quantifier's or a pattern's condition or
    properties too, where `contains` does not look.
    """
    if isinstance(expression, Subquery):
        return True
    if isinstance(expression, ListComprehension):
        return any(holds_subquery(it) for it in _present(expression.list, expression.where, expression.projection))
    if isinstance(expression, Quantifier):
        return any(holds_subquery(it) for it in _present(expression.list, expression.where))
    if isinstance(expression, Reduce):
        return any(holds_subquery(it) for it in [expression.initial, expression.list, expression.expression])
    if isinstance(expression, PatternComprehension):
        return pattern_holds_subquery(expression.pattern) or any(
            holds_subquery(it) for it in _present(expression.where, expression.projection)
        )
    if isinstance(expression, PatternPredicate):
        return pattern_holds_subquery(expression.pattern)
    if isinstance(expression, Exists):
        return any(pattern_holds_subquery(it) for it in expression.patterns) or (
            expression.where is not None and holds_subquery(expression.where)
        )
    return any(holds_subquery(it) for it in children(expression))


def pattern_holds_subquery(pattern: Pattern) -> bool:
    """Whether the pattern holds a subquery in its parts' properties or a quantified path pattern's condition."""
    for part in [*pattern.nodes, *pattern.relationships]:
        if part.properties is not None and holds_subquery(part.properties):
            return True
    for relationship in pattern.relationships:
        group = relationship.group
        if group is not None and (
            pattern_holds_subquery(group.pattern) or (group.where is not None and holds_subquery(group.where))
        ):
            return True
    return False


def replaced(expression: Expression, replacement: Callable[[Expression], Expression | None]) -> Expression:
    """The expression with each part the replacement gives a replacement for replaced, among those `children` finds."""
    found = replacement(expression)
    if found is not None:
        return found

    def r(part: Expression) -> Expression:
        return replaced(part, replacement)

    def maybe(part: Expression | None) -> Expression | None:
        return r(part) if part is not None else None

    if isinstance(expression, (Property, HasLabels)):
        return dataclasses.replace(expression, subject=r(expression.subject))
    if isinstance(expression, ListLiteral):
        return dataclasses.replace(expression, items=tuple(r(it) for it in expression.items))
    if isinstance(expression, MapLiteral):
        return dataclasses.replace(expression, entries=tuple((key, r(value)) for key, value in expression.entries))
    if isinstance(expression, MapProjection):
        elements = tuple(
            dataclasses.replace(it, value=r(it.value)) if isinstance(it, Entry) else it for it in expression.elements
        )
        return dataclasses.replace(expression, elements=elements)
    if isinstance(expression, Binary):
        return dataclasses.replace(expression, left=r(expression.left), right=r(expression.right))
    if isinstance(expression, Unary):
        return dataclasses.replace(expression, operand=r(expression.operand))
    if isinstance(expression, Index):
        return dataclasses.replace(expression, subject=r(expression.subject), index=r(expression.index))
    if isinstance(expression, Slice):
        return dataclasses.replace(
            expression, subject=r(expression.subject), from_=maybe(expression.from_), to=maybe(expression.to)
        )
    if isinstance(expression, Function):
        return dataclasses.replace(expression, arguments=tuple(r(it) for it in expression.arguments))
    if isinstance(expression, Case):
        return dataclasses.replace(
            expression,
            subject=maybe(expression.subject),
            alternatives=tuple((r(condition), r(result)) for condition, result in expression.alternatives),
            otherwise=maybe(expression.otherwise),
        )
    if isinstance(expression, (ListComprehension, Quantifier)):
        return dataclasses.replace(expression, list=r(expression.list))
    if isinstance(expression, Reduce):
        return dataclasses.replace(expression, initial=r(expression.initial), list=r(expression.list))
    return expression


def _collects_distinct(expression: Expression) -> bool:
    return isinstance(expression, Function) and expression.distinct and expression.name.lower() == "collect"


def iterated(expression: Expression) -> list[Expression]:
    """
    The lists the expression reads item by item, in a subquery of its own: a comprehension's, a quantifier's or a
    reduce's, what `IN` searches, what a slice, `reverse` or `tail` takes items of.
    """
    if isinstance(expression, (ListComprehension, Quantifier, Reduce)):
        return [expression.list]
    if isinstance(expression, Slice):
        return [expression.subject]
    if isinstance(expression, Binary):
        return [expression.right] if expression.operator is BinaryOperator.IN else []
    if isinstance(expression, Function) and expression.name.lower() in ("reverse", "tail"):
        return list(expression.arguments[:1])
    return []


def _iterates_aggregate(expression: Expression) -> bool:
    """Whether the expression reads an aggregate item by item: see `iterated`."""
    return any(contains(it, is_aggregate) for it in iterated(expression))


def _aggregates(expression: Expression) -> bool:
    """Whether the expression is an aggregate, or a variable `_aggregates_first` worked one out into."""
    return is_aggregate(expression) or (isinstance(expression, Variable) and expression.name.startswith("#aggregate"))


def children(expression: Expression) -> list[Expression]:
    if isinstance(expression, (Property, HasLabels)):
        return [expression.subject]
    if isinstance(expression, ListLiteral):
        return list(expression.items)
    if isinstance(expression, MapLiteral):
        return [it[1] for it in expression.entries]
    if isinstance(expression, MapProjection):
        return [it.value for it in expression.elements if isinstance(it, Entry)]
    if isinstance(expression, Binary):
        return [expression.left, expression.right]
    if isinstance(expression, Unary):
        return [expression.operand]
    if isinstance(expression, Index):
        return [expression.subject, expression.index]
    if isinstance(expression, Slice):
        return _present(expression.subject, expression.from_, expression.to)
    if isinstance(expression, Function):
        return list(expression.arguments)
    if isinstance(expression, Case):
        return [
            *_present(expression.subject, expression.otherwise),
            *(it for pair in expression.alternatives for it in pair),
        ]
    if isinstance(expression, (ListComprehension, Quantifier)):
        return [expression.list]
    if isinstance(expression, Reduce):
        return [expression.initial, expression.list]
    return []


def _union_of(of: Callable[[Expression], Iterable[str]], expressions: Iterable[Expression]) -> set[str]:
    return {it for expression in expressions for it in of(expression)}


def _ordered(*names: Iterable[str]) -> list[str]:
    """The names, each once, in the order first given: as a Kotlin set keeps them, so SQL is the same from each run."""
    return list(dict.fromkeys(it for listed in names for it in listed))


def _ordered_of(of: Callable[[Expression], list[str]], expressions: Iterable[Expression]) -> list[str]:
    return _ordered(*(of(it) for it in expressions))


def _present(*items: T | None) -> list[T]:
    """The items that are there."""
    return [it for it in items if it is not None]


def _single(items: Sequence[T]) -> T:
    if len(items) != 1:
        raise ValueError("Expected one item.")
    return items[0]


def _returned(clauses: Sequence[Clause]) -> Return:
    """The `RETURN` the clauses end in."""
    last = clauses[-1]
    if not isinstance(last, Return):
        raise DrivineError("A Cypher statement ends with RETURN.")
    return last


def _creates(clauses: Sequence[Clause]) -> list[Clause]:
    """The clauses with each run of `CREATE`s one `CREATE` of all their patterns, which makes the same, in one loop
    over the rows."""
    merged: list[Clause] = []
    for clause in clauses:
        last = merged[-1] if merged else None
        if isinstance(clause, Create) and isinstance(last, Create):
            merged[-1] = Create((*last.patterns, *clause.patterns))
        else:
            merged.append(clause)
    return merged


def _imported(clauses: Sequence[Clause]) -> Sequence[Clause]:
    """The clauses without an importing `WITH` of variables alone that opens them: every variable of the row is
    seen."""
    first = clauses[0] if clauses else None
    if not isinstance(first, With):
        return clauses
    projection = first.projection
    importing = (
        first.where is None
        and not projection.distinct
        and not projection.order_by
        and projection.skip is None
        and projection.limit is None
        and all(isinstance(it.expression, Variable) and it.expression.name == it.name for it in projection.items)
    )
    return clauses[1:] if importing else clauses


def _order_of(select: _Select) -> str | None:
    """The order the select reads its rows in, as an `ORDER BY` lists it: None where it has none."""
    return ", ".join(f"{term} DESC" if descending else term for term, descending in select.order) or None


def _require_explicit_grouping(items: Sequence[ProjectionItem]) -> None:
    """
    Refuses what Cypher refuses: an item that aggregates and names a variable outside its aggregates, where that
    variable is not an item of its own, which a graph engine would have to group by without being told.
    """
    keys = {
        it.expression.name
        for it in items
        if not contains(it.expression, is_aggregate) and isinstance(it.expression, Variable)
    }
    for item in items:
        if not contains(item.expression, is_aggregate):
            continue
        implicit = [it for it in variables_outside_aggregates(item.expression) if it not in keys]
        if implicit:
            named = ", ".join(implicit)
            raise DrivineError(
                f"{item.name} aggregates, and names {named} outside its aggregates, which no other item is: Cypher "
                f"would group by it without being told. Aggregate in a WITH first, as in WITH {named}, collect(…) AS "
                "items RETURN { … }."
            )


def _kind_of(values: Sequence[tuple[Expression, Value]]) -> Temporal | None:
    """
    The temporal kind the statement makes each of the values, as each is written and compiled, where it makes every one
    of the same kind, a null written among them aside: a `CASE`, a `coalesce` or a list of dates is a date, or a list of
    them. None where they are of different kinds or of none.
    """
    kinds = {
        value.temporal if isinstance(value, Json) else None for written, value in values if written != Literal(None)
    }
    return next(iter(kinds)) if len(kinds) == 1 else None


def _is_list(value: Value | None) -> bool:
    """Whether the statement makes the value a list: see Json.list."""
    return isinstance(value, Entities) or (isinstance(value, Json) and value.list)


def _holds_lists(value: Value) -> bool:
    """Whether the statement makes the value a list whose every item is a list: see Json.item_lists."""
    return isinstance(value, Json) and value.item_lists


def _is_text(value: Value) -> bool:
    """Whether the statement makes the value a text, or null: see Json.text."""
    return isinstance(value, Json) and value.text


def _holds_texts(value: Value) -> bool:
    """Whether the statement makes the value a list whose every item is a text, or null: see Json.texts."""
    return isinstance(value, Json) and value.texts


def _is_given_number(value: Any) -> bool:
    """Whether the value, given by the statement, is a number JSON holds as one: NaN and the infinities it holds as
    none."""
    return _is_number(value) and math.isfinite(value)


def java_text(value: Any) -> str:
    """
    A number as Java's `toString` writes it, as Neo4j does: a whole number as itself, and a float as Java writes a
    double, from its shortest decimal, with no exponent from 10^-3 up to 10^7, a point and a digit after it at least,
    `2.0`, and else as `1.0E7`, `1.2E-4`; NaN and the infinities by Java's names.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        # An int's own text, not a subclass's: an Enum of ints writes its name.
        return int.__repr__(value)
    if not isinstance(value, float):
        return str(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0.0:
        return "-0.0" if math.copysign(1.0, value) < 0 else "0.0"
    # A float's own repr, not a subclass's: numpy's float64 writes np.float64(2.5).
    shortest = Decimal(float.__repr__(value))
    if 1e-3 <= abs(value) < 1e7:
        text = format(shortest, "f")
        return text if "." in text else f"{text}.0"
    # The shortest digits, as repr finds them, with the point after the first. Java writes two at least, the two
    # nearest the double where one would do: 5e-324 as 4.9E-324.
    sign, figures, power = shortest.as_tuple()
    digits = "".join(str(it) for it in figures).rstrip("0") or "0"
    exponent = cast(int, power) + len(figures) - 1
    if len(digits) == 1:
        mantissa, exponent_text = f"{abs(value):.1e}".split("e")
        return f"{'-' if sign else ''}{mantissa}E{int(exponent_text)}"
    return f"{'-' if sign else ''}{digits[0]}.{digits[1:]}E{exponent}"


def _items_of(listed: Value) -> Temporal | None:
    """The temporal kind the statement makes each item of the list: see Json.items."""
    return listed.items if isinstance(listed, Json) else None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _written(value: Any) -> str:
    """A literal as Kotlin writes it in a message: true, null, 1.5."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _field_sql(found: Any) -> str:
    """The SQL of a field there is."""
    assert found is not None
    return found.sql


def _temporal_of(name: str) -> Temporal:
    kind = Temporal.of_function(name)
    assert kind is not None
    return kind


def _as_node(value: Value) -> Node:
    assert isinstance(value, Node)
    return value


def _node(row: str) -> Node:
    """The node of a row of the node table."""
    return Node(f"{row}.nid", f"{row}.labels", f"{row}.props", f"{row}.stamp")


def _relationship(row: str) -> Relationship:
    """The relationship of a row of the edge table."""
    return Relationship(f"{row}.eid", f"{row}.src", f"{row}.dst", f"{row}.type", f"{row}.props")


def _reversed(relationship: RelationshipPattern) -> RelationshipPattern:
    direction = {
        Direction.OUTGOING: Direction.INCOMING,
        Direction.INCOMING: Direction.OUTGOING,
        Direction.EITHER: Direction.EITHER,
    }[relationship.direction]
    return dataclasses.replace(relationship, direction=direction)


# ----- Columns of a value -----


def _columns(value: Value, name: str, identities: bool = True) -> list[str]:
    """
    The select-list entries that carry the value out of a query, under the name: with `identities`, a value's identity
    too (see Json.identity).
    """
    if isinstance(value, Json):
        identity = [f"{value.identity} AS {name}_identity"] if value.identity is not None and identities else []
        return [f"{value.sql} AS {name}", *identity]
    if isinstance(value, Bool):
        return [f"{value.sql} AS {name}"]
    if isinstance(value, Entities):
        return [f"{value.ids} AS {name}"]
    if isinstance(value, Node):
        return [
            f"{value.nid} AS {name}_nid",
            f"{value.labels} AS {name}_labels",
            f"{value.props} AS {name}_props",
            f"{value.stamp} AS {name}_stamp",
        ]
    if isinstance(value, Relationship):
        return [
            f"{value.eid} AS {name}_eid",
            f"{value.src} AS {name}_src",
            f"{value.dst} AS {name}_dst",
            f"{value.type} AS {name}_type",
            f"{value.props} AS {name}_props",
        ]
    return [f"{value.nids} AS {name}_nids", f"{value.eids} AS {name}_eids"]


def _columns_of(value: Value, alias: str, name: str | None = None, identities: bool = True) -> Value:
    """
    The value, read from the columns `_columns` gave it under the name in the query named `alias`, with `identities` its
    identity too. Without a name, a value whose columns were named for a query not yet named (PENDING) is read from
    `alias`.
    """
    if name is None:
        return _with_alias(value, alias)
    p = f"{alias}.{name}"
    if isinstance(value, Json):
        identity = f"{p}_identity" if value.identity is not None and identities else None
        return dataclasses.replace(value, sql=p, identity=identity)
    if isinstance(value, Entities):
        return Entities(p, value.relationships)
    if isinstance(value, Bool):
        return Bool(p)
    if isinstance(value, Node):
        return Node(f"{p}_nid", f"{p}_labels", f"{p}_props", f"{p}_stamp")
    if isinstance(value, Relationship):
        return Relationship(f"{p}_eid", f"{p}_src", f"{p}_dst", f"{p}_type", f"{p}_props")
    return Path(f"{p}_nids", f"{p}_eids")


def _with_alias(value: Value, alias: str) -> Value:
    """The value with PENDING read as the alias."""

    def a(text: str) -> str:
        return text.replace(PENDING, alias)

    if isinstance(value, Json):
        return dataclasses.replace(
            value, sql=a(value.sql), identity=a(value.identity) if value.identity is not None else None
        )
    if isinstance(value, Entities):
        return dataclasses.replace(value, ids=a(value.ids), gathered=a(value.gathered) if value.gathered else None)
    if isinstance(value, Bool):
        return Bool(a(value.sql))
    if isinstance(value, Node):
        return Node(a(value.nid), a(value.labels), a(value.props), a(value.stamp))
    if isinstance(value, Relationship):
        return Relationship(a(value.eid), a(value.src), a(value.dst), a(value.type), a(value.props))
    return Path(a(value.nids), a(value.eids))


def _group_keys(value: Value) -> list[str]:
    """What a projection that aggregates is grouped by, to keep the value: a node by its columns."""
    if isinstance(value, Json):
        return [value.sql, *_present(value.identity)]
    if isinstance(value, Bool):
        return [value.sql]
    if isinstance(value, Entities):
        return [value.ids]
    if isinstance(value, Node):
        return [value.nid, value.labels, value.props, value.stamp]
    if isinstance(value, Relationship):
        return [value.eid, value.src, value.dst, value.type, value.props]
    return [value.nids, value.eids]
