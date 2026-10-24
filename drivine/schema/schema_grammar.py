# Rows an engine reports are untyped: they are read here, field by field.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from drivine.backend.cypher import label_expression, quote_name
from drivine.connection.database_type import DatabaseType
from drivine.drivine_error import DrivineError
from drivine.schema.full_text_index_spec import FullTextIndexSpec
from drivine.schema.native_operation import NativeOperation
from drivine.schema.range_index_spec import RangeIndexSpec
from drivine.schema.schema_item import SchemaItem
from drivine.schema.schema_listing import SchemaListing
from drivine.schema.schema_specs import item_name
from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec
from drivine.schema.vector_index_spec import VectorIndexSpec

if TYPE_CHECKING:
    from drivine.backend.sql.sql_graph import SqlGraph


@dataclass(frozen=True)
class SchemaGrammar:
    """
    How an engine writes schema DDL and reports what it holds, as TypeScript's Drivine writes it. A statement is
    Cypher, except where the engine takes a native command (FalkorDB's constraints), given as a NativeOperation.
    """

    engine: str
    list_ranges: SchemaListing
    list_uniques: SchemaListing
    list_vectors: SchemaListing
    list_full_texts: SchemaListing
    create_range: Callable[[RangeIndexSpec], list[str]]
    drop_range: Callable[[RangeIndexSpec], list[str]]
    create_unique: Callable[[UniquenessConstraintSpec], list[str | NativeOperation]]
    drop_unique: Callable[[UniquenessConstraintSpec], list[str | NativeOperation]]
    create_vector: Callable[[VectorIndexSpec], list[str]]
    drop_vector: Callable[[VectorIndexSpec], list[str]]
    create_full_text: Callable[[FullTextIndexSpec], list[str]]
    drop_full_text: Callable[[FullTextIndexSpec, str | None], list[str]]
    """Drops the full-text index; the name is the engine's name for it, where the engine names items."""
    unsupported_vector_tuning: Callable[[VectorIndexSpec], list[str]] = field(default=lambda spec: [])
    """The pins of a vector index the engine can't express, by name: reported and logged, never dropped silently."""
    ranges_per_property: bool = False
    """Whether the engine indexes a label's properties one by one, so that a composite index is one per property."""
    full_text_over_label: bool = False
    """Whether a full-text index over several properties is made over the whole label, and reported with none."""
    unique_needs_range: bool = False
    """Whether a uniqueness constraint needs a range index over its properties, made first."""
    labelled_indexes: bool = False
    """
    Whether its range indexes and uniqueness constraints hold a label's nodes alone, so that a query over references
    reads a filter on one of their properties through them, in place of the label (Oracle). The object manager then
    gives the backend's render_query the ones there are.
    """

    list_failed_uniques: SchemaListing | None = None
    """
    Where a uniqueness constraint is built in the background (FalkorDB), a statement listing the ones whose build
    failed, as when two nodes hold the same values, and how to read its rows.
    """

    list_building_uniques: SchemaListing | None = None
    """Where a uniqueness constraint is built in the background (FalkorDB), the ones listed while still being built."""

    blocked_by_index: Callable[[BaseException], bool] = field(default=lambda failure: False)
    """
    Whether a failure making a uniqueness constraint says a plain range index over the same label and properties is in
    the way of it. Only where the constraint brings its own index, so the plain one may be dropped for it (Neo4j);
    never where the constraint needs the plain index (`unique_needs_range`, FalkorDB) or is made beside one (Memgraph,
    Oracle, Postgres).
    """
    drop_named_range: Callable[[str], list[str]] = field(default=lambda name: [])
    """Drops the range index the engine names so: one in the way of a constraint, whatever made it."""

    conflicts: Callable[[UniquenessConstraintSpec, int], str] = field(
        default=lambda spec, limit: _conflicts(spec, limit, "id")
    )
    """
    A statement giving up to `limit` keys that more than one of the label's nodes hold, one per row: each the
    `properties` that repeat, the `count` of nodes that hold them, and `nodes`, up to `limit` of them, each named by
    its `elementId` and the spec's `node_id_property`, when it has one that isn't a key (see `sampled_node_id`).
    """


