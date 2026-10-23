"""
Schema known only at runtime, ensured under an owner with `SchemaManager.ensure`: drivine4j's
SchemaManagerRuntimeIntegrationTest. A second SchemaManager over the same database stands for the next process.
"""

import dataclasses
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from drivine import (
    ConnectionProperties,
    DatabaseRegistry,
    DrivineError,
    EngineVectorOptions,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    SchemaCatalog,
    SchemaManager,
    SchemaOptions,
    StatelessGraphObjectManager,
    full_text_index,
    range_index,
    uniqueness_constraint,
    vector_index,
)
from drivine.manager.graph_object_manager_factory import GraphObjectManagerFactory
from drivine.schema.recorded_inventory import RecordedInventory, inventory_entries, read_inventory
from drivine.schema.schema_catalog import DEFAULT_SCOPE
from drivine.schema.schema_manager import inventory_key
from drivine.schema.schema_specs import SchemaSpec
from tests.integration.conftest import ENGINES, POSTGRES
from tests.integration.stores import create


async def _drop_labelled(gom: StatelessGraphObjectManager, prefix: str) -> None:
    """Drops the schema items of the labels a test made: they outlive its data."""
    for it in await gom.constraints.list():
        if it.label.startswith(prefix):
            await gom.constraints.drop(uniqueness_constraint(it.label, it.properties))
    for it in await gom.indexes.list("VECTOR"):
        if it.label.startswith(prefix):
            await gom.indexes.drop(vector_index(it.label, it.properties[0], it.dimensions or 1))
    for it in await gom.indexes.list("FULLTEXT"):
        if it.label.startswith(prefix):
            await gom.indexes.drop(full_text_index(it.label, it.properties))
    for it in await gom.indexes.list("RANGE"):
        if it.label.startswith(prefix):
            await gom.indexes.drop(range_index(it.label, it.properties))


@pytest.fixture
def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    return StatelessGraphObjectManager(pm)


@pytest.fixture
async def label(gom: StatelessGraphObjectManager) -> AsyncIterator[str]:
    """A label of the test's own, so schema items, which outlive a test's data, don't meet another test's."""
    name = f"R{uuid.uuid4().hex[:10]}"
    yield name
    await _drop_labelled(gom, name)


class Schema:
    """What a test asks of the store, and how it makes the managers that stand for each process."""

    def __init__(self, factory: PersistenceManagerFactory, pm: PersistenceManager, label: str) -> None:
        self.factory = factory
        self.managers = GraphObjectManagerFactory(factory)
        self.pm = pm
        self.gom = StatelessGraphObjectManager(pm)
        self.db = pm.database
        self.label = label
        self.startup = range_index(label, "contextId")

    def manager(self, *catalogs: SchemaCatalog, **options: Any) -> SchemaManager:
        return SchemaManager(self.factory.registry, self.managers, SchemaOptions(list(catalogs), **options))

    def dice(self, version: str | None = None) -> SchemaCatalog:
        """The owner's catalog at startup."""
        catalog = SchemaCatalog.of(self.startup).named("dice").for_database(self.db)
        return catalog.with_version(version) if version is not None else catalog

    def runtime(self, owner: str, *specs: SchemaSpec) -> SchemaCatalog:
        return SchemaCatalog.of(*specs).named(owner).for_database(self.db)

    def vector(self, dimensions: int) -> SchemaSpec:
        return vector_index(self.label, "embedding", dimensions)

    async def record(self, owner: str) -> RecordedInventory | None:
        backend = self.gom.backend
        return read_inventory(
            backend.marker_items(await self.pm.maybe_get_one(backend.schema_marker("items").spec({"scope": owner})))
        )

    async def version(self, owner: str) -> str | None:
        return await self.pm.maybe_get_one(self.gom.backend.schema_marker("version").spec({"scope": owner}))

    async def write_marker(self, owner: str, version: str | None, items: str) -> None:
        """A marker as another library, or an earlier process, wrote it."""
        await self.pm.execute(
            self.gom.backend.record_schema_marker().spec({"scope": owner, "version": version, "items": items})
        )

    async def dimensions(self, label: str | None = None) -> int | None:
        held = [it for it in await self.gom.indexes.list("VECTOR") if it.label == (label or self.label)]
        return held[0].dimensions if held else None

    async def has_range(self, spec: SchemaSpec) -> bool:
        return any(
            it.label == spec.label and set(it.properties) == set(spec.properties)
            for it in await self.gom.indexes.list("RANGE")
        )


