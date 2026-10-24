from __future__ import annotations

from collections.abc import Sequence

from drivine.backend.sql.sql_graph import DEFAULT_GRAPH, SqlGraph, sql_text
from drivine.model import stamps


class OracleGraph(SqlGraph):
    """
    A graph as Oracle keeps it: the tables of a SqlGraph, and the SQL property graph over the two, which view reads go
    through.
    """

    def __init__(self, name: str = DEFAULT_GRAPH) -> None:
        super().__init__(name, "Oracle", 128)

    @property
    def ddl(self) -> list[str]:
        """What makes the store. Each statement may run again."""
        return [
            f"""CREATE TABLE IF NOT EXISTS {self.nodes} (
                nid    NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                labels JSON NOT NULL,
                props  JSON NOT NULL,
                stamp  VARCHAR2(33)
            )""",
            f"""CREATE TABLE IF NOT EXISTS {self.edges} (
                eid   NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                src   NUMBER NOT NULL REFERENCES {self.nodes} (nid) ON DELETE CASCADE,
                dst   NUMBER NOT NULL REFERENCES {self.nodes} (nid) ON DELETE CASCADE,
                type  VARCHAR2(128) NOT NULL,
                props JSON DEFAULT '{{}}' NOT NULL
            )""",
            f"""CREATE TABLE IF NOT EXISTS {self.catalog} (
                kind        VARCHAR2(40) NOT NULL,
                label       VARCHAR2(400) NOT NULL,
                properties  VARCHAR2(2000) NOT NULL,
                name        VARCHAR2(400),
                object_name VARCHAR2(128),
                details     JSON NOT NULL,
                PRIMARY KEY (kind, label, properties)
            )""",
            f"CREATE INDEX IF NOT EXISTS {self.name}_edge_src ON {self.edges} (src, type)",
            f"CREATE INDEX IF NOT EXISTS {self.name}_edge_dst ON {self.edges} (dst, type)",
            f"CREATE MULTIVALUE INDEX IF NOT EXISTS {self.name}_node_labels ON {self.nodes} n (n.labels.string())",
            f"""CREATE OR REPLACE PROPERTY GRAPH {self.name}
                VERTEX TABLES ({self.nodes} KEY (nid) LABEL node PROPERTIES (nid, labels, props, stamp))
                EDGE TABLES ({self.edges} KEY (eid)
                    SOURCE KEY (src) REFERENCES {self.nodes} (nid)
                    DESTINATION KEY (dst) REFERENCES {self.nodes} (nid)
                    LABEL edge PROPERTIES (eid, src, dst, type, props))""",
        ]

    @property
    def ensuring(self) -> str:
        """
        The block that makes the store when it is not there and does nothing when it is: the property graph is made
        last, so its presence says the rest is. One another process made meanwhile (ORA-00955) is taken as made.
        """
        statements = "\n".join(
            f"    BEGIN EXECUTE IMMEDIATE {sql_text(it)}; EXCEPTION WHEN e_exists THEN NULL; END;" for it in self.ddl
        )
        return f"""DECLARE
    v_made NUMBER;
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    SELECT COUNT(*) INTO v_made FROM user_property_graphs WHERE graph_name = '{self.name.upper()}';
    IF v_made = 0 THEN
{statements}
    END IF;
END;"""


def json_key(name: str) -> str:
    """A JSON path step naming a key, quoted: `."metadata.source"`."""
    return '."' + _escaped(name) + '"'


def holding(alias: str, property: str) -> str:
    """
    The JSON the property of the node row `alias` is read from: its properties, or, for the stamp, which is a column
    of its own, a document of that column under the stamp's property, so a query reads it as any other.
    """
    if property == stamps.PROPERTY:
        return f"JSON_OBJECT({sql_text(property)} VALUE {alias}.stamp ABSENT ON NULL RETURNING JSON)"
    return f"{alias}.props"


def node_document(alias: str) -> str:
    """The map a load returns for the node row `alias`: its properties, its stamp among them, and its labels apart."""
    return (
        f"JSON_OBJECT('props' VALUE JSON_TRANSFORM({alias}.props, SET '${json_key(stamps.PROPERTY)}' = {alias}.stamp "
        f"IGNORE ON NULL), 'labels' VALUE {alias}.labels RETURNING JSON)"
    )


def has_labels(alias: str, labels: Sequence[str]) -> str:
    """
    A predicate that holds when the node row `alias` carries every one of the labels. A label may come from data (a
    NodeLabels filter's value, a search's partition label): it is escaped as `json_key` escapes a name, the backslash
    first, for the JSON string the path or the labels' text holds it in, and then the SQL literal around it.

    The first label is tested so the multivalue index over labels can answer it, and the rest by the labels' text,
    which that index can't: when two tests of the labels are answered by the index, Oracle (23.26) finds no node the
    open transaction has written and not yet committed (drivine#221). drivine4j's `OracleSql.hasLabels` writes the same.
    """
    return " AND ".join(
        f"JSON_EXISTS({alias}.labels, '$?(@ == \"{_escaped(it)}\")')"
        if i == 0
        else f"INSTR(JSON_SERIALIZE({alias}.labels), '\"{_escaped(it)}\"') > 0"
        for i, it in enumerate(labels)
    )


def labelled_value(alias: str, label: str, property: str) -> str:
    """
    A property of the node row `alias` as a range index or a uniqueness constraint over the label holds it: as text,
    for the label's nodes alone, and null for every other node, for a value that isn't a scalar, and for one longer
    than 4,000 bytes. A predicate over this expression implies the label.
    """
    return (
        f"CASE WHEN {has_labels(alias, [label])} THEN JSON_VALUE({alias}.props, '${json_key(property)}' "
        "RETURNING VARCHAR2(4000) NULL ON ERROR) END"
    )


def _escaped(text: str) -> str:
    """
    Text as JSON writes it inside a string, in an SQL literal: backslashes and quotes escaped, then single quotes
    doubled. A label or a key is held so in a JSON path's string and in the labels' text alike.
    """
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("'", "''")


def related_subquery(*hints: str) -> str:
    """
    The SELECT of a subquery over related nodes, with the hint every one carries and any more given. Oracle 23
    coalesces two such subqueries ORed together into one, and rewrites the second's label test as a multivalue index
    lookup that, evaluated as a filter, misses nodes with more than one label: of two roots, one related to each
    subclass, `instance_of(A) | instance_of(B)` found one. NO_COALESCE_SQ prevents it.
    """
    return f"SELECT /*+ {' '.join(['NO_COALESCE_SQ', *hints])} */ 1"
