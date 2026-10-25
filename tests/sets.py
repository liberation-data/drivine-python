"""Sets for tests of how a set is stored, whose iteration order the test can rely on in every process."""


def reordered_set(prefix: str, size: int = 20) -> set[str]:
    """
    A set of `size` strings that iterates neither in sorted order nor in the order of a set built afresh from its list,
    as a load builds one from the stored list.

    A set iterates in the order of its items' hashes within its table, and a string's hash differs in each process, so
    no one set of strings does this in every process. CPython's set never shrinks its table as items are discarded,
    though, so this one, grown with padding that is then discarded, places its items by more bits of their hashes than
    a set of `size` items does. With twenty items the orders agree only by a chance too small to meet; with a handful
    they often do. The assert says so if they ever do, as it would on PyPy, whose sets iterate in insertion order.
    """
    items = {f"{prefix}{it}" for it in range(size)}
    padding = {f"_padding{it}" for it in range(size * 64)}
    reordered = items | padding
    for it in padding:
        reordered.discard(it)  # discard, as difference_update may build a smaller table
    assert reordered == items
    assert list(reordered) != list(set(list(reordered))) and list(reordered) != sorted(reordered)
    return reordered
