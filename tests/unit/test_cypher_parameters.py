"""A Cypher parameter is bound as its JSON text, as drivine4j binds one."""

from datetime import timedelta

import pytest

from drivine.cypher.sql.parameters import Parameters, cypher_json


@pytest.mark.parametrize(
    ("duration", "text"),
    [
        (timedelta(0), "PT0S"),
        (timedelta(hours=-1), "P-1DT23H"),
        (timedelta(seconds=10, microseconds=500_000), "PT10.5S"),
        (timedelta(microseconds=1), "PT0.000001S"),
        (timedelta(days=2, minutes=1), "P2DT1M"),
    ],
)
def test_a_timedelta_is_its_days_and_its_time_as_the_neo4j_driver_sends_it(duration: timedelta, text: str):
    assert cypher_json(duration) == f'"{text}"'


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ({3}, "[3]"),
        (frozenset({3}), "[3]"),
        ((1, 2), "[1,2]"),
        (iter([1, 2]), "[1,2]"),
        ((it for it in [1, 2]), "[1,2]"),
        (b"\x00\xffdrivine", '"AP9kcml2aW5l"'),
        (bytearray(b"\x00\xff"), '"AP8="'),
    ],
)
def test_a_collection_is_a_list_and_bytes_their_base64_as_drivine4j_writes_them(value: object, text: str):
    assert cypher_json(value) == text


def test_an_iterator_named_twice_is_read_once():
    parameters = Parameters({"ids": (it for it in [1, 2])}, lambda marker: marker, lambda marker: marker)
    parameters.json("ids")
    parameters.json("ids")
    assert parameters.values == {"_cy_ids": "[1,2]"}
