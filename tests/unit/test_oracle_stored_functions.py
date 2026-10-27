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
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.oracle.oracle_stored_functions import OracleStoredFunctions, nulls_equal_function
from drivine.backend.sql.sql_graph import sql_text
from drivine.connection.oracle.oracle_connection import OracleConnection
from drivine.connection.oracle.oracle_connection_provider import OracleConnectionProvider
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.sync.connection.oracle.oracle_connection import OracleConnection as SyncOracleConnection

# The lines of drivine4j's `OracleCypherCompiler.nullsEqualFunction`, copied verbatim from drivine4j's main at ae870884
# (liberation-data/drivine4j#323), its Kotlin comment and `$name` too: Kotlin's `"$name"` is the function's name, and
# here is replaced by it. The store keeps this function, so its text is part of the store layout the three libraries
# share, byte for byte. A change to it is made in drivine4j first and copied here again.
# fmt: off
DRIVINE4J_NULLS_EQUAL = [
    "FUNCTION $name(cy_x JSON, cy_y JSON) RETURN NUMBER IS",
    "    FUNCTION eq(a JSON_ELEMENT_T, b JSON_ELEMENT_T) RETURN NUMBER IS",
    "        r NUMBER := 1;",
    "        e NUMBER;",
    "        la JSON_ARRAY_T;",
    "        lb JSON_ARRAY_T;",
    "        oa JSON_OBJECT_T;",
    "        ob JSON_OBJECT_T;",
    "        ks JSON_KEY_LIST;",
    "        too_long EXCEPTION;",
    "        PRAGMA EXCEPTION_INIT(too_long, -40478);",
    "    BEGIN",
    "        IF a.is_Null OR b.is_Null THEN RETURN NULL; END IF;",
    "        IF a.is_Array AND b.is_Array THEN",
    "            la := TREAT(a AS JSON_ARRAY_T);",
    "            lb := TREAT(b AS JSON_ARRAY_T);",
    "            IF la.get_Size <> lb.get_Size THEN RETURN 0; END IF;",
    "            FOR i IN 0 .. la.get_Size - 1 LOOP",
    "                e := eq(la.get(i), lb.get(i));",
    "                IF e = 0 THEN RETURN 0; END IF;",
    "                IF e IS NULL THEN r := NULL; END IF;",
    "            END LOOP;",
    "            RETURN r;",
    "        END IF;",
    "        IF a.is_Object AND b.is_Object THEN",
    "            oa := TREAT(a AS JSON_OBJECT_T);",
    "            ob := TREAT(b AS JSON_OBJECT_T);",
    "            IF oa.get_Size <> ob.get_Size THEN RETURN 0; END IF;",
    "            ks := oa.get_Keys;",
    "            FOR i IN 1 .. ks.COUNT LOOP",
    "                IF NOT ob.has(ks(i)) THEN RETURN 0; END IF;",
    "                e := eq(oa.get(ks(i)), ob.get(ks(i)));",
    "                IF e = 0 THEN RETURN 0; END IF;",
    "                IF e IS NULL THEN r := NULL; END IF;",
    "            END LOOP;",
    "            RETURN r;",
    "        END IF;",
    "        IF a.is_Number AND b.is_Number THEN RETURN CASE WHEN a.to_Number = b.to_Number THEN 1 ELSE 0 END; END IF;",  # noqa: E501
    # As VARCHAR2s, about 3.5 times as fast as CLOBs; as CLOBs where one is past 32 KB, which to_String
    # raises on (ORA-40478).
    "        IF a.is_String AND b.is_String THEN",
    "            BEGIN",
    "                RETURN CASE WHEN a.to_String = b.to_String THEN 1 ELSE 0 END;",
    "            EXCEPTION WHEN too_long THEN",
    "                RETURN CASE WHEN DBMS_LOB.COMPARE(a.to_Clob, b.to_Clob) = 0 THEN 1 ELSE 0 END;",
    "            END;",
    "        END IF;",
    "        IF a.is_Boolean AND b.is_Boolean THEN RETURN CASE WHEN a.to_Boolean = b.to_Boolean THEN 1 ELSE 0 END; END IF;",  # noqa: E501
    "        RETURN 0;",
    "    END;",
    "BEGIN",
    "    RETURN eq(JSON_ELEMENT_T.parse(JSON_SERIALIZE(cy_x RETURNING CLOB)), JSON_ELEMENT_T.parse(JSON_SERIALIZE(cy_y RETURNING CLOB)));",  # noqa: E501
    "END;",
]
# fmt: on


def drivine4j_nulls_equal(name: str) -> str:
    """The text drivine4j makes of the function `name`: its lines joined by `\\n`, with no newline after the last."""
    return "\n".join(DRIVINE4J_NULLS_EQUAL).replace("$name", name)


def test_the_function_is_drivine4js_text_byte_for_byte_as_create_or_replace_of_it():
    assert nulls_equal_function("myGraph_nulls_equal") == drivine4j_nulls_equal("myGraph_nulls_equal")
    graph = OracleGraph("myGraph")
    assert graph.nulls_equal == "myGraph_nulls_equal"
    assert graph.functions == [f"CREATE OR REPLACE {drivine4j_nulls_equal('myGraph_nulls_equal')}"]
    assert graph.functions[0].endswith("END;")


def test_the_function_is_made_after_the_tables_where_missing_or_not_valid_tolerant_of_a_user_who_may_not_make_one():
    # drivine4j's OracleNamesTest of the block (#270): whether or not the tables were there, the function is made where
    # it is missing or not valid, and a user who may not make one (ORA-01031) still has the store.
    graph = OracleGraph("myGraph")
    ensuring = graph.ensuring
    for statement in graph.ddl:
        assert f"BEGIN EXECUTE IMMEDIATE {sql_text(statement)}; EXCEPTION WHEN e_exists THEN NULL; END;" in ensuring
    checked = ensuring.index(
        "SELECT COUNT(*) INTO v_made FROM user_objects\n"
        "        WHERE object_type = 'FUNCTION' AND object_name = 'MYGRAPH_NULLS_EQUAL' AND status = 'VALID';"
    )
    tables_end = ensuring.index("    END IF;")
    assert checked > ensuring.rindex("EXCEPTION WHEN e_exists THEN NULL; END;") and checked > tables_end, ensuring
    assert "PRAGMA EXCEPTION_INIT(e_denied, -1031);" in ensuring
    made = ensuring.index(
        f"BEGIN EXECUTE IMMEDIATE {sql_text(graph.functions[0])}; EXCEPTION WHEN e_denied THEN NULL; END;"
    )
    assert made > checked
    assert ensuring.count("BEGIN EXECUTE IMMEDIATE ") == len(graph.ddl) + len(graph.functions)


class Missing(SimpleNamespace):
    """What python-oracledb's error holds: its number."""


def ora(code: int) -> oracledb.DatabaseError:
    """The error python-oracledb raises with the number `code`."""
    return oracledb.DatabaseError(Missing(code=code, message=f"ORA-{code:05d}"))


class Cursor:
    def __init__(self, session: "Session") -> None:
        self.session = session
        self.description: Any = None

    async def execute(self, statement: str, parameters: Any = None, **_: Any) -> None:
        self.session.run.append(statement)
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
