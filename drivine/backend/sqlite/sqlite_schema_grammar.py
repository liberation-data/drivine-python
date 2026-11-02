# Rows the catalog gives are untyped JSON documents: they are read here, field by field.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from drivine.backend.sql.sql_catalog import CATALOG_KINDS, CatalogKind, catalog_properties, catalog_record
from drivine.backend.sql.sql_graph import sql_text
from drivine.backend.sqlite.sqlite_graph import JSON_ROWS, LOWER_COPIES, SqliteGraph, has_label, text_of
from drivine.schema.full_text_index_spec import FullTextIndexSpec
from drivine.schema.lower_copy_statements import LOWER_COPY_KIND, LowerCopyStatements, Recording, catalog_entries
from drivine.schema.range_index_spec import RangeIndexSpec
from drivine.schema.schema_grammar import SchemaGrammar, portable_pins, sampled_node_id
from drivine.schema.schema_item import SchemaItem
from drivine.schema.schema_listing import SchemaListing
from drivine.schema.schema_specs import item_name
from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec
from drivine.schema.vector_index_spec import VectorIndexSpec

_Spec = RangeIndexSpec | UniquenessConstraintSpec | VectorIndexSpec | FullTextIndexSpec

RANGE = "ix"
UNIQUE = "uq"
"""What the name of a range index, and of a uniqueness constraint's index, says it is: drivine4j's."""


def sqlite_schema_grammar(graph: SqliteGraph) -> SchemaGrammar:
    """
    Indexes and constraints over a graph kept by SQLite, as drivine4j makes them, so a file's schema is one whichever
    library made it. A property is a key of the JSON text `props`, and SQLite indexes an expression over the part of a
    table a predicate selects, so each is a partial index over the property, for the nodes of one label:

    - a range index is `((props read as text)) WHERE instr(labels, '"L"') > 0`. A statement tests each label of a node
      on its own and reads a property as that same expression, so the planner sees that the index holds the nodes the
      statement asks for: a load, a filter, and the write function finding a node by its id;
    - a uniqueness constraint is that index made `UNIQUE`. A node without the property reads it as null, which a unique
      index does not hold, so it is not constrained, as on the graph engines.

    A vector index and a full-text index are recorded, so that a schema that declares one is applied, and nothing is
    made for them yet: a search measures every node it may find (see SqliteSearch).

    What Drivine made is recorded in the graph's catalog, as on Oracle and Postgres, and read back from there: an
    index's expression does not say which label and properties it was made for. An index a record names that is no
    longer there is not listed, so it is made again when asked for. The provider's first connection makes the store
    (see GraphTables), so the catalog is there before a statement of the schema runs.
    """
    grammar = _SqliteSchema(graph)
    return SchemaGrammar(
        engine="SQLite",
        list_ranges=grammar.listing("RANGE"),
        list_uniques=grammar.listing("UNIQUE"),
        list_vectors=grammar.listing("VECTOR"),
        list_full_texts=grammar.listing("FULLTEXT"),
        create_range=grammar.create_range,
        drop_range=lambda spec: grammar.forgotten(spec),
        create_unique=lambda spec: [*grammar.create_unique(spec)],
        drop_unique=lambda spec: [*grammar.forgotten(spec)],
        create_vector=grammar.create_vector,
        drop_vector=lambda spec: grammar.forgotten(spec),
        create_full_text=grammar.create_full_text,
        drop_full_text=lambda spec, name: grammar.forgotten(spec),
        # Nothing is made for a vector index: it takes no HNSW parameters.
        unsupported_vector_tuning=portable_pins,
        conflicts=grammar.conflicts,
        lower_copies=_lower_copies(graph),
    )


def _lower_copies(graph: SqliteGraph) -> LowerCopyStatements:
    """
    The lowercased copies on SQLite, as drivine4j's `SqliteLowerCopies` writes them: a property of the node's JSON,
    filled in by the function Drivine registers, which lowercases it as a lookup lowercases what it compares the copy
    with, and recorded as a row of the graph's catalog, of kind `LOWER_COPY`, as on Oracle and Postgres.
    """

    def fill(label: str, property: str, batch: int) -> str:
        return (
            f"SELECT {LOWER_COPIES}({sql_text(graph.name)}, {sql_text(label)}, {sql_text(property)}, "
            f'CAST({batch} AS INTEGER)) AS "written"'
        )

    def record(label: str, property: str) -> Recording:
        return Recording(
            f"INSERT INTO {graph.catalog} (kind, label, properties, name, object_name, details) "
            f"VALUES ({sql_text(LOWER_COPY_KIND)}, {sql_text(label)}, {sql_text(catalog_properties([property]))}, "
            "NULL, NULL, '{}') ON CONFLICT (kind, label, properties) DO NOTHING"
        )

    return LowerCopyStatements(
        read=f'SELECT label AS "label", properties AS "properties" FROM {graph.catalog} WHERE kind = '
        f"{sql_text(LOWER_COPY_KIND)}",
        fill=fill,
        record=record,
        entries=catalog_entries,
    )