def sampled_node_id(spec: UniquenessConstraintSpec) -> str | None:
    """
    The property a violation's sample names a node by, beside its engine id: the spec's `node_id_property`, unless it
    is one of the keys, which every node of a conflict holds alike.
    """
    held = spec.node_id_property
    return held if held is not None and held not in spec.properties else None


def _conflicts(spec: UniquenessConstraintSpec, limit: int, element_id: str) -> str:
    """
    Keys that repeat over the properties, in Cypher, each with the nodes that hold it, `limit` of them at most:
    `element_id` names the function that gives a node's own id.
    """
    values = [f"n.{quote_name(it)}" for it in spec.properties]
    present = " AND ".join(f"{it} IS NOT NULL" for it in values)
    keys = ", ".join(f"{it} AS _k{k}" for k, it in enumerate(values))
    node_id = sampled_node_id(spec)
    node = ", ".join(
        [f"elementId: {element_id}(n)", *([f"{quote_name(node_id)}: n.{quote_name(node_id)}"] if node_id else [])]
    )
    properties = ", ".join(f"{quote_name(it)}: _k{k}" for k, it in enumerate(spec.properties))
    return (
        f"MATCH (n{label_expression([spec.label])}) WHERE {present} "
        f"WITH {keys}, count(*) AS holders, collect({{{node}}}) AS held WHERE holders > 1 "
        f"RETURN {{properties: {{{properties}}}, count: holders, nodes: held[0..{limit}]}} LIMIT {limit}"
    )


INDEX_ALREADY_EXISTS = "Neo.ClientError.Schema.IndexAlreadyExists"
"""
Neo4j's code for "There already exists an index (:Label {prop}). A constraint cannot be created until the index has
been dropped.", which Neo4j 4.4 to 2026 all give.
"""


def _neo4j_blocked_by_index(failure: BaseException) -> bool:
    """By the code of the driver's error, under whatever wraps it; never by the message, or a clash of names."""
    cause: BaseException | None = failure
    for _ in range(10):
        if cause is None:
            return False
        if getattr(cause, "code", None) == INDEX_ALREADY_EXISTS:
            return True
        cause = cause.__cause__
    return False


def _pattern(spec: Any) -> str:
    return f"(n{label_expression([spec.label])})"


def _props(spec: Any) -> str:
    return ", ".join(f"n.{quote_name(it)}" for it in spec.properties)


def _number(value: Any) -> int | None:
    return None if value is None else int(value)


def portable_pins(spec: VectorIndexSpec) -> list[str]:
    """The portable HNSW pins a spec makes, for an engine with no HNSW parameters of its own."""
    return [
        *(["hnsw_m"] if spec.hnsw_m is not None else []),
        *(["hnsw_ef_construction"] if spec.hnsw_ef_construction is not None else []),
    ]


def _neo4j_items(rows: list[Any]) -> list[SchemaItem]:
    return [SchemaItem(it["labels"][0], tuple(it["properties"]), it["name"]) for it in rows]


def _neo4j_vectors(rows: list[Any]) -> list[SchemaItem]:
    items: list[SchemaItem] = []
    for it in rows:
        config = it.get("config") or {}
        items.append(
            SchemaItem(
                it["labels"][0],
                tuple(it["properties"]),
                it["name"],
                _number(config.get("vector.dimensions")),
                str(config.get("vector.similarity_function")).upper(),  # pyright: ignore[reportArgumentType]
                _number(config.get("vector.hnsw.m")),
                _number(config.get("vector.hnsw.ef_construction")),
                _quantization_enabled(config),
            )
        )
    return items


