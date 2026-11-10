"""
The shared compiler's adapter (drivine/cypher/shared_compiler.py): a statement's parameters written as the tagged JSON
the compiler reads (its README, "drivine4j's own calls"), the values its answer binds read back, its refusals, errors
and warnings, and iterators read once, so a statement compiled again binds their items too
(liberation-data/drivine-python#471).
"""

import logging
import math
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum, IntEnum
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from drivine import DrivineError, QuerySpecification, cypher_statement
from drivine.cypher import shared_compiler
from drivine.cypher.cypher_specification import cypher_compiled
from drivine.cypher.shared_compiler import compile_cypher, decoded, drained, duration, written


class Colour(Enum):
    RED = "red"


class Rank(IntEnum):
    ONE = 1


def letters() -> Iterator[str]:
    yield "a"
    yield "b"


SYDNEY = ZoneInfo("Australia/Sydney")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, True),
        (False, False),
        (0, 0),
        (2**70, 2**70),
        (-(2**70), -(2**70)),
        ("$float", "$float"),
        (2.0, 2.0),
        (-0.0, -0.0),
        (1e-7, 1e-7),
        (math.nan, {"$float": "NaN"}),
        (math.inf, {"$float": "Infinity"}),
        (-math.inf, {"$float": "-Infinity"}),
        (Decimal("3"), 3),
        (Decimal("3.00"), 3),
        (Decimal("1E+30"), 10**30),
        (Decimal("2.5"), 2.5),
        (Decimal("Infinity"), {"$float": "Infinity"}),
        (Decimal("-Infinity"), {"$float": "-Infinity"}),
        (Decimal("NaN"), {"$float": "NaN"}),
        (datetime(2026, 11, 7, 10, 30), {"$temporal": "LOCAL_DATE_TIME", "text": "2026-11-07T10:30:00"}),
        (
            datetime(2026, 11, 7, 10, 30, 0, 123456, tzinfo=SYDNEY),
            {"$temporal": "DATE_TIME", "text": "2026-11-07T10:30:00.123456+11:00"},
        ),
        (
            datetime(2026, 11, 7, 10, 30, tzinfo=UTC),
            {"$temporal": "DATE_TIME", "text": "2026-11-07T10:30:00+00:00"},
        ),
        (date(2026, 1, 2), {"$temporal": "DATE", "text": "2026-01-02"}),
        (time(10, 30, 0, 5), {"$temporal": "LOCAL_TIME", "text": "10:30:00.000005"}),
        (
            time(10, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
            {"$temporal": "TIME", "text": "10:30:00+05:30"},
        ),
        (timedelta(days=1, hours=2), {"$temporal": "DURATION", "text": "P1DT2H"}),
        (b"\x00\xff", {"$bytes": "AP8="}),
        (bytearray(b"\x00\xff"), {"$bytes": "AP8="}),
        (memoryview(b"\x00\xff"), {"$bytes": "AP8="}),
        (Colour.RED, "red"),
        (Rank.ONE, 1),
        ((1, "a"), [1, "a"]),
        (frozenset([1]), [1]),
        ({1}, [1]),
        (letters(), ["a", "b"]),
        ({"a": [Colour.RED, math.nan]}, {"a": ["red", {"$float": "NaN"}]}),
        ({1: "a"}, {"1": "a"}),
        ({"$float": "2.0"}, {"$map": {"$float": "2.0"}}),
        ({"a": {"$b": 1}}, {"a": {"$map": {"$b": 1}}}),
    ],
    ids=repr,
)
def test_each_value_is_written_as_the_compilers_readme_tags_it(value: Any, expected: Any):
    given = written(value)
    assert given == expected
    # A bool is no int, and an int no float: JSON tells them apart, so must this.
    assert type(given) is type(expected)
    if isinstance(expected, float) and expected == 0:
        assert math.copysign(1, given) == math.copysign(1, expected)


def test_a_value_of_another_type_is_refused_naming_it():
    with pytest.raises(DrivineError, match="A parameter of type object can't be bound to a Cypher statement."):
        written({"a": [object()]})


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (timedelta(), "PT0S"),
        (timedelta(hours=-1), "P-1DT23H"),
        (timedelta(microseconds=-1), "P-1DT23H59M59.999999S"),
        (timedelta(days=400, seconds=5), "P400DT5S"),
        (timedelta(minutes=1, microseconds=500000), "PT1M0.5S"),
        (timedelta(days=-2), "P-2D"),
    ],
)
def test_a_timedelta_is_a_duration_of_its_days_and_its_time_as_the_neo4j_driver_sends_one(value: timedelta, text: str):
    assert duration(value) == text


