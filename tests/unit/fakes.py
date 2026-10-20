import asyncio
from typing import Any

from drivine import Connection, ConnectionProvider, Cursor, CursorSpecification, DatabaseType, QuerySpecification


class FakeConnection(Connection):
    """
    Records what it is asked to do. A statement whose text is "fail" raises, and one beginning "slow" yields to the
    event loop part way through, so another task could run a statement inside it if nothing stopped it.
    """

    def __init__(self, provider: "FakeProvider", session_id: str) -> None:
        self.provider = provider
        self.session_id = session_id
        self.running = 0

    @property
    def events(self) -> list[str]:
        return self.provider.events

    async def query(self, spec: QuerySpecification[Any]) -> list[Any]:
        assert spec.statement is not None
        text = spec.statement.text
        self.running += 1
        try:
            assert self.running == 1, "two statements ran at once on one connection"
            self.events.append(f"{self.provider.name}:start:{text}")
            if text.startswith("slow"):
                await asyncio.sleep(0.01)
            if text == "fail":
                raise RuntimeError("the statement failed")
            self.events.append(f"{self.provider.name}:end:{text}")
            return [text]
        finally:
            self.running -= 1

    async def open_cursor(self, spec: CursorSpecification[Any]) -> Cursor[Any]:
        raise NotImplementedError

    async def start_transaction(self) -> None:
        self.events.append(f"{self.provider.name}:begin")

    async def commit_transaction(self) -> None:
        self.events.append(f"{self.provider.name}:commit")

    async def rollback_transaction(self) -> None:
        self.events.append(f"{self.provider.name}:rollback")

    async def release(self, error: BaseException | None = None) -> None:
        self.events.append(f"{self.provider.name}:release")


class FakeProvider(ConnectionProvider):
    def __init__(self, name: str = "default", events: list[str] | None = None) -> None:
        self.name = name
        self.type = DatabaseType.NEO4J
        self.events = [] if events is None else events
        self.connections = 0

    async def connect(self) -> Connection:
        self.connections += 1
        return FakeConnection(self, f"s{self.connections}")

    async def end(self) -> None:
        pass
