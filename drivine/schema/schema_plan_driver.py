"""
Runs the shared module's schema plan (see schema_plan) on a host: each round's actions in order, on the host's
connections, and what each did given back to the module, until it is done; then its log lines are logged and its
report given, or its failure raised. drivine4j's `SchemaPlanDriver`, Drivine for TypeScript's `SchemaPlan.run`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from drivine import concurrency
from drivine.connection.reused_ids_error import ReusedIdsError
from drivine.drivine_error import DrivineError
from drivine.schema.schema_plan import Call, ItemPolicy, PlanSpec, SchemaPlan, causes, module_spec, plan_request
from drivine.schema.schema_plan import with_option_names as named

logger = logging.getLogger("drivine.schema")

_LEVELS = {"INFO": logging.INFO, "WARN": logging.WARNING, "ERROR": logging.ERROR}


class PlanHost(Protocol):
    """What a host runs a plan's actions with, on the database the plan is for."""

    async def run(self, statement: str | Mapping[str, Any], parameters: Mapping[str, Any]) -> list[Any]:
        """Runs a schema statement, or a listing, on a connection of its own: its rows, a row of one column its value.
        A native command (FalkorDB's `GRAPH.CONSTRAINT`) is given as `{"native": [...]}`."""
        ...

    async def marker(self, statement: str, parameters: Mapping[str, Any], compiled: bool, rows: bool) -> list[Any]:
        """Runs a statement of the marker's through the database's auto-commit manager: Cypher where `compiled`."""
        ...

    async def read_lower_copies(self) -> list[list[str]] | None:
        """The lowercased copies recorded as filled in, each `[label, property]`; None where none are kept."""
        ...

    async def fill_lower_copies(self, label: str, properties: Sequence[str]) -> bool:
        """Fills in the lowercased copies of the label's properties, recorded or not; False where there is no store."""
        ...

    def changed(self) -> None:
        """Told once a statement has made or dropped an item, as what the host knows of the store's items is stale."""
        ...


async def run_plan(
    plan: SchemaPlan,
    request: Mapping[str, Any],
    host: PlanHost,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """
    Runs `request` round by round with `host` until the plan is done, logs what the plan says to, and gives its report.
    Where an action's failure ends the plan, the error the host got for it is raised as it was; where the plan fails
    otherwise, a DrivineError with the plan's message, naming SchemaOptions where the module names drivine4j's settings.
    """
    errors: list[BaseException | None] = []
    answer = plan.ask({"start": dict(request), "now": int(clock() * 1000)})
    while answer.get("run") is not None:
        outcomes, error = await _round(answer["run"], host)
        errors.append(error)
        answer = plan.ask({"resume": answer["state"], "outcomes": outcomes, "now": int(clock() * 1000)})
    report: dict[str, Any] = answer["done"]
    # Under drivine.schema, as drivine-python's own schema managers log: the module names drivine4j's class.
    for line in report.get("log", []):
        logger.log(_LEVELS.get(line["level"], logging.INFO), named(line["message"]))
    rethrown = report.get("rethrown")
    if rethrown is not None:
        round_: int = rethrown["round"]
        error: BaseException | None = errors[round_] if 0 <= round_ < len(errors) else None
        raise error or DrivineError(named(report.get("failure") or "The schema plan failed."))
    if report.get("failure"):
        raise DrivineError(named(report["failure"]))
    return report


async def _round(actions: Sequence[Mapping[str, Any]], host: PlanHost) -> tuple[list[Any], BaseException | None]:
    """
    Runs a round's actions in order, up to and with the first that fails: what each did, and the error that ended it. A
    graph refused for every owner alike (ReusedIdsError) is raised out of the round as it is, unanswered, and plans
    nothing more, as drivine4j rethrows its `ReusedIdsException` past its owners' isolation.
    """
    outcomes: list[Any] = []
    for action in actions:
        try:
            outcomes.append(await _answer(action, host))
        except ReusedIdsError:
            raise
        except Exception as error:
            outcomes.append({"type": "failed", "causes": causes(error)})
            return outcomes, error
    return outcomes, None


async def _answer(action: Mapping[str, Any], host: PlanHost) -> dict[str, Any]:
    kind = action["type"]
    if kind == "query":
        delay = action.get("delayMillis") or 0
        if delay > 0:
            await concurrency.sleep(delay / 1000)
        if action["purpose"] in ("MARKER", "SCOPES"):
            rows = await host.marker(action["statement"], action["parameters"], bool(action.get("compiled")), True)
        else:
            rows = await host.run(action["statement"], action["parameters"])
        return {"type": "rows", "rows": rows}
    if kind == "execute":
        if action["purpose"] == "MARKER":
            await host.marker(action["statement"], action["parameters"], False, False)
            return {"type": "ran"}
        try:
            await host.run(action["statement"], action["parameters"])
        finally:
            if action["purpose"] != "PREPARE":
                host.changed()
        return {"type": "ran"}
    if kind == "awaitIndex":
        await host.run(action["statement"], action["parameters"])
        return {"type": "ran"}
    if kind == "readLowerCopies":
        copies = await host.read_lower_copies()
        if copies is None:
            raise DrivineError(_NO_COPIES)
        return {"type": "rows", "rows": copies}
    if kind == "fillLowerCopies":
        if not await host.fill_lower_copies(action["label"], action["properties"]):
            raise DrivineError(_NO_COPIES)
        return {"type": "ran"}
    if kind == "writeMarker":
        await host.marker(action["statement"], action["parameters"], False, False)
        return {"type": "ran"}
    raise DrivineError(f"drivine-python runs no plan action {dict(action)}.")


_NO_COPIES = (
    "The lowercased copies can't be read or filled in: this manager has no store to fill the lowercased copies in "
    "through."
)


async def plan_item(
    plan: SchemaPlan,
    database: str,
    host: PlanHost,
    call: Call,
    spec: PlanSpec,
    policy: ItemPolicy | None = None,
) -> dict[str, Any]:
    """
    One of the public index and constraint calls, `ENSURE`, `RECREATE` or `DROP`, of `spec`, as the module plans it:
    its report's item, `{result, dropped}`.
    """
    report = await run_plan(
        plan,
        plan_request(database, {"type": "item", "call": call, "spec": module_spec(spec)}, policy or ItemPolicy()),
        host,
    )
    item: dict[str, Any] | None = report.get("item")
    if item is None:
        raise DrivineError(f"The plan of {call} of {spec.kind} on {spec.label} gave no item.")
    return item


async def listed(
    plan: SchemaPlan,
    host: PlanHost,
    of: str,
    statement: str | None,
    spec: PlanSpec | None = None,
) -> list[dict[str, Any]]:
    """
    The items of a listing the store holds, read with no plan, as drivine4j's managers list and find: none where the
    listing can't be read, as a store that refuses it holds none this can see. With `spec`, those of its identity.
    """
    if not statement:
        return []
    try:
        rows = await host.run(statement, {})
    except Exception as error:
        logger.debug(
            "%s introspection failed (treating as none exist): %s", "Index" if of == "indexes" else "Constraint", error
        )
        return []
    return plan.items("indexes" if of == "indexes" else "constraints", rows, spec)
