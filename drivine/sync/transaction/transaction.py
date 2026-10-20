# Written by scripts/unasync.py from drivine/transaction/transaction.py:
# change that, and run the script again.
from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any, TypeVar

from drivine.cursor.cursor_specification import CursorSpecification
from drivine.drivine_error import DrivineError
from drivine.query.query_specification import QuerySpecification
from drivine.sync import concurrency
from drivine.sync.connection.connection import Connection
from drivine.sync.connection.connection_provider import ConnectionProvider
from drivine.sync.cursor.cursor import Cursor
from drivine.transaction.transaction_rolled_back_error import TransactionRolledBackError
from drivine.utils.short_id import short_id

T = TypeVar("T")

logger = logging.getLogger("drivine.transaction")

_current: ContextVar[Transaction | None] = ContextVar("drivine.transaction", default=None)


def current_transaction() -> Transaction | None:
    """
    The transaction the calling code runs in, or None. asyncio carries it across every await, and into the tasks
    started inside it.
    """
    return _current.get()


class Transaction:
    """
    One unit of work, across every database it touches. A connection to each database is opened, and its
    transaction begun, the first time the transaction runs a statement there, so a transaction that runs none costs
    nothing.

    Tasks started inside a transaction share it, and a database transaction runs one statement at a time, so each
    connection's statements are queued: each takes the connection's lock, granted in the order asked.
    A statement that fails marks the transaction rollback-only, and the statements after it raise
    `TransactionRolledBackError` at once.
    """

    def __init__(self, rollback: bool = False) -> None:
        self.id = short_id()
        self.rollback = rollback
        """Roll back at the end, even on success: for tests."""
        self._connections: dict[int, _QueuedConnection] = {}
        self._connecting = concurrency.Lock()
        self._cursors: list[Cursor[Any]] = []
        self._failure: BaseException | None = None
        self._ended = False

    @property
    def rollback_only(self) -> bool:
        """Whether a failure has doomed the transaction to roll back."""
        return self._failure is not None

    @property
    def failure(self) -> BaseException | None:
        """The first failure that made the transaction rollback-only."""
        return self._failure

    def mark_rollback_only(self, cause: BaseException) -> None:
        if self._failure is None:
            logger.debug("Transaction %s is rollback-only: %r", self.id, cause)
            self._failure = cause

    @property
    def databases(self) -> list[str]:
        return [it.provider.name for it in self._connections.values()]

    def __repr__(self) -> str:
        return f"Transaction({self.id} {self.databases})"

    def query(self, spec: QuerySpecification[T], provider: ConnectionProvider) -> list[T]:
        queued = self._connection_for(provider, spec)
        with queued.lock:
            self._check_usable(spec)
            try:
                return queued.connection.query(spec)
            except Exception as error:
                self.mark_rollback_only(error)
                raise DrivineError.with_root_cause(error, spec) from error
            except BaseException as error:
                self.mark_rollback_only(error)
                raise

    def open_cursor(self, spec: CursorSpecification[T], provider: ConnectionProvider) -> Cursor[T]:
        queued = self._connection_for(provider, spec)
        with queued.lock:
            self._check_usable(spec)
            try:
                cursor = queued.connection.open_cursor(spec)
            except Exception as error:
                self.mark_rollback_only(error)
                raise DrivineError.with_root_cause(error, spec) from error
        queued_cursor = _QueuedCursor(cursor, queued.lock, self, spec)
        self._cursors.append(queued_cursor)
        return queued_cursor

    def end(self, error: BaseException | None = None) -> None:
        """
        Ends the transaction: closes its cursors, then commits, or rolls back after an error, a failure or when
        marked `rollback`, and releases its connections. A transaction made rollback-only by a failure its caller
        caught raises `TransactionRolledBackError` once it has rolled back, unless it was meant to roll back anyway.
        """
        self._ended = True
        for cursor in self._cursors:
            try:
                cursor.close()
            except Exception as failure:
                logger.warning("Closing a cursor of transaction %s failed: %s", self.id, failure)
        connections = list(self._connections.values())
        commit = error is None and self._failure is None and not self.rollback
        try:
            if commit:
                self._commit(connections)
            else:
                logger.debug("Rolling back transaction %s.", self.id)
                self._rollback(connections)
        finally:
            for it in connections:
                try:
                    it.connection.release(error or self._failure)
                except Exception as failure:
                    logger.warning("Releasing a connection of transaction %s failed: %s", self.id, failure)
        if error is None and self._failure is not None and not self.rollback:
            raise TransactionRolledBackError(
                f"Transaction {self.id} rolled back: {self._failure!r} was raised inside it, though it was caught."
            ) from self._failure

    def _commit(self, connections: list[_QueuedConnection]) -> None:
        logger.debug("Committing transaction %s.", self)
        for index, it in enumerate(connections):
            try:
                it.connection.commit_transaction()
            except Exception as error:
                # Not two-phase: those committed stay committed, and the rest are rolled back.
                self._rollback(connections[index + 1 :])
                raise DrivineError(f"Committing transaction {self.id} on {it.provider.name} failed.") from error

    def _rollback(self, connections: list[_QueuedConnection]) -> None:
        for it in connections:
            try:
                it.connection.rollback_transaction()
            except Exception as error:
                # Rolling back runs on the error path, and must not hide the error that caused it.
                logger.warning("Rolling back transaction %s on %s failed: %s", self.id, it.provider.name, error)

    def _connection_for(self, provider: ConnectionProvider, spec: QuerySpecification[Any]) -> _QueuedConnection:
        self._check_usable(spec)
        key = id(provider)
        queued = self._connections.get(key)
        if queued is not None:
            return queued
        with self._connecting:
            queued = self._connections.get(key)
            if queued is None:
                connection = provider.connect()
                try:
                    connection.start_transaction()
                except BaseException as error:
                    connection.release(error)
                    raise
                queued = _QueuedConnection(provider, connection)
                self._connections[key] = queued
            return queued

    def _check_usable(self, spec: QuerySpecification[Any]) -> None:
        if self._ended:
            raise DrivineError(
                f"Transaction {self.id} has ended. A task that outlives its transaction can't use it.", spec
            )
        if self._failure is not None:
            error = TransactionRolledBackError(
                f"Transaction {self.id} is rolling back, because of an earlier failure: {self._failure!r}.", spec
            )
            raise error from self._failure


class _QueuedConnection:
    def __init__(self, provider: ConnectionProvider, connection: Connection) -> None:
        self.provider = provider
        self.connection = connection
        self.lock = concurrency.Lock()


class _QueuedCursor(Cursor[T]):
    """
    A cursor in a transaction, which takes its connection's lock for each read, so that other statements interleave
    between its reads.
    """

    def __init__(
        self, cursor: Cursor[T], lock: concurrency.Lock, transaction: Transaction, spec: CursorSpecification[T]
    ):
        self.cursor = cursor
        self.lock = lock
        self.transaction = transaction
        self.spec = spec

    def __next__(self) -> T:
        with self.lock:
            self.transaction._check_usable(self.spec)
            try:
                return self.cursor.__next__()
            except StopIteration:
                raise
            except Exception as error:
                self.transaction.mark_rollback_only(error)
                raise DrivineError.with_root_cause(error, self.spec) from error

    def close(self) -> None:
        with self.lock:
            self.cursor.close()
