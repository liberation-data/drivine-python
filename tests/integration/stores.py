"""What a test asks of a store in the store's own language: Cypher on the graph engines, SQL on Oracle."""

import json
from typing import Any

from drivine import DatabaseType, PersistenceManager, QuerySpecification


def is_oracle(pm: PersistenceManager) -> bool:
    return pm.type is DatabaseType.ORACLE


def equals(pm: PersistenceManager, prop: str, parameter: str) -> str:
    """A raw `where` that the node's property equals the parameter: `n.<prop> = $<parameter>` in Cypher."""
    if is_oracle(pm):
        return f"JSON_VALUE(n.props, '$.{prop}') = ${parameter}"
    return f"n.{prop} = ${parameter}"


def below(pm: PersistenceManager, prop: str, value: int) -> str:
    """A raw condition that the node's number property is below the value."""
    if is_oracle(pm):
        return f"JSON_VALUE(n.props, '$.{prop}' RETURNING NUMBER) < {value}"
    return f"n.{prop} < {value}"


def above(pm: PersistenceManager, prop: str, value: int) -> str:
    """A raw condition that the node's number property is above the value."""
    if is_oracle(pm):
        return f"JSON_VALUE(n.props, '$.{prop}' RETURNING NUMBER) > {value}"
    return f"n.{prop} > {value}"


async def nodes(pm: PersistenceManager, label: str) -> list[dict[str, Any]]:
    """The properties of each node with the label, ordered by `uuid`, then `id`."""
    if is_oracle(pm):
        rows = await pm.query(
            QuerySpecification[Any](
                # Re-encoded: the thin driver can't read a document updated in place (oracle/python-oracledb#622).
                f"SELECT JSON_TRANSFORM(n.props, KEEP '$') FROM drivine_node n "
                f"WHERE JSON_EXISTS(n.labels, '$?(@ == \"{label}\")')"
            )
        )
    else:
        rows = await pm.query(QuerySpecification[Any](f"MATCH (n:{label}) RETURN properties(n)"))
    return sorted(rows, key=lambda it: (str(it.get("uuid")), str(it.get("id"))))


async def labels(pm: PersistenceManager, uuid: str) -> list[str]:
    """The labels of the node whose uuid is given."""
    if is_oracle(pm):
        found = await pm.get_one(
            QuerySpecification[Any](
                "SELECT JSON_TRANSFORM(n.labels, KEEP '$') FROM drivine_node n "
                "WHERE JSON_VALUE(n.props, '$.uuid') = $uuid"
            ).bind({"uuid": uuid})
        )
    else:
        found = await pm.get_one(
            QuerySpecification[Any]("MATCH (n {uuid: $uuid}) RETURN labels(n)").bind({"uuid": uuid})
        )
    return sorted(found)


async def create(pm: PersistenceManager, label: str, *props: dict[str, Any]) -> None:
    """Makes a node with the label for each map of properties, outside any object manager."""
    for it in props:
        if is_oracle(pm):
            await pm.execute(
                QuerySpecification[Any](
                    "INSERT INTO drivine_node (labels, props) VALUES (JSON($labels), JSON($props))"
                ).bind({"labels": json.dumps([label]), "props": json.dumps(it)})
            )
        else:
            await pm.execute(QuerySpecification[Any](f"CREATE (n:{label}) SET n = $props").bind({"props": it}))


async def set_property(pm: PersistenceManager, label: str, prop: str, value: Any) -> None:
    """Sets a property of every node with the label, outside any object manager."""
    if is_oracle(pm):
        await pm.execute(
            QuerySpecification[Any](
                f"UPDATE drivine_node n SET n.props = JSON_TRANSFORM(n.props, SET '$.{prop}' = $value) "
                f"WHERE JSON_EXISTS(n.labels, '$?(@ == \"{label}\")')"
            ).bind({"value": value})
        )
    else:
        await pm.execute(QuerySpecification[Any](f"MATCH (n:{label}) SET n.{prop} = $value").bind({"value": value}))
