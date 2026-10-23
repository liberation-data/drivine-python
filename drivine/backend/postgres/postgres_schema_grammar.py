# Rows the catalog gives are untyped JSON documents: they are read here, field by field.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from drivine.backend.postgres.postgres_graph import PostgresGraph, has_labels, text_of
from drivine.backend.postgres.postgres_search import embedding, holds, searched_text
from drivine.backend.sql.sql_catalog import CATALOG_KINDS, CatalogKind, catalog_properties, catalog_record
from drivine.backend.sql.sql_graph import sql_text
from drivine.schema.full_text_index_spec import FullTextIndexSpec
from drivine.schema.range_index_spec import RangeIndexSpec
from drivine.schema.schema_grammar import SchemaGrammar, portable_pins, sampled_node_id
from drivine.schema.schema_item import SchemaItem
from drivine.schema.schema_listing import SchemaListing
from drivine.schema.schema_specs import item_name
from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec
from drivine.schema.vector_index_spec import VectorIndexSpec

_Spec = RangeIndexSpec | UniquenessConstraintSpec | VectorIndexSpec | FullTextIndexSpec


def postgres_schema_grammar(graph: PostgresGraph) -> SchemaGrammar:
    """
    Indexes and constraints on Postgres, as drivine4j makes them, so a store's schema is one whichever library made
    it. A property is a key of the `jsonb` column `props`, and Postgres indexes the part of a table a predicate selects,
    so each is a partial index over an expression, for the nodes of one label:

    - a range index is `((props ->> 'p')) WHERE labels @> '["L"]'`. A statement tests each label of a node on its own
      and reads a property as that same expression, so the planner sees that the index holds the nodes it asks for;
    - a uniqueness constraint is that index made `UNIQUE`. A node without the property reads it as null, which a unique
      index does not hold, so it is not constrained, as on the graph engines;
    - a vector index is an HNSW index of pgvector over the embeddings of the label's nodes that are of its size;
    - a full-text index is a GIN index of the trigrams of the text a search reads, which narrows a search to the nodes
      whose text holds a term's words.

    What is made is recorded in the graph's catalog, as drivine4j records it, and read back from there: an index's
    expression does not say which label and properties it was made for. The object manager makes the store before it
    runs a statement of its schema, so the catalog is there.
    """
    grammar = _PostgresSchema(graph)
    return SchemaGrammar(
        engine="Postgres",
        list_ranges=grammar.listing("RANGE"),
        list_uniques=grammar.listing("UNIQUE"),
        list_vectors=grammar.listing("VECTOR"),
        list_full_texts=grammar.listing("FULLTEXT"),
        create_range=grammar.create_range,
        drop_range=lambda spec: [grammar.forgotten(spec)],
        create_unique=lambda spec: [*grammar.create_unique(spec)],
        drop_unique=lambda spec: [grammar.forgotten(spec)],
        create_vector=grammar.create_vector,
        drop_vector=lambda spec: [grammar.forgotten(spec)],
        create_full_text=grammar.create_full_text,
        drop_full_text=lambda spec, name: [grammar.forgotten(spec)],
        # drivine4j makes pgvector's index with its own parameters.
        unsupported_vector_tuning=portable_pins,
        conflicts=grammar.conflicts,
    )


