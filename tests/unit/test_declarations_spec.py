"""
What drivine-python's schema declarations give the module's schema planner, against the Cypher spec's declarations.yaml
(liberation-data/drivine-cypher-spec#135): for each case, declared in this library's own API, the `catalogs` and
`runtime` of the request an enforce gives the planner, as the module's JSON writes them, in order. The planner decides
everything from these, the store's listings and its markers, so a store one library indexed is up to date for the
others only where each library's declarations give the same catalogs. drivine4j's DeclarationsGoldenTest, ported, on
the async and the sync API. No database is spoken to: the request is read as the planner is given it, and the plan is
not run. A case the spec adds that no declaration here covers fails, so none is passed over. Then how a catalog gives
its items, which those requests are written from, beyond what the cases pin.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import pytest
import yaml

import drivine.schema.schema_manager as async_schema_manager
import drivine.sync.schema.schema_manager as sync_schema_manager
from drivine import (
    CaseInsensitive,
    DatabaseRegistry,
    DrivineError,
    FullTextIndex,
    NodeFragment,
    NodeId,
    PersistenceManagerFactory,
    RangeIndex,
    SchemaCatalog,
    SchemaOptions,
    Unique,
    VectorIndex,
    full_text_index,
    range_index,
    uniqueness_constraint,
    vector_index,
)
from drivine import SchemaManager as AsyncSchemaManager
from drivine.manager.graph_object_manager_factory import GraphObjectManagerFactory
from drivine.schema.engine_vector_options import EngineVectorOptions
from drivine.schema.full_text_index_spec import FullTextIndexSpec
from drivine.schema.range_index_spec import RangeIndexSpec
from drivine.schema.schema_specs import SchemaSpec
from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec
from drivine.schema.vector_index_spec import VectorIndexSpec
from drivine.sync import DatabaseRegistry as SyncDatabaseRegistry
from drivine.sync import PersistenceManagerFactory as SyncPersistenceManagerFactory
from drivine.sync import SchemaManager as SyncSchemaManager
from drivine.sync.manager.graph_object_manager_factory import (
    GraphObjectManagerFactory as SyncGraphObjectManagerFactory,
)

CASES: list[dict[str, Any]] = yaml.safe_load((Path(__file__).parents[1] / "cypher" / "declarations.yaml").read_text())[
    "cases"
]


@dataclass(frozen=True)
class Declared:
    """A case as declared here: the startup catalogs, and what each owner ensures at runtime, before an enforce."""

    catalogs: tuple[SchemaCatalog, ...] = ()
    runtime: tuple[tuple[str, tuple[SchemaSpec, ...]], ...] = field(default_factory=tuple)


def one(*items: SchemaSpec) -> Declared:
    """A catalog of the items, with no owner and no version."""
    return Declared((SchemaCatalog.of(*items),))


# ----- The fragments the cases declare -----


class Article(NodeFragment, labels=["Article"]):
    id: Annotated[str, NodeId()]
    slug: Annotated[str, Unique()]
    title: Annotated[str, RangeIndex()]
    embedding: Annotated[list[float], VectorIndex()]
    body: Annotated[str, FullTextIndex()]


class Account(NodeFragment, labels=["Account"]):
    id: Annotated[str, NodeId(), Unique()]
    name: str | None = None


class Ticket(NodeFragment, labels=["Ticket"], node_id_index=False):
    """Its NodeId's default range index turned off."""

    id: Annotated[str, NodeId()]


class Message(
    NodeFragment,
    labels=["Message"],
    schema=[Unique(["session_id", "sequence"]), FullTextIndex(["subject", "body"])],
):
    id: Annotated[str, NodeId()]
    session_id: str
    sequence: int
    subject: str
    body: str


class Customer(NodeFragment, labels=["Customer"]):
    id: Annotated[str, NodeId()]
    email: Annotated[str, RangeIndex()]


class CustomerAgain(NodeFragment, labels=["Customer"]):
    id: Annotated[str, NodeId()]
    email: Annotated[str, RangeIndex()]


class Member(NodeFragment, labels=["Member"]):
    id: Annotated[str, NodeId()]
    email: Annotated[str, CaseInsensitive(), Unique(ignore_case=True)]


