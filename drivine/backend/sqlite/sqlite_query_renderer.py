from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time
from types import UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from drivine.backend.save_statement_builder import packed
from drivine.backend.sql.compared import Compared
from drivine.backend.sql.sql_json import storable, to_json
from drivine.backend.sql.sql_query_renderer import SqlQueryRenderer
from drivine.backend.sql.value_layout import Held, write
from drivine.backend.sqlite.sqlite_graph import (
    LOWER,
    TIME_KINDS,
    Kind,
    json_type,
    number_of,
    path,
    temporal_text,
    text,
    text_of,
    time_key,
    time_key_of,
    typed,
)
from drivine.backend.sqlite.sqlite_views import SqliteViews
from drivine.drivine_error import DrivineError
from drivine.dsl.compare_test import CompareTest
from drivine.dsl.model_scope import scope_of
from drivine.dsl.query import Query
from drivine.dsl.query_renderer import described
from drivine.dsl.rendered_query import RenderedQuery
from drivine.dsl.root_key import RootKey
from drivine.dsl.scope import Scope
from drivine.model import stamps
from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.target_model import TargetModel

_SQL_OPERATORS = {"EQ": "=", "NEQ": "<>", "GT": ">", "GTE": ">=", "LT": "<", "LTE": "<="}
"""The comparisons SQL writes as an operator."""