class _SqliteSchema:
    def __init__(self, graph: SqliteGraph) -> None:
        self.graph = graph

    def create_range(self, spec: RangeIndexSpec) -> list[str]:
        name = self.graph.object_name(RANGE, [spec.label, *spec.properties])
        return [
            f"CREATE INDEX IF NOT EXISTS {name} ON {self._over(spec.label, spec.properties)}",
            self._recorded(spec, name),
        ]

    def create_unique(self, spec: UniquenessConstraintSpec) -> list[str]:
        name = self.graph.object_name(UNIQUE, [spec.label, *spec.properties])
        return [
            f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {self._over(spec.label, spec.properties)}",
            self._recorded(spec, name),
        ]

    def create_vector(self, spec: VectorIndexSpec) -> list[str]:
        """Recorded and not made: SQLite has no vector index of its own."""
        details = f'{{"dimensions": {spec.dimensions}, "similarity": "{spec.similarity}"}}'
        return [self._recorded(spec, None, details)]

    def create_full_text(self, spec: FullTextIndexSpec) -> list[str]:
        """Recorded and not made yet: a search reads every node of the label."""
        return [self._recorded(spec, None)]

    def conflicts(self, spec: UniquenessConstraintSpec, limit: int) -> str:
        """A node's own id is its `nid`; `limit` of a key's nodes at most are named, the first made first."""
        keys = [text_of("n", it) for it in spec.properties]
        columns = ", ".join(f"{key} AS k{i}" for i, key in enumerate(keys))
        grouping = ", ".join(f"k{i}" for i in range(len(keys)))
        entries = ", ".join(f"{sql_text(it)}, k.k{i}" for i, it in enumerate(spec.properties))
        sampled = sampled_node_id(spec)
        node = ", ".join(["'elementId', k.nid", *([f"{sql_text(sampled)}, k.sampled"] if sampled else [])])
        partition = ", ".join(keys)
        sampling = f", {text_of('n', sampled)} AS sampled" if sampled else ""
        return "\n".join(
            [
                JSON_ROWS,
                f"SELECT json_object('properties', json_object({entries}), 'count', MAX(k.total), "
                f"'nodes', json_group_array(json_object({node}) ORDER BY k.nid) FILTER (WHERE k.position <= {limit})) "
                'AS "value"',
                "FROM (",
                f"    SELECT n.nid, {columns}{sampling},",
                f"           row_number() OVER (PARTITION BY {partition} ORDER BY n.nid) AS position,",
                f"           COUNT(*) OVER (PARTITION BY {partition}) AS total",
                f"    FROM {self.graph.nodes} n",
                f"    WHERE {has_label('n', spec.label)} AND {' AND '.join(f'{it} IS NOT NULL' for it in keys)}",
                ") k",
                "WHERE k.total > 1",
                f"GROUP BY {grouping}",
                f"LIMIT {limit}",
            ]
        )

    def _over(self, label: str, properties: Sequence[str]) -> str:
        """
        The partial index over the properties of the label's nodes: each property as a statement reads it, and the test
        of the label a statement makes, both of the table's own columns.
        """
        columns = ", ".join(text_of("", it) for it in properties)
        return f"{self.graph.nodes} ({columns}) WHERE {has_label('', label)}"

    def _recorded(self, spec: _Spec, object_name: str | None, details: str = "{}") -> str:
        """Records the item as made, as `object_name`, unless it is recorded already. Two processes record it once."""
        listed = sql_text(catalog_properties(spec.properties))
        made = sql_text(object_name) if object_name is not None else "NULL"
        return (
            f"INSERT INTO {self.graph.catalog} (kind, label, properties, name, object_name, details) "
            f"VALUES ({sql_text(CATALOG_KINDS[spec.kind])}, {sql_text(spec.label)}, {listed}, "
            f"{sql_text(item_name(spec))}, {made}, {sql_text(details)}) "
            "ON CONFLICT (kind, label, properties) DO NOTHING"
        )

    def forgotten(self, spec: _Spec) -> list[str]:
        """
        Forgets the item, and drops the index it was made as. SQLite has no block to read the record's object in, so the
        index is named again from the item's kind, label and properties, as drivine4j names it. Nothing was made for a
        vector or a full-text index, so nothing is dropped for one.
        """
        kind = {"RANGE": RANGE, "UNIQUE": UNIQUE}.get(spec.kind)
        return [
            *(
                [f"DROP INDEX IF EXISTS {self.graph.object_name(kind, [spec.label, *spec.properties])}"]
                if kind is not None
                else []
            ),
            f"DELETE FROM {self.graph.catalog} WHERE {catalog_record(spec.kind, spec.label, spec.properties)}",
        ]

    def listing(self, kind: CatalogKind) -> SchemaListing:
        """
        What is recorded of the kind, and still there: an index dropped by hand is not listed, so that it is made again
        when asked for.
        """
        return SchemaListing(
            f"{JSON_ROWS}\n"
            "SELECT json_object('label', c.label, 'properties', json(c.properties), 'name', c.name, "
            f"'details', json(c.details)) AS \"item\" FROM {self.graph.catalog} c "
            f"WHERE c.kind = {sql_text(CATALOG_KINDS[kind])} "
            "AND (c.object_name IS NULL OR EXISTS (SELECT 1 FROM sqlite_master "
            "WHERE type = 'index' AND name = c.object_name)) ORDER BY c.label, c.properties",
            _items,
        )


def _items(rows: list[Any]) -> list[SchemaItem]:
    return [
        SchemaItem(
            it["label"],
            tuple(it["properties"]),
            it["name"],
            dimensions=_number((it.get("details") or {}).get("dimensions")),
            similarity=(it.get("details") or {}).get("similarity"),
        )
        for it in rows
    ]


def _number(value: Any) -> int | None:
    return None if value is None else int(value)
