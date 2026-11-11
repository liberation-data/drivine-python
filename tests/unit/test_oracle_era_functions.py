"""
On Oracle a text or a timestamp whose year may be BC is renumbered by the functions the store keeps beside the graph's
tables, `<graph>_era` and `<graph>_era_date` (see `OracleGraph.era`), each operand written once: the test and the
regular expression they stand for wrote it three or four times, and Oracle took up to a minute to plan a statement of
temporal arithmetic in a subquery (liberation-data/drivine#572). A store without them has each statement compiled as
before. drivine4j's OracleEraFunctionTest of liberation-data/drivine4j#584, and its OracleNamesTest and probe's cases.
The SQL itself is the shared compiler's: these test the texts the store keeps and what a session may call.
"""

import itertools
import logging
from typing import Any

import oracledb
import pytest

from drivine import QuerySpecification, cypher_statement
from drivine.backend.oracle import oracle_stored_functions
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.oracle.oracle_stored_functions import (
    OracleStoredFunctions,
    era_date_function,
    era_function,
)
from drivine.connection.oracle.oracle_connection import OracleConnection
from drivine.connection.oracle.oracle_spec_compiler import OracleSpecCompiler
from drivine.cypher.cypher_specification import cypher_compiled
from tests.unit.test_oracle_stored_functions import ora

# The lines of drivine4j's `OracleCypherCompiler.eraFunction` and `eraDateFunction`, copied verbatim from drivine4j's
# main at liberation-data/drivine4j#584, `$name` and `${TemporalSql.BC_BELOW}` too: Kotlin's `"$name"` is the function's
# name, and `BC_BELOW` is 49. The store keeps these functions, so their texts are part of the store layout the three
# libraries share, byte for byte. A change to them is made in drivine4j first and copied here again.
# fmt: off
DRIVINE4J_ERA = [
    "FUNCTION $name(t VARCHAR2) RETURN VARCHAR2 DETERMINISTIC IS",
    "BEGIN",
    "    IF ASCII(t) < ${TemporalSql.BC_BELOW} THEN",
    "        RETURN REGEXP_REPLACE(t, '^(-[0-9]{4}|0000)', TO_CHAR(TO_NUMBER(REGEXP_SUBSTR(t, '^-?[0-9]{4}')) - 1, 'FM0000'));",  # noqa: E501
    "    END IF;",
    "    RETURN t;",
    "END;",
]
DRIVINE4J_ERA_DATE = [
    "FUNCTION $name(t VARCHAR2) RETURN VARCHAR2 DETERMINISTIC IS",
    "BEGIN",
    "    IF SUBSTR(t, 1, 1) = '-' THEN",
    "        RETURN TO_CHAR(TO_NUMBER(SUBSTR(t, 1, 5)) + 1, 'FM0000') || SUBSTR(t, 6);",
    "    END IF;",
    "    RETURN t;",
    "END;",
]
# fmt: on


def drivine4j(lines: list[str], name: str) -> str:
    """The text drivine4j makes of the function `name`: its lines joined by `\\n`, with no newline after the last."""
    return "\n".join(lines).replace("${TemporalSql.BC_BELOW}", "49").replace("$name", name)


def test_the_era_functions_are_drivine4js_texts_byte_for_byte_as_create_or_replace_of_them():
    assert era_function("myGraph_era") == drivine4j(DRIVINE4J_ERA, "myGraph_era")
    assert era_date_function("myGraph_era_date") == drivine4j(DRIVINE4J_ERA_DATE, "myGraph_era_date")
    graph = OracleGraph("myGraph")
    assert (graph.era, graph.era_date) == ("myGraph_era", "myGraph_era_date")
    assert graph.functions[1:] == [
        f"CREATE OR REPLACE {drivine4j(DRIVINE4J_ERA, 'myGraph_era')}",
        f"CREATE OR REPLACE {drivine4j(DRIVINE4J_ERA_DATE, 'myGraph_era_date')}",
    ]


# Whether a session may call them, asked of a session whose checks of each give what a test says.


CHECKS = (("nulls_equal", "_nulls_equal(JSON('[1]'), JSON('[1]'))"), ("era", "_era('0000-01-01')"))
"""What each check calls, by the function it asks of."""


class Cursor:
    def __init__(self, session: "Session") -> None:
        self.session = session
        self.description: Any = None

    def prepare(self, statement: str, tag: str | None = None, cache_statement: bool = True) -> None:
        pass

    async def execute(self, statement: str, parameters: Any = None, **_: Any) -> None:
        self.session.run.append(statement)
        for function, check in CHECKS:
            if statement.startswith("SELECT ") and check in statement:
                outcome = self.session.outcomes[function].pop(0)
                if outcome != "found":
                    raise ora(outcome)
                self.description = [("R",)]

    async def fetchall(self) -> list[Any]:
        return [(1,)] if self.description else []

    def getimplicitresults(self) -> list[Any]:
        return []

    def close(self) -> None:
        pass


