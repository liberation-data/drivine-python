"""
The function an Oracle graph is made with, `<graph>_nulls_equal`, which a compiled Cypher statement compares two lists
or maps that may hold a null by, and whether a session may call it: drivine4j's tests of liberation-data/drivine4j#323
(#188), and drivine's of liberation-data/drivine#379.
"""

import itertools
import logging
from types import SimpleNamespace
from typing import Any

import oracledb
import pytest

from drivine import QuerySpecification, cypher_statement
from drivine.backend.oracle import oracle_stored_functions
from drivine.backend.oracle.oracle_stored_functions import OracleStoredFunctions
from drivine.connection.oracle.oracle_connection import OracleConnection
from drivine.connection.oracle.oracle_connection_provider import OracleConnectionProvider
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.sync.connection.oracle.oracle_connection import OracleConnection as SyncOracleConnection

pytestmark = pytest.mark.usefixtures("no_labelled_indexes")


class Missing(SimpleNamespace):
    """What python-oracledb's error holds: its number."""


def ora(code: int) -> oracledb.DatabaseError:
    """The error python-oracledb raises with the number `code`."""
    return oracledb.DatabaseError(Missing(code=code, message=f"ORA-{code:05d}"))


class Cursor:
    def __init__(self, session: "Session") -> None:
        self.session = session
        self.description: Any = None

    def prepare(self, statement: str, tag: str | None = None, cache_statement: bool = True) -> None:
        pass

    async def execute(self, statement: str, parameters: Any = None, **_: Any) -> None:
        self.session.run.append(statement)
        self.session.bound.append(parameters)
        if statement.startswith("SELECT ") and "_nulls_equal(JSON('[1]'), JSON('[1]')) FROM DUAL" in statement:
            outcome = self.session.outcomes.pop(0)
            if outcome != "found":
                raise ora(outcome)
            self.description = [("R",)]

    async def fetchall(self) -> list[Any]:
        return [(1,)] if self.description else []

    async def fetchone(self) -> tuple[int]:
        return (7,)

    def getimplicitresults(self) -> list[Any]:
        return []

    def close(self) -> None:
        pass