def dimensions(label: str, property: str) -> int:
    """The dimensions a dimension provider gives every vector index: 384."""
    return 384


# ----- Each case, declared -----

DECLARATIONS: dict[str, Callable[[], Declared]] = {
    "range": lambda: one(range_index("Person", "name")),
    "range named": lambda: one(RangeIndexSpec("Person", ("name",), name="person_name_idx")),
    "range composite": lambda: one(range_index("Person", ["last", "first"])),
    "range long name": lambda: one(
        range_index(
            "AVeryLongLabelNameThatGoesOnAndOnForTheSakeOfAnIdentifierLimit",
            ["aPropertyWithALongName", "anotherPropertyWithALongName"],
        )
    ),
    "unique": lambda: one(uniqueness_constraint("Person", "email")),
    "unique composite": lambda: one(uniqueness_constraint("Person", ["email", "tenant"])),
    "unique named": lambda: one(UniquenessConstraintSpec("Person", ("email",), name="person_email_unique")),
    "ignore case": lambda: one(
        range_index("Person", "name", ignore_case=True), uniqueness_constraint("Person", "email", ignore_case=True)
    ),
    "vector": lambda: one(vector_index("Chunk", "embedding", 384, "EUCLIDEAN")),
    "vector dimensions only": lambda: one(vector_index("Chunk", "embedding", 384)),
    "vector tuned": lambda: one(
        VectorIndexSpec(
            "Chunk",
            ("embedding",),
            384,
            "COSINE",
            hnsw_m=16,
            hnsw_ef_construction=200,
            engine_options=(
                EngineVectorOptions("Neo4j", quantization_enabled=False),
                EngineVectorOptions("FalkorDB", ef_runtime=10),
            ),
        )
    ),
    "full-text": lambda: one(FullTextIndexSpec("Article", ("title", "body"), analyzer="english")),
    "full-text default analyzer": lambda: one(full_text_index("Article", "body")),
    # drivine-python's schema API has no TEXT index (#356): the nearest declaration, none, is what it gives today.
    "text": lambda: one(),
    "fragment": lambda: Declared((SchemaCatalog.from_fragments(Article, dimensions=dimensions),)),
    "fragment id served by a constraint": lambda: Declared((SchemaCatalog.from_fragments(Account),)),
    "fragment without its id's index": lambda: Declared((SchemaCatalog.from_fragments(Ticket),)),
    "fragment class items": lambda: Declared((SchemaCatalog.from_fragments(Message),)),
    "fragments merged": lambda: Declared((SchemaCatalog.from_fragments(Customer, CustomerAgain),)),
    "fragment ignoring case": lambda: Declared((SchemaCatalog.from_fragments(Member),)),
    "owner and version": lambda: Declared(
        (SchemaCatalog.of(range_index("Person", "name")).named("rag").with_version("v2"),)
    ),
    "versions combined": lambda: Declared(
        (
            SchemaCatalog.of(range_index("Person", "name")).named("rag").with_version("v2"),
            SchemaCatalog.of(uniqueness_constraint("Person", "email")).named("rag").with_version("a1"),
        )
    ),
    "co-owned": lambda: Declared(
        (
            SchemaCatalog.of(range_index("Person", "name")).named("a"),
            SchemaCatalog.of(range_index("Person", "name")).named("b"),
        )
    ),
    "ensured at runtime": lambda: Declared(
        runtime=(("dice", (vector_index("Chunk", "embedding", 768), range_index("Tenant", "code"))),)
    ),
}

GAPS: dict[str, list[str]] = {
    "text": ["#356"],
    "fragment class items": ["#442"],
}
"""
The cases drivine-python can't yet declare as drivine4j does, each by the drivine-python issues that bring it level:
what the run of drivine-cypher-spec#135 across the libraries reports of each case (tests/interop) and pins.
"""

GAP_REASONS: dict[str, str] = {
    "#356": "drivine-python's schema API declares no TEXT index",
    "#442": "FullTextIndex takes no analyzer",
}
"""What each issue of GAPS leaves out."""


