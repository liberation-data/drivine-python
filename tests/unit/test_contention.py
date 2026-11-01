"""Which failures over Bolt, and of a SQL store's DDL, are contention, and that only those are run again."""

from typing import Any

import oracledb
import psycopg
import psycopg.errors
import pytest
from neo4j.exceptions import Neo4jError, ServiceUnavailable, SessionExpired

from drivine.backend.cypher_backend import CypherBackend
from drivine.backend.cypher_grammar import FALKORDB_GRAMMAR, MEMGRAPH_GRAMMAR, NEO4J_GRAMMAR
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.oracle.oracle_schema_grammar import oracle_schema_grammar
from drivine.connection.database_type import DatabaseType
from drivine.drivine_error import DrivineError
from drivine.manager.contention import CONTENDED_ATTEMPTS, ddl_contended, ddl_deadlocked, when_not_contended
from drivine.schema.schema_executor import SchemaExecutor
from drivine.schema.schema_specs import uniqueness_constraint


def bolt(code: str) -> Neo4jError:
    """An error as the driver makes one from the server's failure."""
    return Neo4jError._hydrate_neo4j(code=code, message=code)


def caused_by(cause: BaseException) -> Exception:
    try:
        raise DrivineError("wrapped") from cause
    except DrivineError as error:
        return error


def test_a_transient_error_over_bolt_is_contention_however_deep_it_is_wrapped():
    neo4j = CypherBackend(NEO4J_GRAMMAR)
    assert neo4j.contended(bolt("Neo.TransientError.Transaction.DeadlockDetected"))
    assert neo4j.contended(caused_by(bolt("Neo.TransientError.Transaction.LockAcquisitionTimeout")))
    assert CypherBackend(MEMGRAPH_GRAMMAR).contended(bolt("Memgraph.TransientError.MemgraphError.MemgraphError"))


def test_a_lost_connection_or_an_expired_authorization_is_not_contention_though_the_driver_calls_it_retryable():
    neo4j = CypherBackend(NEO4J_GRAMMAR)
    for failure in [
        ServiceUnavailable("gone"),
        SessionExpired("gone"),
        bolt("Neo.ClientError.Security.AuthorizationExpired"),
    ]:
        assert failure.is_retryable()
        assert not neo4j.contended(failure)
        assert not neo4j.contended(caused_by(failure))


def test_a_terminated_transaction_or_a_client_error_is_not_contention():
    neo4j = CypherBackend(NEO4J_GRAMMAR)
    assert not neo4j.contended(bolt("Neo.TransientError.Transaction.Terminated"))
    assert not neo4j.contended(bolt("Neo.ClientError.Schema.ConstraintValidationFailed"))
    assert not neo4j.contended(ValueError("Neo.TransientError.Transaction.DeadlockDetected"))


def test_falkordb_turning_a_write_away_while_another_is_in_progress_is_contention():
    falkordb = CypherBackend(FALKORDB_GRAMMAR)
    assert falkordb.contended(Exception("another write is in progress"))
    assert not falkordb.contended(ConnectionError("Connection closed by server."))


class Statement:
    """Fails once with `failure`, then succeeds."""

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.calls == 1:
            raise caused_by(self.failure)
        return "saved"


async def no_pause(_seconds: float) -> None:
    pass


async def test_a_save_turned_away_by_a_deadlock_is_run_again():
    statement = Statement(bolt("Neo.TransientError.Transaction.DeadlockDetected"))
    assert await when_not_contended(statement, CypherBackend(NEO4J_GRAMMAR).contended, no_pause) == "saved"
    assert statement.calls == 2


@pytest.mark.parametrize(
    "failure",
    [ServiceUnavailable("gone"), SessionExpired("gone"), bolt("Neo.ClientError.Security.AuthorizationExpired")],
)
async def test_a_save_that_lost_its_connection_is_not_run_again_it_may_have_been_applied(failure):
    statement = Statement(failure)
    with pytest.raises(DrivineError) as raised:
        await when_not_contended(statement, CypherBackend(NEO4J_GRAMMAR).contended, no_pause)
    assert raised.value.__cause__ is failure
    assert statement.calls == 1


async def test_does_not_run_again_what_an_inner_run_gave_up_on_as_a_schema_statement_over_a_first_connection_would():
    contended = bolt("Neo.TransientError.Transaction.DeadlockDetected")
    neo4j = CypherBackend(NEO4J_GRAMMAR)
    calls = 0

    async def inner() -> str:
        async def statement() -> str:
            nonlocal calls
            calls += 1
            raise contended

        try:
            return await when_not_contended(statement, neo4j.contended, no_pause)
        except Exception as error:
            raise DrivineError.with_root_cause(error) from error

    with pytest.raises(DrivineError) as raised:
        await when_not_contended(inner, neo4j.contended, no_pause)
    assert raised.value.root_cause is contended
    assert calls == CONTENDED_ATTEMPTS


