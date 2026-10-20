from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from contextvars import Token
from types import TracebackType
from typing import Any, ParamSpec, TypeVar, overload

from drivine import concurrency
from drivine.drivine_error import DrivineError
from drivine.transaction.transaction import Transaction, _current

P = ParamSpec("P")
R = TypeVar("R")


class transaction:
    """
    Runs a block, or each call of a function, in a transaction: the current one, or a new one that commits when the
    outermost block ends, and rolls back when an exception leaves it.

        async with transaction():
            await gom.save(person)

        async with transaction(rollback=True):   # a test that leaves nothing behind
            ...

    An exception that leaves any block, the outermost or one that joined it, marks the transaction rollback-only,
    unless it is one of `no_rollback_for`. The transaction then rolls back, even if a caller catches the exception,
    and its end raises `TransactionRolledBackError` rather than committing quietly.

    A database connection is opened only when a statement runs, so a block whose code never reaches Drivine costs
    no more than setting a context variable.
    """

    def __init__(
        self,
        *,
        rollback: bool = False,
        no_rollback_for: type[BaseException] | tuple[type[BaseException], ...] = (),
    ) -> None:
        self.rollback = rollback
        self.no_rollback_for = no_rollback_for
        self._transaction: Transaction | None = None
        self._token: Token[Transaction | None] | None = None

    async def __aenter__(self) -> Transaction:
        if self._transaction is not None:
            raise DrivineError("This transaction() block is already running. Make a new transaction() for each block.")
        current = _current.get()
        if current is None:
            current = Transaction(rollback=self.rollback)
            self._token = _current.set(current)
        elif self.rollback:
            current.rollback = True
        self._transaction = current
        return current

    async def __aexit__(
        self, kind: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None
    ) -> None:
        current, token = self._transaction, self._token
        self._transaction = self._token = None
        assert current is not None
        rolls_back = error is not None and not isinstance(error, self.no_rollback_for)
        if rolls_back:
            assert error is not None
            current.mark_rollback_only(error)
        if token is None:
            return
        try:
            await current.end(error if rolls_back else None)
        finally:
            _current.reset(token)

    def __call__(self, function: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        if not concurrency.runs_here(function):
            raise TypeError(
                f"@transactional takes {concurrency.FUNCTIONS}, and {function.__qualname__} isn't one: "
                f"take @transactional from {concurrency.OTHER_API}."
            )
        rollback, no_rollback_for = self.rollback, self.no_rollback_for

        @functools.wraps(function)
        async def run_in_transaction(*args: P.args, **kwargs: P.kwargs) -> R:
            async with transaction(rollback=rollback, no_rollback_for=no_rollback_for):
                return await function(*args, **kwargs)

        return run_in_transaction


@overload
def transactional(function: Callable[P, Awaitable[R]], /) -> Callable[P, Awaitable[R]]: ...


@overload
def transactional(
    *, rollback: bool = False, no_rollback_for: type[BaseException] | tuple[type[BaseException], ...] = ()
) -> transaction: ...


def transactional(
    function: Callable[P, Awaitable[R]] | None = None,
    /,
    *,
    rollback: bool = False,
    no_rollback_for: type[BaseException] | tuple[type[BaseException], ...] = (),
) -> Any:
    """
    Runs each call of an async function or method in a transaction: the caller's, or a new one. See `transaction`.

        class IssueService:
            @transactional()
            async def raise_issue(self, issue: Issue) -> Issue:
                ...

    Plain objects call it as they call any method, so a method calling another `@transactional` method on `self`
    gets a transaction too.
    """
    scope = transaction(rollback=rollback, no_rollback_for=no_rollback_for)
    return scope if function is None else scope(function)
