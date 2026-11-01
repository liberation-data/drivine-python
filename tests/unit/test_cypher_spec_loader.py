"""The Cypher spec's files are read as drivine4j reads them: YAML 1.2's core schema, not PyYAML's YAML 1.1."""

import math

import pytest
import yaml

from tests.cypher.cypher_spec import Step, _Loader, check_rows, expanded  # pyright: ignore[reportPrivateUsage]


def test_a_spec_file_is_read_with_yaml_1_2s_core_schema():
    read = yaml.load(  # noqa: S506
        "[2, 2.0, 1e3, true, yes, on, '10:30', 10:30, 2024-01-01, 2024-01-01T10:00:00Z, ~]", Loader=_Loader
    )
    assert read == [2, 2.0, 1000.0, True, "yes", "on", "10:30", "10:30", "2024-01-01", "2024-01-01T10:00:00Z", None]
    assert [type(it) for it in read[:3]] == [int, float, float]


def test_a_float_the_spec_names_is_that_float_and_nan_in_the_rows_is_the_same_as_nan_there():
    nan, infinity = float("nan"), float("inf")
    given = expanded({"p": {"$float": "NaN"}, "l": [{"$float": "Infinity"}, {"$float": "-Infinity"}]})
    assert math.isnan(given["p"])
    assert given["l"] == [infinity, -infinity]
    step = Step("RETURN x", rows=[[{"$float": "NaN"}, [{"$float": "Infinity"}], {"x": {"$float": "-Infinity"}}]])
    check_rows(step, [[nan, [infinity], {"x": -infinity}]], "")
    # A store that gives null, the text "NaN", another number, or an infinity of the other sign, for one, gives other
    # rows.
    for given_rows in (
        [[None, [infinity], {"x": -infinity}]],
        [["NaN", [infinity], {"x": -infinity}]],
        [[1.0, [infinity], {"x": -infinity}]],
        [[nan, [-infinity], {"x": -infinity}]],
        [[nan, [infinity], {"x": infinity}]],
    ):
        with pytest.raises(AssertionError):
            check_rows(step, given_rows, "")
    with pytest.raises(AssertionError):
        check_rows(Step("RETURN x", rows=[[1.0]]), [[nan]], "")
