# Written by scripts/unasync.py from drivine/cursor/cursor.py:
# change that, and run the script again.
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from types import TracebackType
from typing import Generic, Self, TypeVar

T = TypeVar("T")


class Cursor(ABC, Generic[T]):
    """
    Streams a statement's results, without holding them all in memory:

        with pm.open_cursor(spec) as cursor:
            for route in cursor:
                ...

    Closing it releases what it holds: its result, and outside a transaction its connection. A transaction closes
    the cursors still open in it when it ends.
    """

    def __iter__(self) -> Iterator[T]:
        return self

    @abstractmethod
    def __next__(self) -> T: ...

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()