def gap_reason(case: str) -> str:
    return "; ".join(f"{GAP_REASONS[it]} ({it})" for it in GAPS[case])


# ----- The request an enforce gives the planner -----

Api = str
APIS: list[Api] = ["async", "sync"]


@pytest.fixture
def requests(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Each request an enforce or ensure gives the planner, which isn't run: its report holds no owners."""
    seen: list[dict[str, Any]] = []

    async def planned(plan: Any, request: dict[str, Any], host: Any, *_: Any) -> dict[str, Any]:
        seen.append(request)
        return {"owners": []}

    def planned_sync(plan: Any, request: dict[str, Any], host: Any, *_: Any) -> dict[str, Any]:
        seen.append(request)
        return {"owners": []}

    monkeypatch.setattr(async_schema_manager, "run_plan", planned)
    monkeypatch.setattr(sync_schema_manager, "run_plan", planned_sync)
    yield seen


def registry_on(registry: Any, tmp_path: Path) -> Any:
    """`registry`, of a SQLite file of its own: a store whose schema the module plans."""
    env = {"SQLITE_DATABASE_TYPE": "SQLITE", "SQLITE_DATABASE_NAME": str(tmp_path / "graph.db")}
    registry.build_or_resolve_from_env("sqlite", env)
    return registry


async def enforced(api: Api, declared: Declared, tmp_path: Path, seen: list[dict[str, Any]]) -> dict[str, Any]:
    """
    The `catalogs` and `runtime` an enforce after the case's declarations gives the planner. Where it gives one a
    request, they are the request's; where it has nothing to apply, and plans nothing, they are what it would give,
    read where the request takes them from.
    """
    options = SchemaOptions(list(declared.catalogs))
    if api == "async":
        registry = registry_on(DatabaseRegistry(), tmp_path)
        manager = AsyncSchemaManager(registry, GraphObjectManagerFactory(PersistenceManagerFactory(registry)), options)
        try:
            for owner, specs in declared.runtime:
                await manager.ensure(owner, *specs)
            await manager.enforce()
            declared_on = manager._declared_on("sqlite", manager.factory.get("sqlite").schema_planning())  # pyright: ignore[reportPrivateUsage, reportArgumentType]
        finally:
            await registry.close()
    else:
        registry = registry_on(SyncDatabaseRegistry(), tmp_path)
        manager = SyncSchemaManager(
            registry, SyncGraphObjectManagerFactory(SyncPersistenceManagerFactory(registry)), options
        )
        try:
            for owner, specs in declared.runtime:
                manager.ensure(owner, *specs)
            manager.enforce()
            declared_on = manager._declared_on("sqlite", manager.factory.get("sqlite").schema_planning())  # pyright: ignore[reportPrivateUsage, reportArgumentType]
        finally:
            registry.close()
    catalogs, runtime = declared_on
    if seen and seen[-1]["operation"]["type"] == "enforce":
        # The request is what the plan is given: what it is read from is the same.
        assert (seen[-1]["catalogs"], seen[-1]["runtime"]) == (catalogs, runtime)
    return {"catalogs": catalogs, "runtime": runtime}


def test_every_case_the_spec_pins_is_declared_here_and_no_other():
    pinned = [it["case"] for it in CASES]
    assert len(pinned) == len(set(pinned))
    assert sorted(pinned) == sorted(DECLARATIONS), "declare each case of declarations.yaml in DECLARATIONS"
    assert set(GAPS) <= set(DECLARATIONS)
    assert {it for issues in GAPS.values() for it in issues} == set(GAP_REASONS)


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize(
    "case",
    [
        pytest.param(it, id=it["case"], marks=[pytest.mark.xfail(reason=gap_reason(it["case"]), strict=True)])
        if it["case"] in GAPS
        else pytest.param(it, id=it["case"])
        for it in CASES
    ],
)
async def test_the_declarations_give_the_planner_the_catalogs_the_spec_pins(
    case: dict[str, Any], api: Api, tmp_path: Path, requests: list[dict[str, Any]]
):
    declared = DECLARATIONS[case["case"]]()
    assert await enforced(api, declared, tmp_path, requests) == {
        "catalogs": case["catalogs"],
        "runtime": case["runtime"],
    }


# ----- How a catalog gives its items, as drivine4j's SchemaCatalog.build and FragmentSchemaScanner do -----


def test_a_catalog_gives_each_item_once_its_indexes_before_its_constraints_and_so_does_one_added_to():
    unique, name, body = uniqueness_constraint("P", "email"), range_index("P", "name"), full_text_index("P", "body")
    assert SchemaCatalog.of(unique, name, name, body).items == (name, body, unique)
    assert (SchemaCatalog.of(unique) + [name, unique, range_index("P", ["name"])]).items == (name, unique)


@pytest.mark.parametrize("api", APIS)
async def test_two_declarations_of_one_item_that_differ_are_both_kept_for_the_manager_to_refuse(
    api: Api, tmp_path: Path, requests: list[dict[str, Any]]
):
    unnamed, named = range_index("P", "name"), RangeIndexSpec("P", ("name",), name="by_name")
    catalog = SchemaCatalog.of(unnamed, named)
    assert catalog.items == (unnamed, named)
    with pytest.raises(DrivineError, match="declare the RANGE on P\\(name\\) on sqlite differently"):
        await enforced(api, Declared((catalog,)), tmp_path, requests)


class Composite(
    NodeFragment,
    labels=["Composite"],
    schema=[Unique(["a", "z"]), FullTextIndex(["z", "a"]), RangeIndex(["a", "z"]), FullTextIndex(["a"])],
):
    z: Annotated[str, RangeIndex()]
    a: Annotated[str, FullTextIndex()]


def test_a_fragments_class_arguments_come_first_by_kind_each_as_declared_then_its_fields_by_name():
    assert SchemaCatalog.from_fragments(Composite).items == (
        range_index("Composite", ["a", "z"]),
        full_text_index("Composite", ["z", "a"]),
        full_text_index("Composite", "a"),
        range_index("Composite", "z"),
        uniqueness_constraint("Composite", ["a", "z"]),
    )


@pytest.mark.parametrize("api", APIS)
async def test_what_ensure_gives_an_owner_reaches_the_planner_indexes_before_constraints(
    api: Api, tmp_path: Path, requests: list[dict[str, Any]]
):
    unique, code = uniqueness_constraint("Tenant", "id"), range_index("Tenant", "code")
    given = await enforced(api, Declared(runtime=(("dice", (unique, code, code)),)), tmp_path, requests)
    [ensured] = given["runtime"]
    assert [it["kind"] for it in ensured["items"]] == ["RANGE_INDEX", "UNIQUENESS_CONSTRAINT"]
    [ensure] = [it["operation"] for it in requests if it["operation"]["type"] == "ensure"]
    assert ensure["items"] == ensured["items"]


@pytest.mark.parametrize("api", APIS)
async def test_what_remove_takes_from_an_owner_reaches_the_planner_each_once_indexes_before_constraints(
    api: Api, tmp_path: Path, requests: list[dict[str, Any]]
):
    # As drivine4j's remove builds its catalog with SchemaCatalog.of.
    unique, code = uniqueness_constraint("Tenant", "id"), range_index("Tenant", "code")
    if api == "async":
        registry = registry_on(DatabaseRegistry(), tmp_path)
        manager = AsyncSchemaManager(
            registry, GraphObjectManagerFactory(PersistenceManagerFactory(registry)), SchemaOptions()
        )
        try:
            await manager.remove("dice", unique, code, code)
        finally:
            await registry.close()
    else:
        registry = registry_on(SyncDatabaseRegistry(), tmp_path)
        manager = SyncSchemaManager(
            registry, SyncGraphObjectManagerFactory(SyncPersistenceManagerFactory(registry)), SchemaOptions()
        )
        try:
            manager.remove("dice", unique, code, code)
        finally:
            registry.close()
    [remove] = [it["operation"] for it in requests if it["operation"]["type"] == "remove"]
    assert [(it["kind"], it["properties"]) for it in remove["items"]] == [
        ("RANGE_INDEX", ["code"]),
        ("UNIQUENESS_CONSTRAINT", ["id"]),
    ]