def _quantization_enabled(config: dict[str, Any]) -> bool | None:
    """
    Whether a Neo4j vector index quantizes, as its `indexConfig` reports it. Neo4j up to 2026.05 reports
    `vector.quantization.enabled`. From 2026.06 it reports `vector.quantization.type` in its place: `NONE`, or the kind
    of quantization, `SCALAR` or `BINARY`. None when it reports neither.
    """
    kind = config.get("vector.quantization.type")
    if isinstance(kind, str):
        return kind.upper() != "NONE"
    enabled = config.get("vector.quantization.enabled")
    return enabled if isinstance(enabled, bool) else None


def _neo4j_create_vector(spec: VectorIndexSpec) -> list[str]:
    tuning = spec.tuning("Neo4j")
    config = [
        f"`vector.dimensions`: {spec.dimensions}",
        f"`vector.similarity_function`: '{spec.similarity.lower()}'",
        # `enabled`, not `type`: every Neo4j from 5 to 2026.10 accepts `enabled`; those before 2026.06 refuse `type`.
        *(
            [f"`vector.quantization.enabled`: {str(tuning.quantization_enabled).lower()}"]
            if tuning.quantization_enabled is not None
            else []
        ),
        *([f"`vector.hnsw.m`: {tuning.hnsw_m}"] if tuning.hnsw_m is not None else []),
        *(
            [f"`vector.hnsw.ef_construction`: {tuning.hnsw_ef_construction}"]
            if tuning.hnsw_ef_construction is not None
            else []
        ),
    ]
    return [
        f"CREATE VECTOR INDEX {quote_name(item_name(spec))} IF NOT EXISTS FOR {_pattern(spec)} ON ({_props(spec)}) "
        f"OPTIONS {{indexConfig: {{{', '.join(config)}}}}}"
    ]


NEO4J_SCHEMA = SchemaGrammar(
    engine="Neo4j",
    list_ranges=SchemaListing(
        "SHOW INDEXES YIELD name, type, entityType, labelsOrTypes, properties, owningConstraint WHERE type = 'RANGE' "
        "AND entityType = 'NODE' AND owningConstraint IS NULL "
        "RETURN {name: name, labels: labelsOrTypes, properties: properties}",
        _neo4j_items,
    ),
    list_uniques=SchemaListing(
        "SHOW CONSTRAINTS YIELD name, type, entityType, labelsOrTypes, properties WHERE type IN ['UNIQUENESS', "
        "'NODE_PROPERTY_UNIQUENESS'] AND entityType = 'NODE' "
        "RETURN {name: name, labels: labelsOrTypes, properties: properties}",
        _neo4j_items,
    ),
    list_vectors=SchemaListing(
        "SHOW INDEXES YIELD name, type, entityType, labelsOrTypes, properties, options WHERE type = 'VECTOR' AND "
        "entityType = 'NODE' RETURN {name: name, labels: labelsOrTypes, properties: properties, "
        "config: options.indexConfig}",
        _neo4j_vectors,
    ),
    list_full_texts=SchemaListing(
        "SHOW INDEXES YIELD name, type, entityType, labelsOrTypes, properties WHERE type = 'FULLTEXT' AND "
        "entityType = 'NODE' RETURN {name: name, labels: labelsOrTypes, properties: properties}",
        _neo4j_items,
    ),
    create_range=lambda spec: [
        f"CREATE INDEX {quote_name(item_name(spec))} IF NOT EXISTS FOR {_pattern(spec)} ON ({_props(spec)})"
    ],
    drop_range=lambda spec: [f"DROP INDEX {quote_name(item_name(spec))} IF EXISTS"],
    create_unique=lambda spec: [
        f"CREATE CONSTRAINT {quote_name(item_name(spec))} IF NOT EXISTS FOR {_pattern(spec)} REQUIRE "
        f"{_props(spec) if len(spec.properties) == 1 else f'({_props(spec)})'} IS UNIQUE"
    ],
    drop_unique=lambda spec: [f"DROP CONSTRAINT {quote_name(item_name(spec))} IF EXISTS"],
    create_vector=_neo4j_create_vector,
    drop_vector=lambda spec: [f"DROP INDEX {quote_name(item_name(spec))} IF EXISTS"],
    create_full_text=lambda spec: [
        f"CREATE FULLTEXT INDEX {quote_name(item_name(spec))} IF NOT EXISTS FOR {_pattern(spec)} "
        f"ON EACH [{_props(spec)}]"
    ],
    drop_full_text=lambda spec, name: [f"DROP INDEX {quote_name(name or item_name(spec))} IF EXISTS"],
    conflicts=lambda spec, limit: _conflicts(spec, limit, "elementId"),
    blocked_by_index=_neo4j_blocked_by_index,
    drop_named_range=lambda name: [f"DROP INDEX {quote_name(name)} IF EXISTS"],
)


