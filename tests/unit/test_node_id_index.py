"""
A fragment's NodeId is given a range index by default (#552, drivine4j#827): each save and each load by id finds its
node by it, and without one a store reads every node of the label. drivine4j's SchemaCatalogTest cases of "A
fragment's @NodeId's range index", ported, and the schema manager's leaving out a default another catalog serves, on
the async and the sync API. No database is spoken to: the request is read as the planner is given it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

import pytest

import drivine.schema.schema_manager as async_schema_manager
import drivine.sync.schema.schema_manager as sync_schema_manager
from drivine import (
    DatabaseRegistry,
    GraphProperty,
    NodeFragment,
    NodeId,
    PersistenceManagerFactory,
    RangeIndex,
    SchemaCatalog,
    SchemaOptions,
    Unique,
    range_index,
    uniqueness_constraint,
)
from drivine import SchemaManager as AsyncSchemaManager
from drivine.manager.graph_object_manager_factory import GraphObjectManagerFactory
from drivine.schema.range_index_spec import RangeIndexSpec
from drivine.sync import DatabaseRegistry as SyncDatabaseRegistry
from drivine.sync import PersistenceManagerFactory as SyncPersistenceManagerFactory
from drivine.sync import SchemaManager as SyncSchemaManager
from drivine.sync.manager.graph_object_manager_factory import (
    GraphObjectManagerFactory as SyncGraphObjectManagerFactory,
)


class Message(NodeFragment, labels=["Message", "Post"]):
    id: Annotated[str, NodeId()]
    session_id: str | None = None


class OtherMessage(NodeFragment, labels=["Message"]):
    id: Annotated[str, NodeId()]


class Ticket(NodeFragment, labels=["Ticket"]):
    """A NodeId stored under another name."""

    id: Annotated[str, NodeId(), GraphProperty("ticket_id")]


class Badge(NodeFragment, labels=["Badge"]):
    """No NodeId."""

    name: str


class Unindexed(NodeFragment, labels=["Unindexed"], node_id_index=False):
    id: Annotated[str, NodeId()]


class UnindexedToo(Unindexed, labels=["UnindexedToo"]):
    """Its base's opt-out, inherited."""


class UniqueId(NodeFragment, labels=["UniqueId"]):
    id: Annotated[str, NodeId(), Unique()]


class RangedId(NodeFragment, labels=["RangedId"]):
    id: Annotated[str, NodeId(), RangeIndex()]


MESSAGE_ID = range_index("Message", "id")


def test_a_fragments_node_id_is_given_a_range_index_under_its_primary_label_over_the_property_it_is_stored_under():
    message = SchemaCatalog.from_fragments(Message)
    assert message.node_id_indexes == (MESSAGE_ID,)
    assert MESSAGE_ID in message.items
    assert SchemaCatalog.from_fragments(Ticket).node_id_indexes == (range_index("Ticket", "ticket_id"),)


def test_a_fragment_without_a_node_id_or_one_that_opts_out_is_given_no_range_index_for_it():
    assert SchemaCatalog.from_fragments(Badge).node_id_indexes == ()
    for type_ in (Unindexed, UnindexedToo):
        unindexed = SchemaCatalog.from_fragments(type_)
        assert unindexed.node_id_indexes == () and unindexed.items == ()


def test_an_item_declared_over_the_node_id_alone_serves_it_so_none_is_added_beside_it():
    for declared in (RangeIndexSpec("Message", ("id",), name="message_id"), uniqueness_constraint("Message", "id")):
        merged = SchemaCatalog.from_fragments(Message) + [declared]
        assert merged.node_id_indexes == (), declared
        assert [it for it in merged.items if it.label == "Message" and it.properties == ("id",)] == [declared]
    # Declared on the fragment itself.
    assert SchemaCatalog.from_fragments(UniqueId).node_id_indexes == ()
    ranged = SchemaCatalog.from_fragments(RangedId)
    assert ranged.node_id_indexes == () and ranged.items == (range_index("RangedId", "id"),)


def test_an_item_over_more_properties_one_that_ignores_case_or_under_another_label_does_not_serve_it():
    for declared in (
        range_index("Message", ["id", "sessionId"]),
        range_index("Message", "id", ignore_case=True),
        range_index("Other", "id"),
    ):
        merged = SchemaCatalog.from_fragments(Message) + [declared]
        assert merged.node_id_indexes == (MESSAGE_ID,), declared


