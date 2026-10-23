"""A marker's inventory entries, as drivine4j's SchemaVersionStore writes and reads them: the marker is shared."""

from drivine.backend.oracle.oracle_backend import OracleBackend
from drivine.backend.postgres.postgres_backend import PostgresBackend
from drivine.schema.recorded_inventory import RecordedInventory, inventory_entries, read_inventory


def test_an_item_ensured_at_runtime_is_read_back_as_one_and_marked_when_it_waits_to_be_recreated():
    # drivine4j's SchemaVersionStoreTest, as it writes the marker.
    stored = "RANGE_INDEX:Doc:slug:a\nruntime:VECTOR_INDEX:Doc:embedding:b\nruntime-stale:RANGE_INDEX:Tag:name:c"
    record = read_inventory(PostgresBackend.marker_items(stored))
    assert record == RecordedInventory(
        ("RANGE_INDEX:Doc:slug:a", "VECTOR_INDEX:Doc:embedding:b", "RANGE_INDEX:Tag:name:c"),
        ("VECTOR_INDEX:Doc:embedding:b", "RANGE_INDEX:Tag:name:c"),
        ("RANGE_INDEX:Tag:name:c",),
    )


def test_a_record_marks_the_items_ensured_at_runtime_and_those_to_be_recreated():
    assert inventory_entries(["a", "b", "c"], runtime={"b", "c"}, stale={"c"}) == "a\nruntime:b\nruntime-stale:c"
    assert inventory_entries(["a", "b"]) == "a\nb"
    assert inventory_entries([]) == ""


def test_a_stale_key_is_marked_stale_whether_or_not_it_is_also_given_as_runtime():
    assert inventory_entries(["c"], stale={"c"}) == "runtime-stale:c"


def test_a_marker_with_no_inventory_is_not_one_with_an_empty_inventory():
    assert read_inventory(None) is None
    assert read_inventory(PostgresBackend.marker_items("")) == RecordedInventory(())


def test_entries_written_are_read_back_on_the_engine_that_keeps_them_behind_a_mark():
    written = inventory_entries(["a", "b", "c"], runtime={"b", "c"}, stale={"c"})
    assert read_inventory(OracleBackend.marker_items("items:" + written)) == RecordedInventory(
        ("a", "b", "c"), ("b", "c"), ("c",)
    )