def _memgraph_text_items(rows: list[Any]) -> list[SchemaItem]:
    # A text index is a SHOW INDEX INFO row of type 'label_text (name: <name>)'.
    items: list[SchemaItem] = []
    for it in rows:
        if str(it[0]).startswith("label_text"):
            found = re.search(r"name: ([^)]+)", str(it[0]))
            properties = it[2] if isinstance(it[2], list) else ([it[2]] if it[2] is not None else [])
            items.append(SchemaItem(it[1], tuple(properties), found.group(1) if found else None))  # pyright: ignore[reportUnknownArgumentType]
    return items


def _listed(value: Any) -> tuple[str, ...]:
    return tuple(value) if isinstance(value, list) else (value,)  # pyright: ignore[reportUnknownArgumentType]


MEMGRAPH_SCHEMA = SchemaGrammar(
    engine="Memgraph",
    full_text_over_label=True,
    # Memgraph takes no RETURN after SHOW, so rows come as lists. Columns: index type, label, property, count.
    list_ranges=SchemaListing(
        "SHOW INDEX INFO",
        lambda rows: [SchemaItem(it[1], _listed(it[2])) for it in rows if it[0] == "label+property"],
    ),
    # Columns: constraint type, label, properties, data type.
    list_uniques=SchemaListing(
        "SHOW CONSTRAINT INFO",
        lambda rows: [SchemaItem(it[1], _listed(it[2])) for it in rows if it[0] == "unique"],
    ),
    # Columns: index_name, label (as ':Label'), property, capacity, dimension, metric, ...
    list_vectors=SchemaListing(
        "SHOW VECTOR INDEX INFO",
        lambda rows: [
            SchemaItem(
                re.sub(r"^:", "", str(it[1])),
                (it[2],),
                it[0],
                int(it[4]),
                "COSINE" if it[5] == "cos" else "EUCLIDEAN",
            )
            for it in rows
        ],
    ),
    list_full_texts=SchemaListing("SHOW INDEX INFO", _memgraph_text_items),
    create_range=lambda spec: [
        f"CREATE INDEX ON {label_expression([spec.label])}({', '.join(quote_name(it) for it in spec.properties)})"
    ],
    drop_range=lambda spec: [
        f"DROP INDEX ON {label_expression([spec.label])}({', '.join(quote_name(it) for it in spec.properties)})"
    ],
    create_unique=lambda spec: [f"CREATE CONSTRAINT ON {_pattern(spec)} ASSERT {_props(spec)} IS UNIQUE"],
    drop_unique=lambda spec: [f"DROP CONSTRAINT ON {_pattern(spec)} ASSERT {_props(spec)} IS UNIQUE"],
    create_vector=lambda spec: [
        f"CREATE VECTOR INDEX {quote_name(item_name(spec))} ON {label_expression([spec.label])}"
        f"({quote_name(spec.properties[0])}) WITH CONFIG "
        f'{{"dimension": {spec.dimensions}, "capacity": {spec.capacity or 1024}, '
        f'"metric": "{"cos" if spec.similarity == "COSINE" else "l2sq"}"}}'
    ],
    drop_vector=lambda spec: [f"DROP VECTOR INDEX {quote_name(item_name(spec))}"],
    # Memgraph's vector index takes no HNSW parameters.
    unsupported_vector_tuning=portable_pins,
    # A text index over several properties indexes only the first (Memgraph 3.x), so one over several is made over the
    # whole label, which indexes every property: a search may then match the label's other text too.
    create_full_text=lambda spec: [
        f"CREATE TEXT INDEX {quote_name(item_name(spec))} ON {label_expression([spec.label])}"
        f"({quote_name(spec.properties[0])})"
        if len(spec.properties) == 1
        else f"CREATE TEXT INDEX {quote_name(item_name(spec))} ON {label_expression([spec.label])}"
    ],
    drop_full_text=lambda spec, name: [f"DROP TEXT INDEX {name or item_name(spec)}"],
)


