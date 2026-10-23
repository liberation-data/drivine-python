"""Which failures over Bolt are contention, and that only those are run again."""

import pytest
from neo4j.exceptions import Neo4jError, ServiceUnavailable, SessionExpired

from drivine.backend.cypher_backend import CypherBackend
from drivine.backend.cypher_grammar import FALKORDB_GRAMMAR, MEMGRAPH_GRAMMAR, NEO4J_GRAMMAR
from drivine.drivine_error import DrivineError
from drivine.manager.contention import when_not_contended


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
