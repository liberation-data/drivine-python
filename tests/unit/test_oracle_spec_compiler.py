"""
Oracle's `$name` parameters become `:b0`, `:b1`, …, only those the statement names outside quoted text bound. A port of
Drivine for TypeScript's OracleSpecCompiler.spec cases of liberation-data/drivine#789 (drivine-python#541).
"""

import math
from decimal import Decimal

from drivine import QuerySpecification
from drivine.connection.oracle.oracle_spec_compiler import OracleSpecCompiler
from drivine.query.compiled_query import CompiledQuery


def compiled(spec: QuerySpecification[object]) -> CompiledQuery:
    return OracleSpecCompiler(spec.finalized_copy("SQL")).compile()


def test_binds_each_named_parameter_as_a_numbered_bind():
    result = compiled(QuerySpecification("select $a, $b, $a from dual").bind({"a": 1, "b": "x"}))
    assert result.statement == "select :b0, :b1, :b0 from dual"
    assert result.parameters == {"b0": 1, "b1": "x"}


def test_leaves_a_parameters_name_in_a_json_path_as_it_is_and_binds_the_placeholder():
    # The module's SQL names a temporal tag's kind `$t` in a JSON path.
    spec = QuerySpecification("""select json_value(n.props, '$."at"."$t"') from n where kind = $t""").bind(
        {"t": "datetime"}
    )
    result = compiled(spec)
    assert result.statement == """select json_value(n.props, '$."at"."$t"') from n where kind = :b0"""
    assert result.parameters == {"b0": "datetime"}


def test_leaves_a_parameters_name_in_a_q_literal_or_a_quoted_identifier_as_it_is():
    spec = QuerySpecification(
        """select json_value(n.props, '$."$t"'), q'[$u]', "$t" from n where kind = $t and v$u = $u"""
    ).bind({"t": "a", "u": 2})
    result = compiled(spec)
    assert result.statement == (
        """select json_value(n.props, '$."$t"'), q'[$u]', "$t" from n where kind = :b0 and v$u = :b1"""
    )
    assert result.parameters == {"b0": "a", "b1": 2}


def test_binds_no_parameter_named_only_in_quoted_text():
    result = compiled(QuerySpecification("""select '$."$t"' from dual""").bind({"t": "a"}))
    assert result.statement == """select '$."$t"' from dual"""
    assert result.parameters == {}


def test_binds_a_decimal_that_is_no_finite_number_as_the_float_it_stands_for():
    # Not as the JSON text of one, "Infinity", which Oracle reads as no number (drivine-python#303).
    given = {"inf": Decimal("Infinity"), "ninf": Decimal("-Infinity"), "nan": Decimal("NaN"), "snan": Decimal("sNaN")}
    result = compiled(QuerySpecification("select $inf, $ninf, $nan, $snan from dual").bind(given))
    assert result.parameters["b0"] == math.inf
    assert result.parameters["b1"] == -math.inf
    assert math.isnan(result.parameters["b2"])
    assert math.isnan(result.parameters["b3"])
