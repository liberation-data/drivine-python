"""
The Cypher spec's schema.yaml (tests/cypher, see its README.md): each step's Neo4j schema command, given to
`schema_ddl.translate`, reads as the spec, the drop, the wait or the refusal the step says, or as no schema command. The
three Drivine libraries read the same file. drivine4j's SchemaDdlSpecTest, ported.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from drivine.schema import schema_ddl
from drivine.schema.full_text_index_spec import FullTextIndexSpec
from drivine.schema.schema_ddl_result import (
    AwaitIndexes,
    Create,
    Drop,
    NotDdl,
    Refused,
    Reserved,
    SchemaDdlResult,
)
from drivine.schema.vector_index_spec import VectorIndexSpec
from tests.cypher.cypher_spec import SPEC, _Loader  # pyright: ignore[reportPrivateUsage]

FILE = SPEC / "schema.yaml"

CASES: list[dict[str, Any]] = (
    yaml.load(FILE.read_text(encoding="utf-8"), Loader=_Loader)["cases"]  # noqa: S506
    if FILE.is_file()
    else []
)

KINDS = {
    "RANGE": "RANGE_INDEX",
    "UNIQUE": "UNIQUENESS_CONSTRAINT",
    "FULLTEXT": "FULLTEXT_INDEX",
    "VECTOR": "VECTOR_INDEX",
    "TEXT": "TEXT_INDEX",
}
"""Each spec's kind, as a store's catalog names it, and the step writes it."""


def test_the_spec_holds_schema_yaml() -> None:
    assert FILE.is_file(), (
        f"{FILE} is missing: the Cypher spec's schema.yaml is copied with the rest by scripts/sync_cypher_spec.py."
    )
    assert CASES, f"{FILE} holds no cases."


@pytest.mark.parametrize("case", CASES, ids=[it["name"] for it in CASES])
def test_schema(case: dict[str, Any]) -> None:
    for index, step in enumerate(case["steps"]):
        where = f"{case['name']}, step {index + 1}: {step['ddl']}"
        assert read(schema_ddl.translate(step["ddl"])) == expected(step, where), where


def expected(step: dict[str, Any], where: str) -> dict[str, Any]:
    """What the step says the command reads as, in the form `read` gives."""
    given: list[dict[str, Any]] = []
    if "create" in step:
        given.append({"create": {"alias": None, "ifNotExists": False, "options": {}, **step["create"]}})
    if "drop" in step:
        given.append({"drop": {"ifExists": False, **step["drop"]}})
    if step.get("await"):
        given.append({"await": True})
    if "reserved" in step:
        given.append({"reserved": step["reserved"]})
    if step.get("cypher"):
        given.append({"cypher": True})
    if "error" in step:
        given.append({"error": step["error"]})
    assert len(given) == 1, f"{where}: a step gives one of create, drop, await, reserved, cypher and error"
    return given[0]


def read(result: SchemaDdlResult) -> dict[str, Any]:
    """The result, as a step writes it."""
    if isinstance(result, Create):
        spec = result.spec
        return {
            "create": {
                "kind": KINDS[spec.kind],
                "label": spec.label,
                "properties": list(spec.properties),
                "alias": result.alias,
                "ifNotExists": result.if_not_exists,
                "options": options(result),
            }
        }
    if isinstance(result, Drop):
        return {"drop": {"kind": result.kind, "name": result.name, "ifExists": result.if_exists}}
    if isinstance(result, AwaitIndexes):
        return {"await": True}
    if isinstance(result, Reserved):
        return {"reserved": {"label": result.spec.label, "properties": list(result.spec.properties)}}
    if isinstance(result, NotDdl):
        return {"cypher": True}
    assert isinstance(result, Refused)
    return {"error": result.message}


def options(result: Create) -> dict[str, Any]:
    spec = result.spec
    if isinstance(spec, FullTextIndexSpec):
        return {"analyzer": spec.analyzer} if spec.analyzer is not None else {}
    if isinstance(spec, VectorIndexSpec):
        neo4j = next((it for it in spec.engine_options if it.engine == "Neo4j"), None)
        found: dict[str, Any] = {"dimensions": spec.dimensions, "similarity": spec.similarity}
        if spec.hnsw_m is not None:
            found["hnswM"] = spec.hnsw_m
        if spec.hnsw_ef_construction is not None:
            found["hnswEfConstruction"] = spec.hnsw_ef_construction
        if neo4j is not None and neo4j.quantization_enabled is not None:
            found["quantizationEnabled"] = neo4j.quantization_enabled
        return found
    return {}
