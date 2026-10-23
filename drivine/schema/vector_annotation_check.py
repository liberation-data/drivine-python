"""
Refuses a vector index a schema catalog declares over a node fragment's field that has no `VectorIndex`: drivine4j's
VectorAnnotationCheck.

Drivine knows a field is an embedding from its `VectorIndex` alone (the fragment model's `vector_fields`). Without it
the field is saved as a plain list, which on FalkorDB is not a vector the index serves, and `load_nearest` refuses the
fragment. Neither points at the mistake, so it is refused where it was made.

Either side may come first: a catalog can be enforced before a fragment's model is built, or after. So both are kept
for the life of the process: the vector specs SchemaManager has applied, and the fragments whose model has been built.
Each new arrival is checked against the other. A spec for a label no fragment maps has nothing to check, and a spec
beside the marker is how an index is tuned. A spec given to `indexes.ensure` directly is not checked.

Classes are held weakly, so a class made and dropped (in a test, say) isn't kept alive.
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Iterable
from dataclasses import dataclass

from drivine.drivine_error import DrivineError
from drivine.model.fragment_model import FragmentModel
from drivine.schema.vector_index_spec import VectorIndexSpec


@dataclass(frozen=True)
class _Shape:
    """What the check needs of a fragment's model, holding nothing of its class."""

    name: str
    labels: tuple[str, ...]
    fields: tuple[tuple[str, str, bool], ...]
    """Each stored field: its name, the property it is stored under, and whether it has a `VectorIndex`."""


_lock = threading.Lock()
_specs: dict[VectorIndexSpec, None] = {}
"""The vector specs applied so far, in the order applied."""
_admitted: weakref.WeakKeyDictionary[type, set[_Shape]] = weakref.WeakKeyDictionary()
"""The fragments that have passed, each by the shapes of it built (a registry's naming may store fields apart)."""


def register(specs: Iterable[object]) -> None:
    """Records the vector specs among `specs` and checks them against every fragment admitted so far."""
    declared = list(dict.fromkeys(it for it in specs if isinstance(it, VectorIndexSpec)))
    if not declared:
        return
    with _lock:
        _specs.update(dict.fromkeys(declared))
        problems = [
            problem for shapes in list(_admitted.values()) for it in shapes for problem in _uncovered(it, declared)
        ]
        _refuse(list(dict.fromkeys(problems)))


def admit(model: FragmentModel) -> None:
    """Checks a newly built model against the vector specs registered so far. A refused class stays refused."""
    shape = _Shape(
        model.type.__name__,
        tuple(model.labels),
        tuple((field, stored, field in model.vector_fields) for field, stored in model.graph_properties.items()),
    )
    with _lock:
        if shape in _admitted.get(model.type, ()):
            return
        _refuse(_uncovered(shape, _specs))
        _admitted.setdefault(model.type, set()).add(shape)


def _uncovered(shape: _Shape, specs: Iterable[VectorIndexSpec]) -> list[str]:
    return list(
        dict.fromkeys(
            f"`{shape.name}.{field}` is covered by the vector index on `{spec.label}.{spec.properties[0]}` but has no "
            "`VectorIndex`. Add it: without it the field is not written as a vector on FalkorDB, and `load_nearest` "
            "cannot find its index."
            for spec in specs
            if spec.label in shape.labels
            for field, stored, indexed in shape.fields
            if stored == spec.properties[0] and not indexed
        )
    )


def _refuse(problems: list[str]) -> None:
    if problems:
        raise DrivineError("\n".join(problems))
