from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Generic, Self, TypeVar

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
