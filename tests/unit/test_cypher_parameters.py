"""A Cypher parameter is bound as its JSON text, as drivine4j binds one."""

from datetime import timedelta
from decimal import Decimal

import pytest

from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.parameters import Parameters, cypher_json
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler


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
    parameters = Parameters({"ids": (it for it in [1, 2])}, lambda marker, _: marker, lambda marker: marker)
    parameters.json("ids")
    parameters.json("ids")
    assert parameters.values == {"_cy_ids": "[1,2]"}


def test_on_oracle_a_whole_float_parameter_is_bound_as_extended_jsons_double_and_a_whole_decimal_as_its_number():
    # As drivine4j's OracleParameterTextTest: `JSON(… EXTENDED)` reads such a double, where `JSON(…)` reads 2.0 as 2.
    compiled = OracleCypherCompiler("g").compile(
        "RETURN [$d, $f, $m]", {"d": Decimal("12345678901234567890"), "f": 2.0, "m": {"$numberDouble": "2.0"}}
    )
    assert compiled.parameters["_cy_d"] == "12345678901234567890"
    assert compiled.parameters["_cy_f"] == '{"$numberDouble":"2.0"}'
    # A map whose text is the float's is read as a map: only the text a float was written into is read EXTENDED.
    assert compiled.parameters["_cy_m"] == '{"$numberDouble":"2.0"}'
    assert "$_cy_f EXTENDED" in compiled.sql
    assert "$_cy_d EXTENDED" not in compiled.sql and "$_cy_m EXTENDED" not in compiled.sql


def test_on_oracle_a_procedure_reads_its_parameters_extended_only_where_they_hold_a_whole_float():
    assert "EXTENDED" in OracleCypherCompiler("g").compile("CREATE (n:T {f: $f})", {"f": 2.0}).sql
    assert "EXTENDED" not in OracleCypherCompiler("g").compile("CREATE (n:T {f: $f})", {"f": 2.5}).sql
    assert "EXTENDED" not in PostgresCypherCompiler("g").compile("RETURN $f", {"f": 2.0}).sql


def test_on_oracle_a_write_binds_its_parameters_as_text_and_reads_extended_only_those_that_hold_a_whole_float():
    # As drivine4j's OracleParameterTextTest: a JSON variable of the parameters, read once for each row, crashed
    # Oracle's session past 32 KB (#131).
    compiled = OracleCypherCompiler("g").compile(
        "MATCH (n) SET n.a = $a, n.f = $f, n.p = $p.x", {"a": "x", "f": 2.0, "p": {"x": [1.0]}}
    )
    sql = compiled.sql
    assert "cy_params CLOB :=" in sql and "cy_params JSON" not in sql, sql
    assert "JSON_QUERY(cy_params, '$.a' RETURNING JSON)" in sql, sql
    assert "JSON_QUERY(cy_params, '$.a' RETURNING CLOB)" not in sql, sql
    for key in ("f", "p"):
        assert f"JSON(JSON_QUERY(cy_params, '$.{key}' RETURNING CLOB) EXTENDED)" in sql, sql
    assert (
        compiled.parameters["_cy_params"] == '{"a":"x","f":{"$numberDouble":"2.0"},"p":{"x":[{"$numberDouble":"1.0"}]}}'
    )


def test_on_oracle_a_write_reads_extended_only_a_parameter_named_so_not_a_key_of_another_of_that_name():
    # A key of a map parameter named as a parameter that holds a whole float is read from that map, as JSON.
    sql = OracleCypherCompiler("g").compile("MATCH (n) SET n.a = $p.f, n.f = $f", {"f": 2.0, "p": {"f": "y"}}).sql
    assert "JSON(JSON_QUERY(cy_params, '$.f' RETURNING CLOB) EXTENDED)" in sql, sql
    assert "'$.p' RETURNING CLOB" not in sql and "'$.f' RETURNING JSON" in sql, sql
    # A parameter keyed as extended JSON's are keeps a whole float beside it from being written as extended JSON.
    compiled = OracleCypherCompiler("g").compile(
        "MATCH (n) SET n.m = $m.x, n.f = $f", {"f": 2.0, "m": {"$numberLong": "5", "x": 1}}
    )
    assert "EXTENDED" not in compiled.sql, compiled.sql
    assert compiled.parameters["_cy_params"] == '{"f":2.0,"m":{"$numberLong":"5","x":1}}'


def test_on_oracle_a_whole_float_of_a_float_subclass_is_written_as_json_writes_its_number():
    # As numpy's float64 is: its repr, np.float64(2.0), is no number Oracle reads.
    class Float(float):
        def __repr__(self) -> str:
            return f"Float({float(self)})"

    compiled = OracleCypherCompiler("g").compile("RETURN $f", {"f": Float(2.0)})
    assert compiled.parameters["_cy_f"] == '{"$numberDouble":"2.0"}'


def test_on_oracle_a_value_holding_a_map_keyed_as_extended_json_is_written_as_plain_json():
    # `JSON(… EXTENDED)` would read {"$numberLong": "5"} as 5: such a value is written as JSON is, its whole floats too.
    compiled = OracleCypherCompiler("g").compile("RETURN $p", {"p": {"a": 2.0, "m": [{"$numberLong": "5"}]}})
    assert compiled.parameters["_cy_p"] == '{"a":2.0,"m":[{"$numberLong":"5"}]}'
    assert "EXTENDED" not in compiled.sql
    # In a write every parameter is read from one object, so one such map keeps them all plain.
    assert (
        "EXTENDED" not in OracleCypherCompiler("g").compile("CREATE (n {f: $f, l: $l})", {"f": 2.0, "l": {"$x": 1}}).sql
    )
