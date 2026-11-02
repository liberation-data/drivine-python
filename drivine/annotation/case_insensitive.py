from dataclasses import dataclass


@dataclass(frozen=True)
class CaseInsensitive:
    """
    Marks a text field of a node fragment that is looked up without regard to case: by `eq_ignore_case` or
    `contains_ignore_case`, or under a uniqueness constraint that ignores case:
    `email: Annotated[str, CaseInsensitive(), Unique(ignore_case=True)]`.

    Every save of the node also writes a lowercased copy of the property beside it, under `__drivine.lower.<property>`,
    the property the field is stored under. The database lowercases it, as it lowercases what the copy is compared
    with, so drivine4j, TypeScript's Drivine and drivine-python store and match the same thing. A load doesn't read the
    copy, and a flat `PropertyBag` never holds it.

    A `RangeIndex(ignore_case=True)` or `Unique(ignore_case=True)` on the field is made over the copy, and ensuring it
    fills the copy in on the nodes saved before there was one. Once that has been done, a query compares
    `eq_ignore_case` and `contains_ignore_case` on the field with the copy, which the index serves; until then it
    lowercases the property itself, as it does for any other field.

    A write that doesn't go through Drivine, Cypher or SQL of your own, doesn't keep the copy in step: set it with
    `lower_copies.set_clause`, or recreate the index.
    """
