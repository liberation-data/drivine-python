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
from unittest.mock import AsyncMock, MagicMock

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
from drivine.schema.schema_manager import inventory_key
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


class Holder(NodeFragment, labels=["Holder"]):
    id: Annotated[str, NodeId()]
    code: Annotated[str, Unique()]


class HeldByCode(NodeFragment, labels=["Holder"]):
    code: Annotated[str, NodeId()]


def test_a_catalog_built_with_its_defaults_gives_the_declared_indexes_the_defaults_then_the_constraints():
    # As drivine4j's SchemaCatalog.build: one fragment's item serves another's default, whichever comes first.
    code, holder_id = uniqueness_constraint("Holder", "code", node_id_property="id"), range_index("Holder", "id")
    for types in ((Holder, HeldByCode), (HeldByCode, Holder)):
        catalog = SchemaCatalog.from_fragments(*types)
        assert catalog.items == (holder_id, code) and catalog.node_id_indexes == (holder_id,), types
    # Added to, the catalog is built again: an item added once however often given, the defaults it doesn't serve kept
    # between the indexes and the constraints.
    topic, unique = range_index("Topic", "name"), uniqueness_constraint("Topic", "name")
    added = SchemaCatalog.from_fragments(Message) + [unique, topic, topic]
    assert added.items == (topic, MESSAGE_ID, unique) and added.node_id_indexes == (MESSAGE_ID,)
    served = added + [uniqueness_constraint("Message", "id"), uniqueness_constraint("Message", "id")]
    assert served.items == (topic, unique, uniqueness_constraint("Message", "id")) and served.node_id_indexes == ()


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


def test_a_default_declared_the_same_by_an_addition_is_the_declared_item_kept_without_the_defaults():
    merged = SchemaCatalog.from_fragments(Message) + [MESSAGE_ID]
    assert merged.node_id_indexes == () and merged.items.count(MESSAGE_ID) == 1
    assert merged.without_node_id_indexes().items == (MESSAGE_ID,)


# ----- On a graph engine, whose schema the manager plans itself -----


def graph_engine(api: Api) -> tuple[Any, MagicMock]:
    """A factory whose one manager stands in for a graph engine's: no schema planning, no markers, nothing held."""
    call = MagicMock if api == "sync" else AsyncMock
    gom = MagicMock()
    gom.schema_planning.return_value = None
    gom.backend.marker_items.return_value = None
    gom.persistence_manager.maybe_get_one = call(return_value=None)
    gom.persistence_manager.execute = call(return_value=None)
    gom.persistence_manager.query = call(return_value=[])
    for of in (gom.indexes, gom.constraints):
        for name in ("ensure", "recreate", "drop"):
            setattr(of, name, call(return_value=MagicMock(status="CREATED")))
    factory = MagicMock()
    factory.get.return_value = gom
    return factory, gom


async def on_graph_engine(api: Api, options: SchemaOptions, tmp_path: Path, run: Any) -> MagicMock:
    """`run(manager)`, each step awaited on the async API, against the stand-in graph engine; gives its manager."""
    factory, gom = graph_engine(api)
    env = {"SQLITE_DATABASE_TYPE": "SQLITE", "SQLITE_DATABASE_NAME": str(tmp_path / "graph.db")}
    if api == "async":
        registry = DatabaseRegistry()
        registry.build_or_resolve_from_env("sqlite", env)
        try:
            for step in run(AsyncSchemaManager(registry, factory, options)):
                await step
        finally:
            await registry.close()
    else:
        sync_registry = SyncDatabaseRegistry()
        sync_registry.build_or_resolve_from_env("sqlite", env)
        try:
            for _ in run(SyncSchemaManager(sync_registry, factory, options)):
                pass
        finally:
            sync_registry.close()
    return gom


def made(gom: MagicMock) -> list[Any]:
    return [it.args[0] for it in gom.indexes.ensure.call_args_list]


def recorded(gom: MagicMock) -> dict[str, list[str]]:
    """Each owner's inventory, as its marker was last written."""
    marks = [it.args[0] for it in gom.backend.record_schema_marker.return_value.spec.call_args_list]
    return {it["scope"]: it["items"].splitlines() for it in marks}


@pytest.mark.parametrize("api", APIS)
async def test_on_a_graph_engine_a_default_another_catalog_serves_is_left_to_it_and_one_declared_the_same_co_owned(
    api: Api, tmp_path: Path
):
    named = RangeIndexSpec("Message", ("id",), name="message_id")
    options = SchemaOptions([SchemaCatalog.from_fragments(Message), SchemaCatalog.of(named).named("app")])
    gom = await on_graph_engine(api, options, tmp_path, lambda manager: [manager.enforce()])
    assert made(gom) == [named]
    assert recorded(gom) == {"schema": [], "app": [inventory_key(named)]}
    options = SchemaOptions([SchemaCatalog.from_fragments(Message), SchemaCatalog.of(MESSAGE_ID).named("app")])
    gom = await on_graph_engine(api, options, tmp_path, lambda manager: [manager.enforce()])
    assert made(gom) == [MESSAGE_ID, MESSAGE_ID]
    assert recorded(gom) == {"schema": [inventory_key(MESSAGE_ID)], "app": [inventory_key(MESSAGE_ID)]}