class PostgresError(Exception):
    """A failure as psycopg raises one: its SQLSTATE as `sqlstate`."""

    def __init__(self, message: str, sqlstate: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


def test_oracles_busy_lock_library_cache_deadlock_and_row_deadlock_are_ddl_contention_under_a_cause_or_not():
    for message in [
        "ORA-00054: resource busy and acquire with NOWAIT specified or timeout expired",
        "ORA-04020: deadlock detected while trying to lock object DRIVINE.FC_RACE_X_NODE",
        "ORA-00060: deadlock detected while waiting for resource",
    ]:
        assert ddl_contended(RuntimeError(message))
        assert ddl_contended(caused_by(RuntimeError(f"ORA-06512: at line 7\n{message}")))


def test_postgres_deadlock_is_ddl_contention_a_lock_not_had_in_time_is_not():
    assert ddl_contended(PostgresError("deadlock detected", "40P01"))
    assert not ddl_contended(PostgresError("lock timeout", "55P03"))


def test_only_a_deadlock_is_a_ddl_deadlock_not_a_lock_busy_or_not_had_in_time():
    assert ddl_deadlocked(RuntimeError("ORA-04020: deadlock detected while trying to lock object X"))
    assert ddl_deadlocked(caused_by(RuntimeError("ORA-00060: deadlock detected")))
    assert ddl_deadlocked(PostgresError("deadlock detected", "40P01"))
    assert not ddl_deadlocked(RuntimeError("ORA-00054: resource busy and acquire with NOWAIT specified"))
    assert not ddl_deadlocked(PostgresError("lock timeout", "55P03"))
    assert not ddl_deadlocked(None)


def test_another_failure_is_not_ddl_contention():
    assert not ddl_contended(RuntimeError("ORA-01031: insufficient privileges"))
    assert not ddl_contended(RuntimeError("ORA-040200: not a code"))
    assert not ddl_contended(PostgresError("permission denied for schema public", "42501"))
    assert not ddl_contended(None)


@pytest.mark.parametrize(("sqlstate", "runs"), [("40P01", 2), ("55P03", 1)])
async def test_a_schema_statement_postgres_turned_away_for_a_deadlock_is_run_again_not_for_a_lock_not_had_in_time(
    sqlstate: str, runs: int
):
    ran = 0

    class Connection:
        async def query(self, _spec: object) -> list[object]:
            nonlocal ran
            ran += 1
            if ran == 1:
                raise PostgresError("failed", sqlstate)
            return []

        async def release(self, _error: BaseException | None = None) -> None:
            return None

    class Provider:
        name = "postgres"
        type = DatabaseType.POSTGRES

        async def connect(self) -> Connection:
            return Connection()

    run = SchemaExecutor(Provider()).run("CREATE INDEX x ON y (z)")  # pyright: ignore[reportArgumentType]
    if runs == 2:
        assert await run == []
    else:
        with pytest.raises(DrivineError) as raised:
            await run
        assert getattr(raised.value.root_cause, "sqlstate", None) == sqlstate
    assert ran == runs


@pytest.mark.parametrize(
    ("message", "runs"),
    [
        ("ORA-00054: resource busy and acquire with NOWAIT specified or timeout expired", 1),
        ("ORA-04020: deadlock detected while trying to lock object JVM_NODE", 2),
    ],
)
async def test_an_oracle_indexs_block_that_waited_out_a_busy_lock_itself_is_not_run_again_for_it_but_is_for_a_deadlock(
    message: str, runs: int
):
    # An Oracle index's block has waited thirty seconds on the busy lock before it gives up: run again, as other DDL
    # is, it would wait as long again each time, three minutes in all.
    block = next(
        it
        for it in oracle_schema_grammar(OracleGraph("jvm")).create_unique(uniqueness_constraint("DrivineStore", "key"))
        if isinstance(it, str) and "CREATE UNIQUE INDEX" in it
    )
    ran = 0

    class Connection:
        async def query(self, _spec: object) -> list[object]:
            nonlocal ran
            ran += 1
            if ran == 1:
                raise oracledb.DatabaseError(message)
            return []

        async def release(self, _error: BaseException | None = None) -> None:
            return None

    class Provider:
        name = "oracle"
        type = DatabaseType.ORACLE

        async def connect(self) -> Connection:
            return Connection()

    run = SchemaExecutor(Provider()).run(block)  # pyright: ignore[reportArgumentType]
    if runs == 2:
        assert await run == []
    else:
        with pytest.raises(DrivineError, match="ORA-00054"):
            await run
    assert ran == runs


@pytest.mark.parametrize(
    "failure",
    [
        lambda: oracledb.DatabaseError("ORA-04020: deadlock detected while trying to lock object X"),
        lambda: psycopg.errors.DeadlockDetected("deadlock detected"),
    ],
)
async def test_an_outer_run_does_not_run_again_a_drivers_own_deadlock_an_inner_run_gave_up_on(failure: Any):
    # The mark is an attribute set on the driver's own exception: python-oracledb's and psycopg's take one.
    calls = 0

    async def statement() -> None:
        nonlocal calls
        calls += 1
        raise failure()

    async def inner() -> None:
        try:
            await when_not_contended(statement, ddl_deadlocked, no_pause)
        except Exception as error:
            raise DrivineError.with_root_cause(error) from error

    with pytest.raises(DrivineError):
        await when_not_contended(inner, ddl_deadlocked, no_pause)
    assert calls == CONTENDED_ATTEMPTS