@pytest.fixture
def schema(factory: PersistenceManagerFactory, pm: PersistenceManager, label: str) -> Schema:
    return Schema(factory, pm, label)


def statuses(report: dict[str, Sequence[Any]], database: str) -> list[str]:
    return sorted(it.status for it in report[database])


def orphan_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [it.getMessage() for it in caplog.records if "no longer declares" in it.getMessage()]


WARN = {"mode": "WARN"}


async def test_an_item_ensured_at_runtime_joins_the_owners_inventory_and_a_later_enforce_keeps_it(schema, caplog):
    manager = schema.manager(schema.dice("d-1"))
    await manager.enforce()

    await manager.ensure_catalog(schema.runtime("dice", schema.vector(3)))

    assert await schema.dimensions() == 3
    vector, startup = inventory_key(schema.vector(3)), inventory_key(schema.startup)
    assert await schema.record("dice") == RecordedInventory((startup, vector), (vector,))
    assert await schema.version("dice") == "d-1", "ensure records the version it finds"
    with caplog.at_level(logging.WARNING, "drivine.schema"):
        again = await manager.enforce()
    assert statuses(again, schema.db) == ["ALREADY_MATCHING"] * 2, "enforce applies the runtime item with the rest"
    assert await schema.record("dice") == RecordedInventory((startup, vector), (vector,))
    assert await schema.dimensions() == 3
    assert orphan_warnings(caplog) == []


async def test_ensure_applies_only_the_items_given(schema):
    manager = schema.manager(schema.dice())
    await manager.enforce()
    await schema.gom.indexes.drop(schema.startup)

    report = await manager.ensure_catalog(schema.runtime("dice", schema.vector(3)))

    assert statuses(report, schema.db) == ["CREATED"]
    assert not await schema.has_range(schema.startup), "the owner's other items are left as they are"


async def test_a_manager_made_later_keeps_what_an_earlier_one_ensured_without_calling_it_an_orphan(schema, caplog):
    first = schema.manager(schema.dice())
    await first.enforce()
    await first.ensure_catalog(schema.runtime("dice", schema.vector(3)))

    # The next process: the same catalogs, and the runtime item not ensured yet.
    with caplog.at_level(logging.WARNING, "drivine.schema"):
        await schema.manager(schema.dice()).enforce()

    assert orphan_warnings(caplog) == []
    vector = inventory_key(schema.vector(3))
    record = await schema.record("dice")
    assert record is not None and vector in record.keys and record.runtime == (vector,)
    assert await schema.dimensions() == 3


async def test_a_runtime_entry_drivine4j_wrote_is_carried_by_an_enforce_not_dropped_or_called_an_orphan(schema, caplog):
    # drivine4j's marker: its runtime items prefixed, the marker shared by every Drivine.
    startup = inventory_key(schema.startup)
    theirs = f"VECTOR_INDEX:{schema.label}:embedding:{schema.label}_embedding_vector"
    stale = f"RANGE_INDEX:{schema.label}:slug:{schema.label}_slug_range"
    await schema.write_marker("dice", None, inventory_entries([startup, theirs, stale], {theirs, stale}, {stale}))

    with caplog.at_level(logging.WARNING, "drivine.schema"):
        await schema.manager(schema.dice()).enforce()

    assert orphan_warnings(caplog) == []
    assert await schema.record("dice") == RecordedInventory((startup, theirs, stale), (theirs, stale), (stale,))


