"""
StoreLayoutMigration's driver, against a connection kept in memory and a planner that answers rounds as scripted
(drivine-python#514): what each action runs, and on which connection, and what it gives back. The module's own rounds
are the spec's (`migrate.yaml`, the module's LayoutMigratorTest); a store's are proven once drivine-python is on
layout 2.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

import pytest

from drivine.drivine_error import DrivineError
from drivine.migration.store_layout_migration import Declaration, StoreLayoutMigration


class FakeConnection:
    def __init__(self, log: list[str], name: str, fail_on: str | None = None) -> None:
        self.log, self.name, self.fail_on = log, name, fail_on

    async def query(self, spec: Any) -> list[Any]:
        text = getattr(spec.statement, "text", spec.statement)
        self.log.append(f"{self.name}: {text} {json.dumps(spec.parameters, sort_keys=True, default=str)}")
        if self.fail_on and self.fail_on in text:
            raise RuntimeError(f"refused {text}")
        if text == "READ":
            return [{"row": '{"id": 1, "props": "{}"}'}, {"a": 1, "b": 2}]
        return []

    async def start_transaction(self) -> None:
        self.log.append(f"{self.name}: begin")

    async def commit_transaction(self) -> None:
        self.log.append(f"{self.name}: commit")

    async def rollback_transaction(self) -> None:
        self.log.append(f"{self.name}: rollback")

    async def release(self, error: BaseException | None = None) -> None:
        self.log.append(f"{self.name}: release")


class FakeProvider:
    type = "SQLITE"
    graph_name = "drivine"

    def __init__(self, fail_on: str | None = None) -> None:
        self.log: list[str] = []
        self.connections = 0
        self.fail_on = fail_on

    async def connect_unjudged(self) -> FakeConnection:
        self.connections += 1
        return FakeConnection(self.log, f"c{self.connections}", self.fail_on)

    async def prepare_unjudged(self, connection: FakeConnection) -> None:
        self.log.append(f"{connection.name}: prepare")

    def stamp_statement(self) -> tuple[str, dict[str, Any]]:
        return "STAMP", {"layout": 2}


class Planner:
    """Answers each request with the next scripted round, and keeps what it was asked."""

    def __init__(self, rounds: list[list[dict[str, Any]]]) -> None:
        self.rounds, self.asked = rounds, []

    def __call__(self, text: str) -> str:
        request = json.loads(text)["migrate"]
        self.asked.append(request)
        if self.rounds:
            return json.dumps({"run": self.rounds.pop(0), "state": {"n": len(self.asked)}})
        return json.dumps({"done": {"status": "migrated", "message": "done", "notes": ["No index is made."]}})


DECLARED = [Declaration("Person", "born", "date")]


async def test_each_action_runs_where_it_belongs_and_the_lock_is_held_on_a_connection_of_its_own(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = FakeProvider()
    planner = Planner(
        [
            [{"type": "query", "statement": "READ", "parameters": {"after": 0}, "purpose": "batch"}],
            [
                {"type": "execute", "statement": "CREATE", "parameters": {}, "purpose": "checkpoint"},
                {"type": "prepare"},
                {"type": "lock", "statement": "LOCK"},
                {"type": "lock", "statement": "LOCK"},
                {
                    "type": "transaction",
                    "statements": [
                        {"type": "execute", "statement": "UPDATE", "parameters": {"id": 1}, "purpose": "rewrite"}
                    ],
                },
                {"type": "assignStamp"},
            ],
        ]
    )
    with caplog.at_level(logging.INFO, logger="drivine.store"):
        report = await StoreLayoutMigration(provider, planner).migrate(DECLARED, batch_size=2, dry_run=False)
    assert report["status"] == "migrated"
    # Its status logged, and each note, what it leaves to others, warned of, as drivine4j and TypeScript's do.
    assert [(it.levelname, it.getMessage()) for it in caplog.records] == [
        ("INFO", "Store layout migration of 'drivine': migrated: done"),
        ("WARNING", "Store layout migration of 'drivine': No index is made."),
    ]
    assert planner.asked[0]["start"] == {
        "declarations": [{"label": "Person", "property": "born", "kind": "date"}],
        "batchSize": 2,
        "dryRun": False,
        "keepInvalid": False,
    }
    assert planner.asked[0]["store"] == "sqlite" and planner.asked[0]["graph"] == "drivine"
    # A row of one JSON column is read as the object it holds; any other row as it is.
    assert planner.asked[1]["outcomes"] == [{"type": "rows", "rows": [{"id": 1, "props": "{}"}, {"a": 1, "b": 2}]}]
    assert planner.asked[2]["outcomes"] == [{"type": "ran"}] * 6
    assert provider.connections == 2, "the work's connection and the lock's, taken once"
    assert [line for line in provider.log if not line.startswith("c1: READ")] == [
        "c1: CREATE []",
        "c1: prepare",
        "c2: begin",
        "c2: LOCK []",
        "c1: begin",
        'c1: UPDATE {"id": 1}',
        "c1: commit",
        'c1: STAMP {"layout": 2}',
        "c2: rollback",
        "c2: release",
        "c1: release",
    ]


async def test_a_failed_action_ends_its_round_and_a_failed_transaction_is_rolled_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = FakeProvider(fail_on="UPDATE")
    planner = Planner(
        [
            [
                {"type": "transaction", "statements": [{"statement": "UPDATE", "parameters": {}}]},
                {"type": "execute", "statement": "NEVER", "parameters": {}, "purpose": "x"},
            ]
        ]
    )
    with caplog.at_level(logging.WARNING, logger="drivine.store"):
        await StoreLayoutMigration(provider, planner).migrate(DECLARED)
    assert planner.asked[1]["outcomes"] == [{"type": "failed", "message": "refused UPDATE"}]
    assert "c1: rollback" in provider.log and not any("NEVER" in line for line in provider.log)
    # Logged as drivine4j logs it, as the module's report may not say which statement the store refused.
    assert "Store layout migration of 'drivine': transaction failed: refused UPDATE" in caplog.messages


async def test_a_store_without_the_compiler_is_told_how_to_install_it_for_that_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from drivine.cypher import shared_compiler

    monkeypatch.setattr(shared_compiler, "_compile", None)
    monkeypatch.setattr(shared_compiler, "has_wheel", lambda: True)
    monkeypatch.setitem(sys.modules, "drivine_cypher_compiler", None)
    provider = FakeProvider()
    provider.type = "POSTGRES"  # type: ignore[misc]
    with pytest.raises(DrivineError, match=r"drivine\[postgres\]"):
        await StoreLayoutMigration(provider).migrate(DECLARED)
    assert provider.connections == 0


async def test_the_modules_refusal_is_thrown_before_the_store_is_reached() -> None:
    provider = FakeProvider()
    with pytest.raises(DrivineError, match="refused the migration"):
        await StoreLayoutMigration(provider, lambda _: json.dumps({"error": "No kind x"})).migrate(DECLARED)
    assert provider.connections == 0


async def test_a_lock_whose_statement_fails_is_given_back_and_ends_its_round() -> None:
    provider = FakeProvider(fail_on="LOCK")
    planner = Planner(
        [
            [
                {"type": "lock", "statement": "LOCK"},
                {"type": "execute", "statement": "NEVER", "parameters": {}, "purpose": "x"},
            ]
        ]
    )
    await StoreLayoutMigration(provider, planner).migrate(DECLARED)
    assert planner.asked[1]["outcomes"] == [{"type": "failed", "message": "refused LOCK"}]
    assert provider.log == ["c2: begin", "c2: LOCK []", "c2: rollback", "c2: release", "c1: release"]


def test_a_graph_engine_is_refused() -> None:
    provider = FakeProvider()
    provider.type = "NEO4J"  # type: ignore[misc]
    with pytest.raises(DrivineError, match="Postgres, Oracle or SQLite"):
        StoreLayoutMigration(provider, Planner([]))
