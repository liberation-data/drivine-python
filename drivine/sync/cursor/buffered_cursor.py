# Written by scripts/unasync.py from drivine/cursor/buffered_cursor.py:
# change that, and run the script again.
from __future__ import annotations

from abc import abstractmethod
from typing import Any, TypeVar

from drivine.sync.cursor.cursor import Cursor

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

    def __next__(self) -> T:
        # A filter may drop a whole batch, so read until one has something in it or the results run out.
        while self._index == len(self._batch):
            if self._exhausted or self.closed:
                raise StopIteration
            self._batch, self._exhausted = self.read(self.batch_size)
            self._index = 0
        item = self._batch[self._index]
        self._index += 1
        return item

    @abstractmethod
    def read(self, count: int) -> tuple[list[T], bool]:
        """
        Reads at most count rows, and returns them post-processed, with whether they were the last.
        """
