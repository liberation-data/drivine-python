"""
The statements that fill in the lowercased copies of a property (see lower_copies) and record that every node of a
label has its copy, in the store's own language. drivine4j's `LowerCopyStatements`.

The graph engines keep the record on the store's `DrivineStore` node, as a list of entries under `lowered`, each the
JSON array `["<label>","<property>"]` with nothing between its parts and only a backslash and a quote escaped. Oracle
and Postgres keep it as a row of the graph's catalog, of kind `LOWER_COPY`, with the property as the one item of
`properties`, no `name` or `object_name`, and `details` `{}`. Both are drivine4j's, so a record one library wrote is
read by the others.
"""

# Rows a store gives are untyped: they are read here, field by field.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from drivine.backend.cypher import label_expression, quote_name
from drivine.backend.cypher_stamps import LOCK
from drivine.backend.sql.sql_catalog import catalog_properties
from drivine.model import lower_copies

LOWER_COPY_BATCH = 10_000
"""How many nodes one statement fills in: drivine4j's `LowerCopyCatalog.BATCH`."""

LOWER_COPY_KIND = "LOWER_COPY"
"""What a catalog row that records a copy filled in is of, on Oracle and Postgres: drivine4j's."""

LOWERED_LIST = "lowered"
"""The property of the store's `DrivineStore` node that holds the records, on the graph engines: drivine4j's."""


@dataclass(frozen=True)
class Recording:
    """A statement that records a copy filled in, and what it binds."""

    statement: str
    bindings: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class LowerCopyStatements:
    """A store's statements for the lowercased copies: see the module."""

    read: str
    """Reads every record, binding nothing: each row is one, as `entries` reads it."""
    fill: Callable[[str, str, int], str]
    """
    `fill(label, property, batch)`: writes the copy of `property` on at most `batch` nodes of `label` whose copy is not
    the property lowercased, and removes it from those whose property has no value. Returns one row: the number of
    nodes it wrote. Run until it writes fewer than `batch`.
    """
    record: Callable[[str, str], Recording]
    """`record(label, property)`: records that every node of `label` has the copy of `property`, once."""
    entries: Callable[[list[Any]], list[tuple[str, str]]]
    """The label and property of each record among the rows `read` gives."""
    on_store_node: bool = False
    """Whether the record is kept on the store's `DrivineStore` node, which is made first where it isn't there."""


def lowered_entry(label: str, property: str) -> str:
    """The record of `label` and `property` on the graph engines: the JSON array of the two, as a SQL catalog writes."""
    return catalog_properties([label, property])


def column_of(row: Any) -> Any:
    """The value of a row of one column, as a store may give it: the value, or a map of one key."""
    if isinstance(row, Mapping) and len(row) == 1:
        return next(iter(row.values()))
    return row


def _cypher_fill(label: str, property: str, batch: int) -> str:
    copy = f"n.{quote_name(lower_copies.of(property))}"
    value = f"n.{quote_name(property)}"
    return "\n".join(
        [
            f"MATCH (n{label_expression([label])})",
            f"WHERE NOT coalesce({copy} = toLower({value}), {copy} IS NULL AND {value} IS NULL)",
            f"WITH n LIMIT {batch}",
            f"SET {lower_copies.set_clause('n', property)}",
            "RETURN count(n)",
        ]
    )


def _cypher_record(label: str, property: str) -> Recording:
    """The node's lock is taken before its list is read, so two processes recording at once both keep theirs."""
    return Recording(
        "\n".join(
            [
                "MATCH (s:DrivineStore {key: 'singleton'})",
                f"SET s.{LOCK} = true REMOVE s.{LOCK}",
                f"SET s.{LOWERED_LIST} = [e IN coalesce(s.{LOWERED_LIST}, []) WHERE e <> $entry] + $entry",
            ]
        ),
        {"entry": lowered_entry(label, property)},
    )


def _cypher_entries(rows: list[Any]) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for row in rows:
        try:
            parsed = json.loads(str(column_of(row)))
        except ValueError:
            continue
        if isinstance(parsed, list) and len(parsed) == 2 and all(isinstance(it, str) for it in parsed):
            entries.append((parsed[0], parsed[1]))
    return entries


CYPHER_LOWER_COPIES = LowerCopyStatements(
    read=f"MATCH (s:DrivineStore) UNWIND coalesce(s.{LOWERED_LIST}, []) AS e RETURN DISTINCT e",
    fill=_cypher_fill,
    record=_cypher_record,
    entries=_cypher_entries,
    on_store_node=True,
)
"""The copies' statements in Cypher, for the graph engines, as drivine4j's `CypherLowerCopies` writes them."""


def catalog_entries(rows: list[Any]) -> list[tuple[str, str]]:
    """The label and property of each SQL catalog row among `rows`, each its `label` and its `properties`, as text."""
    entries: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        try:
            properties = json.loads(str(row.get("properties")))
        except ValueError:
            continue
        label = row.get("label")
        if isinstance(properties, list) and len(properties) == 1 and isinstance(label, str):
            entries.append((label, str(properties[0])))
    return entries
