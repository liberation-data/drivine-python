# Written by scripts/unasync.py from tests/integration/test_node_id_index.py:
# change that, and run the script again.
"""
A fragment's NodeId's default range index, made by an enforce on each store, and the warning of one no index serves,
read from what the store lists (#552, drivine4j#827 and #828): Drivine for TypeScript's NodeIdIndex.spec.
"""

import logging
from collections.abc import Iterator
from typing import Annotated

import pytest

from drivine.model.model_registry import ModelRegistry
from drivine.schema import node_id_index_check
from drivine.sync import (
    DatabaseType,
    NodeFragment,
    NodeId,
    PersistenceManager,
    PersistenceManagerFactory,
    RangeIndex,
    SchemaCatalog,
    SchemaManager,
    SchemaOptions,
    StatelessGraphObjectManager,
    range_index,
)
from drivine.sync.manager.graph_object_manager_factory import GraphObjectManagerFactory


class IdxNote(NodeFragment, labels=["IdxNote"]):
    id: Annotated[str, NodeId()]
    # Beside the id's, so that FalkorDB indexes the label's range properties together.
    name: Annotated[str, RangeIndex()]


class IdxBare(NodeFragment, labels=["IdxBare"], node_id_index=False):
    id: Annotated[str, NodeId()]


class IdxBareLater(NodeFragment, labels=["IdxBare"]):
    id: Annotated[str, NodeId()]


MADE = [range_index("IdxNote", "id"), range_index("IdxNote", "name"), range_index("IdxBare", "id")]


@pytest.fixture
def gom(pm: PersistenceManager) -> Iterator[StatelessGraphObjectManager]:
    gom = StatelessGraphObjectManager(pm)
    for it in MADE:
        gom.indexes.drop(it)
    node_id_index_check.reset()
    yield gom
    node_id_index_check.reset()
    for it in MADE:
        gom.indexes.drop(it)


def node_id_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [it.getMessage() for it in caplog.records if it.getMessage().startswith("@NodeId")]


def test_the_index_is_made_on_an_enforce_and_the_fragment_that_opted_out_is_warned_of_once_until_one_serves_it(
    gom: StatelessGraphObjectManager,
    pm: PersistenceManager,
    factory: PersistenceManagerFactory,
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.WARNING, logger="drivine.schema")
    # Both used before the enforce, as a fragment may be: each is checked when the enforce lists the store.
    ModelRegistry().fragment(IdxNote)
    ModelRegistry().fragment(IdxBare)
    manager = SchemaManager(
        factory.registry,
        GraphObjectManagerFactory(factory),
        SchemaOptions(
            [SchemaCatalog.from_fragments(IdxNote, IdxBare).named("node-id-index").for_database(pm.database)]
        ),
    )
    report = (manager.enforce())[pm.database]
    assert [(it.spec.label, it.spec.properties, it.status) for it in report] == [
        ("IdxNote", ("name",), "CREATED"),
        ("IdxNote", ("id",), "CREATED"),
    ]
    listed = [it for it in gom.indexes.list("RANGE") if it.label == "IdxNote"]
    assert sorted(p for it in listed for p in it.properties) == ["id", "name"]
    if pm.type is DatabaseType.FALKORDB:
        # Listed one item a property: the id is served all the same.
        assert len(listed) == 2
    [warning] = node_id_warnings(caplog)
    assert warning.startswith("@NodeId 'id' of IdxBare has no index under :IdxBare, ")

    # Again: nothing new to warn of.
    manager.enforce()
    assert len(node_id_warnings(caplog)) == 1

    # An index ensured later serves a fragment of the label first used after it.
    gom.indexes.ensure(range_index("IdxBare", "id"))
    ModelRegistry().fragment(IdxBareLater)
    assert len(node_id_warnings(caplog)) == 1
