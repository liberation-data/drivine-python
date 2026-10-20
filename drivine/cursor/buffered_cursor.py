from __future__ import annotations

from abc import abstractmethod
from typing import Any, TypeVar

from drivine.cursor.cursor import Cursor

T = TypeVar("T")


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
