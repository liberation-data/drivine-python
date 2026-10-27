"""
A list of zoned values, and a list of lists of them, compiled: on Oracle it sorts, and takes its `min` and `max`, as its
JSON, by the text of each item, as Oracle takes many minutes to parse the key that sorts it by moments over a list
worked out item by item with temporal arithmetic (liberation-data/drivine4j#331); on Postgres by their moments (#181).
drivine4j's OracleZonedListSortTest of liberation-data/drivine4j#199. Not in the spec yet (#199).
"""

from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler

UNWOUND = "UNWIND ['2024-03-15T10:00:00+02:00', '2024-03-15T09:00:00Z'] AS at"
SORTED = f"{UNWOUND} WITH [datetime(at)] AS l RETURN 1 ORDER BY l"
EXTREME = f"{UNWOUND} WITH [[datetime(at)]] AS l RETURN min(l), max(l)"


def test_on_oracle_a_list_of_zoned_values_or_of_lists_of_them_sorts_and_takes_its_extremes_with_no_key_of_moments():
    for cypher in (SORTED, EXTREME):
        sql = OracleCypherCompiler("g").compile(cypher).sql
        assert "LISTAGG" not in sql and " mk1" not in sql, sql


def test_on_postgres_a_list_of_zoned_values_or_of_lists_of_them_sorts_and_takes_its_extremes_by_their_moments():
    for cypher in (SORTED, EXTREME):
        sql = PostgresCypherCompiler("g").compile(cypher).sql
        assert "string_agg" in sql and " mk1" in sql, sql
