from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, cast

from drivine.backend.sql.sql_graph import sql_text

CatalogKind = Literal["RANGE", "UNIQUE", "VECTOR", "FULLTEXT"]

CATALOG_KINDS: dict[CatalogKind, str] = {
    "RANGE": "RANGE_INDEX",
    "UNIQUE": "UNIQUENESS_CONSTRAINT",
    "VECTOR": "VECTOR_INDEX",
    "FULLTEXT": "FULLTEXT_INDEX",
}
"""
What an item is recorded as in a graph's catalog (`<name>_schema`): drivine4j's `SchemaItemKind`, so a store whose
schema one library made is read the same by the others.
"""


def catalog_properties(properties: Sequence[str]) -> str:
    """
    The properties of an item as its record holds them, drivine4j's form: part of the catalog's key, so written the
    same, byte for byte, whichever library writes it.
    """
    quoted = ",".join('"' + it.replace("\\", "\\\\").replace('"', '\\"') + '"' for it in properties)
    return f"[{quoted}]"


def catalog_record(kind: CatalogKind, label: str, properties: Sequence[str], kinds: Sequence[str] | None = None) -> str:
    """
    Whether the catalog's row is the record of the item, recorded as any of `kinds` (drivine4j's kind unless given).
    """
    recorded = list(kinds) if kinds is not None else [CATALOG_KINDS[kind]]
    test = (
        f"kind = {sql_text(recorded[0])}"
        if len(recorded) == 1
        else f"kind IN ({', '.join(sql_text(it) for it in recorded)})"
    )
    return f"{test} AND label = {sql_text(label)} AND properties = {sql_text(catalog_properties(properties))}"


ALIASES = "aliases"
"""
The key of a catalog record's `details` that holds the item's aliases, drivine4j's: the names Neo4j's schema DDL gave
it where the schema API names it otherwise (see SchemaCommands). A reader that knows of none reads the record as before.
"""


def aliases_json(aliases: Sequence[str]) -> str:
    """The aliases as the JSON array a record's details keep them in, written byte for byte as drivine4j writes it."""

    def quoted(alias: str) -> str:
        escaped = "".join(
            "\\\\" if c == "\\" else '\\"' if c == '"' else f"\\u{ord(c):04x}" if c < " " else c for c in alias
        )
        return f'"{escaped}"'

    return "[" + ",".join(quoted(it) for it in aliases) + "]"


def aliases_in(details: object) -> tuple[str, ...]:
    """The aliases a catalog record's details keep: none, where there are none."""
    if not isinstance(details, dict):
        return ()
    held: object = cast(dict[str, object], details).get(ALIASES)
    return tuple(str(it) for it in cast(list[object], held)) if isinstance(held, list) else ()
