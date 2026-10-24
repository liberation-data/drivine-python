"""A Cypher parameter is bound as its JSON text, as drivine4j binds one."""

from datetime import timedelta

import pytest

from drivine.cypher.sql.parameters import cypher_json


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
