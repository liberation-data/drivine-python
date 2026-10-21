# Rows the catalog gives are untyped JSON documents: they are read here, field by field.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Any, Literal

from drivine.backend.oracle.oracle_graph import OracleGraph, has_labels, json_key, sql_text
from drivine.backend.oracle.oracle_json import to_json
from drivine.backend.oracle.oracle_side_table import OracleSideTable
from drivine.schema.full_text_index_spec import FullTextIndexSpec
from drivine.schema.schema_grammar import SchemaGrammar, portable_pins
from drivine.schema.schema_item import SchemaItem
from drivine.schema.schema_listing import SchemaListing
from drivine.schema.schema_specs import item_name
from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec
from drivine.schema.vector_index_spec import VectorIndexSpec

_Kind = Literal["RANGE", "UNIQUE"]

_BLOCK_ENDS = re.compile(r"^BEGIN\s*|\s*END;$")


def oracle_schema_grammar(graph: OracleGraph) -> SchemaGrammar:
    """
    Indexes and constraints on Oracle, as drivine4j makes them: over the JSON properties of one label's nodes, a
    function-based index of `CASE WHEN <the node carries the label> THEN JSON_VALUE(props, '$.<p>') END` for each
    property, so other labels' nodes are left out (their entries are null, which an index holds none of). A uniqueness
    constraint is the same index, unique. Each item is recorded in the graph's catalog with the name of the object made
    for it; listing reads the catalog, keeping items whose object is still there, and a drop drops the object recorded.

    A vector index keeps the label's embeddings in a table beside the nodes (Oracle indexes a VECTOR column, and an
    embedding is a list among a node's JSON properties), with a vector index over it by neighbour partitions, which
    have no HNSW parameters. A full-text index, where the database has Oracle Text, keeps the text in a table beside
    the nodes with a JSON search index over it; without Oracle Text it is only recorded, and a search reads every node.
    """
    grammar = _OracleSchema(graph)
    return SchemaGrammar(
        engine="Oracle",
        list_ranges=grammar.listing("RANGE"),
        list_uniques=grammar.listing("UNIQUE"),
        list_vectors=grammar.list_vectors(),
        list_full_texts=grammar.listing("FULLTEXT", made=False),
        create_range=lambda spec: [grammar.create("RANGE", spec.label, spec.properties)],
        drop_range=lambda spec: [grammar.drop("RANGE", spec.label, spec.properties)],
        create_unique=lambda spec: [grammar.create("UNIQUE", spec.label, spec.properties)],
        drop_unique=lambda spec: [grammar.drop("UNIQUE", spec.label, spec.properties)],
        create_vector=grammar.create_vector,
        drop_vector=grammar.drop_vector,
        create_full_text=grammar.create_full_text,
        drop_full_text=lambda spec, name: grammar.drop_full_text(spec),
        unsupported_vector_tuning=portable_pins,
        conflicts=grammar.conflicts,
    )


def object_name(graph: OracleGraph, kind: _Kind, label: str, properties: Sequence[str]) -> str:
    """The name of the object made for an item: the graph's, the kind, and a hash of what it is over."""
    hashed = hashlib.sha1("\u0000".join([kind, label, *properties]).encode()).hexdigest()[:12]
    return f"{graph.name}_{'r' if kind == 'RANGE' else 'u'}_{hashed}".upper()


