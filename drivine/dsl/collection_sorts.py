"""Orders the lists a query's collection terms name, in each object."""

from __future__ import annotations

from collections.abc import Sequence
from functools import cmp_to_key
from typing import Any

from drivine.dsl.collection_sort import CollectionSort


def sort_collections(objects: Sequence[Any], sorts: Sequence[CollectionSort]) -> None:
    """
    The terms of one list apply in turn, each breaking the ties of those before. Nulls go where Cypher puts them unless
    placed: last ascending, first descending.
    """
    by_list: dict[tuple[str, ...], list[CollectionSort]] = {}
    for it in sorts:
        by_list.setdefault(it.relationship, []).append(it)
    for terms in by_list.values():
        for obj in objects:
            for owner, field in _lists_at(obj, terms[0].relationship):
                items = getattr(owner, field)
                ordered = sorted(items, key=cmp_to_key(_comparator(terms)))
                setattr(owner, field, ordered if isinstance(items, list) else type(items)(ordered))


def _lists_at(value: Any, path: tuple[str, ...]) -> list[tuple[Any, str]]:
    """The owners and fields of the lists at the end of a path, through every item of a list met on the way."""
    if value is None or not path:
        return []
    head, rest = path[0], path[1:]
    items: list[Any] = list(value) if isinstance(value, (list, tuple, set, frozenset)) else [value]  # pyright: ignore[reportUnknownArgumentType]
    found: list[tuple[Any, str]] = []
    for it in items:
        if it is None:
            continue
        if not rest:
            if isinstance(getattr(it, head, None), (list, tuple)):
                found.append((it, head))
        else:
            found.extend(_lists_at(getattr(it, head, None), rest))
    return found


def _comparator(terms: list[CollectionSort]) -> Any:
    def compare(a: Any, b: Any) -> int:
        for term in terms:
            order = _compare(_value(a, term.key), _value(b, term.key), term)
            if order:
                return order
        return 0

    return compare


def _value(item: Any, key: tuple[str, ...]) -> Any:
    for field in key:
        item = getattr(item, field, None)
        if item is None:
            return None
    return item


def _compare(x: Any, y: Any, term: CollectionSort) -> int:
    sign = 1 if term.direction == "ASC" else -1
    if x is None or y is None:
        if x is None and y is None:
            return 0
        nulls_first = term.nulls == "FIRST" if term.nulls else term.direction == "DESC"
        return -1 if (x is None) == nulls_first else 1
    return (-1 if x < y else 1 if x > y else 0) * sign
