"""A node's vector properties, written each in the engine's vector form."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from drivine.backend.cypher import quote_name
from drivine.backend.cypher_grammar import CypherGrammar
from drivine.model.fragment_model import FragmentModel


def split_vectors(model: FragmentModel, props: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    A node's vector properties, taken out of its props to be written each in the engine's vector form: the props
    without them, and the vectors by property name (None where a cleared field clears it).
    """
    names = {it.property for it in model.vector_fields.values()}
    if not names:
        return dict(props), {}
    rest = {key: value for key, value in props.items() if key not in names}
    vectors = {key: value for key, value in props.items() if key in names}
    return rest, vectors


def vector_clauses(
    model: FragmentModel, grammar: CypherGrammar, alias: str, source: str, clears: Sequence[str] = ()
) -> tuple[list[str], list[str]]:
    """
    Clauses writing the vectors `source` (a map expression, such as `$root.vectors`) holds onto `alias`, and the
    conditions that say one changes the node. A key that is absent or null leaves the property, unless `clears` names
    it, which removes it.
    """
    sets: list[str] = []
    changed: list[str] = []
    for it in model.vector_fields.values():
        value = f"{source}.{quote_name(it.property)}"
        target = f"{alias}.{quote_name(it.property)}"
        written = grammar.vector_value(value)
        sets.append(f"FOREACH (_ IN CASE WHEN {value} IS NULL THEN [] ELSE [1] END | SET {target} = {written})")
        changed.append(f"({value} IS NOT NULL AND ({target} IS NULL OR {target} <> {written}))")
        if it.property in clears:
            sets.append(f"SET {target} = null")
            changed.append(f"{target} IS NOT NULL")
    return sets, changed