async def test_an_owner_ensured_at_runtime_alone_is_left_as_it_was_by_an_enforce_that_does_not_know_it(schema):
    tenant = range_index(schema.label, "slug")
    await schema.manager().ensure_catalog(schema.runtime("tenants", tenant))

    await schema.manager(schema.dice()).enforce()

    record = await schema.record("tenants")
    assert record is not None and record.runtime == (inventory_key(tenant),)
    assert await schema.has_range(tenant)


async def test_a_manager_with_no_catalog_applies_what_it_was_ensured_under_the_unnamed_owner_too(schema):
    anonymous = range_index(schema.label, "slug")
    manager = schema.manager()
    await manager.ensure_catalog(schema.runtime(DEFAULT_SCOPE, anonymous))
    await schema.gom.indexes.drop(anonymous)

    await manager.enforce()

    assert await schema.has_range(anonymous), "enforce applies the runtime item"
    record = await schema.record(DEFAULT_SCOPE)
    assert record is not None and record.runtime == (inventory_key(anonymous),)


async def test_a_version_change_marks_a_runtime_item_it_has_no_spec_for_and_the_next_ensure_recreates_it(schema):
    first = schema.manager(schema.dice("model-a"), **WARN)
    await first.enforce()
    await first.ensure_catalog(schema.runtime("dice", schema.vector(3)))

    # The next process changes the owner's version; the vector's spec comes later, at a new width.
    following = schema.manager(schema.dice("model-b"), **WARN)
    await following.enforce()
    vector = inventory_key(schema.vector(3))
    record = await schema.record("dice")
    assert record is not None and record.stale == (vector,)
    assert await schema.dimensions() == 3, "not touched before its spec is known"

    report = await following.ensure_catalog(schema.runtime("dice", schema.vector(4)))

    assert await schema.dimensions() == 4, "recreated, not left as drift"
    assert statuses(report, schema.db) == ["RECREATED"]
    record = await schema.record("dice")
    assert record is not None and record.stale == ()


async def test_ensure_neither_acts_on_a_version_change_nor_recreates_on_startup(schema):
    first = schema.manager(schema.dice("d-1"))
    await first.enforce()
    await first.ensure_catalog(schema.runtime("dice", schema.vector(3)))

    changed = await schema.manager(schema.dice("d-2")).ensure_catalog(schema.runtime("dice", schema.vector(3)))
    assert statuses(changed, schema.db) == ["ALREADY_MATCHING"]
    assert await schema.version("dice") == "d-1", "the version change is left to enforce"

    startup = schema.manager(schema.dice("d-1"), recreate_on_startup=True)
    assert statuses(await startup.ensure_catalog(schema.runtime("dice", schema.vector(3))), schema.db) == [
        "ALREADY_MATCHING"
    ]


async def test_a_version_change_spares_a_runtime_item_it_has_no_spec_for_when_another_owner_declares_it(schema):
    shared = range_index(schema.label, "slug")
    first = schema.manager(schema.dice("model-a"))
    await first.enforce()
    await first.ensure_catalog(schema.runtime("dice", shared))

    app = SchemaCatalog.of(shared).named("app").for_database(schema.db)
    await schema.manager(schema.dice("model-b"), app).enforce()

    assert await schema.record("dice") == RecordedInventory(
        (inventory_key(schema.startup), inventory_key(shared)), (inventory_key(shared),)
    ), "spared, as a co-owned item is"


async def test_a_recreate_all_by_a_manager_made_later_marks_a_runtime_item_and_a_later_enforce_keeps_the_mark(
    schema, caplog
):
    first = schema.manager(schema.dice())
    await first.enforce()
    await first.ensure_catalog(schema.runtime("dice", schema.vector(3)))

    following = schema.manager(schema.dice())
    await following.recreate_all()
    vector = inventory_key(schema.vector(3))
    record = await schema.record("dice")
    assert record is not None and record.stale == (vector,)

    with caplog.at_level(logging.WARNING, "drivine.schema"):
        await following.enforce()
    record = await schema.record("dice")
    assert record is not None and record.stale == (vector,), "still waiting for its spec"
    assert orphan_warnings(caplog) == []


