"""
A vector index a catalog declares over a fragment field that has no `VectorIndex` is refused, whichever of the two
Drivine sees first: drivine4j's VectorAnnotationCheckTest. No database is registered, so `enforce` gets as far as the
check and then has nowhere to apply.

What the check sees lasts for the process, so every fragment here has a label of its own.
"""

from typing import Annotated

import pytest

from drivine import (
    DatabaseRegistry,
    DrivineError,
    GraphProperty,
    NodeFragment,
    NodeId,
    PersistenceManagerFactory,
    SchemaCatalog,
    SchemaManager,
    SchemaOptions,
    VectorIndex,
    vector_index,
)
from drivine.manager.graph_object_manager_factory import GraphObjectManagerFactory
from drivine.model.model_registry import ModelRegistry
from drivine.schema.schema_specs import SchemaSpec
from drivine.sync import PersistenceManagerFactory as SyncPersistenceManagerFactory
from drivine.sync import SchemaManager as SyncSchemaManager
from drivine.sync.manager.graph_object_manager_factory import (
    GraphObjectManagerFactory as SyncGraphObjectManagerFactory,
)


class CheckMessageLater(NodeFragment, labels=["CheckMessageLater"]):
    id: Annotated[str, NodeId()]
    embedding: list[float] | None = None


class CheckMessageFirst(NodeFragment, labels=["CheckMessageFirst"]):
    id: Annotated[str, NodeId()]
    embedding: list[float] | None = None


class CheckSync(NodeFragment, labels=["CheckSync"]):
    id: Annotated[str, NodeId()]
    embedding: list[float] | None = None


class CheckRenamed(NodeFragment, labels=["CheckRenamed"]):
    id: Annotated[str, NodeId()]
    vector: Annotated[list[float] | None, GraphProperty("embedding_vec")] = None


class CheckRenamedAway(NodeFragment, labels=["CheckRenamedAway"]):
    id: Annotated[str, NodeId()]
    embedding: Annotated[list[float] | None, GraphProperty("other")] = None


class CheckChunk(NodeFragment, labels=["CheckChunk", "CheckContent"]):
    id: Annotated[str, NodeId()]
    embedding: list[float] | None = None


class CheckBase(NodeFragment, labels=["CheckBase"]):
    id: Annotated[str, NodeId()]
    embedding: list[float] | None = None


class CheckDerived(CheckBase, labels=["CheckDerived"]):
    pass


class CheckTwoFields(NodeFragment, labels=["CheckTwoFields"]):
    id: Annotated[str, NodeId()]
    embedding: list[float] | None = None
    summary: list[float] | None = None


class CheckAnnotated(NodeFragment, labels=["CheckAnnotated"]):
    id: Annotated[str, NodeId()]
    embedding: Annotated[list[float] | None, VectorIndex()] = None


class CheckAnnotatedLater(NodeFragment, labels=["CheckAnnotatedLater"]):
    id: Annotated[str, NodeId()]
    embedding: Annotated[list[float] | None, VectorIndex()] = None


async def enforce(*catalogs: SchemaCatalog) -> None:
    registry = DatabaseRegistry()
    factory = GraphObjectManagerFactory(PersistenceManagerFactory(registry))
    await SchemaManager(registry, factory, SchemaOptions(list(catalogs))).enforce()


async def enforce_specs(*specs: SchemaSpec) -> None:
    await enforce(SchemaCatalog.of(*specs))


def build(type_: type) -> None:
    ModelRegistry().fragment(type_)


def message(field: str, spec: str) -> str:
    return (
        f"`{field}` is covered by the vector index on `{spec}` but has no `VectorIndex`. Add it: without it the field "
        "is not written as a vector on FalkorDB, and `load_nearest` cannot find its index."
    )


async def test_applying_a_catalog_refuses_a_spec_over_a_known_fragments_unmarked_field():
    build(CheckMessageLater)

    with pytest.raises(DrivineError) as refused:
        await enforce_specs(vector_index("CheckMessageLater", "embedding", 4))

    assert str(refused.value) == message("CheckMessageLater.embedding", "CheckMessageLater.embedding")


def test_the_sync_manager_refuses_it_too():
    build(CheckSync)
    registry = DatabaseRegistry()
    manager = SyncSchemaManager(
        registry,
        SyncGraphObjectManagerFactory(SyncPersistenceManagerFactory(registry)),
        SchemaOptions([SchemaCatalog.of(vector_index("CheckSync", "embedding", 4))]),
    )

    with pytest.raises(DrivineError, match="`CheckSync.embedding` is covered"):
        manager.enforce()


async def test_building_a_fragment_refuses_an_unmarked_field_a_catalog_already_indexed():
    await enforce_specs(vector_index("CheckMessageFirst", "embedding", 4))

    with pytest.raises(DrivineError) as refused:
        build(CheckMessageFirst)
    assert str(refused.value) == message("CheckMessageFirst.embedding", "CheckMessageFirst.embedding")
    # Refused every time, not only the first, by any registry.
    registry = ModelRegistry()
    for _ in range(2):
        with pytest.raises(DrivineError):
            registry.fragment(CheckMessageFirst)


async def test_a_field_is_matched_by_the_property_it_is_stored_under():
    build(CheckRenamed)
    build(CheckRenamedAway)

    with pytest.raises(DrivineError) as refused:
        await enforce_specs(vector_index("CheckRenamed", "embedding_vec", 4))
    assert str(refused.value) == message("CheckRenamed.vector", "CheckRenamed.embedding_vec")
    # Stored under "other", so a spec on "embedding" covers nothing of it.
    await enforce_specs(vector_index("CheckRenamedAway", "embedding", 4))


async def test_a_spec_on_any_of_a_fragments_labels_covers_its_field():
    build(CheckChunk)

    with pytest.raises(DrivineError) as refused:
        await enforce_specs(vector_index("CheckContent", "embedding", 4))

    assert str(refused.value) == message("CheckChunk.embedding", "CheckContent.embedding")


async def test_a_spec_on_an_inherited_label_covers_the_subclass_field():
    build(CheckDerived)

    with pytest.raises(DrivineError) as refused:
        await enforce_specs(vector_index("CheckBase", "embedding", 4))

    assert str(refused.value) == message("CheckDerived.embedding", "CheckBase.embedding")


async def test_a_spec_for_a_label_no_fragment_maps_is_allowed():
    await enforce_specs(vector_index("CheckCypherOnly", "embedding", 4))


async def test_the_marker_beside_a_spec_is_allowed_whichever_comes_first():
    build(CheckAnnotated)
    # The catalog's declaration wins where the two differ, so a different similarity is no conflict.
    await enforce_specs(vector_index("CheckAnnotated", "embedding", 4, "EUCLIDEAN"))

    await enforce_specs(vector_index("CheckAnnotatedLater", "embedding", 4))
    build(CheckAnnotatedLater)


async def test_every_uncovered_field_is_reported_once_however_many_specs_cover_it():
    build(CheckTwoFields)

    with pytest.raises(DrivineError) as refused:
        # Two owners may declare the same index; the field is still reported once.
        await enforce(
            SchemaCatalog.of(vector_index("CheckTwoFields", "embedding", 4)).named("check-a"),
            SchemaCatalog.of(
                vector_index("CheckTwoFields", "embedding", 4), vector_index("CheckTwoFields", "summary", 4)
            ).named("check-b"),
        )

    assert str(refused.value) == (
        message("CheckTwoFields.embedding", "CheckTwoFields.embedding")
        + "\n"
        + message("CheckTwoFields.summary", "CheckTwoFields.summary")
    )