class Session:
    """A session whose checks of the function give `outcomes` in turn: found, or the error with its number."""

    def __init__(self, *outcomes: int | str) -> None:
        self.outcomes = list(outcomes)
        self.run: list[str] = []
        self.bound: list[Any] = []
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
    return f"unit_{next(_graphs)}_{id(object())}"


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The clock OracleStoredFunctions reads, which a test moves on."""
    at = [1_000.0]
    monkeypatch.setattr(oracle_stored_functions, "clock", lambda: at[0])
    return at


async def calls(session: Session, graph: str, database: str = "db|me") -> bool:
    return await OracleConnection(session, None, "s", graph, database)._calls_nulls_equal()


WRITE = "MATCH (c:Claim) WITH c, [c.a, null] AS l, [c.b] AS m SET c.e = l = m"


async def test_a_session_is_asked_once_and_a_yes_is_remembered_for_the_database_the_user_and_the_graph(now):
    g = a_graph()
    found = Session("found")
    assert await calls(found, g)
    assert await calls(found, g)
    assert found.checks == [f"SELECT {g}_nulls_equal(JSON('[1]'), JSON('[1]')) FROM DUAL"]
    # Another user is asked of its own.
    other = Session(904)
    assert not await calls(other, g, "db|you")
    assert len(other.checks) == 1


@pytest.mark.parametrize("code", [904, 942, 1031, 6575])
async def test_the_errors_of_a_function_there_is_none_of_mean_it_is_missing(now, code: int):
    assert not await calls(Session(code), a_graph())


async def test_any_other_failure_is_the_statements_to_report_and_nothing_is_remembered_of_it(now):
    g = a_graph()
    with pytest.raises(oracledb.DatabaseError):
        await calls(Session(20001), g)
    with pytest.raises(oracledb.DatabaseError):
        await calls(Session(3113), g)
    assert await calls(Session("found"), g)


async def test_a_no_is_remembered_for_a_minute_then_asked_again(now):
    g = a_graph()
    asked = Session(904, "found")
    assert not await calls(asked, g)
    now[0] += OracleStoredFunctions.ABSENT_FOR - 0.001
    assert not await calls(asked, g)
    assert len(asked.checks) == 1
    now[0] += 0.001
    assert await calls(asked, g)
    assert len(asked.checks) == 2


class Pool:
    def __init__(self, session: Session) -> None:
        self.session = session

    async def acquire(self) -> Session:
        return self.session

    async def release(self, _: Any) -> None:
        pass


async def test_a_no_is_asked_again_at_once_once_the_graph_is_ensured_and_of_that_graph_only(now):
    g, other = a_graph(), a_graph()
    asked = Session(904, 904, "found")
    assert not await calls(asked, g)
    assert not await calls(asked, other)
    # The provider's first connection ensures the graph.
    provider = OracleConnectionProvider("ora", "localhost:1521/FREEPDB1", "me", graph=g)
    provider._pool = Pool(asked)
    await provider.connect()
    assert provider.graph.ensuring in asked.run
    assert not await calls(asked, other)
    assert await calls(asked, g)
    assert len(asked.checks) == 3


async def test_a_graph_without_the_function_is_warned_of_once(now, caplog: pytest.LogCaptureFixture):
    g = a_graph()
    asked = Session(904, 904)
    with caplog.at_level(logging.WARNING, logger="drivine.oracle"):
        await calls(asked, g)
        now[0] += OracleStoredFunctions.ABSENT_FOR
        await calls(asked, g)
    assert len(asked.checks) == 2
    warnings = [it for it in caplog.records if f"has no function {g}_nulls_equal" in it.getMessage()]
    assert len(warnings) == 1, caplog.records


async def test_a_statement_is_compiled_to_call_the_function_where_the_session_may_and_to_call_none_where_it_may_not(
    now,
):
    g = a_graph()
    found = Session("found")
    await OracleConnection(found, None, "s", g, "db|me").query(QuerySpecification[Any](cypher_statement(WRITE)))
    assert [it for it in found.statements if f"{g}_nulls_equal(" in it], found.run
    missing = a_graph()
    absent = Session(904)
    await OracleConnection(absent, None, "s", missing, "db|me").query(QuerySpecification[Any](cypher_statement(WRITE)))
    (written,) = absent.statements
    assert "nulls_equal" not in written and "THEN NULL ELSE FALSE END" in written, written
    # A statement that compares no value that may hold a null asks nothing.
    flat = Session()
    await OracleConnection(flat, None, "s", a_graph(), "db|me").query(
        QuerySpecification[Any](cypher_statement("MATCH (c:Claim) SET c.e = c.a = c.b"))
    )
    assert flat.checks == []
    # Nor does a statement that is not Cypher, which is not compiled.
    plain = Session()
    await OracleConnection(plain, None, "s", a_graph(), "db|me").query(QuerySpecification[Any]("UPDATE t SET v = 1"))
    assert plain.checks == [] and plain.statements == ["UPDATE t SET v = 1"], plain.run


async def test_an_iterator_in_a_parameter_is_bound_as_its_items_by_the_compile_again_to_call_no_function(now):
    # The statement is compiled once, then again where the session may not call the function: the second compile reads
    # the items the first did, not the iterator it emptied.
    absent = Session(904)
    spec = QuerySpecification[Any](cypher_statement(f"{WRITE}, c.f = $p"))
    await OracleConnection(absent, None, "s", a_graph(), "db|me").query(spec.bind({"p": iter([1, None])}))
    (written,) = absent.statements
    assert "nulls_equal" not in written, written
    assert [it for it in absent.bound if it] == [{"b0": '{"p":[1,null]}'}], absent.bound


async def test_a_cursor_is_compiled_to_call_the_function_where_the_session_may_and_to_declare_it_where_it_may_not(now):
    read = "WITH [1, null] AS c RETURN c = [2, null]"
    g = a_graph()
    found = Session("found")
    await OracleConnection(found, None, "s", g, "db|me").open_cursor(CursorSpecification[Any](cypher_statement(read)))
    (opened,) = found.statements
    assert f"{g}_nulls_equal(" in opened and not opened.startswith("WITH\nFUNCTION"), opened
    absent = Session(904)
    await OracleConnection(absent, None, "s", a_graph(), "db|me").open_cursor(
        CursorSpecification[Any](cypher_statement(read))
    )
    (declared,) = absent.statements
    assert declared.startswith("WITH\nFUNCTION") and "cy_nulls_equal(" in declared, declared


class SyncCursor(Cursor):
    def execute(self, statement: str, parameters: Any = None, **_: Any) -> None:  # type: ignore[override]
        self.session.run.append(statement)
        if "_nulls_equal(JSON('[1]'), JSON('[1]')) FROM DUAL" in statement:
            outcome = self.session.outcomes.pop(0)
            if outcome != "found":
                raise ora(outcome)

    def fetchall(self) -> list[Any]:  # type: ignore[override]
        return []


class SyncSession(Session):
    def cursor(self) -> Cursor:
        return SyncCursor(self)


async def test_the_sync_api_shares_what_the_async_one_found_and_its_warning(now, caplog: pytest.LogCaptureFixture):
    g = a_graph()
    caplog.set_level(logging.WARNING, logger="drivine.oracle")
    assert not await calls(Session(904), g)
    asked = SyncSession("found")
    assert not SyncOracleConnection(asked, None, "s", g, "db|me")._calls_nulls_equal()
    assert asked.checks == []
    now[0] += OracleStoredFunctions.ABSENT_FOR
    missing = SyncSession(904)
    assert not SyncOracleConnection(missing, None, "s", g, "db|me")._calls_nulls_equal()
    assert len(missing.checks) == 1
    warnings = [it for it in caplog.records if f"has no function {g}_nulls_equal" in it.getMessage()]
    assert len(warnings) == 1, caplog.records
    # And a yes the sync API finds is the async one's too.
    other = a_graph()
    assert SyncOracleConnection(SyncSession("found"), None, "s", other, "db|me")._calls_nulls_equal()
    unasked = Session()
    assert await calls(unasked, other) and unasked.checks == []
