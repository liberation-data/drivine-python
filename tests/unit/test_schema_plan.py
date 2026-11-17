"""
drivine-python's side of the shared module's schema plan (schema_plan, schema_plan_driver, planned_managers): its specs,
items and results as the module reads and writes them, each driver's error as the module's rules read it, the options a
failure names, and how a plan's actions are run and its failures raised. plans.yaml pins the plan's decisions, which the
module's own tests check on every target. Drivine for TypeScript's SchemaPlan.spec, ported.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest

from drivine import DatabaseType, ReusedIdsError
from drivine.drivine_error import DrivineError
from drivine.schema.engine_vector_options import EngineVectorOptions
from drivine.schema.planned_managers import PlannedConstraintManager, PlannedIndexManager, SchemaPlanning
from drivine.schema.range_index_spec import RangeIndexSpec
from drivine.schema.schema_grammar import schema_grammar_for
from drivine.schema.schema_plan import (
    DRIVINE4J_SETTINGS,
    SCHEMA_OPTIONS,
    SchemaPlan,
    causes,
    ensure_result,
    module_spec,
    plan_spec,
    with_option_names,
)
from drivine.schema.schema_plan_driver import plan_item
from drivine.schema.schema_specs import full_text_index, range_index, uniqueness_constraint, vector_index
from drivine.schema.vector_index_spec import VectorIndexSpec


class Host:
    """A plan's host whose statements `answer` answers, each recorded in `ran`."""

    def __init__(self, answer: Callable[[str], list[Any]] = lambda _: []) -> None:
        self.answer = answer
        self.ran: list[str] = []
        self.executor: Any = None
        self.copies: Any = None
        self.told = 0

    async def run(self, statement: str | Mapping[str, Any], parameters: Mapping[str, Any]) -> list[Any]:
        self.ran.append(str(statement))
        return self.answer(str(statement))

    async def marker(self, statement: str, parameters: Mapping[str, Any], compiled: bool, rows: bool) -> list[Any]:
        self.ran.append(statement)
        return []

    async def read_lower_copies(self) -> list[list[str]] | None:
        return None

    async def fill_lower_copies(self, label: str, properties: Sequence[str]) -> bool:
        return False

    def changed(self) -> None:
        self.told += 1


def planning(store: Any, host: Host) -> SchemaPlanning:
    return SchemaPlanning(SchemaPlan(store, "drivine"), "db", host)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    "spec",
    [
        range_index("Person", "name"),
        RangeIndexSpec("Person", ("name", "born"), ignore_case=True, name="by_name"),
        uniqueness_constraint("Person", "email", node_id_property="id"),
        full_text_index("Note", ["title", "body"]),
        VectorIndexSpec(
            "Doc",
            ("embedding",),
            3,
            "EUCLIDEAN",
            hnsw_m=16,
            engine_options=(EngineVectorOptions("Neo4j", quantization_enabled=True),),
        ),
    ],
)
def test_writes_each_kind_of_spec_as_the_module_reads_one_and_reads_it_back_as_it_was_declared(spec: Any):
    assert plan_spec(module_spec(spec)) == spec


def test_reads_a_result_as_the_module_writes_it_a_drifts_item_and_a_violations_sample_kept():
    spec = vector_index("Doc", "embedding", 4)
    drift = ensure_result({"drift": {"existing": {"label": "Doc", "properties": ["embedding"], "dimensions": 3}}}, spec)
    assert drift.status == "DRIFT" and drift.found is not None and drift.found.dimensions == 3
    sample = [{"properties": {"code": "dup"}, "count": 2, "nodes": []}]
    violation = ensure_result({"violation": {"sample": sample}}, spec)
    assert violation.status == "VIOLATION" and violation.sample == tuple(sample)
    assert ensure_result({"created": {}}, spec).status == "CREATED"
    with pytest.raises(DrivineError, match="doesn't know: sideways"):
        ensure_result({"sideways": {}}, spec)


class PostgresError(Exception):
    sqlstate = "23505"


class OracleError:
    full_code = "ORA-00955"
    code = 955


class Neo4jError(Exception):
    code = "Neo.ClientError.Schema.EquivalentSchemaRuleAlreadyExists"
    gql_status = "22N71"


class SqliteError(Exception):
    result = 19


def test_gives_each_drivers_error_to_the_modules_rules_by_its_state_number_or_code_and_any_other_by_its_message():
    assert causes(PostgresError("dup"))[0] == {
        "sqlState": "23505", "vendorCode": 0, "code": None, "gqlStatus": None, "message": "dup",
    }  # fmt: skip
    assert causes(Exception(OracleError()))[0]["vendorCode"] == 955
    neo4j = causes(Neo4jError("there"))[0]
    assert (neo4j["code"], neo4j["gqlStatus"]) == (Neo4jError.code, "22N71")
    assert causes(SqliteError("constraint"))[0]["vendorCode"] == 19
    assert causes(RuntimeError("plain"))[0] == {
        "sqlState": None, "vendorCode": None, "code": None, "gqlStatus": None, "message": "plain",
    }  # fmt: skip