async def test_recreate_on_startup_by_a_manager_made_later_marks_a_runtime_item_and_the_next_ensure_recreates_it(
    schema,
):
    first = schema.manager(schema.dice())
    await first.enforce()
    await first.ensure_catalog(schema.runtime("dice", schema.vector(3)))

    following = schema.manager(schema.dice(), recreate_on_startup=True)
    await following.enforce()
    vector = inventory_key(schema.vector(3))
    record = await schema.record("dice")
    assert record is not None and record.stale == (vector,)

    report = await following.ensure_catalog(schema.runtime("dice", schema.vector(3)))
    assert statuses(report, schema.db) == ["RECREATED"]
    record = await schema.record("dice")
    assert record is not None and record.stale == ()


async def test_a_runtime_item_marked_while_its_manager_holds_it_keeps_the_mark_until_that_manager_applies_it(schema):
    tag = range_index(schema.label, "name")
    manager = schema.manager(schema.dice())
    await manager.enforce()
    await manager.ensure_catalog(schema.runtime("dice", schema.vector(3)))

    # Another process recreates the owner without the vector's spec.
    await schema.manager(schema.dice()).recreate_all()
    vector = inventory_key(schema.vector(3))
    record = await schema.record("dice")
    assert record is not None and record.stale == (vector,)

    await manager.ensure_catalog(schema.runtime("dice", tag))
    record = await schema.record("dice")
    assert record is not None and record.stale == (vector,), "not applied by this ensure, so still waiting"

    report = await manager.enforce()
    record = await schema.record("dice")
    assert record is not None and record.stale == ()
    assert statuses(report, schema.db) == ["ALREADY_MATCHING", "ALREADY_MATCHING", "RECREATED"]


async def test_without_a_version_change_a_runtime_item_of_another_shape_is_drift_handled_per_the_options(
    schema, caplog
):
    first = schema.manager(schema.dice(), **WARN)
    await first.enforce()
    await first.ensure_catalog(schema.runtime("dice", schema.vector(3)))

    with caplog.at_level(logging.WARNING, "drivine.schema"):
        await schema.manager(schema.dice(), **WARN).ensure_catalog(schema.runtime("dice", schema.vector(4)))
    assert await schema.dimensions() == 3
    assert "DRIFT" in caplog.text

    with pytest.raises(DrivineError, match="DRIFT"):
        await schema.manager(schema.dice()).ensure_catalog(schema.runtime("dice", schema.vector(4)))

    await schema.manager(schema.dice(), recreate_on_drift=True).ensure_catalog(schema.runtime("dice", schema.vector(4)))
    assert await schema.dimensions() == 4


async def test_ensuring_an_item_again_with_the_same_identity_replaces_it_in_the_owners_items(schema):
    manager = schema.manager(schema.dice(), recreate_on_drift=True)
    await manager.ensure_catalog(schema.runtime("dice", schema.vector(3)))
    await manager.ensure_catalog(schema.runtime("dice", schema.vector(4)))

    # Were both kept, the owner's merge would refuse them as a conflict.
    await manager.enforce()

    assert await schema.dimensions() == 4


async def test_two_items_of_one_identity_given_at_once_that_differ_raise_and_nothing_is_kept(schema):
    manager = schema.manager(schema.dice())

    with pytest.raises(
        DrivineError, match=r"Two schema catalogs of 'dice' declare the VECTOR on \w+\(embedding\) differently"
    ):
        await manager.ensure_catalog(schema.runtime("dice", schema.vector(3), schema.vector(4)))

    assert await schema.dimensions() is None
    assert await schema.record("dice") is None
    await manager.enforce()  # would apply one of them, were it kept
    assert await schema.dimensions() is None
    assert await schema.record("dice") == RecordedInventory((inventory_key(schema.startup),))