def _falkordb_by_type(rows: list[Any], kind: str) -> list[tuple[Any, str]]:
    """Each (row, property) whose property has an index of the kind."""
    return [(it, prop) for it in rows for prop in it["properties"] if kind in ((it.get("types") or {}).get(prop) or [])]


def _falkordb_vectors(rows: list[Any]) -> list[SchemaItem]:
    items: list[SchemaItem] = []
    for it, prop in _falkordb_by_type(rows, "VECTOR"):
        options = (it.get("options") or {}).get(prop) or {}
        items.append(
            SchemaItem(
                it["label"],
                (prop,),
                None,
                _number(options.get("dimension")),
                str(options.get("similarityFunction")).upper(),  # pyright: ignore[reportArgumentType]
                _number(options.get("M")),
                _number(options.get("efConstruction")),
                None,
                _number(options.get("efRuntime")),
            )
        )
    return items


def _falkordb_create_vector(spec: VectorIndexSpec) -> list[str]:
    # FalkorDB names the HNSW parameters M / efConstruction, adds efRuntime, and accepts any key without complaint, so
    # the names are the ones it reads back.
    tuning = spec.tuning("FalkorDB")
    options = [
        f"dimension: {spec.dimensions}",
        f"similarityFunction: '{spec.similarity.lower()}'",
        *([f"M: {tuning.hnsw_m}"] if tuning.hnsw_m is not None else []),
        *([f"efConstruction: {tuning.hnsw_ef_construction}"] if tuning.hnsw_ef_construction is not None else []),
        *([f"efRuntime: {tuning.ef_runtime}"] if tuning.ef_runtime is not None else []),
    ]
    return [f"CREATE VECTOR INDEX FOR {_pattern(spec)} ON ({_props(spec)}) OPTIONS {{{', '.join(options)}}}"]


_FALKORDB_INDEXES = (
    "CALL db.indexes() YIELD label, properties, types, entitytype WHERE entitytype = 'NODE' "
    "RETURN {label: label, properties: properties, types: types}"
)
_FALKORDB_VECTOR_INDEXES = (
    "CALL db.indexes() YIELD label, properties, types, options, entitytype WHERE entitytype = 'NODE' "
    "RETURN {label: label, properties: properties, types: types, options: options}"
)
_FALKORDB_UNIQUES = (
    "CALL db.constraints() YIELD type, label, properties, entitytype, status WHERE type = 'UNIQUE' AND "
    "entitytype = 'NODE' RETURN {label: label, properties: properties, status: status}"
)

