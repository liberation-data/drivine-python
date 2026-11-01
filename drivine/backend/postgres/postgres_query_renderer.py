from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Literal

from drivine.backend.postgres.postgres_graph import json_value, number_of, text_of
from drivine.backend.postgres.postgres_views import PostgresViews
from drivine.backend.save_statement_builder import packed
from drivine.backend.sql.compared import Compared
from drivine.backend.sql.sql_json import storable, to_json
from drivine.backend.sql.sql_query_renderer import SqlQueryRenderer
from drivine.drivine_error import DrivineError
from drivine.dsl.compare_test import CompareTest
from drivine.dsl.model_scope import scope_of
from drivine.dsl.query import Query
from drivine.dsl.query_renderer import described
from drivine.dsl.rendered_query import RenderedQuery
from drivine.model import stamps
from drivine.model.target_model import TargetModel

_SQL_OPERATORS = {"EQ": "=", "NEQ": "<>", "GT": ">", "GTE": ">=", "LT": "<", "LTE": "<="}
"""The comparisons SQL writes as an operator."""


class PostgresQueryRenderer(SqlQueryRenderer):
    """
    Writes a query on Postgres (see SqlQueryRenderer), as drivine4j writes one. A property is read with `->>`, as the
    kind of the value it is compared with: a number only where one is stored, so `'7'` is not `7`, as on the graph
    engines; a boolean as the text JSON writes it; anything else as text. A missing property is null, and meets no
    comparison, as in Cypher. Nothing is written with Postgres's `?` operators, which a driver may take for a parameter.
    """

    @staticmethod
    def render(
        query: Query,
        model: TargetModel,
        views: Callable[[dict[str, int]], PostgresViews],
        alias: str,
        labels_of_class: Callable[[type], list[str]],
    ) -> RenderedQuery:
        renderer = PostgresQueryRenderer(views(dict(query.depth or {})), labels_of_class)
        return renderer._render(scope_of(model), query, alias)

    @staticmethod
    def render_where(
        where: Any, model: TargetModel, views: PostgresViews, alias: str, labels_of_class: Callable[[type], list[str]]
    ) -> tuple[str, dict[str, Any]]:
        """A condition over the class's references, written for the store, with the values it binds."""
        return PostgresQueryRenderer(views, labels_of_class)._render_only(where, model, alias)

    def _property(self, compared: Compared, top: bool) -> str:
        test, row, name, json_packed, path = (
            compared.test,
            compared.row,
            compared.name,
            compared.json_packed,
            compared.path,
        )
        if not isinstance(test, CompareTest):
            raise DrivineError(f"{described(path)} is a property, not a node.")
        present = self._has(row, name)
        operator = _SQL_OPERATORS.get(test.operator)
        if operator is not None:
            return f"{self._typed(compared, test.value)} {operator} {self._bind(self._scalar(test.value, json_packed))}"
        if test.operator == "IN":
            return self._listed(compared, self._list(test.value, path)) or "1 = 0"
        if test.operator == "NOT_IN":
            any_ = self._listed(compared, self._list(test.value, path))
            return f"{present} AND NOT COALESCE({any_}, FALSE)" if any_ else present
        if test.operator == "HAS_ITEM":
            item = _packed(test.value) if json_packed else super()._storable(test.value)
            return self._holds(row, name, item)
        if test.operator == "IS_NULL":
            return f"NOT {present}"
        if test.operator == "IS_NOT_NULL":
            return present
        return self._text_test(test.operator, test.value, text_of(row, name))

    def _any_label(self, row: str, labels: Sequence[str]) -> str:
        return (
            f"EXISTS (SELECT 1 FROM jsonb_array_elements_text({row}.labels) l "
            f"WHERE l.value IN (SELECT v FROM jsonb_array_elements_text(CAST({self._bind(to_json(list(labels)))} "
            "AS jsonb)) v))"
        )

    def _seek_test(self, alias: str, property: str, operator: str, value: str) -> str:
        """The property read as the kind of the cursor's value, as a comparison reads it."""
        read = number_of(alias, property) if _is_number(self.bindings[value[1:]]) else text_of(alias, property)
        return f"{read} {'=' if operator == '==' else operator} {value}"

    def _storable(self, value: Any) -> Any:
        """A boolean as the text JSON writes it, which is what `->>` reads it back as; a date as ISO 8601 text."""
        if isinstance(value, bool):
            return "true" if value else "false"
        return super()._storable(value)

    def _ordered_property(
        self, alias: str, property: str, direction: Literal["ASC", "DESC"], nulls: str | None = None
    ) -> str:
        """
        A property no field declares the kind of is ordered as a number first and as text after, so a page of numbers
        is in the order a seek with one of them compares in, and anything else in the order of its text. Where the order
        places its nulls, it says so first: a text is null as a number.
        """
        if property == stamps.PROPERTY:
            return f"{text_of(alias, property)} {direction}{f' NULLS {nulls}' if nulls else ''}"
        text = text_of(alias, property)
        placed = (
            f"CASE WHEN {text} IS NULL THEN {0 if nulls == 'FIRST' else 1} ELSE {1 if nulls == 'FIRST' else 0} END, "
            if nulls
            else ""
        )
        return f"{placed}{number_of(alias, property)} {direction}, {text} {direction}"

    def _read(self, alias: str, property: str, as_: Literal["NUMBER", "TEXT"]) -> str:
        if as_ == "NUMBER" and property != stamps.PROPERTY:
            return number_of(alias, property)
        return text_of(alias, property)

    def _present(self, alias: str, property: str) -> str:
        return self._has(alias, property)

    def _typed(self, compared: Compared, value: Any) -> str:
        """The property read as the kind of the value it is compared with: a number as one, anything else as text."""
        if _is_number(value) and not compared.json_packed:
            return number_of(compared.row, compared.name)
        return text_of(compared.row, compared.name)

    def _scalar(self, value: Any, json_packed: bool) -> Any:
        """
        A value as the property holds it: packed into JSON text where the property is, and as `_storable` writes it.
        """
        return self._storable(_packed(value) if json_packed else value)

    def _listed(self, compared: Compared, values: list[Any]) -> str | None:
        """
        Whether the property is among the values, read as numbers where every value is one: None for no values, which
        nothing is among.
        """
        if not values:
            return None
        numbers = not compared.json_packed and all(_is_number(it) for it in values)
        read = (
            "CASE WHEN jsonb_typeof(v.value) = 'number' THEN (v.value #>> '{}')::numeric END"
            if numbers
            else "v.value #>> '{}'"
        )
        bound = self._bind(to_json([self._scalar(it, compared.json_packed) for it in values]))
        column = number_of(compared.row, compared.name) if numbers else text_of(compared.row, compared.name)
        return f"{column} IN (SELECT {read} FROM jsonb_array_elements(CAST({bound} AS jsonb)) v)"

    def _holds(self, row: str, name: str, item: Any) -> str:
        """Whether the list property holds the item, compared as JSON, so a boolean is one and not its text."""
        value = json_value(row, name)
        return f"(jsonb_typeof({value}) = 'array' AND {value} @> CAST({self._bind(to_json([item]))} AS jsonb))"

    def _text_test(self, operator: str, value: Any, text: str) -> str:
        """A test of a property's text."""
        if operator == "CONTAINS":
            return f"strpos({text}, {self._text(value)}) > 0"
        if operator == "STARTS_WITH":
            return f"starts_with({text}, {self._text(value)})"
        if operator == "ENDS_WITH":
            bound = self._text(value)
            return f"right({text}, length({bound})) = {bound}"
        if operator == "MATCHES":
            return self._matches(text, _string(value))
        if operator == "CONTAINS_IGNORE_CASE":
            return f"strpos(lower({text}), {self._text(_string(value).lower())}) > 0"
        # A lowercased copy, compared with the value as Postgres lowercases it: see lower_copies.
        if operator == "EQ_LOWERED":
            return f"{text} = lower({self._text(value)})"
        if operator == "CONTAINS_LOWERED":
            return f"strpos({text}, lower({self._text(value)})) > 0"
        return f"lower({text}) = {self._text(_string(value).lower())}"

    def _text(self, value: Any) -> str:
        """A value bound as text: a function of text is not left to guess the type of a value bound without one."""
        return f"CAST({self._bind(None if value is None else _string(value))} AS text)"

    def _matches(self, text: str, pattern: str) -> str:
        """
        A regular expression that must match the whole value, as Cypher's `=~` does. A leading `(?i)` ignores case. It
        is held to the whole value by a group that takes no number, so its own groups keep theirs and a backreference
        names the group it was written for.
        """
        ignore_case = pattern.startswith("(?i)")
        body = pattern[4:] if ignore_case else pattern
        return f"{text} {'~*' if ignore_case else '~'} {self._text(f'^(?:{body})$')}"

    def _has(self, row: str, property: str) -> str:
        """Whether the row has the property, and not as a JSON null."""
        if property == stamps.PROPERTY:
            return f"{row}.stamp IS NOT NULL"
        return f"COALESCE(jsonb_typeof({json_value(row, property)}) <> 'null', FALSE)"


def _is_number(value: Any) -> bool:
    """Whether the value is stored as a number: an Enum of numbers, or a Decimal, is the number a save writes."""
    stored = storable(value)
    return isinstance(stored, (int, float)) and not isinstance(stored, bool)


def _packed(value: Any) -> str:
    """A value packed into JSON text, as a JsonPacked field is stored."""
    return packed(value)


def _string(value: Any) -> str:
    """
    A value as text, as JavaScript's String writes it: a boolean in lower case; an Enum, or a date, as the text a save
    writes (see storable), so `Status.OPEN` is its value and not its name.
    """
    stored = storable(value)
    if isinstance(stored, bool):
        return "true" if stored else "false"
    return str(stored)