class SqliteQueryRenderer(SqlQueryRenderer):
    """
    Writes a query on SQLite (see SqlQueryRenderer), as drivine4j writes one. A property is read as the kind of the
    value it is compared with: a number only where one is stored, so `'7'` is not `7`, as on the graph engines; a
    moment as nanoseconds since the epoch and a date as a date, the value bound read alike, so two moments compare as
    the moments they are; a time as its key (see time_key), so two at different offsets compare as Cypher compares
    them, and a local time with a time with an offset as null, as in Cypher; a boolean as the text JSON writes it;
    anything else as text, as Postgres's `->>` reads it. A missing property is null, and meets no comparison, as in
    Cypher. Where an order puts its nulls is always said, as SQLite puts them first when ascending and Cypher last.
    """

    def __init__(self, views: SqliteViews, labels_of_class: Callable[[type], list[str]]) -> None:
        super().__init__(views, labels_of_class)
        self._seek_kinds: dict[str, Kind] = {}
        """The kind of each value a cursor binds, by its parameter."""

    @staticmethod
    def render(
        query: Query,
        model: TargetModel,
        views: Callable[[dict[str, int]], SqliteViews],
        alias: str,
        labels_of_class: Callable[[type], list[str]],
    ) -> RenderedQuery:
        renderer = SqliteQueryRenderer(views(dict(query.depth or {})), labels_of_class)
        return renderer._render(scope_of(model), query, alias)

    @staticmethod
    def render_where(
        where: Any, model: TargetModel, views: SqliteViews, alias: str, labels_of_class: Callable[[type], list[str]]
    ) -> tuple[str, dict[str, Any]]:
        """A condition over the class's references, written for the store, with the values it binds."""
        return SqliteQueryRenderer(views, labels_of_class)._render_only(where, model, alias)

    def _property(self, compared: Compared, top: bool) -> str:
        test, row, name, json_packed, path_ = (
            compared.test,
            compared.row,
            compared.name,
            compared.json_packed,
            compared.path,
        )
        if not isinstance(test, CompareTest):
            raise DrivineError(f"{described(path_)} is a property, not a node.")
        present = self._has(row, name)
        operator = _SQL_OPERATORS.get(test.operator)
        if operator is not None:
            kind = _kind_of(test.value, json_packed)
            bound = typed(kind, self._bind(self._scalar(test.value, json_packed)))
            read = text_of(row, name) if kind == "TEXT" else self._typed(row, name, kind, _tag_of(test.value))
            return f"{read} {operator} {bound}"
        if test.operator == "IN":
            return self._listed(compared, self._list(test.value, path_)) or "1 = 0"
        if test.operator == "NOT_IN":
            any_ = self._listed(compared, self._list(test.value, path_))
            return f"{present} AND NOT COALESCE({any_}, FALSE)" if any_ else present
        if test.operator == "HAS_ITEM":
            if not json_packed and isinstance(test.value, (date, time)):
                return self._holds_temporal(row, name, test.value)
            item = packed(test.value) if json_packed else super()._storable(test.value)
            return self._holds(row, name, item)
        if test.operator == "IS_NULL":
            return f"NOT {present}"
        if test.operator == "IS_NOT_NULL":
            return present
        return self._text_test(test.operator, test.value, text_of(row, name))

    def _any_label(self, row: str, labels: Sequence[str]) -> str:
        return (
            f"EXISTS (SELECT 1 FROM json_each({row}.labels) l "
            f"WHERE l.value IN (SELECT v.value FROM json_each({self._bind(to_json(list(labels)))}) v))"
        )

    def _seek_test(self, alias: str, property: str, operator: str, value: str) -> str:
        """
        The property read as the kind of the cursor's value, as a comparison reads it, a saved temporal value of any
        kind among its kind's as an order reads it, so a page goes on where the one before it ended.
        """
        kind = self._seek_kinds.get(value[1:], "TEXT")
        read = self._typed(alias, property, kind)
        return f"{read} {'=' if operator == '==' else operator} {typed(kind, value)}"

    def _storable(self, value: Any) -> Any:
        """
        A boolean as the text JSON writes it, which is what a property read as text gives; a date as ISO text; a moment
        as ISO text to the microsecond, as it is compared to the nanosecond, a naive one taken as UTC; a time as its key
        (see time_key).
        """
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, time):
            return _time_key(value)
        if isinstance(value, datetime):
            utc = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
            return utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        return super()._storable(value)

    def _keyset(self, keys: Any, seek: Any, query: Query, alias: str) -> str:
        """
        The cursor's values are bound as a save writes them, a moment as its ISO text: its kind is read from what it was
        before, so it is compared as the moment it is.
        """
        # The cursor's values are the next bound, in turn.
        start = len(self.bindings)
        self._seek_kinds = {f"_q{start + i}": _kind_of(it.value, False) for i, it in enumerate(seek)}
        return super()._keyset(keys, seek, query, alias)

    def _order(self, key: RootKey, scope: Scope, alias: str) -> str:
        """
        A property its field declares a moment, a date or a time is ordered as one: offsets order as the time they are,
        and a time with an offset before a local one (see time_key), whichever its field holds.
        """
        kind = _declared_kind(scope, key.property) if key.term.expression is None and key.property else None
        if kind is None:
            return super()._order(key, scope, alias)
        direction = "DESC" if key.term.direction == "DESC" else "ASC"
        nulls = key.term.nulls or ("LAST" if direction == "ASC" else "FIRST")
        return f"{self._typed(alias, key.property or '', kind)} {direction} NULLS {nulls}"

    def _ordered_property(
        self, alias: str, property: str, direction: Literal["ASC", "DESC"], nulls: str | None = None
    ) -> str:
        """
        A property no field declares the kind of is ordered as a number first and as text after, so a page of numbers
        is in the order a seek with one of them compares in, and anything else in the order of its text. Where the order
        places its nulls, it says so first: a text is null as a number. With none asked for, nulls go where Cypher puts
        them, last ascending and first descending.
        """
        placed = f" NULLS {nulls}" if nulls else (" NULLS LAST" if direction == "ASC" else " NULLS FIRST")
        if property == stamps.PROPERTY:
            return f"{text_of(alias, property)} {direction}{placed}"
        text_ = text_of(alias, property)
        first = (
            f"CASE WHEN {text_} IS NULL THEN {0 if nulls == 'FIRST' else 1} ELSE {1 if nulls == 'FIRST' else 0} END, "
            if nulls
            else ""
        )
        default = " NULLS LAST" if direction == "ASC" else " NULLS FIRST"
        return f"{first}{number_of(alias, property)} {direction}{default}, {text_} {direction}{default}"

    def _read(self, alias: str, property: str, as_: Literal["NUMBER", "TEXT"]) -> str:
        if as_ == "NUMBER" and property != stamps.PROPERTY:
            return number_of(alias, property)
        return text_of(alias, property)

    def _present(self, alias: str, property: str) -> str:
        return self._has(alias, property)

    def _typed(self, row: str, name: str, kind: Kind, tag: str | None = None) -> str:
        """
        The property read as `kind`: a number as one, a moment or a date as one, a time as its key, anything else as
        text. Given the tag of the time it is compared with (see _tag_of), a saved time of the other kind is null;
        without one, as an order and a seek read it, either.
        """
        if kind == "NUMBER":
            return number_of(row, name)
        if kind == "TIME":
            return time_key(row, name, tag)
        return typed(kind, temporal_text(row, name) if kind in ("INSTANT", "DATE") else text_of(row, name))

    def _scalar(self, value: Any, json_packed: bool) -> Any:
        """
        A value as the property holds it: packed into JSON text where the property is, and as `_storable` writes it.
        """
        return self._storable(packed(value) if json_packed else value)

    def _listed(self, compared: Compared, values: list[Any]) -> str | None:
        """
        Whether the property is among the values, read as numbers where every value is one, and as moments, dates or
        times where every value is one of those: None for no values, which nothing is among. Each value is read as a
        property of its kind is read, so a text is a string's text or another value's JSON.
        """
        if not values:
            return None
        kinds = {_kind_of(it, compared.json_packed) for it in values if it is not None}
        if len(kinds) == 1 and (kind := next(iter(kinds))) in ("INSTANT", "DATE", "TIME"):
            # Moments, dates or times, each read as one, as a comparison reads it: against the values of each tag in
            # turn, so a saved time is among only times of its own kind. A null among them is null read as one, which
            # SQL's IN reads as Cypher's does.
            tags = sorted({_tag_of(it) for it in values if it is not None}, key=str)
            among: list[str] = []
            for tag in tags:
                own = [it for it in values if it is None or _tag_of(it) == tag]
                bound = self._bind(to_json([self._scalar(it, False) for it in own]))
                read = self._typed(compared.row, compared.name, kind, tag)
                among.append(f"{read} IN (SELECT {typed(kind, 'v.value')} FROM json_each({bound}) v)")
            return among[0] if len(among) == 1 else f"({' OR '.join(among)})"
        numbers = not compared.json_packed and all(_kind_of(it, False) == "NUMBER" for it in values)
        read = (
            "(CASE WHEN v.type IN ('integer', 'real') THEN v.value END)"
            if numbers
            else "(CASE v.type WHEN 'text' THEN v.value WHEN 'null' THEN NULL WHEN 'true' THEN 'true' "
            "WHEN 'false' THEN 'false' WHEN 'integer' THEN json_quote(v.value) WHEN 'real' THEN json_quote(v.value) "
            "ELSE v.value END)"
        )
        bound = self._bind(to_json([self._scalar(it, compared.json_packed) for it in values]))
        column = (number_of if numbers else text_of)(compared.row, compared.name)
        return f"{column} IN (SELECT {read} FROM json_each({bound}) v)"

    def _holds(self, row: str, name: str, item: Any) -> str:
        """
        Whether the list property holds the item, as Postgres's `@>` says an array holds an element: of the same JSON
        type, a number of either kind, and equal. The item is bound as the one-item array of it, so it is read as JSON
        reads it, a boolean as one and not its text.
        """
        elements = f"json_each({row}.props, {path(name)}) h, json_each({self._bind(to_json([item]))}) b"
        same_type = "(h.type = b.type OR (h.type IN ('integer', 'real') AND b.type IN ('integer', 'real')))"
        return (
            f"({json_type(row, name)} = 'array' AND EXISTS (SELECT 1 FROM {elements} WHERE {same_type} "
            "AND h.value IS b.value))"
        )

    def _holds_temporal(self, row: str, name: str, item: date | time) -> str:
        """
        Whether the list property holds the moment, date or time `item`, each held item read as a comparison reads a
        single temporal property (see temporal_text): store layout 2 holds a saved one as its tag, an object `_holds`
        never finds equal to the item's text (#525), so a tag is read by its text `v`, and a text as itself. A moment
        compares as the moment it is, whatever its offset; a date as a date; a time as its key (see time_key), and a
        text never as a time.
        """
        kind, tag = _kind_of(item, False), _tag_of(item)
        if kind == "TIME":
            held = f"(CASE WHEN h.type = 'object' THEN {time_key_of('h.value', '$', tag)} END)"
        else:
            own = "json_type(h.value, '$.\"$t\"') = 'text'"
            held = (
                f"(CASE WHEN h.type = 'object' AND {own} THEN h.value ->> '$.v' WHEN h.type = 'text' THEN h.value END)"
            )
        bound = typed(kind, self._bind(self._scalar(item, False)))
        return (
            f"({json_type(row, name)} = 'array' AND EXISTS (SELECT 1 FROM json_each({row}.props, {path(name)}) h "
            f"WHERE {typed(kind, held)} = {bound}))"
        )

    def _text_test(self, operator: str, value: Any, text_: str) -> str:
        """A test of a property's text."""
        if operator == "CONTAINS":
            return f"instr({text_}, {self._text(value)}) > 0"
        if operator == "STARTS_WITH":
            bound = self._text(value)
            return f"substr({text_}, 1, length({bound})) = {bound}"
        if operator == "ENDS_WITH":
            # `substr(t, -0)` is not the empty end of t, so an empty suffix is said to end every text.
            bound = self._text(value)
            return f"(length({bound}) = 0 AND {text_} IS NOT NULL OR substr({text_}, -length({bound})) = {bound})"
        if operator == "MATCHES":
            return self._matches(text_, _string(value))
        if operator == "CONTAINS_IGNORE_CASE":
            return f"instr({LOWER}({text_}), {self._text(_string(value).lower())}) > 0"
        # A lowercased copy, compared with the value as SQLite lowercases it: see lower_copies.
        if operator == "EQ_LOWERED":
            return f"{text_} = {LOWER}({self._text(value)})"
        if operator == "CONTAINS_LOWERED":
            return f"instr({text_}, {LOWER}({self._text(value)})) > 0"
        return f"{LOWER}({text_}) = {self._text(_string(value).lower())}"

    def _text(self, value: Any) -> str:
        """A value bound as text."""
        return text(self._bind(None if value is None else _string(value)))

    def _matches(self, text_: str, pattern: str) -> str:
        """
        A regular expression that must match the whole value, as Cypher's `=~` does. A leading `(?i)` ignores case.
        SQLite has no regular expressions of its own, so the expression is Java's, as on drivine4j, read by the function
        SQLite's `REGEXP` calls, the shared module's (see sqlite_extension). It is held to the whole value by a group
        that takes no number, so its own groups keep theirs and a backreference names the group it was written for, and
        ends at `\\z`, the text's end, where Java's `\\Z` and `$` would match before a line break that ends it.
        """
        ignore_case = pattern.startswith("(?i)")
        body = pattern[4:] if ignore_case else pattern
        whole = ("(?i)" if ignore_case else "") + f"^(?:{body})" + r"\z"
        return f"{text_} REGEXP {self._text(whole)}"

    def _has(self, row: str, property: str) -> str:
        """Whether the row has the property, and not as a JSON null."""
        if property == stamps.PROPERTY:
            return f"{row}.stamp IS NOT NULL"
        return f"COALESCE({json_type(row, property)} <> 'null', FALSE)"