class _PostgresSchema:
    def __init__(self, graph: PostgresGraph) -> None:
        self.graph = graph

    def create_range(self, spec: RangeIndexSpec) -> list[str]:
        name = self.graph.object_name("ix", [spec.label, *spec.properties])
        return [
            f"CREATE INDEX IF NOT EXISTS {name} ON {self._over(spec.label, spec.properties)}",
            self._recorded(spec, name),
        ]

    def create_unique(self, spec: UniquenessConstraintSpec) -> list[str]:
        name = self.graph.object_name("uq", [spec.label, *spec.properties])
        return [
            f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {self._over(spec.label, spec.properties)}",
            self._recorded(spec, name),
        ]

    def create_vector(self, spec: VectorIndexSpec) -> list[str]:
        """
        An HNSW index over the embeddings of the label's nodes that are of its size: one of another size is left out.
        """
        prop = spec.properties[0]
        name = self.graph.object_name("v", [spec.label, prop])
        operators = "vector_cosine_ops" if spec.similarity == "COSINE" else "vector_l2_ops"
        return [
            "CREATE EXTENSION IF NOT EXISTS vector",
            f"CREATE INDEX IF NOT EXISTS {name} ON {self.graph.nodes} USING hnsw "
            f"(({embedding('', prop, spec.dimensions)}) {operators}) "
            f"WHERE {has_labels('', [spec.label])} AND {holds('', prop, spec.dimensions)}",
            self._recorded(spec, name, f'{{"dimensions": {spec.dimensions}, "similarity": "{spec.similarity}"}}'),
        ]

    def create_full_text(self, spec: FullTextIndexSpec) -> list[str]:
        """Trigrams of the text a search reads, which answer whether it holds a term's words."""
        name = self.graph.object_name("t", [spec.label, *spec.properties])
        return [
            "CREATE EXTENSION IF NOT EXISTS pg_trgm",
            f"CREATE INDEX IF NOT EXISTS {name} ON {self.graph.nodes} USING gin "
            f"(({searched_text('', spec.properties)}) gin_trgm_ops) WHERE {has_labels('', [spec.label])}",
            self._recorded(spec, name),
        ]

    def conflicts(self, spec: UniquenessConstraintSpec, limit: int) -> str:
        """A node's own id is its `nid`; `limit` of a key's nodes at most are named, the first made first."""
        values = [text_of("n", it) for it in spec.properties]
        properties = ", ".join(f"{sql_text(it)}, {values[k]}" for k, it in enumerate(spec.properties))
        node_id = sampled_node_id(spec)
        node = ", ".join(
            ["'elementId', n.nid", *([f"{sql_text(node_id)}, {text_of('n', node_id)}"] if node_id else [])]
        )
        return (
            f"SELECT jsonb_build_object('properties', jsonb_build_object({properties}), 'count', COUNT(*), 'nodes', "
            f'to_jsonb((array_agg(jsonb_build_object({node}) ORDER BY n.nid))[1:{limit}])) AS "value" '
            f"FROM {self.graph.nodes} n "
            f"WHERE {has_labels('n', [spec.label])} AND {' AND '.join(f'{it} IS NOT NULL' for it in values)} "
            f"GROUP BY {', '.join(values)} HAVING COUNT(*) > 1 LIMIT {limit}"
        )

    def _over(self, label: str, properties: Sequence[str]) -> str:
        """
        The partial index over the properties of the label's nodes: each property as a statement reads it, and the test
        of the label a statement makes, both of the table's own columns.
        """
        columns = ", ".join(f"({text_of('', it)})" for it in properties)
        return f"{self.graph.nodes} ({columns}) WHERE {has_labels('', [label])}"

    def _recorded(self, spec: _Spec, object_name: str, details: str = "{}") -> str:
        """Records the item as made, as `object_name`, unless it is recorded already. Two processes record it once."""
        listed = sql_text(catalog_properties(spec.properties))
        return (
            f"INSERT INTO {self.graph.catalog} (kind, label, properties, name, object_name, details) "
            f"VALUES ({sql_text(CATALOG_KINDS[spec.kind])}, {sql_text(spec.label)}, {listed}, "
            f"{sql_text(item_name(spec))}, {sql_text(object_name)}, {sql_text(details)}) "
            "ON CONFLICT (kind, label, properties) DO NOTHING"
        )

    def forgotten(self, spec: _Spec) -> str:
        """
        Forgets the item, and drops what it was made as when nothing else recorded uses it: the object the record names,
        so an item is dropped as it was made whatever a later version would call it.
        """
        record = catalog_record(spec.kind, spec.label, spec.properties)
        return f"""DO $forget$
DECLARE
    v_object text;
    v_others int;
BEGIN
    IF to_regclass({sql_text(self.graph.catalog)}) IS NULL THEN
        RETURN;
    END IF;
    SELECT max(object_name) INTO v_object FROM {self.graph.catalog} WHERE {record};
    SELECT count(*) INTO v_others FROM {self.graph.catalog} WHERE object_name = v_object AND NOT ({record});
    IF v_object IS NOT NULL AND v_others = 0 THEN
        EXECUTE 'DROP INDEX IF EXISTS ' || v_object;
    END IF;
    DELETE FROM {self.graph.catalog} WHERE {record};
END
$forget$"""

    def listing(self, kind: CatalogKind) -> SchemaListing:
        """
        What is recorded of the kind, and still there: an index dropped by hand is not listed, so that it is made again
        when asked for.
        """
        return SchemaListing(
            "SELECT jsonb_build_object('label', c.label, 'properties', c.properties::jsonb, 'name', c.name, "
            f"'details', c.details) AS \"item\" FROM {self.graph.catalog} c "
            f"WHERE c.kind = {sql_text(CATALOG_KINDS[kind])} "
            "AND (c.object_name IS NULL OR to_regclass(c.object_name) IS NOT NULL) ORDER BY c.label, c.properties",
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
