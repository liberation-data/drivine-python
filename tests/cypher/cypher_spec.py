"""
A file of the Cypher spec, in tests/cypher: the statements each store runs, and what each gives. The three Drivine
libraries run the same files; README.md there describes them, and scripts/sync_cypher_spec.py copies them from a tag of
drivine-cypher-spec. drivine4j's CypherSpec, ported.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SPEC = Path(__file__).parent

GROUPS = {
    "graph": {"neo4j", "memgraph", "falkordb", "neptune"},
    "sql": {"postgres", "oracle", "sqlite"},
}
"""The groups a step may name for its stores: the graph engines, and the stores Cypher is compiled to SQL for."""


class _Loader(yaml.SafeLoader):
    """
    YAML 1.2's core schema: `true` and `false` alone are booleans, and a number is written as JSON writes one, so a
    whole number stays an int and one with a fraction a float (2 is not 2.0). PyYAML reads YAML 1.1, where `yes`, `no`,
    `on` and `off` are booleans and `10:30` is a number.
    """


_Loader.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in resolvers if tag.rsplit(":", 1)[-1] not in ("bool", "int", "float")]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$"), list("tf"))
_Loader.add_implicit_resolver("tag:yaml.org,2002:int", re.compile(r"^[-+]?[0-9]+$"), list("-+0123456789"))
_Loader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?$"),
    list("-+0123456789."),
)


@dataclass(frozen=True)
class Step:
    cypher: str
    note: str | None = None
    parameters: dict[str, Any] = field(default_factory=lambda: {})
    rows: list[Any] | None = None
    """The rows expected; with no rows and no error, the statement is run and what it gives is not read."""
    error: str | list[str] | bool | None = None
    """The message expected of the statement's failure, a list of messages any one of which it may give, or true for
    any failure."""
    only: list[str] | None = None
    """The stores or groups the step is run on."""
    except_: list[str] = field(default_factory=lambda: [])
    """The stores or groups the step is not run on."""
    unordered: bool = False
    """The rows are compared in any order."""
    unordered_lists: bool = False
    """Each list in the rows is compared in any order: not the rows, which `unordered` is for."""
    key_order: bool = False
    """Each map in the rows has its keys in the order expected."""

    def runs_on(self, store: str) -> bool:
        def named(names: list[str]) -> bool:
            return any(it == store or store in GROUPS.get(it, set()) for it in names)

        return (self.only is None or named(self.only)) and not named(self.except_)


@dataclass(frozen=True)
class Case:
    name: str
    steps: list[Step]
    description: str | None = None


@dataclass(frozen=True)
class CypherSpec:
    cases: list[Case]
    label: str | None = None
    """The label every node the spec writes carries, so a graph engine's are cleared by it."""
    graph: str | None = None
    """The Cypher that writes the graph each case starts from."""

    def case(self, name: str) -> Case:
        return next(it for it in self.cases if it.name == name)


_KEYS = {"except": "except_", "unorderedLists": "unordered_lists", "keyOrder": "key_order"}


def load(file: str) -> CypherSpec:
    """The spec file of that name, in tests/cypher."""
    content = yaml.load((SPEC / file).read_text(encoding="utf-8"), Loader=_Loader)  # noqa: S506
    cases = [
        Case(
            it["name"],
            [Step(**{_KEYS.get(key, key): value for key, value in step.items()}) for step in it["steps"]],
            it.get("description"),
        )
        for it in content.get("cases", [])
    ]
    return CypherSpec(cases, content.get("label"), content.get("graph"))


def expanded(value: Any) -> Any:
    """The value with each `{$repeat: text, times: n}` in it written out: the text repeated n times."""
    if isinstance(value, Mapping):
        if set(value) == {"$repeat", "times"}:  # pyright: ignore[reportUnknownArgumentType]
            return str(value["$repeat"]) * int(value["times"])
        return {key: expanded(it) for key, it in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list):
        return [expanded(it) for it in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def canonical(value: Any, step: Step, sort: bool | None = None) -> Any:
    """
    The value in a form that compares as the step says: of its kind (true is not 1, and 2 is not 2.0), a map as its
    entries in order where key order counts, and a list sorted where its order does not.
    """
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("float", value)
    if isinstance(value, Mapping):
        entries = {str(key): canonical(it, step) for key, it in value.items()}  # pyright: ignore[reportUnknownVariableType]
        return ("ordered", tuple(entries.items())) if step.key_order else entries
    if isinstance(value, (list, tuple)):
        items = [canonical(it, step) for it in value]  # pyright: ignore[reportUnknownVariableType]
        return sorted(items, key=_sort_key) if (step.unordered_lists if sort is None else sort) else items
    return value


def _sort_key(value: Any) -> str:
    """A text that orders canonical values, the same for two that are equal: a map's entries in the order of their
    keys."""
    if isinstance(value, dict):
        entries: dict[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
        return "{" + ",".join(f"{key}:{_sort_key(it)}" for key, it in sorted(entries.items())) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_sort_key(it) for it in value) + "]"  # pyright: ignore[reportUnknownVariableType]
    return repr(value)


def failure_matches(step: Step, failure: BaseException) -> bool:
    """Whether the failure is the one the step expects."""
    message = str(failure)
    if isinstance(step.error, str):
        return message == step.error
    if isinstance(step.error, list):
        return message in step.error
    return True


def check_rows(step: Step, rows: list[Any], where: str) -> None:
    """That the rows are those the step expects, compared as it says."""
    if step.rows is None:
        return
    expected = canonical(expanded(step.rows), step, step.unordered)
    actual = canonical(rows, step, step.unordered)
    assert actual == expected, f"{where}\nexpected: {step.rows}\nactual:   {rows}"