def test_two_fragments_of_one_label_give_it_one_and_without_node_id_indexes_leaves_only_what_is_declared():
    both = SchemaCatalog.from_fragments(Message, OtherMessage)
    assert both.items.count(MESSAGE_ID) == 1
    declared = both.without_node_id_indexes()
    assert declared.node_id_indexes == () and MESSAGE_ID not in declared.items
    assert len(declared.items) == len(both.items) - 1
    # The defaults survive a copy, and an addition.
    assert both.for_database("x").with_version("1").named("app").node_id_indexes == both.node_id_indexes
    assert (both + [range_index("Topic", "name")]).node_id_indexes == both.node_id_indexes


# ----- Applied beside the other catalogs on a database -----

Api = str
APIS: list[Api] = ["async", "sync"]


@pytest.fixture
def requests(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Each request the manager gives the planner, which isn't run: its report holds no owners."""
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


async def enforced(
    api: Api, options: SchemaOptions, tmp_path: Path, seen: list[dict[str, Any]], *ensured: tuple[str, Any]
) -> list[list[dict[str, Any]]]:
    """The items of each catalog an enforce gives the planner, on SQLite, after the items `ensured` at runtime."""
    env = {"SQLITE_DATABASE_TYPE": "SQLITE", "SQLITE_DATABASE_NAME": str(tmp_path / "graph.db")}
    if api == "async":
        registry = DatabaseRegistry()
        registry.build_or_resolve_from_env("sqlite", env)
        manager = AsyncSchemaManager(registry, GraphObjectManagerFactory(PersistenceManagerFactory(registry)), options)
        try:
            for owner, spec in ensured:
                await manager.ensure(owner, spec)
            await manager.enforce()
        finally:
            await registry.close()
    else:
        sync_registry = SyncDatabaseRegistry()
        sync_registry.build_or_resolve_from_env("sqlite", env)
        sync_manager = SyncSchemaManager(
            sync_registry, SyncGraphObjectManagerFactory(SyncPersistenceManagerFactory(sync_registry)), options
        )
        try:
            for owner, spec in ensured:
                sync_manager.ensure(owner, spec)
            sync_manager.enforce()
        finally:
            sync_registry.close()
    [request] = [it for it in seen if it["operation"]["type"] == "enforce"]
    return [it["items"] for it in request["catalogs"]]


def item(spec: RangeIndexSpec) -> dict[str, Any]:
    return {"kind": "RANGE_INDEX", "label": spec.label, "properties": list(spec.properties), "name": spec.name,
            "ignoreCase": spec.ignore_case}  # fmt: skip


@pytest.mark.parametrize("api", APIS)
async def test_the_planner_is_given_a_node_ids_range_index_where_nothing_else_serves_it(
    api: Api, tmp_path: Path, requests: list[dict[str, Any]]
):
    options = SchemaOptions([SchemaCatalog.from_fragments(Message)])
    assert await enforced(api, options, tmp_path, requests) == [[item(MESSAGE_ID)]]


@pytest.mark.parametrize("api", APIS)
async def test_a_default_another_catalog_or_an_item_ensured_at_runtime_serves_is_left_to_it(
    api: Api, tmp_path: Path, requests: list[dict[str, Any]]
):
    named = RangeIndexSpec("Message", ("id",), name="message_id")
    options = SchemaOptions([SchemaCatalog.from_fragments(Message), SchemaCatalog.of(named).named("app")])
    assert await enforced(api, options, tmp_path, requests) == [[], [item(named)]]
    requests.clear()
    unique = uniqueness_constraint("Message", "id")
    options = SchemaOptions([SchemaCatalog.from_fragments(Message)])
    assert await enforced(api, options, tmp_path / "runtime", requests, ("dice", unique)) == [[]]


@pytest.mark.parametrize("api", APIS)
async def test_a_default_declared_the_same_elsewhere_is_kept_beside_it_co_owned(
    api: Api, tmp_path: Path, requests: list[dict[str, Any]]
):
    # The same item, unnamed: kept by both owners, so that neither's marker calls the index they share an orphan.
    options = SchemaOptions([SchemaCatalog.from_fragments(Message), SchemaCatalog.of(MESSAGE_ID).named("app")])
    assert await enforced(api, options, tmp_path, requests) == [[item(MESSAGE_ID)], [item(MESSAGE_ID)]]


@pytest.mark.parametrize("api", APIS)
async def test_index_node_ids_false_gives_the_planner_only_what_is_declared(
    api: Api, tmp_path: Path, requests: list[dict[str, Any]]
):
    options = SchemaOptions(
        [SchemaCatalog.from_fragments(Message) + [range_index("Topic", "name")]], index_node_ids=False
    )
    assert await enforced(api, options, tmp_path, requests) == [[item(range_index("Topic", "name"))]]