@pytest.mark.parametrize("api", APIS)
async def test_on_a_graph_engine_an_item_ensured_at_runtime_that_serves_a_default_takes_its_place(
    api: Api, tmp_path: Path
):
    # Beside the default, the named index would be a conflicting declaration of the same item.
    named = RangeIndexSpec("Message", ("id",), name="message_id")
    options = SchemaOptions([SchemaCatalog.from_fragments(Message)])
    gom = await on_graph_engine(
        api, options, tmp_path, lambda manager: [manager.ensure("dice", named), manager.enforce()]
    )
    assert made(gom) == [named, named]
    assert recorded(gom)["schema"] == []


@pytest.mark.parametrize("api", APIS)
async def test_on_a_graph_engine_an_item_removed_is_dropped_where_only_a_default_another_item_serves_matches_it(
    api: Api, tmp_path: Path
):
    # The default is left to the constraint, so it declares nothing the removed index is kept for.
    named = RangeIndexSpec("Message", ("id",), name="message_id")
    options = SchemaOptions(
        [SchemaCatalog.from_fragments(Message), SchemaCatalog.of(uniqueness_constraint("Message", "id")).named("app")]
    )
    gom = await on_graph_engine(
        api, options, tmp_path, lambda manager: [manager.ensure("dice", named), manager.remove("dice", named)]
    )
    assert [it.args[0] for it in gom.indexes.drop.call_args_list] == [named]
    # Without the constraint the default is applied, and the index of its identity is kept for it.
    options = SchemaOptions([SchemaCatalog.from_fragments(Message)])
    gom = await on_graph_engine(
        api, options, tmp_path, lambda manager: [manager.ensure("dice", named), manager.remove("dice", named)]
    )
    assert gom.indexes.drop.call_args_list == []


@pytest.mark.parametrize("api", APIS)
async def test_on_a_graph_engine_a_default_served_on_one_database_only_is_left_out_there_and_kept_on_the_other(
    api: Api, tmp_path: Path
):
    (factory, one), (_, two) = graph_engine(api), graph_engine(api)
    factory.get.side_effect = lambda database: one if database == "one" else two
    options = SchemaOptions(
        [
            SchemaCatalog.from_fragments(Message).for_databases("one", "two"),
            SchemaCatalog.of(uniqueness_constraint("Message", "id")).named("app").for_database("one"),
        ]
    )
    env = {
        "ONE_DATABASE_TYPE": "SQLITE",
        "ONE_DATABASE_NAME": str(tmp_path / "one.db"),
        "TWO_DATABASE_TYPE": "SQLITE",
        "TWO_DATABASE_NAME": str(tmp_path / "two.db"),
    }
    if api == "async":
        registry = DatabaseRegistry()
        for name in ("one", "two"):
            registry.build_or_resolve_from_env(name, env)
        try:
            await AsyncSchemaManager(registry, factory, options).recreate_all()
        finally:
            await registry.close()
    else:
        sync_registry = SyncDatabaseRegistry()
        for name in ("one", "two"):
            sync_registry.build_or_resolve_from_env(name, env)
        try:
            SyncSchemaManager(sync_registry, factory, options).recreate_all()
        finally:
            sync_registry.close()
    assert [it.args[0] for it in one.indexes.recreate.call_args_list] == []
    assert recorded(one)["schema"] == []
    assert [it.args[0] for it in two.indexes.recreate.call_args_list] == [MESSAGE_ID]
    assert recorded(two) == {"schema": [inventory_key(MESSAGE_ID)]}


@pytest.mark.parametrize("api", APIS)
async def test_on_a_graph_engine_index_node_ids_false_leaves_the_defaults_out_of_every_path(
    api: Api, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    named = RangeIndexSpec("Message", ("id",), name="message_id")
    options = SchemaOptions([SchemaCatalog.from_fragments(Message)], index_node_ids=False)
    with caplog.at_level("INFO"):
        gom = await on_graph_engine(
            api,
            options,
            tmp_path,
            lambda manager: [manager.enforce(), manager.ensure("dice", named), manager.remove("dice", named)],
        )
    assert "all empty: unnamed; nothing to apply" in caplog.text
    # Only the item ensured is made, and with no default to keep it for, it is dropped when removed.
    assert made(gom) == [named]
    assert [it.args[0] for it in gom.indexes.drop.call_args_list] == [named]
