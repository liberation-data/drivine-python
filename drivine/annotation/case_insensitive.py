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

    The object manager keeps the copy in step: `save`, `save_all`, `create`, `update`, `patch`, `patch_all` and the save
    of a view. Cypher you send yourself does not, on any store: Drivine runs it as written, or compiles it to SQL, and
    adds nothing to it. Nor does SQL of your own. Such a write to the property sets the copy too, in a `SET` of its own
    after the one that writes the property, or is followed by a `recreate` of the index that ignores case, which fills
    every copy of the label in again::

        await pm.execute(
            QuerySpecification(
                cypher_statement(
                    f"MATCH (a:Account {{id: $id}}) SET a.email = $email SET {lower_copies.set_clause('a', 'email')}"
                )
            ).bind({"id": id, "email": email})
        )
        # or, after the writes:
        await gom.indexes.recreate(range_index("Account", "email", ignore_case=True))

    `lower_copies.set_clause("a", "email")` gives ``a.`__drivine.lower.email` = toLower(a.email)``.
    """
