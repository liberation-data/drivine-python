"""
The spec's fragments.yaml run on each SQL store (liberation-data/drivine-cypher-spec#235), as drivine4j's
FragmentsCrossEngineTest runs it: each case's fragment, asked of the shared module as the object manager asks it, run
in SQL of the test's own over `setup`'s nodes, keeps or orders the nodes Neo4j does for the same expression.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from drivine import DatabaseType, PersistenceManager, PersistenceManagerFactory, QuerySpecification, cypher_statement
from drivine.cypher.shared_compiler import SharedStore, SqlFragment, compile_fragment
from tests.cypher.fragment_spec import CONDITIONS, ORDERS, SPEC, parameters, spec_indexed, spec_variables
from tests.integration.sql.new_graph import drop_graph, registry_on

GRAPH = "cy_fragments"
"""The graph the runner makes `setup`'s nodes in, a graph of its own, dropped before and after."""


def text(pm: PersistenceManager, row: str, key: str) -> str:
    """The text of the plain text property `key` of the node `row`, as the store reads it."""
    if pm.type is DatabaseType.ORACLE:
        return f"JSON_VALUE({row}.props, '$.{key}')"
    if pm.type is DatabaseType.SQLITE:
        return f"({row}.props ->> '$.{key}')"
    return f"({row}.props ->> '{key}')"


@pytest.fixture
async def spec_pm(store: str, factory: PersistenceManagerFactory) -> AsyncIterator[PersistenceManager]:
    """A manager of the runner's graph, made anew with `setup`'s nodes."""
    await drop_graph(factory.get(store), GRAPH)
    registry = registry_on(store, GRAPH)
    try:
        pm = PersistenceManagerFactory(registry).get(store)
        for statement in SPEC["setup"]:
            await pm.execute(QuerySpecification[Any](cypher_statement(statement)))
        yield pm
    finally:
        await registry.close()
        await drop_graph(factory.get(store), GRAPH)


async def test_each_fragment_keeps_or_orders_the_nodes_neo4j_does(spec_pm: PersistenceManager):
    pm = spec_pm
    store: SharedStore = (
        "ORACLE" if pm.type is DatabaseType.ORACLE else "SQLITE" if pm.type is DatabaseType.SQLITE else "POSTGRES"
    )
    indexed = spec_indexed()
    variables = spec_variables()
    row = variables["n"].row

    def fragment(part: str, cypher: str, given: dict[str, Any], descending: bool = False) -> SqlFragment:
        return compile_fragment(
            store, GRAPH, "order" if part == "order" else "condition", cypher, variables, given, descending, indexed
        )

    labelled = fragment("condition", "n:Person", {})
    id_ = text(pm, row, "id")
    wrong: list[str] = []
    checked = 0

    async def check(
        label: str,
        expected: list[str],
        condition: str | None,
        order: str | None,
        descending: bool,
        given: dict[str, Any],
    ) -> None:
        # One fragment run as the library runs it, its hints in the outermost SELECT, the ids it keeps or orders.
        nonlocal checked
        asked = (
            fragment("condition", condition, given)
            if condition is not None
            else fragment("order", order or "", given, descending)
        )
        hints = list(dict.fromkeys([*labelled.hints, *asked.hints]))
        hint = f"/*+ {' '.join(hints)} */ " if hints else ""
        sql = f"SELECT {hint}{id_} AS id FROM {GRAPH}_node {row} WHERE {labelled.term}"
        if condition is not None:
            sql += f" AND ({asked.term}) ORDER BY {id_}"
        else:
            sql += f" ORDER BY {', '.join([*asked.terms, id_])}"
        try:
            rows = await pm.query(QuerySpecification[Any](sql).bind({**labelled.parameters, **asked.parameters}))
            got = [str(next(iter(it.values())) if isinstance(it, dict) else it) for it in rows]
        except Exception as error:  # noqa: BLE001 - a failure is a mismatch the run reports with the rest
            got = [f"ERROR {str(error)[:300]}"]
        if got != expected:
            wrong.append(f"{label}: expected {expected}, got {got}\n    {sql}")
        checked += 1

    for case in SPEC["cases"]:
        if "rows" not in case:
            continue
        await check(
            case["name"],
            case["rows"],
            case.get("condition"),
            case.get("order"),
            bool(case.get("descending", False)),
            parameters(case.get("parameters")),
        )
    # The table: each form in each position, each value as an order both ways.
    for entry, at, condition in CONDITIONS:
        await check(condition, entry["rows"][at], condition, None, False, parameters(entry.get("parameters")))
    for entry, at, descending in ORDERS:
        order = entry["value"]
        await check(f"ORDER BY {order}{' DESC' if descending else ''}", entry["rows"][at], None, order, descending, {})
    assert not wrong, f"{len(wrong)} of {checked} differ from Neo4j:\n" + "\n".join(wrong)
    assert checked == 272, "fragments.yaml's cases with rows and its table"