FALKORDB_SCHEMA = SchemaGrammar(
    engine="FalkorDB",
    ranges_per_property=True,
    unique_needs_range=True,
    # One item per property with a range index: a composite spec matches when all of its are present.
    list_ranges=SchemaListing(
        _FALKORDB_INDEXES,
        lambda rows: [SchemaItem(it["label"], (prop,)) for it, prop in _falkordb_by_type(rows, "RANGE")],
    ),
    list_uniques=SchemaListing(
        _FALKORDB_UNIQUES,
        lambda rows: [SchemaItem(it["label"], tuple(it["properties"])) for it in rows if it.get("status") != "FAILED"],
    ),
    list_failed_uniques=SchemaListing(
        _FALKORDB_UNIQUES,
        lambda rows: [SchemaItem(it["label"], tuple(it["properties"])) for it in rows if it.get("status") == "FAILED"],
    ),
    # UNDER CONSTRUCTION until it is OPERATIONAL or FAILED.
    list_building_uniques=SchemaListing(
        _FALKORDB_UNIQUES,
        lambda rows: [
            SchemaItem(it["label"], tuple(it["properties"]))
            for it in rows
            if it.get("status") and it.get("status") not in ("OPERATIONAL", "FAILED")
        ],
    ),
    list_vectors=SchemaListing(_FALKORDB_VECTOR_INDEXES, _falkordb_vectors),
    # One full-text index per label, over the properties that have one.
    list_full_texts=SchemaListing(
        _FALKORDB_INDEXES,
        lambda rows: [
            SchemaItem(it["label"], props)
            for it in rows
            if (props := tuple(p for p in it["properties"] if "FULLTEXT" in ((it.get("types") or {}).get(p) or [])))
        ],
    ),
    create_range=lambda spec: [f"CREATE INDEX FOR {_pattern(spec)} ON ({_props(spec)})"],
    drop_range=lambda spec: [f"DROP INDEX FOR {_pattern(spec)} ON (n.{quote_name(it)})" for it in spec.properties],
    create_unique=lambda spec: [NativeOperation("CONSTRAINT_CREATE", spec.label, spec.properties)],
    drop_unique=lambda spec: [NativeOperation("CONSTRAINT_DROP", spec.label, spec.properties)],
    create_vector=_falkordb_create_vector,
    drop_vector=lambda spec: [f"DROP VECTOR INDEX FOR {_pattern(spec)} ON ({_props(spec)})"],
    create_full_text=lambda spec: [f"CREATE FULLTEXT INDEX FOR {_pattern(spec)} ON ({_props(spec)})"],
    drop_full_text=lambda spec, name: [
        f"DROP FULLTEXT INDEX FOR {_pattern(spec)} ON (n.{quote_name(it)})" for it in spec.properties
    ],
)
"""
FalkorDB indexes each property of a label on its own, so a composite range index is one per property, and present
when each is. Its uniqueness constraints are a Redis command, and need a range index over their properties.
"""


def schema_grammar_for(type: DatabaseType, graph: SqlGraph | None = None) -> SchemaGrammar:
    """
    The engine's schema grammar; on Oracle and Postgres, over the tables of `graph` (the default graph when None, and
    the graph of that name when it is another store's).
    """
    if type is DatabaseType.ORACLE:
        from drivine.backend.oracle.oracle_graph import OracleGraph
        from drivine.backend.oracle.oracle_schema_grammar import oracle_schema_grammar

        return oracle_schema_grammar(graph if isinstance(graph, OracleGraph) else OracleGraph(_name_of(graph)))
    if type is DatabaseType.POSTGRES:
        from drivine.backend.postgres.postgres_graph import PostgresGraph
        from drivine.backend.postgres.postgres_schema_grammar import postgres_schema_grammar

        return postgres_schema_grammar(graph if isinstance(graph, PostgresGraph) else PostgresGraph(_name_of(graph)))
    grammars = {
        DatabaseType.NEO4J: NEO4J_SCHEMA,
        DatabaseType.MEMGRAPH: MEMGRAPH_SCHEMA,
        DatabaseType.FALKORDB: FALKORDB_SCHEMA,
    }
    grammar = grammars.get(type)
    if grammar is None:
        raise DrivineError(f"{type} has no schema management.")
    return grammar


def _name_of(graph: SqlGraph | None) -> str:
    """The name of the graph, the default graph's when None."""
    from drivine.backend.sql.sql_graph import DEFAULT_GRAPH

    return graph.name if graph is not None else DEFAULT_GRAPH
