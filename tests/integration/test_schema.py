import logging
import uuid
from typing import Annotated

import pytest

from drivine import (
    DrivineError,
    FullTextIndex,
    NodeFragment,
    NodeId,
    PersistenceManager,
    RangeIndex,
    SchemaCatalog,
    SchemaManager,
    SchemaOptions,
    StatelessGraphObjectManager,
    Unique,
    VectorIndex,
    full_text_index,
    range_index,
    uniqueness_constraint,
    vector_index,
)
from drivine.manager.graph_object_manager_factory import GraphObjectManagerFactory
from tests.integration.stores import create


@pytest.fixture
def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    return StatelessGraphObjectManager(pm)


@pytest.fixture
def label() -> str:
    """A label of the test's own, so schema items, which outlive a test's data, don't meet another test's."""
    return f"T{uuid.uuid4().hex[:10]}"


class TestIndexes:
    async def test_range_index_is_made_once_and_dropped(self, gom, label):
        spec = range_index(label, ["a", "b"])
        assert (await gom.indexes.ensure(spec)).status == "CREATED"
        assert (await gom.indexes.ensure(spec)).status == "ALREADY_MATCHING"
        held = [it for it in await gom.indexes.list("RANGE") if it.label == label]
        assert {p for it in held for p in it.properties} == {"a", "b"}
        await gom.indexes.drop(spec)
        assert [it for it in await gom.indexes.list("RANGE") if it.label == label] == []

    async def test_vector_index_drift_and_recreate(self, gom, label):
        spec = vector_index(label, "embedding", 3, "COSINE")
        assert (await gom.indexes.ensure(spec)).status == "CREATED"
        assert (await gom.indexes.ensure(spec)).status == "ALREADY_MATCHING"
        wider = vector_index(label, "embedding", 4, "COSINE")
        drift = await gom.indexes.ensure(wider)
        assert drift.status == "DRIFT" and drift.found is not None and drift.found.dimensions == 3
        assert (await gom.indexes.recreate(wider)).status == "RECREATED"
        assert (await gom.indexes.ensure(wider)).status == "ALREADY_MATCHING"
        await gom.indexes.drop(wider)

    async def test_full_text_index(self, gom, label):
        spec = full_text_index(label, ["title", "body"])
        assert (await gom.indexes.ensure(spec)).status == "CREATED"
        assert (await gom.indexes.ensure(spec)).status == "ALREADY_MATCHING"
        await gom.indexes.drop(spec)
        assert (await gom.indexes.ensure(spec)).status == "CREATED"
        await gom.indexes.drop(spec)


class TestConstraints:
    async def test_made_once_and_dropped(self, gom, label):
        spec = uniqueness_constraint(label, "code")
        assert (await gom.constraints.ensure(spec)).status == "CREATED"
        assert (await gom.constraints.ensure(spec)).status == "ALREADY_MATCHING"
        await create(gom.persistence_manager, label, {"code": "x"})
        with pytest.raises(DrivineError):
            await create(gom.persistence_manager, label, {"code": "x"})
        await gom.constraints.drop(spec)
        assert not [it for it in await gom.constraints.list() if it.label == label]

    async def test_reports_data_that_breaks_it(self, gom, label):
        await create(gom.persistence_manager, label, {"code": "dup"}, {"code": "dup"}, {"code": "u"})
        result = await gom.constraints.ensure(uniqueness_constraint(label, "code"))
        assert result.status == "VIOLATION" and result.sample == ("dup",)
        assert not [it for it in await gom.constraints.list() if it.label == label]


class TestSchemaManager:
    async def test_enforces_what_fragments_declare(self, factory, pm, label):
        fragment = type(
            "Chunk",
            (NodeFragment,),
            {
                "__annotations__": {
                    "uuid": Annotated[str, NodeId(), Unique()],
                    "section_id": Annotated[str, RangeIndex()],
                    "text": Annotated[str, FullTextIndex()],
                    "embedding": Annotated[list[float] | None, VectorIndex()],
                },
                "embedding": None,
                "__module__": __name__,
            },
            labels=[label],
        )
        catalog = SchemaCatalog.from_fragments(fragment, dimensions=lambda _label, _prop: 3).named(label)
        assert {(it.kind, it.properties) for it in catalog.items} == {
            ("UNIQUE", ("uuid",)),
            ("RANGE", ("sectionId",)),
            ("FULLTEXT", ("text",)),
            ("VECTOR", ("embedding",)),
        }
        manager = SchemaManager(
            factory.registry, GraphObjectManagerFactory(factory), SchemaOptions([catalog.for_database(pm.database)])
        )
        first = await manager.enforce()
        assert sorted(it.status for it in first[pm.database]) == ["CREATED"] * 4
        again = await manager.enforce()
        assert [it.status for it in again[pm.database]] == ["ALREADY_MATCHING"] * 4
        gom = StatelessGraphObjectManager(pm)
        for it in catalog.items:
            if it.kind == "UNIQUE":
                await gom.constraints.drop(it)
            else:
                await gom.indexes.drop(it)

    async def test_a_new_version_recreates_and_orphans_are_reported(self, factory, pm, label, caplog):
        first = SchemaCatalog.of(range_index(label, "a"), range_index(label, "b")).named(label).with_version("1")
        managers = GraphObjectManagerFactory(factory)

        def manager(catalog: SchemaCatalog) -> SchemaManager:
            return SchemaManager(factory.registry, managers, SchemaOptions([catalog.for_database(pm.database)]))

        assert [it.status for it in (await manager(first).enforce())[pm.database]] == ["CREATED", "CREATED"]
        second = SchemaCatalog.of(range_index(label, "a")).named(label).with_version("2")
        with caplog.at_level(logging.WARNING):
            results = (await manager(second).enforce())[pm.database]
        assert [it.status for it in results] == ["RECREATED"]
        assert f"applied 1 item(s) it no longer declares: RANGE_INDEX:{label}:b:{label}_b_range" in caplog.text
        gom = StatelessGraphObjectManager(pm)
        for prop in ("a", "b"):
            await gom.indexes.drop(range_index(label, prop))

    async def test_a_violation_fails_fast_or_warns(self, factory, pm, label, caplog):
        await create(pm, label, {"code": "dup"}, {"code": "dup"})
        catalog = SchemaCatalog.of(uniqueness_constraint(label, "code")).named(label).for_database(pm.database)
        managers = GraphObjectManagerFactory(factory)
        with pytest.raises(DrivineError, match="VIOLATION for the UNIQUE"):
            await SchemaManager(factory.registry, managers, SchemaOptions([catalog])).enforce()
        with caplog.at_level(logging.WARNING):
            await SchemaManager(factory.registry, managers, SchemaOptions([catalog], mode="WARN")).enforce()
        assert '"dup"' in caplog.text


async def test_store_identity_is_stable(factory, pm):
    one = await StatelessGraphObjectManager(pm).store_identity()
    two = await StatelessGraphObjectManager(factory.get(pm.database)).store_identity()
    assert one.id == two.id and one.engine == pm.type
