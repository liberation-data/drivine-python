from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Literal

from drivine.backend.oracle.oracle_graph import holding, json_key, labelled_value
from drivine.backend.oracle.oracle_views import OracleViews
from drivine.backend.save_statement_builder import packed as packed_
from drivine.backend.save_statement_builder import stored
from drivine.backend.sql.compared import Compared
from drivine.backend.sql.sql_json import storable
from drivine.backend.sql.sql_query_renderer import SqlQueryRenderer
from drivine.drivine_error import DrivineError
from drivine.dsl.compare_test import CompareTest
from drivine.dsl.condition import Test
from drivine.dsl.model_scope import scope_of
from drivine.dsl.query import Query
from drivine.dsl.query_renderer import described
from drivine.dsl.rendered_query import RenderedQuery
from drivine.model import stamps
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.target_model import TargetModel
from drivine.schema.schema_item import SchemaItem

_PATH_OPERATORS = {"EQ": "==", "NEQ": "!=", "GT": ">", "GTE": ">=", "LT": "<", "LTE": "<="}

_SQL_OPERATORS = {"EQ": "=", "GT": ">", "GTE": ">=", "LT": "<", "LTE": "<="}
"""The comparisons an index answers, as SQL writes them."""


class OracleQueryRenderer(SqlQueryRenderer):
    """
    Writes a query on Oracle (see SqlQueryRenderer). A property is tested in a JSON path filter against a bound value
    (`JSON_EXISTS(props, '$?(@."k" > $v)' PASSING …)`), so numbers, text and booleans compare as their JSON types and a
    missing property meets no comparison, as in Cypher.

    Oracle takes a test of a node's labels for a lookup of one row, so it reads a label's nodes even where a range
    index or a uniqueness constraint would find a few (drivine4j#65). So a comparison with text that every root meets,
    of a property such an index of one of the root's labels begins with, is also written as the index's expression,
    which Oracle reads through the index: see `_through_index`.
    """

    def __init__(
        self,
        views: OracleViews,
        labels_of_class: Callable[[type], list[str]],
        root: str = "",
        root_labels: Sequence[str] = (),
        indexed: Sequence[SchemaItem] = (),
    ) -> None:
        """
        `root` is the root's row, `root_labels` its labels, and `indexed` the indexes a comparison of one of its
        properties may be read through. Without them, none is.
        """
        super().__init__(views, labels_of_class)
        self.root = root
        self.root_labels = list(root_labels)
        self.indexed = list(indexed)
        self.tested: list[str] = []
        """The root's labels whose test a comparison makes, through a labelled index."""

    @staticmethod
    def render(
        query: Query,
        model: TargetModel,
        views: Callable[[dict[str, int]], OracleViews],
        alias: str,
        labels_of_class: Callable[[type], list[str]],
        indexed: Sequence[SchemaItem] = (),
    ) -> RenderedQuery:
        scope = scope_of(model)
        root = model.root if isinstance(model, GraphViewModel) else model
        renderer = OracleQueryRenderer(views(dict(query.depth or {})), labels_of_class, alias, root.labels, indexed)
        return renderer._render(scope, query, alias)

    @staticmethod
    def render_where(
        where: Any, model: TargetModel, views: OracleViews, alias: str, labels_of_class: Callable[[type], list[str]]
    ) -> tuple[str, dict[str, Any]]:
        """A condition over the class's references, written for the store, with the values it binds."""
        return OracleQueryRenderer(views, labels_of_class)._render_only(where, model, alias)

    def _labels_tested(self) -> list[str]:
        return self.tested

    def _property(self, compared: Compared, top: bool) -> str:
        """A test of a property: of the root, which every root meets when `top`, through an index where it can be."""
        column = f"{compared.row}.props" if compared.on_relationship else holding(compared.row, compared.name)
        indexed = (
            self._through_index(compared.test, compared.name, compared.json_packed)
            if top and not compared.on_relationship and compared.row == self.root
            else None
        )
        tested = self._compare(compared.test, column, compared.name, compared.json_packed, compared.path)
        return f"{indexed} AND {tested}" if indexed else tested

    def _any_label(self, row: str, labels: Sequence[str]) -> str:
        passing = ", ".join(f'{self._bind(label)} AS "l{i}"' for i, label in enumerate(labels))
        tested = " || ".join(f"@ == $l{i}" for i in range(len(labels)))
        return f"JSON_EXISTS({row}.labels, '$?({tested})' PASSING {passing})"

    def _seek_test(self, alias: str, property: str, operator: str, value: str) -> str:
        """Each a JSON path filter, so the cursor's values compare as their JSON types."""
        return (
            f"JSON_EXISTS({holding(alias, property)}, '$?(@{json_key(property)} {operator} $v)' PASSING {value} "
            'AS "v")'
        )

    def _ordered_property(
        self, alias: str, property: str, direction: Literal["ASC", "DESC"], nulls: str | None = None
    ) -> str:
        """A property by its JSON type: numbers as numbers."""
        return (
            f"JSON_QUERY({holding(alias, property)}, '${json_key(property)}' RETURNING JSON)"
            f"{' DESC' if direction == 'DESC' else ''}{f' NULLS {nulls}' if nulls else ''}"
        )

    def _read(self, alias: str, property: str, as_: Literal["NUMBER", "TEXT"]) -> str:
        k = json_key(property)
        if as_ == "NUMBER":
            return f"JSON_VALUE({holding(alias, property)}, '${k}' RETURNING NUMBER)"
        return f"JSON_VALUE({holding(alias, property)}, '${k}')"

    def _present(self, alias: str, property: str) -> str:
        return f"JSON_EXISTS({holding(alias, property)}, '$?(@{json_key(property)} != null)')"

    def _through_index(self, test: Test, name: str, json_packed: bool) -> str | None:
        """
        The comparison of the root's property `name` with text as the expression of a range index or a uniqueness
        constraint of one of the root's labels that begins with the property, the first label with one: None where
        there is none, or where the comparison isn't `eq`, `gt`, `gte`, `lt` or `lte` with text of at most 4,000 bytes.

        It is written beside the JSON test, which still decides: the expression is the property as text, so it holds of
        every node the test holds of, and of a number written as the same text too. It is null for a node without the
        label, so it tests the label, and the statement needn't (see labels_tested).
        """
        if not isinstance(test, CompareTest) or json_packed or name == stamps.PROPERTY:
            return None
        operator = _SQL_OPERATORS.get(test.operator)
        value = storable(stored(test.value))
        if operator is None or not isinstance(value, str) or len(value.encode()) > 4000:
            return None
        label = next(
            (it for it in self.root_labels if any(i.label == it and i.properties[:1] == (name,) for i in self.indexed)),
            None,
        )
        if label is None:
            return None
        if label not in self.tested:
            self.tested.append(label)
        return f"{labelled_value(self.root, label, name)} {operator} {self._bind(value)}"

    def _compare(self, test: Test, column: str, name: str, json_packed: bool, path: tuple[str, ...]) -> str:
        """A test of the property `name` of the JSON column `column`."""
        if not isinstance(test, CompareTest):
            raise DrivineError(f"{described(path)} is a property, not a node.")
        k = json_key(name)

        def packed(value: Any) -> Any:
            return packed_(value) if json_packed else storable(stored(value))

        def filtered(expression: str, values: list[Any]) -> str:
            passing = ", ".join(f'{self._bind(packed(value))} AS "v{i}"' for i, value in enumerate(values))
            return f"JSON_EXISTS({column}, '$?({expression})'{f' PASSING {passing}' if passing else ''})"

        present = f"JSON_EXISTS({column}, '$?(@{k} != null)')"
        operator = test.operator
        if operator in _PATH_OPERATORS:
            return filtered(f"@{k} {_PATH_OPERATORS[operator]} $v0", [test.value])
        if operator in ("IN", "NOT_IN"):
            values: Any = test.value
            if not isinstance(values, list):
                raise DrivineError(f"{described(path)} is tested against a list, not {values!r}.")
            listed: list[Any] = values  # pyright: ignore[reportUnknownVariableType]
            any_ = filtered(" || ".join(f"@{k} == $v{i}" for i in range(len(listed))), listed) if listed else "1 = 0"
            return any_ if operator == "IN" else f"{present} AND NOT {any_}"
        if operator == "HAS_ITEM":
            return f"JSON_EXISTS({column}, '${k}[*]?(@ == $v0)' PASSING {self._bind(packed(test.value))} AS \"v0\")"
        if operator == "IS_NULL":
            return f"NOT {present}"
        if operator == "IS_NOT_NULL":
            return present
        return self._compare_text(operator, test.value, f"JSON_VALUE({column}, '${k}')")

    def _compare_text(self, operator: str, value: Any, text: str) -> str:
        """A test of a property's text."""
        if operator == "CONTAINS":
            return f"INSTR({text}, {self._bind(value)}) > 0"
        if operator == "STARTS_WITH":
            return f"INSTR({text}, {self._bind(value)}) = 1"
        if operator == "ENDS_WITH":
            bound = self._bind(value)
            return f"SUBSTR({text}, -LENGTH({bound})) = {bound}"
        if operator == "MATCHES":
            return self._matches(text, str(value))
        if operator == "CONTAINS_IGNORE_CASE":
            return f"INSTR(LOWER({text}), {self._bind(str(value).lower())}) > 0"
        return f"LOWER({text}) = {self._bind(str(value).lower())}"

    def _matches(self, text: str, pattern: str) -> str:
        """
        A regular expression that must match the whole value, as Cypher's `=~` does. A leading `(?i)` ignores case. The
        expression itself is Oracle's, in the POSIX dialect: what Java adds to it, such as `(?:…)`, a look-around or
        `\\b`, is not read, and a flag anywhere but the start is not either.

        The expression is anchored in a group of its own, which POSIX can only write as one that captures: so each group
        the caller wrote is one further on, and a backreference to it is renumbered to match (see `_renumbered`).
        It ends at `\\z`, the value's end, as Oracle's `$` also matches before a line break that ends it.
        """
        ignore_case = pattern.startswith("(?i)")
        body = _renumbered(pattern[4:] if ignore_case else pattern)
        flags = ", 'i'" if ignore_case else ""
        anchored = f"^({body})\\z"
        return f"REGEXP_LIKE({text}, {self._bind(anchored)}{flags})"


def _renumbered(body: str) -> str:
    """
    `body` with each backreference, `\\1` to `\\8`, one higher, for the group that anchors it is the first. An escaped
    backslash is no backreference, nor is a backslash inside a bracket expression, where POSIX reads it as itself. `\\9`
    would need a tenth group, which POSIX does not have, and is refused.
    """
    out: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == "[":
            # A bracket expression is copied whole: `[`, a `^`, a `]` first among its characters, and on to the `]` that
            # ends it.
            end = i + 1
            if body[end : end + 1] == "^":
                end += 1
            if body[end : end + 1] == "]":
                end += 1
            close = body.find("]", end)
            end = len(body) if close < 0 else close + 1
            out.append(body[i:end])
            i = end
        elif c == "\\" and i + 1 < len(body):
            following = body[i + 1]
            if "1" <= following <= "8":
                out.append(f"\\{int(following) + 1}")
            elif following == "9":
                raise DrivineError(
                    f"matches on Oracle reads at most eight groups by reference: '{body}' refers to the ninth, and "
                    "Oracle's expressions have no tenth to anchor it beside."
                )
            else:
                out.append(c + following)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)
