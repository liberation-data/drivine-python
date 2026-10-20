from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from drivine.query.specification import QuerySpecification


class DrivineError(Exception):
    """
    The base of Drivine's errors. One raised for a failed statement holds the statement's specification, and the
    database's error as its `__cause__`.
    """

    def __init__(self, message: str | None = None, spec: QuerySpecification[Any] | None = None) -> None:
        super().__init__(message)
        self.spec = spec

    @classmethod
    def with_root_cause(cls, cause: BaseException, spec: QuerySpecification[Any] | None = None) -> DrivineError:
        error = cls(str(cause) or type(cause).__name__, spec)
        error.__cause__ = cause
        return error

    @property
    def root_cause(self) -> BaseException | None:
        return self.__cause__


class TransactionRolledBackError(DrivineError):
    """
    The transaction is rolling back, because a statement in it failed or an exception left a `@transactional` call,
    though the caller caught it. A later statement in the transaction raises it at once, and so does the end of the
    transaction that was meant to commit. `__cause__` is the first failure.
    """