class _OracleSchema:
    def __init__(self, graph: OracleGraph) -> None:
        self.graph = graph

    def listing(self, kind: _Kind | Literal["FULLTEXT"], made: bool = True) -> SchemaListing:
        exists = " AND EXISTS (SELECT 1 FROM user_indexes i WHERE i.index_name = c.object_name)" if made else ""
        return SchemaListing(
            "SELECT JSON_OBJECT('label' VALUE c.label, 'properties' VALUE JSON(c.properties), 'name' VALUE c.name "
            f"RETURNING JSON) AS \"item\" FROM {self.graph.catalog} c WHERE c.kind = '{kind}'{exists}",
            lambda rows: [SchemaItem(it["label"], tuple(it["properties"]), it["name"]) for it in rows],
        )

    def list_vectors(self) -> SchemaListing:
        """Vector indexes as recorded: each with the table made for it, where one was."""
        return SchemaListing(
            "SELECT JSON_OBJECT('label' VALUE c.label, 'properties' VALUE JSON(c.properties), 'name' VALUE c.name, "
            f"'details' VALUE c.details RETURNING JSON) AS \"item\" FROM {self.graph.catalog} c "
            "WHERE c.kind = 'VECTOR'",
            lambda rows: [
                SchemaItem(
                    it["label"],
                    tuple(it["properties"]),
                    it["name"],
                    dimensions=_number(it["details"].get("dimensions")),
                    similarity=it["details"].get("similarity"),
                )
                for it in rows
            ],
        )

    def create_vector(self, spec: VectorIndexSpec) -> list[str]:
        """
        A vector index, as drivine4j makes it. The table is filled before the index is made, so the partitions are
        trained on the embeddings there are.
        """
        prop = spec.properties[0]
        path = f"'${json_key(prop)}'"
        side = OracleSideTable(
            self.graph.vectors(spec.label, prop),
            self.graph.nodes,
            "embedding",
            f"VECTOR({spec.dimensions}, FLOAT32)",
            lambda row: f"TO_VECTOR(JSON_QUERY({row}.props, {path} RETURNING CLOB))",
            lambda row: f"{has_labels(row, [spec.label])} AND JSON_EXISTS({row}.props, {path})",
            lambda row: (
                f"VECTOR_DIMENSION_COUNT(TO_VECTOR(JSON_QUERY({row}.props, {path} RETURNING CLOB))) = {spec.dimensions}"
            ),
            True,
        )
        metric = "COSINE" if spec.similarity == "COSINE" else "EUCLIDEAN_SQUARED"
        return [
            side.create,
            side.trigger_statement,
            side.backfill,
            f"CREATE VECTOR INDEX IF NOT EXISTS {side.index} ON {side.name} (embedding) ORGANIZATION NEIGHBOR "
            f"PARTITIONS DISTANCE {metric}",
            self._record("VECTOR", spec, {"dimensions": spec.dimensions, "similarity": spec.similarity}, side.name),
        ]

    def drop_vector(self, spec: VectorIndexSpec) -> list[str]:
        return [
            *OracleSideTable.dropping(self.graph.vectors(spec.label, spec.properties[0])),
            self._unrecord("VECTOR", spec),
        ]

    def create_full_text(self, spec: FullTextIndexSpec) -> list[str]:
        """
        Not over the node table: once an Oracle Text index is there, Oracle answers ordinary tests of a JSON property
        from it, and it holds only what has been committed, so a save would not find a node it made a moment before
        (drivine4j's finding). Without Oracle Text (ORA-29833) nothing is made, and the index is recorded.
        """
        side = OracleSideTable(
            self.graph.texts(spec.label, spec.properties),
            self.graph.nodes,
            "doc",
            "JSON",
            lambda row: (
                "JSON_OBJECT("
                + ", ".join(
                    f"{sql_text(it)} VALUE JSON_VALUE({row}.props, '${json_key(it)}' RETURNING CLOB)"
                    for it in spec.properties
                )
                + " ABSENT ON NULL RETURNING JSON)"
            ),
            lambda row: has_labels(row, [spec.label]),
        )
        search_index = f"CREATE SEARCH INDEX {side.index} ON {side.name} (doc) FOR JSON PARAMETERS ('SYNC (ON COMMIT)')"
        return [
            f"""DECLARE
    v_indexed NUMBER;
    e_no_text EXCEPTION;
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_no_text, -29833);
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE {sql_text(side.create)};
    SELECT COUNT(*) INTO v_indexed FROM user_indexes WHERE index_name = '{side.index.upper()}';
    IF v_indexed = 0 THEN
        BEGIN
            EXECUTE IMMEDIATE {sql_text(search_index)};
        EXCEPTION
            WHEN e_exists THEN NULL;
            WHEN e_no_text THEN
                EXECUTE IMMEDIATE 'DROP TABLE {side.name} PURGE';
                {_BLOCK_ENDS.sub("", self._record("FULLTEXT", spec, {}))}
                RETURN;
        END;
    END IF;
    EXECUTE IMMEDIATE {sql_text(side.trigger_ddl)};
    EXECUTE IMMEDIATE {sql_text(side.backfill)};
    {_BLOCK_ENDS.sub("", self._record("FULLTEXT", spec, {}, side.name))}
END;"""
        ]

    def drop_full_text(self, spec: FullTextIndexSpec) -> list[str]:
        return [
            *OracleSideTable.dropping(self.graph.texts(spec.label, spec.properties)),
            self._unrecord("FULLTEXT", spec),
        ]

    def conflicts(self, spec: UniquenessConstraintSpec, limit: int) -> str:
        values = [f"JSON_VALUE(n.props, '${json_key(it)}')" for it in spec.properties]
        # One property's value as it is, several as a list, as the graph engines give them.
        key = values[0] if len(values) == 1 else f"JSON_ARRAY({', '.join(values)} RETURNING JSON)"
        present = " AND ".join(f"{it} IS NOT NULL" for it in values)
        return (
            f'SELECT {key} AS "value" FROM {self.graph.nodes} n '
            f"WHERE {has_labels('n', [spec.label])} AND {present} "
            f"GROUP BY {', '.join(values)} HAVING COUNT(*) > 1 FETCH FIRST {limit} ROWS ONLY"
        )

    def create(self, kind: _Kind, label: str, properties: Sequence[str]) -> str:
        """
        Makes the index and records it, in one block; an item recorded already is recorded again. Another session may
        make and record the same item at once, as managers that start together each ensure the store's key: an index
        of the name is this one, its name a hash of what it is over, and a record of the item is this record.
        """
        made = object_name(self.graph, kind, label, properties)
        columns = ", ".join(
            f"CASE WHEN {has_labels('n', [label])} THEN JSON_VALUE(n.props, '${json_key(it)}' RETURNING VARCHAR2(4000) "
            "NULL ON ERROR) END"
            for it in properties
        )
        ddl = f"CREATE {'UNIQUE ' if kind == 'UNIQUE' else ''}INDEX {made} ON {self.graph.nodes} n ({columns})"
        name = f"{label}_{'_'.join(properties)}_{'range' if kind == 'RANGE' else 'unique'}"
        listed = sql_text(to_json(list(properties)))
        return f"""DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    BEGIN EXECUTE IMMEDIATE {sql_text(ddl)}; EXCEPTION WHEN e_exists THEN NULL; END;
    DELETE FROM {self.graph.catalog} WHERE kind = '{kind}' AND label = {sql_text(label)} AND properties = {listed};
    INSERT INTO {self.graph.catalog} (kind, label, properties, name, object_name, details)
        VALUES ('{kind}', {sql_text(label)}, {listed}, {sql_text(name)}, '{made}', JSON('{{}}'));
EXCEPTION
    WHEN DUP_VAL_ON_INDEX THEN NULL;
END;"""

    def drop(self, kind: _Kind, label: str, properties: Sequence[str]) -> str:
        where = f"kind = '{kind}' AND label = {sql_text(label)} AND properties = {sql_text(to_json(list(properties)))}"
        return f"""DECLARE
    e_missing EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_missing, -1418);
BEGIN
    FOR it IN (SELECT object_name FROM {self.graph.catalog} WHERE {where}) LOOP
        BEGIN EXECUTE IMMEDIATE 'DROP INDEX ' || it.object_name; EXCEPTION WHEN e_missing THEN NULL; END;
    END LOOP;
    DELETE FROM {self.graph.catalog} WHERE {where};
END;"""

    def _record(
        self,
        kind: Literal["VECTOR", "FULLTEXT"],
        spec: VectorIndexSpec | FullTextIndexSpec,
        details: dict[str, Any],
        made: str | None = None,
    ) -> str:
        """Records an index: what it is over and how, and the table made for it, if one was."""
        listed = sql_text(to_json(list(spec.properties)))
        values = ", ".join(
            [
                f"'{kind}'",
                sql_text(spec.label),
                listed,
                sql_text(item_name(spec)),
                sql_text(made) if made else "NULL",
                f"JSON({sql_text(to_json(details))})",
            ]
        )
        return f"""BEGIN
    DELETE FROM {self.graph.catalog} WHERE kind = '{kind}' AND label = {sql_text(spec.label)} AND properties = {listed};
    INSERT INTO {self.graph.catalog} (kind, label, properties, name, object_name, details)
        VALUES ({values});
END;"""

    def _unrecord(self, kind: Literal["VECTOR", "FULLTEXT"], spec: VectorIndexSpec | FullTextIndexSpec) -> str:
        return (
            f"DELETE FROM {self.graph.catalog} WHERE kind = '{kind}' AND label = {sql_text(spec.label)} "
            f"AND properties = {sql_text(to_json(list(spec.properties)))}"
        )


def _number(value: Any) -> int | None:
    return None if value is None else int(value)
