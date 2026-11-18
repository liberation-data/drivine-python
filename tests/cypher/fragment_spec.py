"""
The spec's fragments.yaml (see README.md, "Fragments") as its two runners read it: the unit test of the terms and
digests the module gives (tests/unit/test_fragment_spec.py), and the runner that holds each SQL store to Neo4j's rows
(tests/integration/sql/test_fragments_spec.py).
"""

from datetime import date, datetime
from typing import Any, cast

from drivine.cypher.shared_compiler import FragmentVariable
from tests.cypher.cypher_spec import read

SPEC: dict[str, Any] = read("fragments.yaml")

TABLE: dict[str, Any] = SPEC["table"]

CONDITIONS: list[tuple[dict[str, Any], int, str]] = [
    (entry, at, position.replace("{f}", entry["form"]))
    for entry in TABLE["conditions"]
    for at, position in enumerate(TABLE["positions"])
]
"""Each condition of the table in each of its positions: the entry, the position's index, and the condition."""

ORDERS: list[tuple[dict[str, Any], int, bool]] = [
    (entry, at, descending) for entry in TABLE["orders"] for at, descending in enumerate((False, True))
]
"""Each value of the table as an order, ascending and then descending: the entry, the index, and whether descending."""


def spec_variables() -> dict[str, FragmentVariable]:
    """The rows the file's fragments are over, as the request names them."""
    return {name: FragmentVariable(it["node"], tuple(it["labels"])) for name, it in SPEC["variables"].items()}


def spec_indexed() -> list[tuple[str, str]]:
    """The graph's indexed properties, each a label and a property."""
    return [(label, key) for label, key in SPEC["indexed"]]


def value(given: Any) -> Any:
    """A parameter as the spec writes it, the JSON entry's way, as the Python value Drivine binds for it."""
    if isinstance(given, dict) and "$temporal" in given:
        tag = cast(dict[str, str], given)
        kind, text = tag["$temporal"], tag["text"]
        if kind == "DATE":
            return date.fromisoformat(text)
        if kind in ("DATE_TIME", "LOCAL_DATE_TIME"):
            return datetime.fromisoformat(text)
        raise AssertionError(f"No temporal kind {kind} in the runner")
    if isinstance(given, list):
        return [value(it) for it in cast(list[Any], given)]
    return given


def parameters(given: dict[str, Any] | None) -> dict[str, Any]:
    """A case's parameters, as the Python values Drivine binds for them."""
    return {name: value(it) for name, it in (given or {}).items()}
