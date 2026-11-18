"""
An object manager's statement on Oracle that the query DSL's fragments say calls the graph's stored functions: the
module compiles a fragment to call those a graph is made with, and the library's own SQL can't be compiled again without
them, as a Cypher statement is, so the session is asked first, and the statement refused, saying why, where it may not
call one. Drivine for TypeScript's OracleConnection.spec of liberation-data/drivine#797.
"""

import logging
from typing import Annotated, Any

import pytest

from drivine import CursorSpecification, NodeFragment, NodeId, QuerySpecification, times
from drivine.backend.graph_statement import StoredFunctionCalls
from drivine.backend.oracle.oracle_backend import OracleBackend
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.oracle.oracle_stored_functions import OracleStoredFunctions
from drivine.backend.page import Page
from drivine.drivine_error import DrivineError
from drivine.dsl.query import Query
from drivine.dsl.ref import refs
from drivine.model.model_registry import ModelRegistry
from tests.unit.test_oracle_era_functions import Session, a_graph, connection, now  # noqa: F401

pytestmark = pytest.mark.usefixtures("now")
"""Each test lets the time a session found to call no function is remembered pass when it says (see `now`)."""


class Metered(NodeFragment, labels=["Metered"]):
    id: Annotated[str, NodeId()]
    meta: dict[str, Any] | None = None
    size: int | None = None


def labels(it: type) -> list[str]:
    return ModelRegistry().fragment(it).labels


def counted(g: str) -> QuerySpecification[Any]:
    """The DSL's count of a map property equal to one holding a null, which the module compares by nulls_equal."""
    backend = OracleBackend(OracleGraph(g))
    model = ModelRegistry().fragment(Metered)
    where, bindings = backend.render_where(refs(Metered).meta.eq({"a": None}), model, labels)
    return backend.count_fragments(model, where).spec(bindings)


def era(g: str) -> QuerySpecification[Any]:
    """A statement that says it calls the graph's era functions."""
    spec = QuerySpecification[Any]("SELECT 1 FROM dual")
    spec.stored_functions = StoredFunctionCalls(g, era=True)
    return spec


async def test_one_runs_where_the_session_may_call_them_asking_once():
    g = a_graph()
    session = Session(nulls_equal=("found",))
    await connection(session, g).query(counted(g))
    await connection(session, g).query(counted(g))
    assert len(session.checks) == 1
    assert len(session.statements) == 2
    assert f"{g}_nulls_equal(" in session.statements[0]
    # What the fragments ask is taken out of the text: their hints written where Oracle reads them, the calls said.
    assert "/*drivine:" not in session.statements[0]
    assert "SELECT /*+ NO_JSON_TABLE_TRANSFORM */ COUNT(*)" in session.statements[0]


async def test_one_is_refused_saying_why_where_the_session_may_not_call_them_and_not_run():
    g = a_graph()
    session = Session(nulls_equal=(904,))
    with pytest.raises(DrivineError) as refused:
        await connection(session, g).query(counted(g))
    assert f"calls {g}_nulls_equal, a function the Oracle graph '{g}' keeps, which this session may not call" in str(
        refused.value
    )
    assert session.statements == []


async def test_one_opened_as_a_cursor_is_refused_too_where_the_session_may_not_call_them_and_not_run():
    g = a_graph()
    session = Session(era=(904,))
    spec = CursorSpecification[Any]("SELECT 1 FROM dual")
    spec.stored_functions = StoredFunctionCalls(g, era=True)
    with pytest.raises(DrivineError, match=f"calls {g}_era, {g}_era_date and {g}_temporal_key, functions"):
        await connection(session, g).open_cursor(spec)
    assert len(session.checks) == 1
    assert session.statements == []


async def test_one_that_calls_the_graphs_temporal_arithmetic_is_refused_where_the_session_may_not_call_it():
    g = a_graph()
    backend = OracleBackend(OracleGraph(g))
    model = ModelRegistry().fragment(Metered)
    query = backend.render_query(Query(order_by=times(refs(Metered).size, 2).asc()), model, labels)
    statement = backend.load_all_fragments(model, query.where, Page(order_by=query.order_by))
    assert statement.calls == StoredFunctionCalls(g, temporal_arithmetic=True)
    session = Session(temporal_arithmetic=(904,))
    with pytest.raises(DrivineError) as refused:
        await connection(session, g).query(statement.spec(query.bindings))
    assert f"calls {g}_temporal_arith, a function the Oracle graph '{g}' keeps" in str(refused.value)
    assert session.statements == []


async def test_the_temporal_arithmetic_is_asked_of_apart_from_the_others_and_warned_of_once_saying_what_is_lost(
    caplog: pytest.LogCaptureFixture,
    now: list[float],  # noqa: F811 (the fixture imported above)
):
    # A store ensured by a version before it has nulls_equal and the era functions, and not this.
    g = a_graph()
    session = Session(nulls_equal=("found",), era=("found",), temporal_arithmetic=(904, 904))
    arithmetic = connection(session, g)
    with caplog.at_level(logging.WARNING, logger="drivine.oracle"):
        assert await arithmetic._calls("nulls_equal")
        assert await arithmetic._calls("era")
        assert not await arithmetic._calls("temporal_arithmetic")
        assert not await arithmetic._calls("temporal_arithmetic")
        now[0] += OracleStoredFunctions.ABSENT_FOR
        assert not await arithmetic._calls("temporal_arithmetic")
    assert len(session.checks) == 4
    assert f"{g}_temporal_arith('+', JSON(" in session.checks[2]
    warned = [it.getMessage() for it in caplog.records if f"has no function {g}_temporal_arith," in it.getMessage()]
    assert len(warned) == 1, caplog.records
    assert "an order of the query DSL by arithmetic of a property's value is refused" in warned[0]


async def test_one_is_refused_naming_each_of_the_graphs_era_functions_where_it_calls_them_and_the_session_may_not():
    g = a_graph()
    session = Session(era=(904,))
    with pytest.raises(DrivineError) as refused:
        await connection(session, g).query(era(g))
    assert (
        f"calls {g}_era, {g}_era_date and {g}_temporal_key, functions the Oracle graph '{g}' keeps, which this "
        "session may not call"
    ) in str(refused.value)
    assert len(session.checks) == 1
    assert f"{g}_era('0000-01-01')" in session.checks[0]
    assert session.statements == []


async def test_one_that_calls_the_era_functions_runs_where_the_session_may_call_them():
    g = a_graph()
    session = Session(nulls_equal=(904,), era=("found",))
    await connection(session, g).query(era(g))
    assert len(session.checks) == 1
    assert f"{g}_era('0000-01-01')" in session.checks[0]
    assert session.statements == ["SELECT 1 FROM dual"]


async def test_nothing_is_asked_of_one_that_calls_none():
    session = Session(nulls_equal=(904,))
    await connection(session, a_graph()).query(QuerySpecification[Any]("SELECT 1 FROM dual"))
    assert session.checks == []
