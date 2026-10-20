from __future__ import annotations

from typing import Self, TypeVar

from drivine.query.query_specification import QuerySpecification
from drivine.query.statement import Statement

T = TypeVar("T")


class CursorSpecification(QuerySpecification[T]):
    """
    A query specification for a cursor, which reads its results `batch_size` at a time.
    """

    def __init__(self, statement: str | Statement | None = None) -> None:
        super().__init__(statement)
        self.batch = 100

    def batch_size(self, size: int) -> Self:
        if size < 1:
            raise ValueError("batch_size must be more than zero")
        self.batch = size
        return self
