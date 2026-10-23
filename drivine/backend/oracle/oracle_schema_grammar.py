# Rows the catalog gives are untyped JSON documents: they are read here, field by field.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import re
from typing import Any

from drivine.backend.oracle.oracle_graph import OracleGraph, has_labels, json_key, labelled_value, sql_text
from drivine.backend.oracle.oracle_side_table import OracleSideTable
from drivine.backend.sql.sql_catalog import CATALOG_KINDS, CatalogKind, catalog_properties, catalog_record
from drivine.schema.full_text_index_spec import FullTextIndexSpec
from drivine.schema.range_index_spec import RangeIndexSpec
from drivine.schema.schema_grammar import SchemaGrammar, portable_pins, sampled_node_id
from drivine.schema.schema_item import SchemaItem
from drivine.schema.schema_listing import SchemaListing
from drivine.schema.schema_specs import item_name, range_index
from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec
from drivine.schema.vector_index_spec import VectorIndexSpec

_Spec = RangeIndexSpec | UniquenessConstraintSpec | VectorIndexSpec | FullTextIndexSpec

_EARLIER: dict[CatalogKind, str] = {"RANGE": "RANGE", "UNIQUE": "UNIQUE", "VECTOR": "VECTOR", "FULLTEXT": "FULLTEXT"}
"""
What this library recorded each kind as before it recorded drivine4j's: Drivine's own name for it, with the objects
named otherwise. These are read beside drivine4j's kinds and never written, so a store written so is still understood:
listed, searched through, and dropped as it was made.
"""


def recorded_as(kind: CatalogKind) -> list[str]:
    """The kinds a record of `kind` is read as."""
    return [CATALOG_KINDS[kind], _EARLIER[kind]]


_LABELLED_THERE = (
    "EXISTS (SELECT 1 FROM user_indexes l WHERE l.index_name = UPPER(JSON_VALUE(c.details, '$.labelled')))"
)
"""Whether the labelled index the catalog's record `c` names is there."""

_UNIQUE_THERE = (
    f"u.kind IN ({', '.join(sql_text(it) for it in recorded_as('UNIQUE'))}) "
    "AND EXISTS (SELECT 1 FROM user_indexes q WHERE q.index_name = UPPER(u.object_name))"
)
"""
Whether the catalog's record `u` is of a uniqueness constraint whose index is there, which stands for a labelled one.
"""

_PLAIN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _drivine4j_step(key: str) -> str:
    """
    The step of a JSON path naming `key` as drivine4j writes it: bare where SQL would take it for an identifier, else
    quoted as `json_key` quotes it, for the SQL literal it is written in.
    """
    return key if _PLAIN_KEY.match(key) and key not in ("true", "false", "null") else json_key(key)[1:]


def _made(ddl: str) -> str:
    """A block that runs `ddl`, an object another session made meanwhile (ORA-00955) taken as made."""
    return f"""DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE {sql_text(ddl)};
EXCEPTION
    WHEN e_exists THEN NULL;
END;"""


def oracle_schema_grammar(graph: OracleGraph) -> SchemaGrammar:
    """
    Indexes and constraints on Oracle, made and recorded as drivine4j makes and records them, so a store's schema is
    one whichever library made it. A property is a path into the JSON column `props`, so each index is over an
    expression:

    - a range index is two indexes. One reads the properties as text whatever the label, so two labels that index a
      property of one name share it. The other, the labelled index, reads the properties of the label's nodes and is
      null for every other node (see `labelled_value`), which an index holds none of: a filter on the label's nodes is
      read through it, so the one index answers both the label and the property;
    - a uniqueness constraint is a unique index of the labelled index's expression. So it stands for one: the range
      index it comes with is recorded as served by it, and has no labelled index of its own (Oracle refuses a second
      index over the same expressions).

    A vector index keeps the label's embeddings in a table beside the nodes (Oracle indexes a VECTOR column, and an
    embedding is a list among a node's JSON properties), with a vector index over it by neighbour partitions, which
    have no HNSW parameters. A full-text index, where the database has Oracle Text, keeps the text in a table beside
    the nodes with a JSON search index over it; without Oracle Text it is only recorded, and a search reads every node.
    Oracle's dictionary holds the expressions and not what they were made for, so each item is recorded in the graph's
    catalog with the object made for it, and read back from there.
    """
    grammar = _OracleSchema(graph)
    return SchemaGrammar(
        engine="Oracle",
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
        unsupported_vector_tuning=portable_pins,
        conflicts=grammar.conflicts,
        labelled_indexes=True,
    )


