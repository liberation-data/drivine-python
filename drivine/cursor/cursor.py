from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any, Generic, Self, TypeVar

T = TypeVar("T")


class Cursor(ABC, Generic[T]):
    """
    Streams a statement's results, without holding them all in memory:

        async with await pm.open_cursor(spec) as cursor:
            async for route in cursor:
                ...

    Closing it releases what it holds: its result, and outside a transaction its connection. A transaction closes
    the cursors still open in it when it ends.
    """

    def __aiter__(self) -> AsyncIterator[T]:
        return self

    @abstractmethod
    async def __anext__(self) -> T: ...

    @abstractmethod
    async def close(self) -> None: ...

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.close()


class BufferedCursor(Cursor[T]):
    """
    Reads `batch_size` results at a time, and hands them out one by one.
    """

    def __init__(self, batch_size: int) -> None:
        self.batch_size = batch_size
        self._batch: list[Any] = []
        self._index = 0
        self._exhausted = False
        self.closed = False

    async def __anext__(self) -> T:
        # A filter may drop a whole batch, so read until one has something in it or the results run out.
        while self._index == len(self._batch):
            if self._exhausted or self.closed:
                raise StopAsyncIteration
            self._batch, self._exhausted = await self.read(self.batch_size)
            self._index = 0
        item = self._batch[self._index]
        self._index += 1
        return item

    @abstractmethod
    async def read(self, count: int) -> tuple[list[T], bool]:
        """
        Reads at most count rows, and returns them post-processed, with whether they were the last.
        """
