"""
A year as Neo4j and ISO 8601 number it, `0000` for 1 BC and `-0044` for 45 BC, compiled for Postgres and Oracle, which
number a year BC otherwise (#326, a port of liberation-data/drivine4j#469). Drivine for TypeScript's
TemporalText.spec.ts of liberation-data/drivine#564. The spec's case reads and writes such years; these pin what its
results don't show.
"""

import re

import pytest

from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler
from drivine.cypher.sql.sql_cypher_compiler import SqlCypherCompiler
from drivine.cypher.sql.temporal import Temporal
from drivine.cypher.sql.temporals import canonical

COMPILERS: dict[str, type[SqlCypherCompiler]] = {"postgres": PostgresCypherCompiler, "oracle": OracleCypherCompiler}


@pytest.mark.parametrize(
    ("text", "kind", "written"),
    [
        ("0000-01-01", Temporal.DATE, "0000-01-01"),
        ("-0001-12-31", Temporal.DATE, "-0001-12-31"),
        ("-0044-03-15", Temporal.DATE, "-0044-03-15"),
        ("0000-02-29", Temporal.DATE, "0000-02-29"),
        ("-0004-02-29", Temporal.DATE, "-0004-02-29"),
        ("-0044-03-15T12:00:00.5+0200", Temporal.DATE_TIME, "-0044-03-15T12:00:00.500+02:00"),
        ("-0044-03-15T12:00:00.5+02:00", Temporal.DATE, "-0044-03-15"),
        ("-0044-03-15T12:00", Temporal.LOCAL_DATE_TIME, "-0044-03-15T12:00:00"),
        ("-0044-03-15T12:00:00.5+02:00", Temporal.LOCAL_TIME, "12:00:00.500"),
        ("12345-01-01", Temporal.DATE, None),
        ("-12345-01-01", Temporal.DATE, None),
    ],
)
def test_the_canonical_text_of_a_year_before_1_is_written_back_as_it_reads(
    text: str, kind: Temporal, written: str | None
) -> None:
    # A year of five digits is of no kind, as in drivine4j.
    assert canonical(text, kind) == written
    if written is not None:
        assert canonical(written, kind) == written


@pytest.mark.parametrize(
    ("text", "ad"),
    [
        ("1000-01-01", True),
        ("9999-12-31", True),
        ("0999-12-31", False),
        ("0001-01-01", False),
        ("0000-01-01", False),
        ("-0044-03-15", False),
    ],
)
def test_only_a_text_of_a_year_from_1000_on_has_the_pattern_read_with_no_test_of_its_era(text: str, ad: bool) -> None:
    # A year after 0 but before 1000, `0999`, opens with `0` as 1 BC does, `0000`, so is read by its era.
    for kind, written in (
        (Temporal.DATE, text),
        (Temporal.LOCAL_DATE_TIME, f"{text}T12:00"),
        (Temporal.DATE_TIME, f"{text}T12:00:00+02:00"),
    ):
        assert kind.ad_pattern is not None
        assert bool(re.search(kind.ad_pattern, written)) is ad, (kind, written)
        assert re.search(kind.pattern, written), (kind, written)


def test_a_kind_of_no_date_has_no_pattern_of_a_year_from_1000_on() -> None:
    assert [it.ad_pattern for it in (Temporal.LOCAL_TIME, Temporal.TIME, Temporal.DURATION)] == [None, None, None]


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
def test_a_text_of_a_year_from_1000_on_is_compared_with_no_test_of_its_era_ahead_of_a_text_of_any_year(
    compiler: type[SqlCypherCompiler],
) -> None:
    # The store gives the same rows without the first WHEN, so only the SQL shows it is there: an ordinary date then
    # costs no more than its kind's test.
    sql = compiler("g").compile("MATCH (n:C) WHERE n.day < date('0001-01-01') RETURN n.id").sql
    where = sql[sql.index("CASE WHEN", sql.index("WHERE")) :]
    ad, any_year = (where.find(f"'{it}'") for it in (Temporal.DATE.ad_pattern, Temporal.DATE.pattern))
    assert -1 < ad < any_year, where
    # The era is tested for only after the kind's own test.
    assert "ascii(" not in where[:any_year].lower(), where
    assert "ascii(" in where[any_year:].lower(), where