class Session:
    """A session whose checks of each function, `nulls_equal` and `era`, give their outcomes in turn."""

    def __init__(self, nulls_equal: tuple[int | str, ...] = (), era: tuple[int | str, ...] = ()) -> None:
        self.outcomes = {"nulls_equal": list(nulls_equal), "era": list(era)}
        self.run: list[str] = []
        self.autocommit = True

    def cursor(self) -> Cursor:
        return Cursor(self)

    @property
    def checks(self) -> list[str]:
        return [it for it in self.run if it.startswith("SELECT ") and it.endswith("FROM DUAL")]

    @property
    def statements(self) -> list[str]:
        return [it for it in self.run if it not in self.checks]


_graphs = itertools.count()


def a_graph() -> str:
    """A graph of its own for each test: what a session found is remembered for the process."""
    return f"era_{next(_graphs)}_{id(object())}"


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    at = [1_000.0]
    monkeypatch.setattr(oracle_stored_functions, "clock", lambda: at[0])
    return at


def connection(session: Session, graph: str) -> OracleConnection:
    return OracleConnection(session, None, "s", graph, "db|me")


async def test_both_era_functions_are_asked_of_in_one_query_and_a_yes_is_remembered(now):
    g = a_graph()
    found = Session(era=("found",))
    assert await connection(found, g)._calls_era()
    assert await connection(found, g)._calls_era()
    assert found.checks == [f"SELECT {g}_era('0000-01-01'), {g}_era_date('-0001-01-01') FROM DUAL"]


async def test_the_era_functions_are_asked_of_apart_from_nulls_equal_and_warned_of_once(
    now, caplog: pytest.LogCaptureFixture
):
    # A store ensured by a version before drivine4j#584 has nulls_equal and not these.
    g = a_graph()
    asked = Session(nulls_equal=("found",), era=(904, 904))
    with caplog.at_level(logging.WARNING, logger="drivine.oracle"):
        assert await connection(asked, g)._calls_nulls_equal()
        assert not await connection(asked, g)._calls_era()
        assert not await connection(asked, g)._calls_era()
        now[0] += OracleStoredFunctions.ABSENT_FOR
        assert not await connection(asked, g)._calls_era()
        assert await connection(asked, g)._calls_nulls_equal()
    assert len(asked.checks) == 3
    warned = [it.getMessage() for it in caplog.records]
    assert len([it for it in warned if f"has no function {g}_era and {g}_era_date" in it]) == 1, warned
    assert not [it for it in warned if f"{g}_nulls_equal" in it], warned
    assert "it gives the same rows" in next(it for it in warned if f"{g}_era and" in it)


@pytest.mark.parametrize("code", [904, 942, 1031, 6575])
async def test_the_errors_of_an_era_function_there_is_none_of_mean_they_are_missing(now, code: int):
    assert not await connection(Session(era=(code,)), a_graph())._calls_era()


async def test_another_failure_of_the_era_check_is_the_statements_to_report(now):
    g = a_graph()
    with pytest.raises(oracledb.DatabaseError):
        await connection(Session(era=(20001,)), g)._calls_era()
    assert await connection(Session(era=("found",)), g)._calls_era()


WRITE = (
    "CREATE (n:CyWrite {day: date('-0044-03-15') + duration('P1D')}) WITH n, [null] AS a, [1] AS b "
    "RETURN [coalesce(a = b, 'null'), toString(n.day)]"
)
"""A statement that writes, calling both nulls_equal and the era functions."""


@pytest.mark.parametrize(
    ("nulls_equal", "era"),
    [("found", "found"), ("found", 904), (904, "found"), (904, 904)],
    ids=["both", "nulls-equal-alone", "era-alone", "neither"],
)
async def test_a_statement_is_compiled_again_to_call_only_the_functions_the_session_may_call(
    now, nulls_equal: int | str, era: int | str
):
    g = a_graph()
    session = Session(nulls_equal=(nulls_equal,), era=(era,))
    await connection(session, g).query(QuerySpecification[Any](cypher_statement(WRITE)))
    (written,) = session.statements
    assert (f"{g}_nulls_equal(" in written) == (nulls_equal == "found"), written
    assert (f"{g}_era(" in written and f"{g}_era_date(" in written) == (era == "found"), written
    assert ("_era" in written) == (era == "found"), written
    assert len(session.checks) == 2
    # As compiled with what the session may call, its parameters bound as OracleConnection binds them.
    sql, _ = cypher_compiled(
        QuerySpecification[Any](cypher_statement(WRITE)).finalized_copy("SQL"),
        "ORACLE",
        g,
        nulls_equal_stored=nulls_equal == "found",
        era_stored=era == "found",
    )
    assert written == OracleSpecCompiler(sql).compile().statement


async def test_a_statement_with_no_year_to_renumber_asks_nothing_of_the_era_functions(now):
    session = Session()
    await connection(session, a_graph()).query(QuerySpecification[Any](cypher_statement("RETURN date('2024-03-15')")))
    assert session.checks == []


async def test_the_graph_ensured_has_every_session_ask_again_of_the_era_functions(now):
    g = a_graph()
    asked = Session(era=(904, "found"))
    assert not await connection(asked, g)._calls_era()
    OracleGraph(g).ensured()
    assert await connection(asked, g)._calls_era()