async def test_an_untuned_and_a_tuned_item_of_one_identity_given_at_once_keep_the_tuned_one(schema):
    plain = schema.vector(3)
    tuned = dataclasses.replace(plain, engine_options=(EngineVectorOptions("Neo4j", hnsw_m=16),))
    manager = schema.manager(schema.dice())

    report = await manager.ensure_catalog(schema.runtime("dice", tuned, plain))

    assert [it.spec for it in report[schema.db]] == [tuned]
    assert [it.spec for it in (await manager.enforce())[schema.db] if it.spec.kind == "VECTOR"] == [tuned]


async def test_recreate_all_rebuilds_the_runtime_items_too(schema):
    manager = schema.manager(schema.dice())
    await manager.enforce()
    await manager.ensure_catalog(schema.runtime("dice", schema.vector(3)))

    assert statuses(await manager.recreate_all(), schema.db) == ["RECREATED"] * 2


async def test_a_runtime_item_that_conflicts_with_another_owners_raises_naming_both_and_nothing_is_kept(schema):
    app = SchemaCatalog.of(schema.vector(3)).named("app").for_database(schema.db)
    manager = schema.manager(app)
    await manager.enforce()

    with pytest.raises(DrivineError, match="Schema catalogs of 'app' and 'rag' declare"):
        await manager.ensure_catalog(schema.runtime("rag", schema.vector(4)))

    await manager.enforce()  # would raise, were the conflicting item kept
    assert await schema.dimensions() == 3
    assert await schema.record("rag") is None


async def test_a_runtime_item_that_conflicts_with_the_owners_own_declaration_raises_and_nothing_is_kept(schema):
    dice = SchemaCatalog.of(schema.vector(3)).named("dice").for_database(schema.db)
    manager = schema.manager(dice)
    await manager.enforce()

    with pytest.raises(DrivineError, match="Two schema catalogs of 'dice' declare"):
        await manager.ensure_catalog(schema.runtime("dice", schema.vector(4)))

    await manager.enforce()
    assert await schema.dimensions() == 3


async def test_a_conflict_names_the_owner_whose_declaration_won_the_merge_not_the_first_to_declare_the_item(schema):
    def vector(hnsw_m: int | None = None) -> SchemaSpec:
        options = () if hnsw_m is None else (EngineVectorOptions("Neo4j", hnsw_m=hnsw_m),)
        return vector_index(schema.label, "embedding", 3, engine_options=options)

    # app's plain declaration agrees with rag's, whose engine options win; tenants' differ from rag's.
    app = SchemaCatalog.of(vector()).named("app").for_database(schema.db)
    rag = SchemaCatalog.of(vector(16)).named("rag").for_database(schema.db)

    with pytest.raises(DrivineError, match="Schema catalogs of 'rag' and 'tenants' declare"):
        await schema.manager(app, rag).ensure_catalog(schema.runtime("tenants", vector(32)))


async def test_a_runtime_item_identical_to_another_owners_is_co_owned_and_the_two_dedupe(schema):
    shared = range_index(schema.label, "slug")
    manager = schema.manager(SchemaCatalog.of(shared).named("app").for_database(schema.db))
    await manager.enforce()

    report = await manager.ensure_catalog(schema.runtime("rag", shared))

    assert statuses(report, schema.db) == ["ALREADY_MATCHING"]
    assert await schema.record("rag") == RecordedInventory((inventory_key(shared),), (inventory_key(shared),))


async def test_an_item_the_owner_declares_ensured_again_at_runtime_is_recorded_as_declared(schema):
    manager = schema.manager(schema.dice())
    await manager.enforce()

    report = await manager.ensure_catalog(schema.runtime("dice", schema.startup))

    assert statuses(report, schema.db) == ["ALREADY_MATCHING"]
    assert await schema.record("dice") == RecordedInventory((inventory_key(schema.startup),)), "not marked runtime"


async def test_a_runtime_item_renamed_is_an_orphan_of_its_owner_under_the_old_name(schema, caplog):
    # drivine4j names an item as its spec says; this one recorded it as tag_one.
    tag = range_index(schema.label, "name")
    await schema.write_marker("dice", None, f"runtime:RANGE_INDEX:{schema.label}:name:tag_one")

    with caplog.at_level(logging.WARNING, "drivine.schema"):
        await schema.manager(schema.dice(), **WARN).ensure_catalog(schema.runtime("dice", tag))

    [orphan] = orphan_warnings(caplog)
    assert f"Owner 'dice' on {schema.db} applied 1 item(s)" in orphan and "tag_one" in orphan


