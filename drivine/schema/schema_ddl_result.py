"""
What `schema_ddl.translate` makes of a statement: Neo4j's schema DDL as Drivine's schema specs, a refusal, or not DDL
at all. drivine4j's `SchemaDdlResult`, ported.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from drivine.schema.schema_specs import SchemaSpec, item_name
from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec


@dataclass(frozen=True)
class TextIndexSpec:
    """
    Neo4j's TEXT index, over one property of a label's nodes, as `CREATE TEXT INDEX` declares it. Only Neo4j makes one:
    the schema API has no TEXT index, and a SQL store refuses it when the DDL is applied. For tokenized search declare
    a full-text index; for exact lookups, a range index.
    """

    label: str
    properties: tuple[str]
    kind: Literal["TEXT"] = "TEXT"


DdlSpec: TypeAlias = SchemaSpec | TextIndexSpec
"""A spec that DDL declares: one of the schema API's, or Neo4j's TEXT index."""


def default_name(spec: DdlSpec) -> str:
    """The name the schema API gives the spec: see `item_name`. A TEXT index is `<label>_<property>_text`."""
    if isinstance(spec, TextIndexSpec):
        return f"{spec.label}_{'_'.join(spec.properties)}_text"
    return item_name(spec)


@dataclass(frozen=True)
class NotDdl:
    """The statement is not schema DDL: Cypher, or anything else, to be run or compiled as it is."""


NOT_DDL = NotDdl()


@dataclass(frozen=True)
class Create:
    """
    `CREATE … INDEX` or `CREATE CONSTRAINT`, as `spec`. The spec is unnamed, so the name the schema API gives it,
    `default_name`, is the name it is made under. A name the DDL gave that differs from that one is `alias`, kept beside
    the item so that a `DROP` of it finds the item.
    """

    spec: DdlSpec
    alias: str | None
    if_not_exists: bool


@dataclass(frozen=True)
class Drop:
    """`DROP INDEX` or `DROP CONSTRAINT` of the item `name` names: by the schema API's name, or by an alias."""

    kind: Literal["INDEX", "CONSTRAINT"]
    name: str
    if_exists: bool


@dataclass(frozen=True)
class AwaitIndexes:
    """
    `CALL db.awaitIndexes(…)` or `CALL db.awaitIndex(…)`: an index on a SQL store is made whole as it is created, so
    nothing is waited for.
    """


AWAIT_INDEXES = AwaitIndexes()


@dataclass(frozen=True)
class Reserved:
    """
    A uniqueness constraint on one of Drivine's own nodes, the schema marker's `scope` or the store stamp's `key`, by
    any name: ensured as Drivine ensures it for itself, and recorded in no owner's inventory.
    """

    spec: UniquenessConstraintSpec


@dataclass(frozen=True)
class Refused:
    """Schema DDL that a SQL store cannot honour, or that is not well formed, and why."""

    message: str


SchemaDdlResult: TypeAlias = NotDdl | Create | Drop | AwaitIndexes | Reserved | Refused
