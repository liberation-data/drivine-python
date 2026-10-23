# Written by scripts/unasync.py from tests/integration/stores.py:
# change that, and run the script again.
"""What a test asks of a store in the store's own language: Cypher on the graph engines, SQL on Postgres and Oracle."""

import json
from typing import Any

from drivine.sync import DatabaseType, PersistenceManager, QuerySpecification


def is_oracle(pm: PersistenceManager) -> bool:
    return pm.type is DatabaseType.ORACLE


def is_postgres(pm: PersistenceManager) -> bool:
    return pm.type is DatabaseType.POSTGRES


def equals(pm: PersistenceManager, prop: str, parameter: str) -> str:
    """A raw `where` that the node's property equals the parameter: `n.<prop> = $<parameter>` in Cypher."""
    if is_oracle(pm):
        return f"JSON_VALUE(n.props, '$.{prop}') = ${parameter}"
    if is_postgres(pm):
        return f"(n.props ->> '{prop}') = ${parameter}"
    return f"n.{prop} = ${parameter}"


def below(pm: PersistenceManager, prop: str, value: int) -> str:
    """A raw condition that the node's number property is below the value."""
    if is_oracle(pm):
        return f"JSON_VALUE(n.props, '$.{prop}' RETURNING NUMBER) < {value}"
    if is_postgres(pm):
        return f"CAST(n.props ->> '{prop}' AS numeric) < {value}"
    return f"n.{prop} < {value}"


def above(pm: PersistenceManager, prop: str, value: int) -> str:
    """A raw condition that the node's number property is above the value."""
    if is_oracle(pm):
        return f"JSON_VALUE(n.props, '$.{prop}' RETURNING NUMBER) > {value}"
    if is_postgres(pm):
        return f"CAST(n.props ->> '{prop}' AS numeric) > {value}"
    return f"n.{prop} > {value}"


def nodes(pm: PersistenceManager, label: str) -> list[dict[str, Any]]:
    """The properties of each node with the label, ordered by `uuid`, then `id`."""
    if is_oracle(pm):
        rows = pm.query(
            QuerySpecification[Any](
                # Re-encoded: the thin driver can't read a document updated in place (oracle/python-oracledb#622).
                f"SELECT JSON_TRANSFORM(n.props, KEEP '$') FROM drivine_node n "
                f"WHERE JSON_EXISTS(n.labels, '$?(@ == \"{label}\")')"
            )
        )
    elif is_postgres(pm):
        found = pm.query(
            QuerySpecification[Any](f"SELECT n.props FROM drivine_node n WHERE n.labels @> '[\"{label}\"]'")
        )
        rows = [it["props"] for it in found]
    else:
        rows = pm.query(QuerySpecification[Any](f"MATCH (n:{label}) RETURN properties(n)"))
    return sorted(rows, key=lambda it: (str(it.get("uuid")), str(it.get("id"))))


def labels(pm: PersistenceManager, uuid: str) -> list[str]:
    """The labels of the node whose uuid is given."""
    if is_oracle(pm):
        found = pm.get_one(
            QuerySpecification[Any](
                "SELECT JSON_TRANSFORM(n.labels, KEEP '$') FROM drivine_node n "
                "WHERE JSON_VALUE(n.props, '$.uuid') = $uuid"
            ).bind({"uuid": uuid})
        )
    elif is_postgres(pm):
        row = pm.get_one(
            QuerySpecification[Any]("SELECT n.labels FROM drivine_node n WHERE (n.props ->> 'uuid') = $uuid").bind(
                {"uuid": uuid}
            )
        )
        found = row["labels"]
    else:
        found = pm.get_one(QuerySpecification[Any]("MATCH (n {uuid: $uuid}) RETURN labels(n)").bind({"uuid": uuid}))
    return sorted(found)


def create(pm: PersistenceManager, label: str, *props: dict[str, Any]) -> None:
    """Makes a node with the label for each map of properties, outside any object manager."""
    for it in props:
        if is_oracle(pm):
            pm.execute(
                QuerySpecification[Any](
                    "INSERT INTO drivine_node (labels, props) VALUES (JSON($labels), JSON($props))"
                ).bind({"labels": json.dumps([label]), "props": json.dumps(it)})
            )
        elif is_postgres(pm):
            pm.execute(
                QuerySpecification[Any](
                    "INSERT INTO drivine_node (labels, props) VALUES (CAST($labels AS jsonb), CAST($props AS jsonb))"
                ).bind({"labels": json.dumps([label]), "props": json.dumps(it)})
            )
        else:
            pm.execute(QuerySpecification[Any](f"CREATE (n:{label}) SET n = $props").bind({"props": it}))


def set_property(pm: PersistenceManager, label: str, prop: str, value: Any) -> None:
    """Sets a property of every node with the label, outside any object manager."""
    if is_oracle(pm):
        pm.execute(
            QuerySpecification[Any](
                f"UPDATE drivine_node n SET n.props = JSON_TRANSFORM(n.props, SET '$.{prop}' = $value) "
                f"WHERE JSON_EXISTS(n.labels, '$?(@ == \"{label}\")')"
            ).bind({"value": value})
        )
    elif is_postgres(pm):
        pm.execute(
            QuerySpecification[Any](
                f"UPDATE drivine_node n SET props = jsonb_set(n.props, '{{{prop}}}', CAST($value AS jsonb)) "
                f"WHERE n.labels @> '[\"{label}\"]'"
            ).bind({"value": json.dumps(value)})
        )
    else:
        pm.execute(QuerySpecification[Any](f"MATCH (n:{label}) SET n.{prop} = $value").bind({"value": value}))


def relate(pm: PersistenceManager, src: str, type: str, dst: str) -> None:
    """Relates the node whose `id` is `src` to the one whose `id` is `dst`, outside any object manager."""
    bindings = {"a": src, "b": dst, "type": type}
    if is_oracle(pm):
        pm.execute(
            QuerySpecification[Any](
                "INSERT INTO drivine_edge (src, dst, type, props) SELECT a.nid, b.nid, $type, JSON('{}') "
                "FROM drivine_node a, drivine_node b WHERE JSON_VALUE(a.props, '$.id') = $a "
                "AND JSON_VALUE(b.props, '$.id') = $b"
            ).bind(bindings)
        )
    elif is_postgres(pm):
        pm.execute(
            QuerySpecification[Any](
                "INSERT INTO drivine_edge (src, dst, type, props) SELECT a.nid, b.nid, CAST($type AS text), "
                "CAST('{}' AS jsonb) FROM drivine_node a, drivine_node b WHERE (a.props ->> 'id') = CAST($a AS text) "
                "AND (b.props ->> 'id') = CAST($b AS text)"
            ).bind(bindings)
        )
    else:
        pm.execute(
            QuerySpecification[Any](f"MATCH (a {{id: $a}}), (b {{id: $b}}) CREATE (a)-[:{type}]->(b)").bind(bindings)
        )


def node_ids(pm: PersistenceManager) -> list[str]:
    """The `id` of every node, in order."""
    if is_oracle(pm):
        rows = pm.query(QuerySpecification[Any]("SELECT JSON_VALUE(n.props, '$.id') FROM drivine_node n"))
    elif is_postgres(pm):
        found = pm.query(QuerySpecification[Any]("SELECT n.props ->> 'id' AS id FROM drivine_node n"))
        rows = [it["id"] for it in found]
    else:
        rows = pm.query(QuerySpecification[Any]("MATCH (n) RETURN n.id"))
    return sorted(rows)