def test_the_tagged_values_the_answer_binds_are_read_back_at_any_depth():
    assert decoded({"$float": "2.0"}) == 2.0 and type(decoded({"$float": "2.0"})) is float
    assert decoded({"$float": 2}) == 2.0 and type(decoded({"$float": 2})) is float
    assert math.isnan(decoded({"$float": "NaN"}))
    assert decoded({"$float": "-Infinity"}) == -math.inf
    assert decoded({"$decimal": "2.50"}) == Decimal("2.50") and str(decoded({"$decimal": "2.50"})) == "2.50"
    assert decoded({"a": [{"$float": "1.5"}, {"b": {"$decimal": "3"}}], "c": "text", "d": 7}) == {
        "a": [1.5, {"b": Decimal("3")}],
        "c": "text",
        "d": 7,
    }
    # Only the map of the one key is a tag.
    assert decoded({"$float": "2.0", "other": 1}) == {"$float": "2.0", "other": 1}


def test_a_map_with_a_dollar_key_is_bound_as_the_map_it_is():
    compiled = compile_cypher("RETURN $x AS x", {"x": {"$a": 1, "b": [{"$float": "c"}]}}, "SQLITE", "g")
    assert compiled.parameters == {"_cy_x": '{"$a":1,"b":[{"$float":"c"}]}'}


def test_a_statement_given_nan_or_an_infinity_says_so():
    assert compile_cypher("RETURN $x AS x", {"x": math.nan}, "SQLITE", "g").non_finite
    assert compile_cypher("RETURN $x AS x", {"x": Decimal("-Infinity")}, "POSTGRES", "g").non_finite
    assert not compile_cypher("RETURN $x AS x", {"x": 1.5}, "POSTGRES", "g").non_finite


def test_a_refusal_is_a_drivine_error_with_the_compilers_message_and_the_statements_spec():
    spec = QuerySpecification[Any](cypher_statement("MATCH (n) WHERE n.a = $x RETURN n"))
    for store in ("POSTGRES", "ORACLE", "SQLITE"):
        with pytest.raises(DrivineError) as refused:
            cypher_compiled(spec, store, "g")
        assert str(refused.value) == "The statement has a parameter $x, and no value is bound to it."
        assert refused.value.spec is spec


def test_a_request_the_compiler_cant_read_is_a_drivine_error_saying_so(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(shared_compiler, "_compile", lambda _: '{"error": "nested too deep"}')
    spec = QuerySpecification[Any](cypher_statement("RETURN 1"))
    with pytest.raises(DrivineError, match="^The Cypher compiler can't read the request: nested too deep$") as error:
        cypher_compiled(spec, "SQLITE", "g")
    assert error.value.spec is spec


def test_what_the_compiler_warns_of_is_logged(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING, logger="drivine.cypher.shared_compiler"):
        compile_cypher("RETURN 1 AS a, 2 AS b", {}, "SQLITE", "g")
    assert [it.getMessage() for it in caplog.records if it.name == "drivine.cypher.shared_compiler"] == [
        "The statement returns 2 items (a, b). Prefer returning a single map or value, such as RETURN { key1: val1, "
        "key2: val2 }: on Oracle, Postgres and SQLite it is returned as a list of its items, as drivine gives a row of "
        "several columns."
    ]


def test_an_iterator_at_any_depth_is_drained_to_its_items_and_whatever_holds_none_is_kept():
    kept = [1, [2, "a"], (3,)]
    items = (1, 2)
    given = drained({"a": letters(), "b": {"c": [letters(), 1]}, "d": kept, "e": items, "f": {1, 2}, "g": b"x"})
    assert given == {"a": ["a", "b"], "b": {"c": [["a", "b"], 1]}, "d": kept, "e": items, "f": {1, 2}, "g": b"x"}
    assert given["d"] is kept and given["e"] is items
    # A list that holds one is a new list, and a tuple a list; the caller's is left as it was.
    inner = [letters()]
    assert drained({"x": (inner,)})["x"] == [[["a", "b"]]]
    assert isinstance(inner[0], Iterator)
    # An iterable that is no list, as a range or a dict's values, is its items.
    assert drained({"r": range(2), "v": {"k": 1}.values()}) == {"r": [0, 1], "v": [1]}


@pytest.mark.parametrize("store", ["POSTGRES", "ORACLE", "SQLITE"])
def test_drained_parameters_are_bound_alike_by_a_second_compile(store: Any):
    parameters = drained({"p": {"ids": letters()}})
    first = compile_cypher("MATCH (n) WHERE n.id IN $p.ids RETURN n", parameters, store, "g").parameters
    second = compile_cypher("MATCH (n) WHERE n.id IN $p.ids RETURN n", parameters, store, "g").parameters
    listed = compile_cypher("MATCH (n) WHERE n.id IN $p.ids RETURN n", {"p": {"ids": ["a", "b"]}}, store, "g")
    assert first == second == listed.parameters


def test_a_signalling_decimal_nan_is_the_float_nan_as_a_quiet_one_is():
    assert written(Decimal("sNaN")) == {"$float": "NaN"}