async def test_a_violation_found_by_ensure_raises_under_fail_fast_and_the_marker_is_not_written(schema):
    await create(schema.pm, schema.label, {"email": "x"}, {"email": "x"})

    with pytest.raises(DrivineError, match="VIOLATION"):
        await schema.manager().ensure_catalog(schema.runtime("dice", uniqueness_constraint(schema.label, "email")))

    assert await schema.record("dice") is None


async def test_a_runtime_catalog_may_name_its_database_which_must_be_registered_and_may_not_carry_a_version(schema):
    named, lost = range_index(schema.label, "slug"), range_index(schema.label, "lost")
    manager = schema.manager()
    await manager.ensure_catalog(schema.runtime("dice", named))
    assert await schema.has_range(named)

    with pytest.raises(DrivineError, match="not a registered graph database"):
        await manager.ensure_catalog(SchemaCatalog.of(lost).named("dice").for_database("nowhere"))
    with pytest.raises(DrivineError, match="cannot carry a version"):
        await manager.ensure_catalog(schema.runtime("dice", lost).with_version("v1"))

    await manager.enforce()  # neither refused catalog was kept
    assert not await schema.has_range(lost)


async def test_nothing_given_ensures_nothing(schema):
    assert await schema.manager(schema.dice()).ensure("dice") == {}
    assert await schema.record("dice") is None


async def test_a_marker_records_and_reads_back_runtime_and_stale_entries(schema):
    await schema.write_marker("dice", "v1", inventory_entries(["a", "b", "c"], {"b", "c"}, {"c"}))
    assert await schema.record("dice") == RecordedInventory(("a", "b", "c"), ("b", "c"), ("c",))
    assert await schema.version("dice") == "v1"
    await schema.write_marker("dice", "v1", inventory_entries([]))
    assert await schema.record("dice") == RecordedInventory(())


async def test_a_runtime_item_that_conflicts_on_one_database_applies_nothing_on_any_other(factory):
    label = f"R{uuid.uuid4().hex[:10]}"
    fresh, theirs = range_index(label, "slug"), vector_index(label, "embedding", 3)
    app = SchemaCatalog.of(theirs).named("app").for_database("memgraph")
    manager = SchemaManager(factory.registry, GraphObjectManagerFactory(factory), SchemaOptions([app]))
    neo4j = StatelessGraphObjectManager(factory.get("default"))
    try:
        with pytest.raises(DrivineError, match="differently"):
            await manager.ensure_catalog(
                SchemaCatalog.of(fresh, vector_index(label, "embedding", 4))
                .named("rag")
                .for_databases("default", "memgraph")
            )
        held = [it for it in await neo4j.indexes.list("RANGE") if it.label == label]
        assert held == [], "nothing applied on one database before the conflict on the other was found"
        await manager.enforce()  # would raise, were the conflicting item kept
    finally:
        await _drop_labelled(neo4j, label)
        await _drop_labelled(StatelessGraphObjectManager(factory.get("memgraph")), label)


async def test_an_item_ensured_again_on_the_same_databases_named_in_another_order_replaces_it(factory):
    # drivine4j keeps a catalog's databases as a set: the order they are named in is no part of the owner's items.
    label = f"R{uuid.uuid4().hex[:10]}"
    options = SchemaOptions(recreate_on_drift=True)
    manager = SchemaManager(factory.registry, GraphObjectManagerFactory(factory), options)
    goms = [StatelessGraphObjectManager(factory.get(it)) for it in ("default", "memgraph")]
    try:
        rag = SchemaCatalog.of(vector_index(label, "embedding", 3)).named("rag")
        await manager.ensure_catalog(rag.for_databases("default", "memgraph"))
        again = SchemaCatalog.of(vector_index(label, "embedding", 4)).named("rag")
        await manager.ensure_catalog(again.for_databases("memgraph", "default"))

        await manager.enforce()  # would raise, were both kept

        for gom in goms:
            assert [it.dimensions for it in await gom.indexes.list("VECTOR") if it.label == label] == [4]
    finally:
        for gom in goms:
            await _drop_labelled(gom, label)