def test_gives_a_failures_chain_outermost_first():
    try:
        try:
            raise PostgresError("inner")
        except PostgresError as inner:
            raise DrivineError("outer") from inner
    except DrivineError as outer:
        chain = causes(outer)
    assert [it["message"] for it in chain] == ["outer", "inner"]


def test_names_schema_options_where_a_plans_failure_names_drivine4js_settings():
    named = with_option_names(f"3 problem(s). {DRIVINE4J_SETTINGS}")
    assert named == f"3 problem(s). {SCHEMA_OPTIONS}"
    assert "drivine.schema" not in named and "recreate_on_drift" in named


async def test_raises_the_error_an_action_got_as_it_got_it_where_its_failure_ends_the_plan():
    reset = ConnectionResetError("connection reset")

    def answer(statement: str) -> list[Any]:
        if statement.startswith("CREATE"):
            raise reset
        return []

    host = Host(answer)
    with pytest.raises(ConnectionResetError) as raised:
        await plan_item(SchemaPlan("sqlite", "drivine"), "db", host, "ENSURE", range_index("Person", "name"))  # pyright: ignore[reportArgumentType]
    assert raised.value is reset


async def test_raises_a_graph_refused_for_every_owner_out_of_its_round_unanswered_planning_nothing_more(
    monkeypatch: pytest.MonkeyPatch,
):
    asked: list[Mapping[str, Any]] = []
    ask = SchemaPlan.ask

    def recorded(self: SchemaPlan, question: Mapping[str, Any]) -> dict[str, Any]:
        asked.append(question)
        return ask(self, question)

    monkeypatch.setattr(SchemaPlan, "ask", recorded)
    refused = ReusedIdsError("drivine", "/data/graph.db")

    def answer(_: str) -> list[Any]:
        raise refused

    with pytest.raises(ReusedIdsError) as raised:
        await plan_item(SchemaPlan("sqlite", "drivine"), "db", Host(answer), "ENSURE", range_index("Person", "name"))  # pyright: ignore[reportArgumentType]
    assert raised.value is refused
    assert not any("resume" in it for it in asked)


async def test_recreate_raises_where_a_table_stands_under_the_indexs_name_dropping_nothing_as_drivine4j(
    caplog: pytest.LogCaptureFixture,
):
    def answer(statement: str) -> list[Any]:
        # Postgres's listing, which names a table that stands under the range index's name.
        if "SELECT listed" in statement:
            return [
                {
                    "type": "r",
                    "table": None,
                    "where": None,
                    "method": "heap",
                    "object": "drivine_ix2_personname_44ce5436",
                }
            ]
        return []

    host = Host(answer)
    manager = PlannedIndexManager(planning("postgres", host), schema_grammar_for(DatabaseType.POSTGRES))
    with caplog.at_level(logging.CRITICAL), pytest.raises(DrivineError) as raised:
        await manager.recreate(range_index("Person", "name"))
    assert str(raised.value).startswith(
        "Could not recreate RANGE_INDEX on Person[name]: drivine_ix2_personname_44ce5436, an object Drivine didn't "
        "make, stands under its name. Drop or rename it"
    )
    assert not [it for it in host.ran if "DROP" in it.upper() or "CREATE" in it.upper()]


async def test_reads_the_data_once_the_store_refuses_a_constraint_a_sample_size_of_0_giving_a_violation_with_none(
    caplog: pytest.LogCaptureFixture,
):
    def answer(statement: str) -> list[Any]:
        if "CREATE UNIQUE INDEX" in statement:
            raise PostgresError("could not create unique index")
        # The sample of the keys held more than once.
        return [{"properties": {"email": "a"}, "count": 2, "nodes": []}] if "'count', COUNT(*)" in statement else []

    host = Host(answer)
    manager = PlannedConstraintManager(planning("postgres", host), schema_grammar_for(DatabaseType.POSTGRES))
    with caplog.at_level(logging.CRITICAL):
        result = await manager.ensure(uniqueness_constraint("Person", "email"), 0)
    assert result.status == "VIOLATION" and result.sample == ()
    created = next(i for i, it in enumerate(host.ran) if "CREATE UNIQUE INDEX" in it)
    assert any("'count', COUNT(*)" in it for it in host.ran[created + 1 :])


async def test_a_statement_that_makes_or_drops_an_item_tells_the_host_its_items_changed():
    host = Host()
    await plan_item(SchemaPlan("sqlite", "drivine"), "db", host, "ENSURE", range_index("Person", "name"))  # pyright: ignore[reportArgumentType]
    assert any(it.startswith("CREATE") for it in host.ran)
    assert host.told > 0
