from __future__ import annotations

import re
from collections.abc import Sequence

from drivine.drivine_error import DrivineError
from drivine.model import stamps

DEFAULT_GRAPH = "drivine"
"""The graph a manager on Oracle works on when none is named."""

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,113}$")
"""
A graph's name: the longest leaves room for the longest name it gives an object, a table beside the nodes (`_v__` and
a hash, with nothing readable) and the trigger beside that, within Oracle's 128. drivine4j's rule.
"""


class OracleGraph:
    """
    A graph as Oracle keeps it (drivine4j's layout, so a store written from the JVM, Node or Python is one): a table of
    nodes, each with its labels (a JSON array), its properties (a JSON object keyed by stored name) and its stamp; a
    table of relationships, from `src` to `dst`, with a type and JSON properties; and the SQL property graph over the
    two. A node's relationships go with it: the relationship table cascades.
    """

    def __init__(self, name: str = DEFAULT_GRAPH) -> None:
        if not _NAME.match(name):
            raise DrivineError(
                f"'{name}' can't name a graph on Oracle: it names tables, so it is letters, digits and underscores, "
                "beginning with a letter, at most 114."
            )
        self.name = name
        self.nodes = f"{name}_node"
        self.edges = f"{name}_edge"
        self.catalog = f"{name}_schema"
        """What Drivine made of indexes and constraints."""

    def vectors(self, label: str, property: str) -> str:
        """The table that holds the embeddings a vector index on the label's property is over."""
        return self.object_name("v", [label, property])

    def texts(self, label: str, properties: Sequence[str]) -> str:
        """The table that holds the text a full-text index on the label's properties is over."""
        return self.object_name("t", [label, *properties])

    def object_name(self, kind: str, parts: Sequence[str]) -> str:
        """
        A name for an object of `kind` made for `parts`, as drivine4j names it, so the JVM, Node and Python make one:
        the graph's name, the kind, what of the parts reads as a name, and a hash of the parts (Java's String.hashCode),
        leaving room for the `_t` or `_i` an object made beside it adds within Oracle's 128.
        """
        hashed = format(_java_hash("\u0000".join(parts)) & 0xFFFFFFFF, "x")
        fixed = len(f"{self.name}_{kind}__{hashed}") + 2
        room = min(max(128 - fixed, 0), 40)
        readable = "".join(re.sub(r"[^A-Za-z0-9]", "", it) for it in parts)[:room]
        return f"{self.name}_{kind}_{readable}_{hashed}"

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


def sql_text(text: str) -> str:
    """A string as an SQL literal."""
    return "'" + text.replace("'", "''") + "'"


def json_key(name: str) -> str:
    """A JSON path step naming a key, quoted: `."metadata.source"`."""
    return '."' + name.replace("\\", "\\\\").replace('"', '\\"').replace("'", "''") + '"'


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
    first, for the JSON path's string and then the SQL literal around it.
    """
    return " AND ".join(f"JSON_EXISTS({alias}.labels, '$?(@ == \"{_escaped(it)}\")')" for it in labels)


def _escaped(text: str) -> str:
    """Text for a JSON path string inside an SQL literal: backslashes and quotes escaped, then single quotes doubled."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("'", "''")


def _java_hash(text: str) -> int:
    """Java's String.hashCode, over UTF-16 code units, as a 32-bit int."""
    units = text.encode("utf-16-le")
    hashed = 0
    for i in range(0, len(units), 2):
        hashed = (31 * hashed + int.from_bytes(units[i : i + 2], "little")) & 0xFFFFFFFF
    return hashed - (1 << 32) if hashed >= 1 << 31 else hashed


def related_subquery(*hints: str) -> str:
    """
    The SELECT of a subquery over related nodes, with the hint every one carries and any more given. Oracle 23
    coalesces two such subqueries ORed together into one, and rewrites the second's label test as a multivalue index
    lookup that, evaluated as a filter, misses nodes with more than one label: of two roots, one related to each
    subclass, `instance_of(A) | instance_of(B)` found one. NO_COALESCE_SQ prevents it.
    """
    return f"SELECT /*+ {' '.join(['NO_COALESCE_SQ', *hints])} */ 1"