async def test_two_differing_items_of_one_identity_given_at_once_raise_with_no_graph_database_registered():
    # drivine4j refuses them when it builds the catalog, whatever databases are registered.
    own = DatabaseRegistry()
    manager = SchemaManager(own, GraphObjectManagerFactory(PersistenceManagerFactory(own)), SchemaOptions())
    label = f"R{uuid.uuid4().hex[:10]}"

    with pytest.raises(DrivineError, match=r"declare the VECTOR on \w+\(embedding\) differently"):
        await manager.ensure("dice", vector_index(label, "embedding", 3), vector_index(label, "embedding", 4))


async def test_a_tuned_item_given_before_an_untuned_one_keeps_its_options_with_no_graph_database_registered():
    own = DatabaseRegistry()
    factory = PersistenceManagerFactory(own)
    manager = SchemaManager(own, GraphObjectManagerFactory(factory), SchemaOptions())
    label = f"R{uuid.uuid4().hex[:10]}"
    plain = vector_index(label, "embedding", 3)
    tuned = dataclasses.replace(plain, engine_options=(EngineVectorOptions("Neo4j", hnsw_m=16),))
    try:
        assert await manager.ensure("dice", tuned, plain) == {}
        own.build_or_resolve_from_env(None)  # a graph registered after the items were ensured

        report = await manager.enforce()

        assert [it.spec for it in report["default"]] == [tuned]
    finally:
        await _drop_labelled(StatelessGraphObjectManager(factory.get("default")), label)
        await own.close()


async def test_ensure_by_owner_applies_on_every_graph_database():
    # A registry of its own: the session's may hold a store another test made and dropped.
    own = DatabaseRegistry()
    for name in ENGINES:
        own.build_or_resolve_from_env(None if name == "default" else name)
    factory = PersistenceManagerFactory(own)
    label = f"R{uuid.uuid4().hex[:10]}"
    manager = SchemaManager(own, GraphObjectManagerFactory(factory), SchemaOptions())
    try:
        report = await manager.ensure("tenants", range_index(label, "slug"))
        assert sorted(report) == sorted(it.name for it in own.providers)
        assert {statuses(report, it)[0] for it in report} == {"CREATED"}
    finally:
        for it in own.providers:
            await _drop_labelled(StatelessGraphObjectManager(factory.get(it.name)), label)
        await own.close()


@pytest.mark.skipif(not POSTGRES, reason="Postgres is not configured.")
async def test_ensure_on_a_postgres_store_not_yet_made_makes_the_store_first(factory):
    name = f"pgstore_ensure_{uuid.uuid4().hex[:8]}"
    own = DatabaseRegistry()
    properties = dataclasses.replace(ConnectionProperties.from_env("postgres"), default_graph_path=name)
    own.builder().with_properties(properties).register("fresh")
    manager = SchemaManager(own, GraphObjectManagerFactory(PersistenceManagerFactory(own)), SchemaOptions())
    spec = range_index("Tenant", "slug")
    try:
        report = await manager.ensure("tenants", spec)
        assert statuses(report, "fresh") == ["CREATED"]
        gom = StatelessGraphObjectManager(PersistenceManagerFactory(own).get("fresh"))
        record = read_inventory(
            gom.backend.marker_items(
                await gom.persistence_manager.maybe_get_one(
                    gom.backend.schema_marker("items").spec({"scope": "tenants"})
                )
            )
        )
        assert record == RecordedInventory((inventory_key(spec),), (inventory_key(spec),))
    finally:
        await factory.get("postgres").execute(
            QuerySpecification[Any](f"DROP TABLE IF EXISTS {name}_edge, {name}_node, {name}_schema CASCADE")
        )
        await own.close()