class _OracleSchema:
    def __init__(self, graph: OracleGraph) -> None:
        self.graph = graph

    def create_range(self, spec: RangeIndexSpec) -> list[str]:
        """
        A range index: the index over the properties of every node, and the labelled index, unless a uniqueness
        constraint's stands for it. It is recorded with the labelled index's name among its details, and a record there
        already takes them, so one listed as incomplete is made whole.
        """
        shared, index = self._shared_index(spec)
        labelled = self.graph.object_name("lx", [spec.label, *spec.properties])
        columns = ", ".join(labelled_value("n", spec.label, it) for it in spec.properties)
        ddl = f"CREATE INDEX IF NOT EXISTS {labelled} ON {self.graph.nodes} n ({columns})"
        return [
            index,
            f"""DECLARE
    v_unique NUMBER;
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    SELECT COUNT(*) INTO v_unique FROM {self.graph.catalog} u WHERE u.label = {sql_text(spec.label)} """
            f"""AND u.properties = {sql_text(catalog_properties(spec.properties))} AND {_UNIQUE_THERE};
    IF v_unique = 0 THEN
        EXECUTE IMMEDIATE {sql_text(ddl)};
    END IF;
EXCEPTION
    WHEN e_exists THEN NULL;
END;""",
            self._recorded(spec, shared, f'{{"labelled": "{labelled}"}}', updated=True),
        ]

    def create_unique(self, spec: UniquenessConstraintSpec) -> list[str]:
        """
        A uniqueness constraint, with the range index a node is found by, which it serves: the range index is recorded
        as served by the unique index, and a labelled index made for it before is dropped first, as Oracle would refuse
        the unique index over the same expressions (ORA-01408). Data the constraint refuses stops the statements at the
        unique index, and leaves the range index incomplete, to be made whole by an `ensure` of it.

        drivine4j's unique index over several properties is of another expression than its labelled index, and it
        makes both; this library's is the labelled index's expression whatever the number of properties, so it stands
        for one.
        """
        name = self.graph.object_name("uq", [spec.label, *spec.properties])
        columns = ", ".join(labelled_value("n", spec.label, it) for it in spec.properties)
        served_range = range_index(spec.label, spec.properties)
        shared, index = self._shared_index(served_range)
        served = f'{{"unique": "{name}"}}'
        return [
            index,
            self._recorded(served_range, shared, served),
            self._served_by(served_range, served),
            _made(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {self.graph.nodes} n ({columns})"),
            self._recorded(spec, name),
        ]

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
            self._recorded(spec, side.name, f'{{"dimensions": {spec.dimensions}, "similarity": "{spec.similarity}"}}'),
        ]

    def create_full_text(self, spec: FullTextIndexSpec) -> list[str]:
        """
        Not over the node table: once an Oracle Text index is there, Oracle answers ordinary tests of a JSON property
        from it, and it holds only what has been committed, so a save would not find a node it made a moment before
        (drivine4j's finding). Without Oracle Text (ORA-29833) nothing is made, and the index is recorded with no
        object.
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
                {self._recorded(spec, None).replace(chr(10), chr(10) + " " * 16)}
                RETURN;
        END;
    END IF;
    EXECUTE IMMEDIATE {sql_text(side.trigger_ddl)};
    EXECUTE IMMEDIATE {sql_text(side.backfill)};
    {self._recorded(spec, side.name).replace(chr(10), chr(10) + " " * 4)}
END;"""
        ]

    def conflicts(self, spec: UniquenessConstraintSpec, limit: int) -> str:
        """
        Each node of a key that repeats is numbered within its key, and counted with it, so that a row names `limit` of
        its nodes at most however many hold the key. A node's own id is its `nid`. The nodes are aggregated as JSON,
        since as text they would be held to 4000 bytes, and a large sample overflows that (ORA-40478).
        """

        def value(prop: str) -> str:
            return f"JSON_VALUE(n.props, '${json_key(prop)}')"

        values = [value(it) for it in spec.properties]
        properties = ", ".join(f"{sql_text(it)} VALUE k{k}" for k, it in enumerate(spec.properties))
        node_id = sampled_node_id(spec)
        node = ", ".join(["'elementId' VALUE nid", *([f"{sql_text(node_id)} VALUE i"] if node_id else [])])
        selected = ", ".join(
            [*(f"{it} AS k{k}" for k, it in enumerate(values)), *([f"{value(node_id)} AS i"] if node_id else [])]
        )
        partition = ", ".join(values)
        return (
            f"SELECT JSON_OBJECT('properties' VALUE JSON_OBJECT({properties}), 'count' VALUE MAX(c), 'nodes' VALUE "
            f'JSON_ARRAYAGG(JSON_OBJECT({node}) ORDER BY nid RETURNING JSON) RETURNING JSON) AS "value" '
            f"FROM (SELECT n.nid, {selected}, COUNT(*) OVER (PARTITION BY {partition}) AS c, "
            f"ROW_NUMBER() OVER (PARTITION BY {partition} ORDER BY n.nid) AS r "
            f"FROM {self.graph.nodes} n "
            f"WHERE {has_labels('n', [spec.label])} AND {' AND '.join(f'{it} IS NOT NULL' for it in values)}) "
            f"WHERE c > 1 AND r <= {limit} GROUP BY {', '.join(f'k{k}' for k in range(len(values)))} "
            f"FETCH FIRST {limit} ROWS ONLY"
        )

    def _shared_index(self, spec: RangeIndexSpec) -> tuple[str, str]:
        """
        The index over the properties of every node, whatever the label: its name, and the statement that makes it. Its
        paths are drivine4j's, so it is one index whichever library makes it, and not those this library reads a
        property by (`'$."p"'`): Oracle would then read the property through this index's column inside a filter on
        the labelled index's expression, which it then no longer matches to the labelled index, and reads every node.
        """
        name = self.graph.object_name("ix", spec.properties)
        columns = ", ".join(f"JSON_VALUE(props, '$.{_drivine4j_step(it)}')" for it in spec.properties)
        return name, _made(f"CREATE INDEX IF NOT EXISTS {name} ON {self.graph.nodes} ({columns})")

    def listing(self, kind: CatalogKind) -> SchemaListing:
        """
        What is recorded of the kind, and still there: an index or a table beside the nodes dropped by hand is not
        listed, so that it is made again when asked for. A range index with neither its labelled index nor a uniqueness
        constraint's to stand for it is listed as incomplete, so that `ensure` makes the rest; an earlier record's
        object is its labelled index, so it is whole while it is there.
        """
        incomplete = (
            f"CASE WHEN c.kind = {sql_text(CATALOG_KINDS['RANGE'])} AND NOT {_LABELLED_THERE} "
            f"AND NOT EXISTS (SELECT 1 FROM {self.graph.catalog} u WHERE u.label = c.label "
            f"AND u.properties = c.properties AND {_UNIQUE_THERE}) THEN 'true' END"
        )
        kinds = ", ".join(sql_text(it) for it in recorded_as(kind))
        return SchemaListing(
            "SELECT JSON_OBJECT('label' VALUE c.label, 'properties' VALUE JSON(c.properties), 'name' VALUE c.name, "
            f"'details' VALUE c.details, 'incomplete' VALUE {incomplete} RETURNING JSON) AS \"item\" "
            f"FROM {self.graph.catalog} c WHERE c.kind IN ({kinds}) "
            "AND (c.object_name IS NULL "
            "OR EXISTS (SELECT 1 FROM user_indexes i WHERE i.index_name = UPPER(c.object_name)) "
            "OR EXISTS (SELECT 1 FROM user_tables t WHERE t.table_name = UPPER(c.object_name))) "
            "ORDER BY c.label, c.properties",
            _items,
        )

    def _recorded(self, spec: _Spec, object_name: str | None, details: str = "{}", updated: bool = False) -> str:
        """
        Records the item as made, as `object_name` (none for one nothing was made for), unless it is recorded already;
        with `updated`, a record there already takes `details`. A MERGE doesn't read again before it inserts, so of two
        sessions recording it at once the second is refused on the catalog's key: that refusal is taken as recorded.
        """
        key = (
            f"SELECT {sql_text(CATALOG_KINDS[spec.kind])} AS kind, {sql_text(spec.label)} AS label, "
            f"{sql_text(catalog_properties(spec.properties))} AS properties FROM dual"
        )
        values = ", ".join(
            [sql_text(item_name(spec)), sql_text(object_name) if object_name else "NULL", sql_text(details)]
        )
        matched = f"\n    WHEN MATCHED THEN UPDATE SET c.details = {sql_text(details)}" if updated else ""
        return f"""BEGIN
    MERGE INTO {self.graph.catalog} c
    USING ({key}) k
    ON (c.kind = k.kind AND c.label = k.label AND c.properties = k.properties){matched}
    WHEN NOT MATCHED THEN INSERT (kind, label, properties, name, object_name, details)
        VALUES (k.kind, k.label, k.properties, {values});
EXCEPTION
    WHEN DUP_VAL_ON_INDEX THEN NULL;
END;"""

    def _served_by(self, spec: RangeIndexSpec, details: str) -> str:
        """
        Records the range index as served by a uniqueness constraint's index, `details`, and drops the labelled index it
        was made with, when it was: the one its record names, and an earlier record's, which the record now made stands
        for. The index is dropped first: a drop that fails leaves the record naming it, for the next constraint made to
        find and drop.
        """
        record = catalog_record(spec.kind, spec.label, spec.properties)
        earlier = catalog_record(spec.kind, spec.label, spec.properties, [_EARLIER["RANGE"]])
        return f"""DECLARE
    v_labelled VARCHAR2(128);
BEGIN
    SELECT MAX(JSON_VALUE(details, '$.labelled')) INTO v_labelled FROM {self.graph.catalog} WHERE {record};
    IF v_labelled IS NOT NULL THEN
        EXECUTE IMMEDIATE 'DROP INDEX IF EXISTS ' || v_labelled;
    END IF;
    FOR it IN (SELECT object_name FROM {self.graph.catalog} WHERE {earlier}) LOOP
        EXECUTE IMMEDIATE 'DROP INDEX IF EXISTS ' || it.object_name;
    END LOOP;
    DELETE FROM {self.graph.catalog} WHERE {earlier};
    UPDATE {self.graph.catalog} SET details = {sql_text(details)} WHERE {record};
    COMMIT;
END;"""

    def forgotten(self, spec: _Spec) -> str:
        """
        Forgets the item, recorded as drivine4j records it or as this library did before, and drops what each record was
        made as when nothing else recorded uses it: an index, or for a vector or a full-text index the table beside the
        nodes with its trigger, which takes the index over it along. What is dropped is the object the record names, so
        an item is dropped as it was made whatever a later version would call it. A range index's labelled index is its
        own, and is dropped with the record. Dropped first and forgotten last: a drop that fails leaves the record
        naming what is still there, for the next drop to find.
        """
        record = catalog_record(spec.kind, spec.label, spec.properties, recorded_as(spec.kind))
        drops = (
            OracleSideTable.dropping("it.object_name")
            if spec.kind in ("VECTOR", "FULLTEXT")
            else ["'DROP INDEX IF EXISTS ' || it.object_name"]
        )
        labelled = (
            """
        IF it.labelled IS NOT NULL THEN
            EXECUTE IMMEDIATE 'DROP INDEX IF EXISTS ' || it.labelled;
        END IF;"""
            if spec.kind == "RANGE"
            else ""
        )
        dropped = "\n            ".join(f"EXECUTE IMMEDIATE {it};" for it in drops)
        rows = (
            "SELECT object_name, JSON_VALUE(details, '$.labelled') AS labelled "
            f"FROM {self.graph.catalog} WHERE {record}"
        )
        return f"""DECLARE
    v_others NUMBER;
BEGIN
    FOR it IN ({rows}) LOOP
        SELECT COUNT(*) INTO v_others FROM {self.graph.catalog}
            WHERE UPPER(object_name) = UPPER(it.object_name) AND NOT ({record});{labelled}
        IF it.object_name IS NOT NULL AND v_others = 0 THEN
            {dropped}
        END IF;
    END LOOP;
    DELETE FROM {self.graph.catalog} WHERE {record};
    COMMIT;
END;"""


def _items(rows: list[Any]) -> list[SchemaItem]:
    return [
        SchemaItem(
            it["label"],
            tuple(it["properties"]),
            it["name"],
            dimensions=_number((it.get("details") or {}).get("dimensions")),
            similarity=(it.get("details") or {}).get("similarity"),
            incomplete=it.get("incomplete") == "true",
        )
        for it in rows
    ]


def _number(value: Any) -> int | None:
    return None if value is None else int(value)
