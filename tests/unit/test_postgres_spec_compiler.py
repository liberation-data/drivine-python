"""TypeScript's PostgresSpecCompiler.spec, ported: `$name` parameters become psycopg's, only those used bound."""

import pytest

from drivine import DrivineError, QuerySpecification
from drivine.connection.postgres.postgres_spec_compiler import PostgresSpecCompiler
from drivine.query.compiled_query import CompiledQuery
from drivine.query.statement import QueryLanguage


def compiled(spec: QuerySpecification[object], language: QueryLanguage = "SQL") -> CompiledQuery:
    return PostgresSpecCompiler(spec.finalized_copy(language)).compile()


def test_binds_positional_parameters_by_position():
    result = compiled(QuerySpecification("select $1, $2").bind([1, "foo"]))
    assert result.statement == "select %(p1)s, %(p2)s"
    assert result.parameters == {"p1": 1, "p2": "foo"}


def test_rewrites_named_parameters_to_psycopgs():
    spec = QuerySpecification("select * from person where name = $name and age > $age").bind(
        {"name": "Moon Girl", "age": 13}
    )
    result = compiled(spec)
    assert result.statement == "select * from person where name = %(name)s and age > %(age)s"
    assert result.parameters == {"age": 13, "name": "Moon Girl"}


def test_rewrites_every_occurrence_of_a_named_parameter():
    result = compiled(QuerySpecification("select $id as a, $id as b").bind({"id": 7}))
    assert result.statement == "select %(id)s as a, %(id)s as b"
    assert result.parameters == {"id": 7}


def test_does_not_match_a_name_as_the_prefix_of_a_longer_one():
    spec = QuerySpecification("select * from t where id = $id or id = any($idList)").bind({"id": 1, "idList": [2, 3]})
    result = compiled(spec)
    assert result.statement == "select * from t where id = %(id)s or id = any(%(idList)s)"
    assert result.parameters == {"id": 1, "idList": [2, 3]}


def test_binds_only_the_names_the_statement_uses_as_postgres_refuses_one_it_does_not():
    spec = QuerySpecification("select * from t where name = $name").bind(
        {"name": "Moon Girl", "unused": 1, "threshold": None}
    )
    result = compiled(spec)
    assert result.statement == "select * from t where name = %(name)s"
    assert result.parameters == {"name": "Moon Girl"}


def test_escapes_every_other_percent_sign_where_there_are_parameters():
    spec = QuerySpecification("select * from t where name like '%' || $name || '%'").bind({"name": "Moon"})
    assert compiled(spec).statement == "select * from t where name like '%%' || %(name)s || '%%'"


def test_leaves_a_statement_without_parameters_as_it_is():
    statement = "DO $ensure$ BEGIN EXECUTE format('DROP INDEX %I', 'x'); END $ensure$"
    assert compiled(QuerySpecification(statement)).statement == statement
    unused = compiled(QuerySpecification(statement).bind({"_text": "graph"}))
    assert unused.statement == statement
    assert unused.parameters == {}


def test_applies_skip_and_limit_as_offset_and_limit():
    spec = QuerySpecification("select * from t").skip(10).limit(5)
    assert compiled(spec).statement == "select * from t OFFSET 10 LIMIT 5"


def test_refuses_cypher():
    with pytest.raises(DrivineError, match="CYPHER is not supported on Postgres"):
        compiled(QuerySpecification("match (n) return n"), "CYPHER")