def _declared_kind(scope: Scope, property: str) -> Kind | None:
    """
    INSTANT, DATE or TIME where the root's field stored under `property` is declared a moment, a date or a time; None
    otherwise.
    """
    root = scope.model.root if isinstance(scope.model, GraphViewModel) else scope.model
    if not isinstance(root, FragmentModel) or root.type is None:
        return None
    field = root.fields_by_property.get(property)
    info = getattr(root.type, "model_fields", {}).get(field) if field else None
    if info is None:
        return None
    for it in _types_of(info.annotation):
        if it is datetime:
            return "INSTANT"
        if it is date:
            return "DATE"
        if it is time:
            return "TIME"
    return None


def _types_of(annotation: Any) -> list[Any]:
    """The types an annotation names: itself, or each of a union's, each without its Annotated metadata."""
    if get_origin(annotation) is Annotated:
        return _types_of(get_args(annotation)[0])
    if get_origin(annotation) in (Union, UnionType):
        return [it for arg in get_args(annotation) for it in _types_of(arg)]
    return [annotation]


def _kind_of(value: Any, json_packed: bool) -> Kind:
    """How a property compared with the value is read: as the kind of the value."""
    if json_packed:
        return "TEXT"
    if isinstance(value, datetime):
        return "INSTANT"
    if isinstance(value, date):
        return "DATE"
    if isinstance(value, time):
        return "TIME"
    stored = storable(value)
    if isinstance(stored, (int, float)) and not isinstance(stored, bool):
        return "NUMBER"
    return "TEXT"


def _tag_of(value: Any) -> str | None:
    """
    The kind store layout 2 tags a saved temporal value of the value's kind with (its `$t`, see value_layout), which a
    saved one must have to be compared with it; None for a value of no temporal kind.
    """
    if isinstance(value, datetime):
        return "localdatetime" if value.tzinfo is None else "datetime"
    if isinstance(value, date):
        return "date"
    if isinstance(value, time):
        return "localtime" if value.tzinfo is None else "time"
    return None


def _time_key(value: time) -> str:
    """The time's key, as time_key reads a saved one's: the digit of its kind, then the sort key the module writes."""
    held = write(value, "sqlite")
    tag: dict[str, str] = json.loads(held.text) if isinstance(held, Held) else {}
    return TIME_KINDS[tag["$t"]] + tag["s"]


def _string(value: Any) -> str:
    """
    A value as text, as JavaScript's String writes it: a boolean in lower case; an Enum, or a date, as the text a save
    writes (see storable), so `Status.OPEN` is its value and not its name.
    """
    stored = storable(value)
    if isinstance(stored, bool):
        return "true" if stored else "false"
    return str(stored)
